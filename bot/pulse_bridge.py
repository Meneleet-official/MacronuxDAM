"""Интеграция MacronuxDAM (DAP) с плагином Pulse (F:\\Code\\pulse) и серверами в Pterodactyl.

Обеспечивает:
1. Автопоиск контейнеров Pterodactyl на локальном VPS (/var/lib/pterodactyl/volumes/*, /srv/daemon-data/*)
   и автоматический проброс папки бота (/home/container/pmx-bot и plugins/Pulse/modules/MacronuxDAM)
   внутрь изолированного Docker-контейнера Pterodactyl с корректными правами (UID/GID контейнера, 0666/0777).
2. Двустороннюю 3-way синхронизацию SQLite (modbot.db), telemetry.jsonl и логов (modbot.log)
   между процессом бота и контейнером Pterodactyl без конфликтов блокировок WAL/SHM.
3. Реальное выполнение в Discord вердиктов, принятых администратором в веб-панели Pulse
   (🔨 Бан, ⏳ Мут, 🗑️ Удаление сообщения, ✅ Снятие страйка и обучение ИИ, 🔄 Сброс страйков).
4. Поддержку удалённых серверов Pterodactyl через официальный Pterodactyl Client API
   (PTERODACTYL_URL + PTERODACTYL_API_KEY) — если сервер Minecraft находится на другом хостинге.
5. Встроенный HTTP API мост (/api/dap/*) на порту PULSE_API_PORT (по умолчанию 8765).
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from typing import Any, Optional
import urllib.parse

import discord
import httpx

import config
from bot.database import SCHEMA, get_db, migrate_conn
from bot.logger import get_logger
from bot.rules import RULES

log = get_logger("pulse")

# Шим-скрипт .venv/bin/python для контейнеров Pterodactyl (работает на чистом /bin/sh даже в Java-образах без Python).
# Позволяет DapService.java внутри контейнера видеть активный процесс бота (PID, аптайм, .venv) и управлять им.
CONTAINER_PYTHON_SHIM = """#!/bin/sh
# MacronuxDAM <-> Pulse Pterodactyl Container Agent
BOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
DATA_DIR="${BOT_DIR}/data"
mkdir -p "${DATA_DIR}"
CTRL_FILE="${DATA_DIR}/pulse_control.json"
HB_FILE="${DATA_DIR}/pulse_heartbeat.json"
LOG_FILE="${BOT_DIR}/bot_run2.log"

# Если запущен с флагом --version
if [ "${1:-}" = "--version" ] || [ "${1:-}" = "-V" ]; then
    echo "Python 3.12 (MacronuxDAM Pterodactyl Bridge)"
    exit 0
fi

# Если на хосте VPS активен основной процесс MacronuxDAM (свежий heartbeat < 30 сек),
# работаем как легковесный агент статуса внутри контейнера Pterodactyl.
is_host_alive() {
    if [ -f "${HB_FILE}" ]; then
        now=$(date +%s 2>/dev/null || echo 0)
        mtime=$(date -r "${HB_FILE}" +%s 2>/dev/null || stat -c %Y "${HB_FILE}" 2>/dev/null || echo 0)
        diff=$((now - mtime))
        if [ "${diff}" -lt 30 ]; then
            return 0
        fi
    fi
    return 1
}

if is_host_alive || [ "${1:-}" = "--pulse-agent" ] || ! command -v python3 >/dev/null 2>&1; then
    printf '{"cmd":"start","ts":%s}\n' "$(date +%s 2>/dev/null || echo 0)" > "${CTRL_FILE}" 2>/dev/null || true
    trap 'printf "{\"cmd\":\"stop\",\"ts\":%s}\\n" "$(date +%s 2>/dev/null || echo 0)" > "${CTRL_FILE}" 2>/dev/null; exit 0' INT TERM
    echo "[PULSE-BRIDGE] Агент контейнера Pterodactyl подключён к MacronuxDAM." >> "${LOG_FILE}" 2>/dev/null || true
    while true; do
        if [ -f "${CTRL_FILE}" ] && grep -q '"cmd":"stop_ack"' "${CTRL_FILE}" 2>/dev/null; then
            rm -f "${CTRL_FILE}" 2>/dev/null || true
            exit 0
        fi
        sleep 3
    done
fi

exec python3 "$@"
"""


class BridgeSnapshot:
    """Снимок состояния БД моста для точного 3-way слияния изменений между ботом и веб-панелью Pulse."""

    def __init__(self) -> None:
        self.initialized: bool = False
        self.servers: dict[int, dict[str, Any]] = {}
        self.violation_ids: set[int] = set()
        self.punishment_ids: set[int] = set()
        self.punishments_by_vid: dict[int, tuple[str, int, str]] = {}
        self.manual_punishment_sigs: set[tuple] = set()
        self.web_action_ids: set[int] = set()
        self.banwords: set[tuple[int, str]] = set()
        self.exceptions: set[tuple[int, int, str]] = set()


class PulseBridge:
    """Менеджер синхронизации с плагином Pulse (локальные тома Pterodactyl, удалённый Pterodactyl API и HTTP API)."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self.start_time = time.time()
        self.root_dir = Path(__file__).resolve().parent.parent
        self._snapshots: dict[str, BridgeSnapshot] = {}
        self._processed_web_punishments: set[int] = set()
        self._http_server: Optional[asyncio.AbstractServer] = None
        self._docker_agent_checked_at: float = 0.0
        self._meta_synced_at: float = 0.0
        self._remote_sync_at: float = 0.0
        self._discovered_Server_id: str = config.PTERODACTYL_SERVER_ID

    # ------------------------------------------------------------------
    # Инициализация и фоновый цикл
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if not config.PULSE_ENABLED:
            log.info("[PULSE] Интеграция с Pulse отключена (PULSE_ENABLED=0).")
            return

        # Запоминаем уже существующие наказания moderator_id=0 как обработанные при старте,
        # чтобы не повторять старые баны/муты после перезапуска бота, если они уже applied/dismissed.
        try:
            self._init_processed_punishments()
        except Exception as e:
            log.debug("[PULSE] Ошибка инициализации списка наказаний: %s", e)

        # Запускаем встроенный HTTP API сервер (если порт > 0)
        if config.PULSE_API_PORT > 0:
            try:
                self._http_server = await asyncio.start_server(
                    self._handle_http_client,
                    host=config.PULSE_API_HOST,
                    port=config.PULSE_API_PORT,
                )
                log.info(
                    "[PULSE] HTTP API мост запущен на http://%s:%d/api/dap/overview",
                    config.PULSE_API_HOST,
                    config.PULSE_API_PORT,
                )
            except Exception as e:
                log.warning("[PULSE] Не удалось занять порт HTTP API %d: %s", config.PULSE_API_PORT, e)

        # Первичная синхронизация локальных томов Pterodactyl
        try:
            await asyncio.to_thread(self.sync_all_local_bridges)
        except Exception as e:
            log.warning("[PULSE] Ошибка первичной синхронизации томов Pterodactyl: %s", e)

    async def stop(self) -> None:
        if self._http_server is not None:
            self._http_server.close()
            try:
                await self._http_server.wait_closed()
            except Exception:
                pass
            self._http_server = None

    async def sync_loop(self) -> None:
        """Фоновый цикл синхронизации с контейнерами Pterodactyl и обработки решений из веб-панели Pulse."""
        await self.bot.wait_until_ready()
        await self.start()
        while not self.bot.is_closed():
            try:
                # 1. Синхронизация локальных томов Pterodactyl (/var/lib/pterodactyl/volumes/*)
                await asyncio.to_thread(self.sync_all_local_bridges)

                # 2. Выполнение в Discord новых вердиктов модерации из веб-панели Pulse
                await self.execute_pending_web_decisions()

                # 3. Если настроен удалённый Pterodactyl Client API — синхронизируемся раз в 6 секунд
                if config.PTERODACTYL_URL and config.PTERODACTYL_API_KEY:
                    now = time.time()
                    if now - self._remote_sync_at >= 6.0:
                        self._remote_sync_at = now
                        await self.sync_remote_pterodactyl()
                        await self.execute_pending_web_decisions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("[PULSE] Ошибка цикла синхронизации: %s", e)
            await asyncio.sleep(2.0)

    # ------------------------------------------------------------------
    # Поиск томов Pterodactyl и папок плагина Pulse
    # ------------------------------------------------------------------
    def discover_bridge_targets(self) -> list[Path]:
        """Находит все директории внутри томов Pterodactyl / папок сервера, куда нужно пробросить бота."""
        targets: list[Path] = []
        seen: set[str] = set()

        def _add_target(p: Path) -> None:
            try:
                norm = str(p.resolve())
            except Exception:
                norm = str(p)
            if norm == str(self.root_dir.resolve()):
                return
            if norm not in seen:
                seen.add(norm)
                targets.append(p)

        # 1. Явно указанные директории в PULSE_BRIDGE_DIRS
        if config.PULSE_BRIDGE_DIRS:
            for raw in re.split(r"[,;]+", config.PULSE_BRIDGE_DIRS):
                raw_s = raw.strip()
                if not raw_s:
                    continue
                cand = Path(raw_s)
                if cand.name in ("pmx-bot", "MacronuxDAM"):
                    _add_target(cand)
                elif (cand / "plugins" / "Pulse").is_dir():
                    _add_target(cand / "pmx-bot")
                    _add_target(cand / "plugins" / "Pulse" / "modules" / "MacronuxDAM")
                    self._auto_configure_pulse_yml(cand / "plugins" / "Pulse" / "config.yml")
                elif cand.name == "Pulse" and cand.parent.name == "plugins":
                    _add_target(cand.parent.parent / "pmx-bot")
                    _add_target(cand / "modules" / "MacronuxDAM")
                    self._auto_configure_pulse_yml(cand / "config.yml")
                else:
                    _add_target(cand / "pmx-bot")

        # 2. Автопоиск томов Pterodactyl Wings на Linux VPS
        ptero_roots = [
            Path("/var/lib/pterodactyl/volumes"),
            Path("/srv/daemon-data"),
            Path("/var/lib/pufferpanel/servers"),
        ]
        pulse_volumes: list[Path] = []
        mc_volumes: list[Path] = []

        for proot in ptero_roots:
            if not proot.is_dir():
                continue
            try:
                for vol in proot.iterdir():
                    if not vol.is_dir():
                        continue
                    plugins_dir = vol / "plugins"
                    has_pulse = (plugins_dir / "Pulse").is_dir()
                    if not has_pulse and plugins_dir.is_dir():
                        try:
                            has_pulse = any(
                                f.name.lower().startswith("pulse") and f.name.lower().endswith(".jar")
                                for f in plugins_dir.iterdir()
                                if f.is_file()
                            )
                        except Exception:
                            pass
                    if has_pulse:
                        pulse_volumes.append(vol)
                    elif (
                        (vol / "server.properties").is_file()
                        or (vol / "spigot.yml").is_file()
                        or (vol / "bukkit.yml").is_file()
                        or plugins_dir.is_dir()
                    ):
                        mc_volumes.append(vol)
            except Exception:
                pass

        chosen_volumes = pulse_volumes if pulse_volumes else mc_volumes
        for vol in chosen_volumes:
            # ./pmx-bot внутри контейнера (/home/container/pmx-bot) — мгновенно находится автопоиском Pulse!
            _add_target(vol / "pmx-bot")
            # А также plugins/Pulse/modules/MacronuxDAM (стандартный путь докачки Pulse)
            if (vol / "plugins" / "Pulse").is_dir():
                _add_target(vol / "plugins" / "Pulse" / "modules" / "MacronuxDAM")
            self._auto_configure_pulse_yml(vol / "plugins" / "Pulse" / "config.yml")

        return targets

    def _auto_configure_pulse_yml(self, cfg_path: Path) -> None:
        """Автоматически включает модуль dap и прописывает путь /home/container/pmx-bot в plugins/Pulse/config.yml."""
        if not cfg_path.is_file():
            return
        try:
            text = cfg_path.read_text(encoding="utf-8")
            updated = text
            # Если в секции dap указан дефолтный F:/Code/DAP — меняем на pmx-bot (работает и на хосте, и в контейнере)
            if 'bot-dir: "F:/Code/DAP"' in updated or "bot-dir: 'F:/Code/DAP'" in updated:
                updated = updated.replace('bot-dir: "F:/Code/DAP"', 'bot-dir: "pmx-bot"')
                updated = updated.replace("bot-dir: 'F:/Code/DAP'", 'bot-dir: "pmx-bot"')
            # Включаем dap.enabled: true в конце файла
            updated = re.sub(
                r"(?m)^(dap:\s*\r?\n(?:\s*#.*\r?\n)*\s*enabled:\s*)false\b",
                r"\1true",
                updated,
            )
            if updated != text:
                cfg_path.write_text(updated, encoding="utf-8")
                log.info("[PULSE] Автоматически обновлён конфиг плагина Pulse: %s", cfg_path)
        except Exception as e:
            log.debug("[PULSE] Не удалось обновить %s: %s", cfg_path, e)

    # ------------------------------------------------------------------
    # Подготовка файлов моста внутри контейнера Pterodactyl и права доступа
    # ------------------------------------------------------------------
    def _detect_owner_uid_gid(self, target_dir: Path) -> tuple[Optional[int], Optional[int]]:
        """Определяет UID/GID владельца тома Pterodactyl (обычно 988:988 container/pterodactyl)."""
        if os.name == "nt":
            return None, None
        cur = target_dir
        while cur != cur.parent:
            if cur.exists():
                try:
                    st = cur.stat()
                    if st.st_uid != 0:
                        return st.st_uid, st.st_gid
                except Exception:
                    pass
            cur = cur.parent
        return None, None

    def _fix_permissions(self, path: Path, uid: Optional[int], gid: Optional[int], is_dir: bool = False) -> None:
        if os.name == "nt":
            return
        try:
            if uid is not None and gid is not None:
                os.chown(path, uid, gid)
        except Exception:
            pass
        try:
            os.chmod(path, 0o777 if is_dir else 0o666)
        except Exception:
            pass

    def _prepare_bridge_directory(self, target_dir: Path) -> tuple[Optional[int], Optional[int]]:
        """Создаёт структуру папки бота внутри тома Pterodactyl, чтобы Pulse видел её как полноценную установку."""
        uid, gid = self._detect_owner_uid_gid(target_dir)
        data_dir = target_dir / "data"
        venv_bin = target_dir / ".venv" / "bin"
        data_dir.mkdir(parents=True, exist_ok=True)
        venv_bin.mkdir(parents=True, exist_ok=True)

        self._fix_permissions(target_dir, uid, gid, is_dir=True)
        self._fix_permissions(data_dir, uid, gid, is_dir=True)
        self._fix_permissions(target_dir / ".venv", uid, gid, is_dir=True)
        self._fix_permissions(venv_bin, uid, gid, is_dir=True)

        # Копируем маркерные файлы проекта (без секретов)
        for fname in ("main.py", "botctl.sh", "install.sh", "requirements.txt"):
            src = self.root_dir / fname
            dst = target_dir / fname
            if src.is_file() and (not dst.is_file() or src.stat().st_mtime > dst.stat().st_mtime + 1):
                try:
                    shutil.copy2(src, dst)
                    self._fix_permissions(dst, uid, gid, is_dir=False)
                    if fname.endswith(".sh") or fname == "main.py":
                        try:
                            os.chmod(dst, 0o755)
                        except Exception:
                            pass
                except Exception:
                    pass

        # Записываем безопасный .env (только метаданные провайдера ИИ и настроек для отображения в Pulse)
        safe_env = (
            "# MacronuxDAM Safe Metadata for Pulse Web Panel (Pterodactyl Bridge)\n"
            f"LLM_PROVIDER={config.LLM_PROVIDER}\n"
            f"GROQ_MODEL={config.GROQ_MODEL}\n"
            f"GEMINI_MODEL={config.GEMINI_MODEL}\n"
            f"DEEPSEEK_MODEL={config.DEEPSEEK_MODEL}\n"
            f"OPENROUTER_MODEL={config.OPENROUTER_MODEL}\n"
            f"STRIKE_DECAY_DAYS={config.STRIKE_DECAY_DAYS}\n"
        )
        env_file = target_dir / ".env"
        try:
            if not env_file.is_file() or env_file.read_text(encoding="utf-8", errors="ignore") != safe_env:
                env_file.write_text(safe_env, encoding="utf-8")
                self._fix_permissions(env_file, uid, gid, is_dir=False)
        except Exception:
            pass

        # Создаём шим .venv/bin/python для контейнера Pterodactyl
        py_shim = venv_bin / "python"
        try:
            if not py_shim.is_file() or py_shim.read_text(encoding="utf-8", errors="ignore") != CONTAINER_PYTHON_SHIM:
                py_shim.write_bytes(CONTAINER_PYTHON_SHIM.encode("utf-8"))
            if os.name != "nt":
                if uid is not None and gid is not None:
                    try:
                        os.chown(py_shim, uid, gid)
                    except Exception:
                        pass
                os.chmod(py_shim, 0o755)
        except Exception:
            pass

        # Обновляем heartbeat хоста
        hb_file = data_dir / "pulse_heartbeat.json"
        try:
            hb_data = {
                "running": True,
                "pid": os.getpid(),
                "uptimeSeconds": int(time.time() - self.start_time),
                "llmProvider": config.LLM_PROVIDER,
                "ts": int(time.time()),
            }
            hb_file.write_text(json.dumps(hb_data), encoding="utf-8")
            self._fix_permissions(hb_file, uid, gid, is_dir=False)
        except Exception:
            pass

        return uid, gid

    def _ensure_docker_container_agent(self, target_dir: Path) -> None:
        """Если на VPS запущен Docker-контейнер Pterodactyl для этого тома, запускает внутри него
        легковесный процесс-агент .venv/bin/python main.py, чтобы панель Pulse сразу видела статус Online."""
        if os.name == "nt":
            return
        now = time.time()
        if now - self._docker_agent_checked_at < 30.0:
            return
        self._docker_agent_checked_at = now

        # Ищем UUID тома в пути (/var/lib/pterodactyl/volumes/<uuid>/...)
        parts = target_dir.parts
        vol_uuid = None
        for i, part in enumerate(parts):
            if part in ("volumes", "daemon-data") and i + 1 < len(parts):
                vol_uuid = parts[i + 1]
                break
        if not vol_uuid or not shutil.which("docker"):
            return

        try:
            # Проверяем, запущен ли контейнер с именем/меткой vol_uuid
            res = subprocess.run(
                ["docker", "ps", "--filter", f"name={vol_uuid}", "--format", "{{.ID}}"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            cid = (res.stdout or "").strip().splitlines()
            if not cid:
                return
            container_id = cid[0].strip()
            # Проверяем, запущен ли уже агент внутри контейнера
            chk = subprocess.run(
                ["docker", "exec", container_id, "sh", "-c", "ps aux 2>/dev/null || ps -ef 2>/dev/null"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            ps_out = (chk.stdout or "").lower()
            if "main.py" in ps_out and "python" in ps_out:
                return

            # Вычисляем путь внутри контейнера (/home/container/...)
            vol_root = Path("/var/lib/pterodactyl/volumes") / vol_uuid
            if not vol_root.exists():
                vol_root = Path("/srv/daemon-data") / vol_uuid
            rel = target_dir.relative_to(vol_root).as_posix()
            container_bot_dir = f"/home/container/{rel}"
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-d",
                    container_id,
                    f"{container_bot_dir}/.venv/bin/python",
                    f"{container_bot_dir}/main.py",
                    "--pulse-agent",
                ],
                capture_output=True,
                timeout=3,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Двусторонняя 3-Way синхронизация SQLite, телеметрии и логов
    # ------------------------------------------------------------------
    def sync_all_local_bridges(self) -> list[str]:
        """Синхронизирует основную БД бота со всеми найденными контейнерами Pterodactyl / папками Pulse."""
        # Гарантируем, что все текущие серверы Discord, их каналы, роли и профили записаны в основную БД
        now = time.time()
        if now - self._meta_synced_at >= 30.0:
            self._meta_synced_at = now
            try:
                from bot.client import sync_guild_discord_metadata
                for g in list(getattr(self.bot, "guilds", [])):
                    sync_guild_discord_metadata(g)
            except Exception:
                pass

        synced_paths: list[str] = []
        for target_dir in self.discover_bridge_targets():
            try:
                uid, gid = self._prepare_bridge_directory(target_dir)
                self.sync_sqlite_bridge(target_dir / "data" / "modbot.db", uid, gid)
                self._sync_telemetry_and_logs(target_dir, uid, gid)
                self._check_control_file(target_dir / "data" / "pulse_control.json")
                if target_dir.name == "pmx-bot":
                    self._ensure_docker_container_agent(target_dir)
                synced_paths.append(str(target_dir))
            except Exception as e:
                log.debug("[PULSE] Ошибка синхронизации с %s: %s", target_dir, e)
        return synced_paths

    def sync_sqlite_bridge(
        self,
        bridge_db_path: Path,
        uid: Optional[int] = None,
        gid: Optional[int] = None,
    ) -> None:
        """Двусторонняя 3-way синхронизация между основной БД бота и БД внутри контейнера Pterodactyl."""
        main_db_path = Path(config.DB_PATH).resolve()
        try:
            if bridge_db_path.resolve() == main_db_path:
                return
        except Exception:
            pass

        bridge_db_path.parent.mkdir(parents=True, exist_ok=True)
        key = str(bridge_db_path)
        snap = self._snapshots.setdefault(key, BridgeSnapshot())

        main_conn = sqlite3.connect(str(main_db_path), timeout=10.0)
        main_conn.row_factory = sqlite3.Row
        bridge_conn = sqlite3.connect(str(bridge_db_path), timeout=10.0)
        bridge_conn.row_factory = sqlite3.Row

        try:
            migrate_conn(main_conn)
            migrate_conn(bridge_conn)

            def _srv_cfg_sig(row_dict: dict[str, Any]) -> tuple:
                return (
                    row_dict.get("mod_channel_id"),
                    row_dict.get("mod_role_id"),
                    row_dict.get("target_language") or "ru",
                    int(row_dict.get("delete_message") or 0),
                    int(row_dict.get("llm_enabled") if row_dict.get("llm_enabled") is not None else 1),
                    int(row_dict.get("manual_strikes") or 0),
                )

            # --- ШАГ 1: Если снимок уже инициализирован, применяем изменения из веб-панели Pulse в основную БД ---
            if snap.initialized:
                # 1a. Настройки серверов (servers)
                b_servers = {
                    int(r["guild_id"]): dict(r)
                    for r in bridge_conn.execute("SELECT * FROM servers").fetchall()
                }
                for gid_val, b_row in b_servers.items():
                    old_row = snap.servers.get(gid_val)
                    if old_row is None or _srv_cfg_sig(old_row) != _srv_cfg_sig(b_row):
                        main_conn.execute(
                            """INSERT INTO servers (guild_id, mod_channel_id, mod_role_id, target_language,
                                                    delete_message, llm_enabled, strike_thresholds, manual_strikes)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(guild_id) DO UPDATE SET
                                   mod_channel_id = excluded.mod_channel_id,
                                   mod_role_id = excluded.mod_role_id,
                                   target_language = excluded.target_language,
                                   delete_message = excluded.delete_message,
                                   llm_enabled = excluded.llm_enabled,
                                   manual_strikes = excluded.manual_strikes""",
                            (
                                gid_val,
                                b_row.get("mod_channel_id"),
                                b_row.get("mod_role_id"),
                                b_row.get("target_language") or "ru",
                                int(b_row.get("delete_message") or 0),
                                int(b_row.get("llm_enabled") if b_row.get("llm_enabled") is not None else 1),
                                b_row.get("strike_thresholds") or "{}",
                                int(b_row.get("manual_strikes") or 0),
                            ),
                        )

                # 1b. Удалённые нарушения (кнопка «Обнулить страйки» или удаление нарушения в веб-панели)
                b_vids = {
                    int(r["id"])
                    for r in bridge_conn.execute("SELECT id FROM violations").fetchall()
                }
                deleted_vids = snap.violation_ids - b_vids
                if deleted_vids:
                    for vid in deleted_vids:
                        main_conn.execute("DELETE FROM punishments WHERE violation_id = ?", (vid,))
                        main_conn.execute("DELETE FROM violations WHERE id = ?", (vid,))
                    log.info("[PULSE] Из веб-панели удалено/сброшено нарушений: %d шт.", len(deleted_vids))

                # 1c. Новые или изменённые вердикты модерации из веб-панели (по нарушению ИЛИ прямые ручные наказания)
                b_puns = bridge_conn.execute(
                    "SELECT * FROM punishments ORDER BY id ASC"
                ).fetchall()
                for pr in b_puns:
                    pr_d = dict(pr)
                    vid = pr_d.get("violation_id")
                    mod_id = int(pr_d.get("moderator_id") or 0)
                    st = str(pr_d.get("status") or "applied")
                    if vid is not None and int(vid) > 0:
                        vid_int = int(vid)
                        sig = (
                            str(pr_d.get("action") or ""),
                            int(pr_d.get("duration_seconds") or 0),
                            st,
                        )
                        if snap.punishments_by_vid.get(vid_int) != sig and (mod_id == 0 or st == "pending"):
                            main_conn.execute("DELETE FROM punishments WHERE violation_id = ?", (vid_int,))
                            main_conn.execute(
                                """INSERT INTO punishments
                                   (guild_id, user_id, user_name, user_display_name, user_avatar_url,
                                    violation_id, moderator_id, moderator_name, moderator_display_name,
                                    action, duration_seconds, reason, status, created_at)
                                   VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    pr_d.get("guild_id"),
                                    pr_d.get("user_id"),
                                    pr_d.get("user_name"),
                                    pr_d.get("user_display_name"),
                                    pr_d.get("user_avatar_url"),
                                    vid_int,
                                    pr_d.get("moderator_name") or "pulse_web",
                                    pr_d.get("moderator_display_name") or "Веб-панель Pulse",
                                    pr_d.get("action"),
                                    pr_d.get("duration_seconds") or 0,
                                    pr_d.get("reason"),
                                    st,
                                    pr_d.get("created_at"),
                                ),
                            )
                    elif mod_id == 0 or st == "pending":
                        # Прямое (ручное) наказание из веб-панели без привязки к старому violation_id
                        msig = (
                            int(pr_d.get("guild_id") or 0),
                            int(pr_d.get("user_id") or 0),
                            str(pr_d.get("action") or ""),
                            int(pr_d.get("duration_seconds") or 0),
                            str(pr_d.get("reason") or ""),
                            str(pr_d.get("created_at") or ""),
                        )
                        if msig not in snap.manual_punishment_sigs and int(pr_d["id"]) not in snap.punishment_ids:
                            main_conn.execute(
                                """INSERT INTO punishments
                                   (guild_id, user_id, user_name, user_display_name, user_avatar_url,
                                    violation_id, moderator_id, moderator_name, moderator_display_name,
                                    action, duration_seconds, reason, status, created_at)
                                   VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    pr_d.get("guild_id"),
                                    pr_d.get("user_id"),
                                    pr_d.get("user_name"),
                                    pr_d.get("user_display_name"),
                                    pr_d.get("user_avatar_url"),
                                    pr_d.get("moderator_name") or "pulse_web",
                                    pr_d.get("moderator_display_name") or "Веб-панель Pulse",
                                    pr_d.get("action"),
                                    pr_d.get("duration_seconds") or 0,
                                    pr_d.get("reason"),
                                    st,
                                    pr_d.get("created_at"),
                                ),
                            )

                # 1d. Очередь команд web_actions из веб-панели Pulse
                b_actions = bridge_conn.execute(
                    "SELECT * FROM web_actions WHERE status = 'pending' ORDER BY id ASC"
                ).fetchall()
                for wa in b_actions:
                    wa_d = dict(wa)
                    if int(wa_d["id"]) not in snap.web_action_ids:
                        main_conn.execute(
                            """INSERT INTO web_actions (
                                   guild_id, user_id, violation_id, rule_id, action,
                                   duration_seconds, reason, new_nick,
                                   moderator_id, moderator_name, moderator_display_name,
                                   status, created_at
                               )
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', COALESCE(?, datetime('now')))""",
                            (
                                wa_d.get("guild_id"),
                                wa_d.get("user_id"),
                                wa_d.get("violation_id"),
                                wa_d.get("rule_id"),
                                wa_d.get("action"),
                                wa_d.get("duration_seconds") or 0,
                                wa_d.get("reason"),
                                wa_d.get("new_nick"),
                                wa_d.get("moderator_id") or 0,
                                wa_d.get("moderator_name") or "pulse_web",
                                wa_d.get("moderator_display_name") or "Веб-панель Pulse",
                                wa_d.get("created_at"),
                            ),
                        )

                # 1e. Серверные банворды (добавленные/удалённые в веб-панели Pulse)
                b_words = {
                    (int(r["guild_id"]), str(r["word"]))
                    for r in bridge_conn.execute("SELECT guild_id, word FROM banned_words").fetchall()
                }
                for gid_val, w in b_words - snap.banwords:
                    main_conn.execute(
                        "INSERT OR IGNORE INTO banned_words (guild_id, word) VALUES (?, ?)",
                        (gid_val, w),
                    )
                for gid_val, w in snap.banwords - b_words:
                    main_conn.execute(
                        "DELETE FROM banned_words WHERE guild_id = ? AND word = ?",
                        (gid_val, w),
                    )

                # 1f. Исключения правил по каналам (добавленные/удалённые в веб-панели Pulse)
                b_ex = {
                    (int(r["guild_id"]), int(r["channel_id"]), str(r["rule"]))
                    for r in bridge_conn.execute(
                        "SELECT guild_id, channel_id, rule FROM rule_exceptions"
                    ).fetchall()
                }
                for gid_val, cid, rl in b_ex - snap.exceptions:
                    main_conn.execute(
                        "INSERT OR IGNORE INTO rule_exceptions (guild_id, channel_id, rule) VALUES (?, ?, ?)",
                        (gid_val, cid, rl),
                    )
                for gid_val, cid, rl in snap.exceptions - b_ex:
                    main_conn.execute(
                        "DELETE FROM rule_exceptions WHERE guild_id = ? AND channel_id = ? AND rule = ?",
                        (gid_val, cid, rl),
                    )

                main_conn.commit()
            else:
                # При первой инициализации: если в контейнере уже были настройки серверов/банворды/решения,
                # бережно переносим их в основную БД без удаления данных бота.
                for r in bridge_conn.execute("SELECT * FROM servers").fetchall():
                    r_d = dict(r)
                    main_conn.execute(
                        """INSERT OR IGNORE INTO servers
                           (guild_id, mod_channel_id, mod_role_id, target_language,
                            delete_message, llm_enabled, strike_thresholds, manual_strikes)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            r_d["guild_id"],
                            r_d.get("mod_channel_id"),
                            r_d.get("mod_role_id"),
                            r_d.get("target_language") or "ru",
                            r_d.get("delete_message") or 0,
                            r_d.get("llm_enabled") if r_d.get("llm_enabled") is not None else 1,
                            r_d.get("strike_thresholds") or "{}",
                            r_d.get("manual_strikes") or 0,
                        ),
                    )
                for r in bridge_conn.execute("SELECT guild_id, word FROM banned_words").fetchall():
                    main_conn.execute(
                        "INSERT OR IGNORE INTO banned_words (guild_id, word) VALUES (?, ?)",
                        (r["guild_id"], r["word"]),
                    )
                for r in bridge_conn.execute("SELECT guild_id, channel_id, rule FROM rule_exceptions").fetchall():
                    main_conn.execute(
                        "INSERT OR IGNORE INTO rule_exceptions (guild_id, channel_id, rule) VALUES (?, ?, ?)",
                        (r["guild_id"], r["channel_id"], r["rule"]),
                    )
                # Переносим ожидающие команды из web_actions, если их создали, пока бот перезапускался
                for wa in bridge_conn.execute("SELECT * FROM web_actions WHERE status = 'pending'").fetchall():
                    wa_d = dict(wa)
                    main_conn.execute(
                        """INSERT INTO web_actions (
                               guild_id, user_id, violation_id, rule_id, action,
                               duration_seconds, reason, new_nick,
                               moderator_id, moderator_name, moderator_display_name,
                               status, created_at
                           )
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', COALESCE(?, datetime('now')))""",
                        (
                            wa_d.get("guild_id"),
                            wa_d.get("user_id"),
                            wa_d.get("violation_id"),
                            wa_d.get("rule_id"),
                            wa_d.get("action"),
                            wa_d.get("duration_seconds") or 0,
                            wa_d.get("reason"),
                            wa_d.get("new_nick"),
                            wa_d.get("moderator_id") or 0,
                            wa_d.get("moderator_name") or "pulse_web",
                            wa_d.get("moderator_display_name") or "Веб-панель Pulse",
                            wa_d.get("created_at"),
                        ),
                    )
                main_conn.commit()

            # --- ШАГ 2: Зеркалируем актуальное состояние основной БД в БД контейнера Pterodactyl ---
            # 2a. servers (включая название сервера, иконку, число участников, названия канала и роли модерации)
            m_servers = [dict(r) for r in main_conn.execute("SELECT * FROM servers").fetchall()]
            for r in m_servers:
                bridge_conn.execute(
                    """INSERT INTO servers (
                           guild_id, guild_name, guild_icon_url, member_count,
                           mod_channel_id, mod_channel_name, mod_role_id, mod_role_name,
                           target_language, delete_message, llm_enabled, strike_thresholds,
                           manual_strikes, updated_at
                       )
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(guild_id) DO UPDATE SET
                           guild_name = COALESCE(excluded.guild_name, servers.guild_name),
                           guild_icon_url = COALESCE(excluded.guild_icon_url, servers.guild_icon_url),
                           member_count = COALESCE(excluded.member_count, servers.member_count),
                           mod_channel_id = excluded.mod_channel_id,
                           mod_channel_name = COALESCE(excluded.mod_channel_name, servers.mod_channel_name),
                           mod_role_id = excluded.mod_role_id,
                           mod_role_name = COALESCE(excluded.mod_role_name, servers.mod_role_name),
                           target_language = excluded.target_language,
                           delete_message = excluded.delete_message,
                           llm_enabled = excluded.llm_enabled,
                           strike_thresholds = excluded.strike_thresholds,
                           manual_strikes = excluded.manual_strikes,
                           updated_at = excluded.updated_at""",
                    (
                        r["guild_id"],
                        r.get("guild_name"),
                        r.get("guild_icon_url"),
                        r.get("member_count") or 0,
                        r.get("mod_channel_id"),
                        r.get("mod_channel_name"),
                        r.get("mod_role_id"),
                        r.get("mod_role_name"),
                        r.get("target_language") or "ru",
                        r.get("delete_message") or 0,
                        r.get("llm_enabled") if r.get("llm_enabled") is not None else 1,
                        r.get("strike_thresholds") or "{}",
                        r.get("manual_strikes") or 0,
                        r.get("updated_at"),
                    ),
                )

            # 2b. violations (с полными метаданными пользователя, аватаркой, каналом, причиной и ссылкой)
            m_viols = [dict(r) for r in main_conn.execute("SELECT * FROM violations").fetchall()]
            m_vid_set = {int(r["id"]) for r in m_viols}
            b_vid_set = {int(r["id"]) for r in bridge_conn.execute("SELECT id FROM violations").fetchall()}
            for r in m_viols:
                bridge_conn.execute(
                    """INSERT INTO violations (
                           id, guild_id, guild_name, user_id, user_name, user_display_name, user_avatar_url,
                           rule_id, severity, method, reason, message_snapshot,
                           original_text, translated_text, detected_language,
                           channel_id, channel_name, message_id, panel_channel_id, panel_message_id,
                           jump_url, attachments_json, created_at
                       )
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           guild_name = COALESCE(excluded.guild_name, violations.guild_name),
                           user_name = COALESCE(excluded.user_name, violations.user_name),
                           user_display_name = COALESCE(excluded.user_display_name, violations.user_display_name),
                           user_avatar_url = COALESCE(excluded.user_avatar_url, violations.user_avatar_url),
                           reason = COALESCE(excluded.reason, violations.reason),
                           channel_name = COALESCE(excluded.channel_name, violations.channel_name),
                           panel_channel_id = COALESCE(excluded.panel_channel_id, violations.panel_channel_id),
                           panel_message_id = COALESCE(excluded.panel_message_id, violations.panel_message_id),
                           jump_url = COALESCE(excluded.jump_url, violations.jump_url),
                           attachments_json = COALESCE(excluded.attachments_json, violations.attachments_json)""",
                    (
                        r["id"],
                        r["guild_id"],
                        r.get("guild_name"),
                        r["user_id"],
                        r.get("user_name"),
                        r.get("user_display_name"),
                        r.get("user_avatar_url"),
                        r.get("rule_id"),
                        r.get("severity"),
                        r.get("method"),
                        r.get("reason"),
                        r.get("message_snapshot"),
                        r.get("original_text"),
                        r.get("translated_text"),
                        r.get("detected_language"),
                        r.get("channel_id"),
                        r.get("channel_name"),
                        r.get("message_id"),
                        r.get("panel_channel_id"),
                        r.get("panel_message_id"),
                        r.get("jump_url"),
                        r.get("attachments_json"),
                        r.get("created_at"),
                    ),
                )
            for stale_vid in b_vid_set - m_vid_set:
                bridge_conn.execute("DELETE FROM violations WHERE id = ?", (stale_vid,))

            # 2c. punishments (с именами и аватарками нарушителей и модераторов)
            m_puns = [dict(r) for r in main_conn.execute("SELECT * FROM punishments ORDER BY id ASC").fetchall()]
            bridge_conn.execute("DELETE FROM punishments")
            for r in m_puns:
                bridge_conn.execute(
                    """INSERT INTO punishments (
                           id, guild_id, user_id, user_name, user_display_name, user_avatar_url,
                           violation_id, moderator_id, moderator_name, moderator_display_name, moderator_avatar_url,
                           action, duration_seconds, reason, status, created_at
                       )
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        r["id"],
                        r["guild_id"],
                        r["user_id"],
                        r.get("user_name"),
                        r.get("user_display_name"),
                        r.get("user_avatar_url"),
                        r.get("violation_id"),
                        r.get("moderator_id"),
                        r.get("moderator_name"),
                        r.get("moderator_display_name"),
                        r.get("moderator_avatar_url"),
                        r.get("action"),
                        r.get("duration_seconds"),
                        r.get("reason"),
                        r.get("status"),
                        r.get("created_at"),
                    ),
                )

            # 2d. users, channels, roles
            m_users = [dict(r) for r in main_conn.execute("SELECT * FROM users").fetchall()]
            for r in m_users:
                bridge_conn.execute(
                    """INSERT INTO users (
                           guild_id, user_id, username, global_name, display_name, avatar_url,
                           language, account_created_at, joined_at, top_role_name, top_role_color,
                           roles_json, is_bot, is_moderator, updated_at
                       )
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(guild_id, user_id) DO UPDATE SET
                           username = COALESCE(excluded.username, users.username),
                           global_name = COALESCE(excluded.global_name, users.global_name),
                           display_name = COALESCE(excluded.display_name, users.display_name),
                           avatar_url = COALESCE(excluded.avatar_url, users.avatar_url),
                           language = COALESCE(excluded.language, users.language),
                           account_created_at = COALESCE(excluded.account_created_at, users.account_created_at),
                           joined_at = COALESCE(excluded.joined_at, users.joined_at),
                           top_role_name = COALESCE(excluded.top_role_name, users.top_role_name),
                           top_role_color = COALESCE(excluded.top_role_color, users.top_role_color),
                           roles_json = COALESCE(excluded.roles_json, users.roles_json),
                           is_bot = excluded.is_bot,
                           is_moderator = excluded.is_moderator,
                           updated_at = excluded.updated_at""",
                    (
                        r["guild_id"],
                        r["user_id"],
                        r.get("username"),
                        r.get("global_name"),
                        r.get("display_name"),
                        r.get("avatar_url"),
                        r.get("language"),
                        r.get("account_created_at"),
                        r.get("joined_at"),
                        r.get("top_role_name"),
                        r.get("top_role_color"),
                        r.get("roles_json"),
                        r.get("is_bot") or 0,
                        r.get("is_moderator") or 0,
                        r.get("updated_at"),
                    ),
                )

            m_channels = [dict(r) for r in main_conn.execute("SELECT * FROM channels").fetchall()]
            bridge_conn.execute("DELETE FROM channels")
            for r in m_channels:
                bridge_conn.execute(
                    """INSERT INTO channels (channel_id, guild_id, name, type, category_name, position, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        r["channel_id"],
                        r["guild_id"],
                        r["name"],
                        r.get("type") or "text",
                        r.get("category_name"),
                        r.get("position") or 0,
                        r.get("updated_at"),
                    ),
                )

            m_roles = [dict(r) for r in main_conn.execute("SELECT * FROM roles").fetchall()]
            bridge_conn.execute("DELETE FROM roles")
            for r in m_roles:
                bridge_conn.execute(
                    """INSERT INTO roles (role_id, guild_id, name, color, position, is_staff, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        r["role_id"],
                        r["guild_id"],
                        r["name"],
                        r.get("color"),
                        r.get("position") or 0,
                        r.get("is_staff") or 0,
                        r.get("updated_at"),
                    ),
                )

            # 2e. banned_words, rule_exceptions & web_actions
            m_words = [dict(r) for r in main_conn.execute("SELECT * FROM banned_words").fetchall()]
            bridge_conn.execute("DELETE FROM banned_words")
            for r in m_words:
                bridge_conn.execute(
                    "INSERT OR IGNORE INTO banned_words (id, guild_id, word) VALUES (?, ?, ?)",
                    (r["id"], r["guild_id"], r["word"]),
                )

            m_ex = [dict(r) for r in main_conn.execute("SELECT * FROM rule_exceptions").fetchall()]
            bridge_conn.execute("DELETE FROM rule_exceptions")
            for r in m_ex:
                bridge_conn.execute(
                    "INSERT OR IGNORE INTO rule_exceptions (guild_id, channel_id, rule) VALUES (?, ?, ?)",
                    (r["guild_id"], r["channel_id"], r["rule"]),
                )

            m_actions = [dict(r) for r in main_conn.execute("SELECT * FROM web_actions ORDER BY id ASC").fetchall()]
            bridge_conn.execute("DELETE FROM web_actions")
            for r in m_actions:
                bridge_conn.execute(
                    """INSERT INTO web_actions (
                           id, guild_id, user_id, violation_id, rule_id, action,
                           duration_seconds, reason, new_nick,
                           moderator_id, moderator_name, moderator_display_name,
                           status, result_message, created_at, executed_at
                       )
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        r["id"],
                        r["guild_id"],
                        r["user_id"],
                        r.get("violation_id"),
                        r.get("rule_id"),
                        r["action"],
                        r.get("duration_seconds") or 0,
                        r.get("reason"),
                        r.get("new_nick"),
                        r.get("moderator_id") or 0,
                        r.get("moderator_name"),
                        r.get("moderator_display_name"),
                        r.get("status") or "pending",
                        r.get("result_message"),
                        r.get("created_at"),
                        r.get("executed_at"),
                    ),
                )

            bridge_conn.commit()

            # --- ШАГ 3: Обновляем снимок состояния ---
            snap.initialized = True
            snap.servers = {
                int(r["guild_id"]): dict(r)
                for r in bridge_conn.execute("SELECT * FROM servers").fetchall()
            }
            snap.violation_ids = {int(r["id"]) for r in m_viols}
            snap.punishment_ids = {int(r["id"]) for r in m_puns}
            snap.punishments_by_vid = {}
            snap.manual_punishment_sigs = set()
            for r in m_puns:
                if r.get("violation_id") is not None and int(r["violation_id"]) > 0:
                    snap.punishments_by_vid[int(r["violation_id"])] = (
                        str(r.get("action") or ""),
                        int(r.get("duration_seconds") or 0),
                        str(r.get("status") or "applied"),
                    )
                else:
                    snap.manual_punishment_sigs.add((
                        int(r.get("guild_id") or 0),
                        int(r.get("user_id") or 0),
                        str(r.get("action") or ""),
                        int(r.get("duration_seconds") or 0),
                        str(r.get("reason") or ""),
                        str(r.get("created_at") or ""),
                    ))
            snap.web_action_ids = {int(r["id"]) for r in m_actions}
            snap.banwords = {(int(r["guild_id"]), str(r["word"])) for r in m_words}
            snap.exceptions = {
                (int(r["guild_id"]), int(r["channel_id"]), str(r["rule"])) for r in m_ex
            }
        finally:
            main_conn.close()
            bridge_conn.close()

        # Выставляем права 0666 на БД и файлы журнала SQLite, чтобы контейнер Pterodactyl мог писать без ошибок
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = Path(str(bridge_db_path) + suffix)
            if p.exists():
                self._fix_permissions(p, uid, gid, is_dir=False)

    def _sync_telemetry_and_logs(
        self,
        target_dir: Path,
        uid: Optional[int],
        gid: Optional[int],
    ) -> None:
        """Копирует свежие логи и telemetry.jsonl в папку контейнера Pterodactyl."""
        data_dir = target_dir / "data"
        src_tel = self.root_dir / "data" / "telemetry.jsonl"
        dst_tel = data_dir / "telemetry.jsonl"
        if src_tel.is_file():
            try:
                if not dst_tel.is_file() or src_tel.stat().st_size != dst_tel.stat().st_size:
                    shutil.copy2(src_tel, dst_tel)
                    self._fix_permissions(dst_tel, uid, gid, is_dir=False)
            except Exception:
                pass

        src_log = Path(config.LOG_PATH)
        if not src_log.is_absolute():
            src_log = self.root_dir / src_log
        dst_log = data_dir / "modbot.log"
        if src_log.is_file():
            try:
                if not dst_log.is_file() or src_log.stat().st_size != dst_log.stat().st_size:
                    shutil.copy2(src_log, dst_log)
                    self._fix_permissions(dst_log, uid, gid, is_dir=False)
            except Exception:
                pass

    def _check_control_file(self, ctrl_file: Path) -> None:
        """Проверяет управляющие сигналы (start/restart/stop) от кнопок веб-панели Pulse внутри контейнера."""
        if not ctrl_file.is_file():
            return
        try:
            raw = ctrl_file.read_text(encoding="utf-8", errors="ignore").strip()
            if not raw:
                return
            data = json.loads(raw.splitlines()[-1])
            cmd = str(data.get("cmd") or "").lower()
            if cmd in ("start", "restart"):
                ctrl_file.unlink(missing_ok=True)
                log.info("[PULSE] Получена команда '%s' из веб-панели Pulse.", cmd)
            elif cmd == "stop":
                ctrl_file.write_text('{"cmd":"stop_ack"}\n', encoding="utf-8")
                log.info("[PULSE] Получен сигнал 'stop' от агента контейнера Pulse.")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Выполнение в Discord вердиктов и наказаний из веб-панели Pulse
    # ------------------------------------------------------------------
    def _init_processed_punishments(self) -> None:
        db = get_db()
        with db._session() as conn:
            rows = conn.execute(
                """SELECT id FROM punishments
                   WHERE (moderator_id = 0 OR moderator_id IS NULL)
                     AND COALESCE(status, '') != 'pending'"""
            ).fetchall()
            for r in rows:
                self._processed_web_punishments.add(int(r["id"]))

    async def execute_pending_web_decisions(self) -> None:
        """Находит новые решения и прямые наказания из веб-панели Pulse
        (как из очереди `web_actions`, так и из таблицы `punishments`)
        и реально применяет их в Discord с обновлением карточек в #mod-log и отправкой ЛС."""
        db = get_db()

        # 1. Сначала обрабатываем очередь команд `web_actions` (status = 'pending')
        for wa in db.list_pending_web_actions():
            wa_id = int(wa["id"])
            gid = int(wa.get("guild_id") or 0)
            uid = int(wa.get("user_id") or 0)
            vid = int(wa.get("violation_id") or 0)
            rule_id = str(wa.get("rule_id") or "").strip()
            raw_act = str(wa.get("action") or "").lower().strip()
            dur_sec = int(wa.get("duration_seconds") or 0)
            reason = str(wa.get("reason") or "").strip()
            new_nick = str(wa.get("new_nick") or "").strip()
            mod_name = str(wa.get("moderator_name") or "pulse_web")
            mod_disp = str(wa.get("moderator_display_name") or "Веб-панель Pulse")

            if raw_act in ("reset_user", "clear_strikes"):
                if gid and uid:
                    db.reset_user_stats(gid, uid)
                    await self._apply_discord_punishment(
                        guild_id=gid,
                        user_id=uid,
                        action="reset_user",
                        duration_sec=0,
                        violation_id=0,
                        rule_id="",
                        reason=reason or "Сброс страйков через веб-панель Pulse",
                        moderator_display=mod_disp,
                    )
                    db.mark_web_action_status(wa_id, "applied", "Страйки и наказания пользователя сброшены")
                else:
                    db.mark_web_action_status(wa_id, "failed", "Не указан guild_id или user_id")
                continue

            # Если наказание выдано напрямую пользователю без violation_id — создаём запись в violations для истории и страйков
            if not vid and raw_act in ("warn", "timeout", "mute", "kick", "ban") and gid and uid:
                vid = db.add_violation(
                    gid,
                    uid,
                    rule_id=rule_id or "manual",
                    severity="high" if raw_act == "ban" else "medium",
                    method="pulse_web",
                    reason=reason or f"Наказание ({raw_act}) выдано через веб-панель Pulse",
                    message_snapshot=reason or f"Выдано вручную из веб-панели Pulse ({mod_disp})",
                    original_text=reason or f"Выдано вручную из веб-панели Pulse ({mod_disp})",
                )

            norm_act = {"mute": "timeout", "unmute": "untimeout", "reset_nick": "nick"}.get(raw_act, raw_act)
            pid = 0
            if gid and (uid or vid):
                if vid:
                    with db._session() as conn:
                        conn.execute("DELETE FROM punishments WHERE violation_id = ?", (vid,))
                        conn.commit()
                pid = db.add_punishment(
                    gid,
                    uid,
                    violation_id=vid or None,
                    moderator_id=0,
                    moderator_name=mod_name,
                    moderator_display_name=mod_disp,
                    action=norm_act,
                    duration_seconds=dur_sec,
                    reason=reason or new_nick or None,
                    status="dismissed" if norm_act == "dismiss" else "applied",
                )
                self._processed_web_punishments.add(pid)

            ok, msg = await self._apply_discord_punishment(
                guild_id=gid,
                user_id=uid,
                action=norm_act,
                duration_sec=dur_sec,
                violation_id=vid,
                rule_id=rule_id,
                reason=reason,
                new_nick=new_nick,
                moderator_display=mod_disp,
                punishment_id=pid,
            )
            db.mark_web_action_status(wa_id, "applied" if ok else "failed", msg)

        # 2. Обрабатываем новые записи из таблицы `punishments` (добавленные напрямую или через actionDecide)
        with db._session() as conn:
            rows = conn.execute(
                """SELECT p.id AS pid, p.guild_id, p.user_id, p.violation_id, p.action,
                          p.duration_seconds, p.reason AS p_reason, p.status,
                          p.moderator_name, p.moderator_display_name,
                          v.channel_id, v.message_id, v.original_text, v.rule_id,
                          v.reason AS v_reason, v.panel_channel_id, v.panel_message_id
                   FROM punishments p
                   LEFT JOIN violations v ON v.id = p.violation_id
                   WHERE (p.moderator_id = 0 OR p.moderator_id IS NULL OR p.status = 'pending')
                   ORDER BY p.id ASC"""
            ).fetchall()

        for r in rows:
            r_d = dict(r)
            pid = int(r_d["pid"])
            if pid in self._processed_web_punishments:
                continue
            self._processed_web_punishments.add(pid)

            raw_act = str(r_d.get("action") or "").lower().strip()
            norm_act = {"mute": "timeout", "unmute": "untimeout", "reset_nick": "nick"}.get(raw_act, raw_act)
            guild_id = int(r_d.get("guild_id") or 0)
            user_id = int(r_d.get("user_id") or 0)
            vid = int(r_d.get("violation_id") or 0)
            duration_sec = int(r_d.get("duration_seconds") or 0)
            rule_id = str(r_d.get("rule_id") or "")
            reason = str(r_d.get("p_reason") or r_d.get("v_reason") or "").strip()
            mod_disp = str(r_d.get("moderator_display_name") or "Веб-панель Pulse")

            # Если модератор вставил ручное наказание в `punishments` без violation_id — создаём связанный кейс в violations
            if not vid and norm_act in ("warn", "timeout", "kick", "ban") and guild_id and user_id:
                vid = db.add_violation(
                    guild_id,
                    user_id,
                    rule_id=rule_id or "manual",
                    severity="high" if norm_act == "ban" else "medium",
                    method="pulse_web",
                    reason=reason or f"Наказание ({norm_act}) выдано через веб-панель Pulse",
                    message_snapshot=reason or f"Выдано вручную из веб-панели Pulse ({mod_disp})",
                    original_text=reason or f"Выдано вручную из веб-панели Pulse ({mod_disp})",
                )
                with db._session() as conn:
                    conn.execute("UPDATE punishments SET violation_id = ? WHERE id = ?", (vid, pid))
                    conn.commit()

            ok, _ = await self._apply_discord_punishment(
                guild_id=guild_id,
                user_id=user_id,
                action=norm_act,
                duration_sec=duration_sec,
                violation_id=vid,
                rule_id=rule_id,
                reason=reason,
                moderator_display=mod_disp,
                punishment_id=pid,
            )
            if str(r_d.get("status") or "") == "pending":
                final_st = "dismissed" if norm_act == "dismiss" else ("applied" if ok else "failed")
                db.mark_punishment_status(pid, final_st)

    async def _apply_discord_punishment(
        self,
        guild_id: int,
        user_id: int,
        action: str,
        duration_sec: int = 0,
        violation_id: int = 0,
        rule_id: str = "",
        reason: str = "",
        new_nick: str = "",
        moderator_display: str = "Веб-панель Pulse",
        punishment_id: int = 0,
    ) -> tuple[bool, str]:
        """Непосредственно выполняет действие модерации в Discord, отправляет уведомление
        нарушителю в ЛС и обновляет/публикует эмбед в канале модерации."""
        db = get_db()
        v = db.get_violation(violation_id) if violation_id else None
        if v:
            guild_id = guild_id or int(v.get("guild_id") or 0)
            user_id = user_id or int(v.get("user_id") or 0)
            rule_id = rule_id or str(v.get("rule_id") or "")
            reason = reason or str(v.get("reason") or "")

        orig_text = str(v.get("original_text") or "") if v else ""
        channel_id = int(v.get("channel_id") or 0) if v else 0
        message_id = int(v.get("message_id") or 0) if v else 0

        from bot.rules import fmt_duration

        # 1. Снятие нарушения («Не нарушение») и пропуск («Пропустить»)
        if action == "dismiss":
            try:
                from bot.moderation import mark_phrase_dismissed
                if orig_text:
                    mark_phrase_dismissed(orig_text, rule_id)
                log.info("[PULSE] Нарушение #%d снято через веб-панель Pulse (фраза добавлена в исключения ИИ).", violation_id)
            except Exception as e:
                log.debug("[PULSE] Ошибка mark_phrase_dismissed: %s", e)
            await self._update_or_post_mod_panel(
                guild_id, user_id, violation_id, "Не нарушение (панель снята)", "", moderator_display, reason
            )
            self._log_web_telemetry(guild_id, user_id, violation_id, action, 0, "dismissed", moderator_display, v)
            return True, "Нарушение снято (добавлено в исключения ИИ)"

        if action == "skip":
            log.info("[PULSE] Нарушение #%d пропущено через веб-панель Pulse.", violation_id)
            await self._update_or_post_mod_panel(
                guild_id, user_id, violation_id, "Пропущено (без наказания)", "", moderator_display, reason
            )
            self._log_web_telemetry(guild_id, user_id, violation_id, action, 0, "applied", moderator_display, v)
            return True, "Нарушение пропущено без страйка"

        if getattr(self, "bot", None) is None:
            return True, "Записано в БД"

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False, f"Сервер Discord {guild_id} не найден в кэше бота"

        # 2. Удаляем исходное сообщение-нарушение при delete / timeout / kick / ban
        msg_deleted = False
        if action in ("delete", "timeout", "kick", "ban") and channel_id and message_id:
            ch = guild.get_channel(channel_id)
            if ch is not None:
                try:
                    await ch.get_partial_message(message_id).delete()
                    msg_deleted = True
                except discord.HTTPException:
                    pass

        audit_reason = f"Pulse Web Panel ({moderator_display})"
        if violation_id:
            audit_reason += f" · Нарушение #{violation_id}"
        if rule_id:
            audit_reason += f" (п. {rule_id})"
        if reason:
            audit_reason += f": {reason[:180]}"

        member = guild.get_member(user_id) if user_id else None
        if member is None and user_id and action in ("warn", "timeout", "untimeout", "kick", "nick", "reset_user"):
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                member = None

        if member is not None:
            try:
                db.record_discord_member(guild_id, member)
            except Exception:
                pass

        action_title = action
        detail_str = ""
        ok = True
        result_msg = "Выполнено"

        if action == "delete":
            action_title = "Удаление сообщения"
            result_msg = "Сообщение удалено" if msg_deleted else "Запись об удалении сохранена"
            log.info("[PULSE] Сообщение нарушения #%d удалено по команде из веб-панели Pulse.", violation_id)

        elif action == "warn":
            action_title = "Предупреждение (Варн)"
            await self._notify_user_dm(guild, member, user_id, action_title, "", rule_id, reason)
            result_msg = "Предупреждение выдано"
            log.info("[PULSE] Предупреждение выдано пользователю %d из веб-панели Pulse.", user_id)

        elif action == "timeout" and user_id:
            dur = duration_sec if duration_sec > 0 else 3600
            dur = min(dur, 28 * 86400)
            detail_str = fmt_duration(dur)
            action_title = "Тайм-аут (Мут)"
            if member is None:
                ok = False
                result_msg = "Участник не найден на сервере"
            else:
                try:
                    await member.timeout(timedelta(seconds=dur), reason=audit_reason)
                    await self._notify_user_dm(guild, member, user_id, action_title, detail_str, rule_id, reason)
                    result_msg = f"Тайм-аут ({detail_str}) выдан"
                    log.info("[PULSE] Тайм-аут (%d сек) выдан пользователю %d из веб-панели Pulse.", dur, user_id)
                except discord.HTTPException as e:
                    ok = False
                    result_msg = f"Ошибка Discord API при выдаче тайм-аута: {e}"
                    log.warning("[PULSE] Не удалось выдать тайм-аут пользователю %d: %s", user_id, e)

        elif action == "untimeout" and user_id:
            action_title = "Снятие тайм-аута (Размут)"
            if member is None:
                ok = False
                result_msg = "Участник не найден на сервере"
            else:
                try:
                    await member.timeout(None, reason=audit_reason)
                    result_msg = "Тайм-аут снят"
                    log.info("[PULSE] Тайм-аут снят с пользователя %d из веб-панели Pulse.", user_id)
                except discord.HTTPException as e:
                    ok = False
                    result_msg = f"Ошибка Discord API: {e}"

        elif action == "kick" and user_id:
            action_title = "Кик с сервера"
            if member is None:
                ok = False
                result_msg = "Участник не найден на сервере"
            else:
                await self._notify_user_dm(guild, member, user_id, action_title, "", rule_id, reason)
                try:
                    await member.kick(reason=audit_reason)
                    result_msg = "Участник кикнут с сервера"
                    log.info("[PULSE] Пользователь %d кикнут из веб-панели Pulse.", user_id)
                except discord.HTTPException as e:
                    ok = False
                    result_msg = f"Ошибка Discord API при кике: {e}"
                    log.warning("[PULSE] Не удалось кикнуть пользователя %d: %s", user_id, e)

        elif action == "ban" and user_id:
            action_title = "Бан"
            detail_str = fmt_duration(duration_sec) if duration_sec > 0 else "перманентно"
            await self._notify_user_dm(guild, member, user_id, action_title, detail_str, rule_id, reason)
            try:
                await guild.ban(
                    discord.Object(id=user_id),
                    reason=audit_reason,
                    delete_message_days=1,
                )
                result_msg = f"Бан ({detail_str}) применён"
                log.info("[PULSE] Пользователь %d забанен (%s) по команде из веб-панели Pulse (#%d).", user_id, detail_str, violation_id)
            except discord.HTTPException as e:
                ok = False
                result_msg = f"Ошибка Discord API при бане: {e}"
                log.warning("[PULSE] Не удалось забанить пользователя %d: %s", user_id, e)

        elif action == "unban" and user_id:
            action_title = "Разбан"
            try:
                await guild.unban(discord.Object(id=user_id), reason=audit_reason)
                with db._session() as conn:
                    conn.execute(
                        "UPDATE punishments SET status = 'unbanned' WHERE guild_id = ? AND user_id = ? AND action = 'ban' AND status = 'applied'",
                        (guild_id, user_id),
                    )
                    conn.commit()
                result_msg = "Пользователь разбанен"
                log.info("[PULSE] Пользователь %d разбанен по команде из веб-панели Pulse.", user_id)
            except discord.HTTPException as e:
                ok = False
                result_msg = f"Ошибка Discord API при разбане: {e}"

        elif action == "nick" and user_id:
            target_nick = (new_nick or reason or f"Участник #{str(user_id)[-4:]}").strip()[:32]
            action_title = "Смена никнейма"
            detail_str = f"«{target_nick}»"
            if member is None:
                ok = False
                result_msg = "Участник не найден на сервере"
            else:
                try:
                    await member.edit(nick=target_nick, reason=audit_reason)
                    db.record_discord_member(guild_id, member)
                    result_msg = f"Никнейм изменён на {detail_str}"
                except discord.HTTPException as e:
                    ok = False
                    result_msg = f"Ошибка смены никнейма: {e}"

        elif action == "reset_user" and user_id:
            action_title = "Сброс страйков и наказаний"
            if member is not None and getattr(member, "is_timed_out", lambda: False)():
                try:
                    await member.timeout(None, reason=audit_reason)
                except discord.HTTPException:
                    pass
            result_msg = "Все страйки сброшены"

        if ok:
            await self._update_or_post_mod_panel(
                guild_id, user_id, violation_id, action_title, detail_str, moderator_display, reason
            )
            self._log_web_telemetry(
                guild_id, user_id, violation_id, action, duration_sec, "applied", moderator_display, v
            )
        return ok, result_msg

    async def _notify_user_dm(
        self,
        guild: discord.Guild,
        member: Optional[discord.Member],
        user_id: int,
        action_title: str,
        duration_label: str,
        rule_id: str,
        reason: str,
    ) -> None:
        """Отправляет участнику уведомление в ЛС о вынесенном из веб-панели наказании."""
        target = member
        if target is None and getattr(self, "bot", None) is not None and user_id:
            try:
                target = await self.bot.fetch_user(user_id)
            except Exception:
                target = None
        if target is None:
            return
        try:
            from bot.rules import RULES
            rule_title = RULES.get(rule_id, {}).get("title", "") if rule_id else ""
            embed = discord.Embed(
                title=f"⚖ Модерация сервера «{guild.name}»",
                color=discord.Color(0xE53E3E if "Бан" in action_title else 0x7F5AF0),
            )
            act_val = f"**{action_title}**" + (f" ({duration_label})" if duration_label else "")
            embed.add_field(name="Наказание", value=act_val, inline=True)
            if rule_id:
                r_str = f"**{rule_id}**" + (f" — {rule_title}" if rule_title else "")
                embed.add_field(name="Пункт правил", value=r_str, inline=True)
            if reason:
                embed.add_field(name="Причина", value=reason[:900], inline=False)
            embed.set_footer(text=f"Вынесено через веб-панель Pulse · {guild.name}")
            await target.send(embed=embed)
        except Exception:
            pass

    async def _update_or_post_mod_panel(
        self,
        guild_id: int,
        user_id: int,
        violation_id: int,
        action_title: str,
        detail_str: str,
        moderator_display: str,
        reason: str = "",
    ) -> None:
        """Обновляет существующую панель нарушения в #mod-log (перекрашивает в зелёный и отключает кнопки)
        либо отправляет новый отчётный эмбед, если наказание было выдано напрямую из веб-панели."""
        if getattr(self, "bot", None) is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        db = get_db()
        v = db.get_violation(violation_id) if violation_id else None
        panel_ch_id = int(v.get("panel_channel_id") or 0) if v else 0
        panel_msg_id = int(v.get("panel_message_id") or 0) if v else 0

        from bot.client import RECENT_PANELS, BRAND_GREEN, _resolve_mod_channel
        from bot.views.mod_buttons import ModActionView

        if (not panel_ch_id or not panel_msg_id) and user_id:
            info = RECENT_PANELS.get((guild_id, user_id))
            if info and (not violation_id or info[2] == violation_id):
                panel_ch_id, panel_msg_id = info[0], info[1]

        stamp = time.strftime("%d.%m %H:%M")
        val_text = f"**{action_title}**" + (f" ({detail_str})" if detail_str else "")
        if reason:
            val_text += f"\nПричина: *{reason[:200]}*"
        val_text += f"\nМодератор: 🌐 **{moderator_display}** · {stamp}"

        # Если есть сообщение карточки нарушения в Discord — редактируем его и отключаем кнопки
        if panel_ch_id and panel_msg_id:
            ch = guild.get_channel(panel_ch_id)
            if ch is not None:
                try:
                    panel_msg = await ch.fetch_message(panel_msg_id)
                    if panel_msg.embeds:
                        embed = discord.Embed.from_dict(panel_msg.embeds[0].to_dict())
                        embed.add_field(
                            name="⚖ Наказание вынесено",
                            value=val_text,
                            inline=False,
                        )
                        embed.color = discord.Color(BRAND_GREEN)
                        view = ModActionView(
                            guild_id,
                            user_id,
                            violation_id,
                            target_msg_id=v.get("message_id") if v else None,
                            channel_id=v.get("channel_id") if v else None,
                        )
                        for child in view.children:
                            if isinstance(child, discord.ui.Button):
                                child.disabled = True
                        await panel_msg.edit(embed=embed, view=view)
                        return
                except Exception:
                    pass

        # Если карточки не было (ручное наказание из веб-панели) — отправляем лог в канал модерации
        server = db.get_server(guild_id)
        mod_ch = _resolve_mod_channel(guild, server.get("mod_channel_id"))
        if mod_ch is None:
            return
        try:
            u_prof = db.get_user_profile(guild_id, user_id) if user_id else None
            u_label = f"<@{user_id}> (`{user_id}`)" if user_id else "—"
            if u_prof and u_prof.get("display_name"):
                u_label = f"**{u_prof['display_name']}** (<@{user_id}>)"
            embed = discord.Embed(
                title=f"🌐 Наказание из веб-панели Pulse" + (f" (№{violation_id})" if violation_id else ""),
                color=discord.Color(BRAND_GREEN),
            )
            if u_prof and u_prof.get("avatar_url"):
                embed.set_thumbnail(url=u_prof["avatar_url"])
            embed.add_field(name="Участник", value=u_label, inline=True)
            embed.add_field(name="Решение", value=val_text, inline=False)
            await mod_ch.send(embed=embed)
        except Exception:
            pass

    def _log_web_telemetry(
        self,
        guild_id: int,
        user_id: int,
        violation_id: int,
        action: str,
        duration_sec: int,
        status: str,
        moderator_display: str,
        v: Optional[dict[str, Any]],
    ) -> None:
        try:
            from bot.telemetry import log_event
            log_event(
                "moderator_action",
                source="pulse_web",
                guild_id=guild_id,
                guild_name=v.get("guild_name") if v else None,
                user_id=user_id,
                user_name=v.get("user_name") if v else None,
                user_display_name=v.get("user_display_name") if v else None,
                user_avatar_url=v.get("user_avatar_url") if v else None,
                violation_id=violation_id,
                moderator_id=0,
                moderator_name="pulse_web",
                moderator_display_name=moderator_display,
                action=action,
                duration_seconds=duration_sec,
                status=status,
                rule_id=v.get("rule_id") if v else None,
                severity=v.get("severity") if v else None,
                method=v.get("method") if v else "pulse_web",
                text=v.get("original_text") if v else None,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Синхронизация с удалённым сервером через Pterodactyl Client API
    # ------------------------------------------------------------------
    async def sync_remote_pterodactyl(self) -> bool:
        """Синхронизирует modbot.db, telemetry.jsonl и логи с контейнером через Pterodactyl Client API."""
        if not config.PTERODACTYL_URL or not config.PTERODACTYL_API_KEY:
            return False

        headers = {
            "Authorization": f"Bearer {config.PTERODACTYL_API_KEY}",
            "Accept": "application/json",
        }
        base_url = config.PTERODACTYL_URL

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            # 1. Автоопределение ID сервера в Pterodactyl, если не задан
            if not self._discovered_Server_id:
                r = await client.get(f"{base_url}/api/client", headers=headers)
                if r.status_code != 200:
                    log.warning("[PULSE-PTERO] Ошибка доступа к Pterodactyl API (%d): проверьте PTERODACTYL_API_KEY", r.status_code)
                    return False
                items = (r.json() or {}).get("data") or []
                if not items:
                    return False
                self._discovered_Server_id = items[0]["attributes"]["identifier"]
                log.info("[PULSE-PTERO] Автоматически выбран сервер Pterodactyl: %s", self._discovered_Server_id)

            srv_id = self._discovered_Server_id
            api_files = f"{base_url}/api/client/servers/{srv_id}/files"

            # 2. Создаём папки /pmx-bot и /pmx-bot/data в контейнере
            await client.post(
                f"{api_files}/create-folder",
                headers=headers,
                json={"root": "/", "name": "pmx-bot"},
            )
            await client.post(
                f"{api_files}/create-folder",
                headers=headers,
                json={"root": "/pmx-bot", "name": "data"},
            )

            # 3. Скачиваем текущую БД из контейнера (если она уже есть), сливаем изменения и загружаем обратно
            tmp_dir = Path(tempfile.mkdtemp(prefix="pulse_ptero_"))
            try:
                tmp_db = tmp_dir / "modbot.db"
                dl = await client.get(
                    f"{api_files}/contents",
                    params={"file": "/pmx-bot/data/modbot.db"},
                    headers=headers,
                )
                if dl.status_code == 200 and dl.content and dl.content.startswith(b"SQLite format 3"):
                    tmp_db.write_bytes(dl.content)

                await asyncio.to_thread(self.sync_sqlite_bridge, tmp_db, None, None)

                # Отправляем обновлённую БД в контейнер Pterodactyl
                db_bytes = tmp_db.read_bytes()
                up_headers = {**headers, "Content-Type": "application/octet-stream"}
                await client.post(
                    f"{api_files}/write",
                    params={"file": "/pmx-bot/data/modbot.db"},
                    headers=up_headers,
                    content=db_bytes,
                )

                # Отправляем метаданные .env и маркер main.py
                safe_env = (
                    f"LLM_PROVIDER={config.LLM_PROVIDER}\n"
                    f"GROQ_MODEL={config.GROQ_MODEL}\n"
                    f"GEMINI_MODEL={config.GEMINI_MODEL}\n"
                    f"DEEPSEEK_MODEL={config.DEEPSEEK_MODEL}\n"
                    f"OPENROUTER_MODEL={config.OPENROUTER_MODEL}\n"
                    f"STRIKE_DECAY_DAYS={config.STRIKE_DECAY_DAYS}\n"
                ).encode("utf-8")
                await client.post(
                    f"{api_files}/write",
                    params={"file": "/pmx-bot/.env"},
                    headers=up_headers,
                    content=safe_env,
                )
                await client.post(
                    f"{api_files}/write",
                    params={"file": "/pmx-bot/main.py"},
                    headers=up_headers,
                    content=(self.root_dir / "main.py").read_bytes(),
                )
                return True
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Встроенный HTTP API сервер (/api/dap/*)
    # ------------------------------------------------------------------
    async def _handle_http_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            req_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not req_line:
                return
            parts = req_line.decode("utf-8", errors="ignore").strip().split(" ")
            if len(parts) < 2:
                return
            method, raw_target = parts[0].upper(), parts[1]

            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not line or line in (b"\r\n", b"\n"):
                    break
                h_str = line.decode("utf-8", errors="ignore").strip()
                if ":" in h_str:
                    k, v = h_str.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            content_len = int(headers.get("content-length") or "0")
            body = b""
            if content_len > 0:
                body = await asyncio.wait_for(reader.readexactly(min(content_len, 10 * 1024 * 1024)), timeout=10.0)

            # Проверка токена, если задан PULSE_API_TOKEN
            if config.PULSE_API_TOKEN:
                auth = headers.get("authorization", "")
                x_tok = headers.get("x-pulse-token", "")
                if auth != f"Bearer {config.PULSE_API_TOKEN}" and x_tok != config.PULSE_API_TOKEN:
                    await self._send_http_json(writer, 401, {"ok": False, "error": "Unauthorized"})
                    return

            parsed = urllib.parse.urlsplit(raw_target)
            path = parsed.path
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

            if path == "/api/dap/export":
                days = int(query.get("days") or "30")
                csv_data = await asyncio.to_thread(self._build_csv_export, query.get("guild"), days)
                await self._send_http_raw(
                    writer,
                    200,
                    "text/csv; charset=utf-8",
                    csv_data.encode("utf-8"),
                )
                return

            if path == "/api/dap/sync/db" and method == "GET":
                db_bytes = Path(config.DB_PATH).read_bytes() if Path(config.DB_PATH).is_file() else b""
                await self._send_http_raw(writer, 200, "application/octet-stream", db_bytes)
                return

            resp = await self._dispatch_http_api(method, path, query, body)
            await self._send_http_json(writer, 200, resp)
        except Exception as e:
            try:
                await self._send_http_json(writer, 500, {"ok": False, "error": str(e)})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch_http_api(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        body: bytes,
    ) -> dict[str, Any]:
        if method == "POST" and path in ("/api/dap/action", "/api/dap/punish"):
            payload = json.loads(body.decode("utf-8", errors="ignore") or "{}")
            if path == "/api/dap/punish" and not payload.get("type"):
                payload["type"] = "punish"
            res = await asyncio.to_thread(self._handle_api_action, payload)
            await self.execute_pending_web_decisions()
            return res

        days = int(query.get("days") or "30")
        guild = query.get("guild") or "_all"
        if path in ("/api/dap/module", "/api/dap/status"):
            return self._api_module_status()
        if path == "/api/dap/overview":
            return await asyncio.to_thread(self._api_overview, guild, days)
        return self._api_module_status()

    def _api_module_status(self) -> dict[str, Any]:
        db_file = Path(config.DB_PATH)
        db_exists = db_file.is_file()
        db_kb = db_file.stat().st_size // 1024 if db_exists else 0
        return {
            "ok": True,
            "enabled": True,
            "installed": True,
            "botDir": str(self.root_dir).replace("\\", "/"),
            "dbPath": str(db_file.resolve()).replace("\\", "/"),
            "dbExists": db_exists,
            "dbSizeKb": db_kb,
            "writable": True,
            "readOnly": False,
            "process": {
                "running": not self.bot.is_closed(),
                "pid": os.getpid(),
                "uptimeSeconds": int(time.time() - self.start_time),
                "llmProvider": config.LLM_PROVIDER,
                "llmModel": getattr(config, f"{config.LLM_PROVIDER.upper()}_MODEL", ""),
                "strikeDecayDays": config.STRIKE_DECAY_DAYS,
                "hasEnv": (self.root_dir / ".env").is_file(),
                "hasMainPy": (self.root_dir / "main.py").is_file(),
            },
        }

    def _api_overview(self, guild_param: str, days: int) -> dict[str, Any]:
        out = self._api_module_status()
        db = get_db()
        gid = int(guild_param) if guild_param and guild_param.isdigit() else None
        with db._session() as conn:
            g_rows = conn.execute("SELECT * FROM servers").fetchall()
            guilds = []
            for gr in g_rows:
                gr_d = dict(gr)
                vc = conn.execute(
                    "SELECT COUNT(*) AS c FROM violations WHERE guild_id = ?",
                    (gr_d["guild_id"],),
                ).fetchone()["c"]
                guilds.append({
                    "guildId": str(gr_d["guild_id"]),
                    "guildName": gr_d.get("guild_name") or "",
                    "guildIconUrl": gr_d.get("guild_icon_url") or "",
                    "memberCount": int(gr_d.get("member_count") or 0),
                    "modChannelId": str(gr_d.get("mod_channel_id") or ""),
                    "modChannelName": gr_d.get("mod_channel_name") or "",
                    "modRoleId": str(gr_d.get("mod_role_id") or ""),
                    "modRoleName": gr_d.get("mod_role_name") or "",
                    "targetLanguage": gr_d.get("target_language") or "ru",
                    "deleteMessage": bool(gr_d.get("delete_message")),
                    "llmEnabled": bool(gr_d.get("llm_enabled")),
                    "manualStrikes": bool(gr_d.get("manual_strikes")),
                    "violationsCount": vc,
                })
            out["guilds"] = guilds
            out["activeGuild"] = str(gid) if gid else "_all"
            if gid:
                dstats = db.get_digest_stats(gid, days=days)
                out["kpis"] = {
                    "totalViolations": dstats["total_violations"],
                    "realViolations": dstats["real_violations"],
                    "dismissedCount": dstats["dismissed"],
                    "skippedCount": dstats["skipped"],
                    "punishedCount": sum(dstats["actions"].values()),
                }
                out["channels"] = [
                    {
                        "channelId": str(r["channel_id"]),
                        "name": r["name"],
                        "type": r["type"] or "text",
                        "categoryName": r["category_name"] or "",
                    }
                    for r in conn.execute(
                        "SELECT * FROM channels WHERE guild_id = ? ORDER BY position ASC",
                        (gid,),
                    ).fetchall()
                ]
                out["roles"] = [
                    {
                        "roleId": str(r["role_id"]),
                        "name": r["name"],
                        "color": r["color"] or "",
                        "isStaff": bool(r["is_staff"]),
                    }
                    for r in conn.execute(
                        "SELECT * FROM roles WHERE guild_id = ? ORDER BY position DESC",
                        (gid,),
                    ).fetchall()
                ]
                out["users"] = [
                    {
                        "userId": str(r["user_id"]),
                        "username": r["username"] or "",
                        "globalName": r["global_name"] or "",
                        "displayName": r["display_name"] or r["username"] or str(r["user_id"]),
                        "avatarUrl": r["avatar_url"] or "",
                        "topRoleName": r["top_role_name"] or "",
                        "topRoleColor": r["top_role_color"] or "",
                        "isModerator": bool(r["is_moderator"]),
                        "strikes": db.count_real_violations(gid, int(r["user_id"])),
                    }
                    for r in conn.execute(
                        "SELECT * FROM users WHERE guild_id = ? ORDER BY updated_at DESC LIMIT 300",
                        (gid,),
                    ).fetchall()
                ]
        return out

    def _handle_api_action(self, body: dict[str, Any]) -> dict[str, Any]:
        from bot.views.mod_buttons import parse_duration

        act_type = str(body.get("type") or "").strip().lower()
        action = str(body.get("action") or "").strip().lower()
        if not act_type and action:
            act_type = "decide" if body.get("violationId") else "punish"
        elif act_type in (
            "warn", "delete", "timeout", "mute", "untimeout", "unmute",
            "kick", "ban", "unban", "nick", "reset_nick", "dismiss", "skip",
        ):
            action = act_type
            act_type = "decide" if body.get("violationId") else "punish"

        dur = int(body.get("durationSeconds") or 0)
        if not dur and body.get("duration"):
            default_unit = "d" if action == "ban" else "m"
            dur = parse_duration(str(body.get("duration")), default_unit=default_unit)

        reason = str(body.get("reason") or "").strip()
        new_nick = str(body.get("newNick") or body.get("nick") or "").strip()
        rule_id = str(body.get("ruleId") or body.get("rule") or "").strip()
        mod_name = str(body.get("moderatorName") or body.get("moderator") or "Веб-панель Pulse").strip()

        db = get_db()
        if act_type == "decide":
            vid = int(body.get("violationId") or 0)
            v = db.get_violation(vid)
            if not v:
                return {"ok": False, "error": f"Нарушение #{vid} не найдено"}
            action_id = db.enqueue_web_action(
                guild_id=int(v["guild_id"]),
                user_id=int(v["user_id"]),
                violation_id=vid,
                rule_id=rule_id or v.get("rule_id"),
                action=action or "dismiss",
                duration_seconds=dur,
                reason=reason,
                new_nick=new_nick,
                moderator_display_name=mod_name,
            )
            return {"ok": True, "actionId": action_id, "violationId": vid}

        if act_type in ("punish", "mod_action"):
            gid = int(body.get("guildId") or 0)
            uid = int(body.get("userId") or 0)
            vid = int(body.get("violationId") or 0)
            if vid and (not gid or not uid):
                v = db.get_violation(vid)
                if v:
                    gid = gid or int(v["guild_id"])
                    uid = uid or int(v["user_id"])
            if not gid or not uid:
                return {"ok": False, "error": "Укажите guildId и userId"}
            action_id = db.enqueue_web_action(
                guild_id=gid,
                user_id=uid,
                violation_id=vid or None,
                rule_id=rule_id or None,
                action=action or "timeout",
                duration_seconds=dur,
                reason=reason or None,
                new_nick=new_nick or None,
                moderator_display_name=mod_name,
            )
            return {"ok": True, "actionId": action_id}

        if act_type in ("reset_user", "clear_strikes"):
            gid = int(body.get("guildId") or 0)
            uid = int(body.get("userId") or 0)
            if gid and uid:
                action_id = db.enqueue_web_action(
                    guild_id=gid,
                    user_id=uid,
                    action="reset_user",
                    reason=reason or "Сброс статистики из веб-панели",
                    moderator_display_name=mod_name,
                )
                return {"ok": True, "actionId": action_id}
            return {"ok": False, "error": "Укажите guildId и userId"}

        return {"ok": True}

    def _build_csv_export(self, guild_param: Optional[str], days: int) -> str:
        db = get_db()
        gid = int(guild_param) if guild_param and guild_param.isdigit() else 0
        rows = db.export_guild_records(gid, days=days) if gid else []
        lines = [
            "\ufeffID нарушения;Дата нарушения (UTC);ID пользователя;ID канала;Правило;Тяжесть;Метод;Текст;Перевод;Решение;Длительность (сек);ID модератора;Статус;Дата решения"
        ]
        for r in rows:
            lines.append(
                f"{r['violation_id']};{r['violation_time']};{r['user_id']};{r['channel_id']};"
                f"{r['rule_id']};{r['severity']};{r['method']};{r['original_text'] or ''};"
                f"{r['translated_text'] or ''};{r['mod_action'] or 'ожидает'};"
                f"{r['duration_seconds'] or ''};{r['moderator_id'] or ''};"
                f"{r['punishment_status'] or ''};{r['decision_time'] or ''}"
            )
        return "\r\n".join(lines) + "\r\n"

    async def _send_http_json(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict[str, Any],
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await self._send_http_raw(writer, status, "application/json; charset=utf-8", raw)

    async def _send_http_raw(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        content_type: str,
        body: bytes,
    ) -> None:
        status_text = {200: "OK", 401: "Unauthorized", 404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
        header = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(header + body)
        await writer.drain()


def setup_pterodactyl_bridge_cli(custom_dir: Optional[str] = None) -> list[str]:
    """Утилита для CLI (`pmx pulse` / `install.sh`): инициализирует БД и сразу пробрасывает бота
    во все найденные контейнеры Pterodactyl на сервере."""
    get_db().init()
    if custom_dir:
        prev = config.PULSE_BRIDGE_DIRS
        config.PULSE_BRIDGE_DIRS = f"{prev},{custom_dir}" if prev else custom_dir

    class _DummyClient:
        guilds = []

        def is_closed(self) -> bool:
            return False

        def get_guild(self, _gid: int):
            return None

    bridge = PulseBridge(_DummyClient())  # type: ignore[arg-type]
    return bridge.sync_all_local_bridges()

