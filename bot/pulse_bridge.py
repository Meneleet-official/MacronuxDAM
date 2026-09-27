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
from bot.database import SCHEMA, get_db
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
        # Гарантируем, что все текущие серверы Discord записаны в таблицу servers основной БД
        try:
            db = get_db()
            for g in list(getattr(self.bot, "guilds", [])):
                db.get_server(g.id)
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
            bridge_conn.executescript(SCHEMA)
            try:
                bridge_conn.execute("ALTER TABLE servers ADD COLUMN mod_role_id INTEGER")
            except sqlite3.OperationalError:
                pass
            bridge_conn.commit()

            # --- ШАГ 1: Если снимок уже инициализирован, применяем изменения из веб-панели Pulse в основную БД ---
            if snap.initialized:
                # 1a. Настройки серверов (servers)
                b_servers = {
                    int(r["guild_id"]): dict(r)
                    for r in bridge_conn.execute("SELECT * FROM servers").fetchall()
                }
                for gid, b_row in b_servers.items():
                    old_row = snap.servers.get(gid)
                    if old_row != b_row:
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
                                gid,
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

                # 1c. Новые или изменённые вердикты модерации из веб-панели (actionDecide в Pulse)
                b_puns = bridge_conn.execute(
                    "SELECT * FROM punishments ORDER BY id ASC"
                ).fetchall()
                for pr in b_puns:
                    vid = pr["violation_id"]
                    if vid is None:
                        continue
                    vid = int(vid)
                    sig = (
                        str(pr["action"] or ""),
                        int(pr["duration_seconds"] or 0),
                        str(pr["status"] or "applied"),
                    )
                    if snap.punishments_by_vid.get(vid) != sig and int(pr["moderator_id"] or 0) == 0:
                        main_conn.execute("DELETE FROM punishments WHERE violation_id = ?", (vid,))
                        main_conn.execute(
                            """INSERT INTO punishments
                               (guild_id, user_id, violation_id, moderator_id, action, duration_seconds, status, created_at)
                               VALUES (?, ?, ?, 0, ?, ?, ?, ?)""",
                            (
                                pr["guild_id"],
                                pr["user_id"],
                                vid,
                                pr["action"],
                                pr["duration_seconds"] or 0,
                                pr["status"] or "applied",
                                pr["created_at"],
                            ),
                        )

                # 1d. Серверные банворды (добавленные/удалённые в веб-панели Pulse)
                b_words = {
                    (int(r["guild_id"]), str(r["word"]))
                    for r in bridge_conn.execute("SELECT guild_id, word FROM banned_words").fetchall()
                }
                for gid, w in b_words - snap.banwords:
                    main_conn.execute(
                        "INSERT OR IGNORE INTO banned_words (guild_id, word) VALUES (?, ?)",
                        (gid, w),
                    )
                for gid, w in snap.banwords - b_words:
                    main_conn.execute(
                        "DELETE FROM banned_words WHERE guild_id = ? AND word = ?",
                        (gid, w),
                    )

                # 1e. Исключения правил по каналам (добавленные/удалённые в веб-панели Pulse)
                b_ex = {
                    (int(r["guild_id"]), int(r["channel_id"]), str(r["rule"]))
                    for r in bridge_conn.execute(
                        "SELECT guild_id, channel_id, rule FROM rule_exceptions"
                    ).fetchall()
                }
                for gid, cid, rl in b_ex - snap.exceptions:
                    main_conn.execute(
                        "INSERT OR IGNORE INTO rule_exceptions (guild_id, channel_id, rule) VALUES (?, ?, ?)",
                        (gid, cid, rl),
                    )
                for gid, cid, rl in snap.exceptions - b_ex:
                    main_conn.execute(
                        "DELETE FROM rule_exceptions WHERE guild_id = ? AND channel_id = ? AND rule = ?",
                        (gid, cid, rl),
                    )

                main_conn.commit()
            else:
                # При первой инициализации: если в контейнере уже были настройки серверов/банворды/решения,
                # бережно переносим их в основную БД без удаления данных бота.
                for r in bridge_conn.execute("SELECT * FROM servers").fetchall():
                    main_conn.execute(
                        """INSERT OR IGNORE INTO servers
                           (guild_id, mod_channel_id, mod_role_id, target_language,
                            delete_message, llm_enabled, strike_thresholds, manual_strikes)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            r["guild_id"],
                            r["mod_channel_id"],
                            r["mod_role_id"],
                            r["target_language"] or "ru",
                            r["delete_message"] or 0,
                            r["llm_enabled"] if r["llm_enabled"] is not None else 1,
                            r["strike_thresholds"] or "{}",
                            r["manual_strikes"] or 0,
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
                main_conn.commit()

            # --- ШАГ 2: Зеркалируем актуальное состояние основной БД в БД контейнера Pterodactyl ---
            # 2a. servers
            m_servers = [dict(r) for r in main_conn.execute("SELECT * FROM servers").fetchall()]
            for r in m_servers:
                bridge_conn.execute(
                    """INSERT INTO servers (guild_id, mod_channel_id, mod_role_id, target_language,
                                            delete_message, llm_enabled, strike_thresholds, manual_strikes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(guild_id) DO UPDATE SET
                           mod_channel_id = excluded.mod_channel_id,
                           mod_role_id = excluded.mod_role_id,
                           target_language = excluded.target_language,
                           delete_message = excluded.delete_message,
                           llm_enabled = excluded.llm_enabled,
                           strike_thresholds = excluded.strike_thresholds,
                           manual_strikes = excluded.manual_strikes""",
                    (
                        r["guild_id"],
                        r["mod_channel_id"],
                        r.get("mod_role_id"),
                        r["target_language"],
                        r["delete_message"],
                        r["llm_enabled"],
                        r["strike_thresholds"],
                        r["manual_strikes"],
                    ),
                )

            # 2b. violations
            m_viols = [dict(r) for r in main_conn.execute("SELECT * FROM violations").fetchall()]
            m_vid_set = {int(r["id"]) for r in m_viols}
            b_vid_set = {int(r["id"]) for r in bridge_conn.execute("SELECT id FROM violations").fetchall()}
            for r in m_viols:
                if int(r["id"]) not in b_vid_set:
                    bridge_conn.execute(
                        """INSERT INTO violations
                           (id, guild_id, user_id, rule_id, severity, method, message_snapshot,
                            original_text, translated_text, detected_language, channel_id, message_id, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            r["id"],
                            r["guild_id"],
                            r["user_id"],
                            r["rule_id"],
                            r["severity"],
                            r["method"],
                            r["message_snapshot"],
                            r["original_text"],
                            r["translated_text"],
                            r["detected_language"],
                            r["channel_id"],
                            r["message_id"],
                            r["created_at"],
                        ),
                    )
            for stale_vid in b_vid_set - m_vid_set:
                bridge_conn.execute("DELETE FROM violations WHERE id = ?", (stale_vid,))

            # 2c. punishments
            m_puns = [dict(r) for r in main_conn.execute("SELECT * FROM punishments ORDER BY id ASC").fetchall()]
            bridge_conn.execute("DELETE FROM punishments")
            for r in m_puns:
                bridge_conn.execute(
                    """INSERT INTO punishments
                       (id, guild_id, user_id, violation_id, moderator_id, action, duration_seconds, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        r["id"],
                        r["guild_id"],
                        r["user_id"],
                        r["violation_id"],
                        r["moderator_id"],
                        r["action"],
                        r["duration_seconds"],
                        r["status"],
                        r["created_at"],
                    ),
                )

            # 2d. banned_words & rule_exceptions
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
            for r in m_puns:
                if r["violation_id"] is not None:
                    snap.punishments_by_vid[int(r["violation_id"])] = (
                        str(r["action"] or ""),
                        int(r["duration_seconds"] or 0),
                        str(r["status"] or "applied"),
                    )
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
    # Выполнение в Discord вердиктов из веб-панели Pulse (moderator_id = 0)
    # ------------------------------------------------------------------
    def _init_processed_punishments(self) -> None:
        db = get_db()
        with db._session() as conn:
            rows = conn.execute(
                "SELECT id FROM punishments WHERE moderator_id = 0 OR moderator_id IS NULL"
            ).fetchall()
            for r in rows:
                self._processed_web_punishments.add(int(r["id"]))

    async def execute_pending_web_decisions(self) -> None:
        """Находит новые решения из веб-панели Pulse (moderator_id = 0) и реально применяет их в Discord."""
        db = get_db()
        with db._session() as conn:
            rows = conn.execute(
                """SELECT p.id AS pid, p.guild_id, p.user_id, p.violation_id, p.action,
                          p.duration_seconds, p.status,
                          v.channel_id, v.message_id, v.original_text, v.rule_id
                   FROM punishments p
                   LEFT JOIN violations v ON v.id = p.violation_id
                   WHERE (p.moderator_id = 0 OR p.moderator_id IS NULL)
                   ORDER BY p.id ASC"""
            ).fetchall()

        for r in rows:
            pid = int(r["pid"])
            if pid in self._processed_web_punishments:
                continue
            self._processed_web_punishments.add(pid)

            action = str(r["action"] or "").lower()
            guild_id = int(r["guild_id"] or 0)
            user_id = int(r["user_id"] or 0)
            vid = int(r["violation_id"] or 0)
            duration_sec = int(r["duration_seconds"] or 0)
            channel_id = int(r["channel_id"] or 0)
            message_id = int(r["message_id"] or 0)
            orig_text = r["original_text"] or ""
            rule_id = r["rule_id"] or ""

            if action == "dismiss":
                try:
                    from bot.moderation import mark_phrase_dismissed
                    if orig_text:
                        mark_phrase_dismissed(orig_text, rule_id)
                    log.info("[PULSE] Нарушение #%d снято через веб-панель Pulse (фраза добавлена в исключения ИИ).", vid)
                except Exception as e:
                    log.debug("[PULSE] Ошибка mark_phrase_dismissed: %s", e)
                continue

            if action == "skip":
                log.info("[PULSE] Нарушение #%d пропущено через веб-панель Pulse.", vid)
                continue

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue

            # Удаляем исходное сообщение-нарушение при delete / timeout / ban
            if action in ("delete", "timeout", "ban") and channel_id and message_id:
                ch = guild.get_channel(channel_id)
                if ch is not None:
                    try:
                        await ch.get_partial_message(message_id).delete()
                    except discord.HTTPException:
                        pass

            if action == "delete":
                log.info("[PULSE] Сообщение нарушения #%d удалено по команде из веб-панели Pulse.", vid)
            elif action == "timeout" and user_id:
                dur = duration_sec if duration_sec > 0 else 3600
                dur = min(dur, 28 * 86400)
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.HTTPException:
                        member = None
                if member is not None:
                    try:
                        await member.timeout(
                            timedelta(seconds=dur),
                            reason=f"Pulse Web Panel: Нарушение #{vid} (п. {rule_id})",
                        )
                        log.info("[PULSE] Тайм-аут (%d сек) выдан пользователю %d из веб-панели Pulse.", dur, user_id)
                    except discord.HTTPException as e:
                        log.warning("[PULSE] Не удалось выдать тайм-аут пользователю %d: %s", user_id, e)
            elif action == "ban" and user_id:
                try:
                    await guild.ban(
                        discord.Object(id=user_id),
                        reason=f"Pulse Web Panel: Нарушение #{vid} (п. {rule_id})",
                        delete_message_days=1,
                    )
                    log.info("[PULSE] Пользователь %d забанен по команде из веб-панели Pulse (#%d).", user_id, vid)
                except discord.HTTPException as e:
                    log.warning("[PULSE] Не удалось забанить пользователя %d: %s", user_id, e)

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
        if method == "POST" and path == "/api/dap/action":
            payload = json.loads(body.decode("utf-8", errors="ignore") or "{}")
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
                vc = conn.execute(
                    "SELECT COUNT(*) AS c FROM violations WHERE guild_id = ?",
                    (gr["guild_id"],),
                ).fetchone()["c"]
                guilds.append({
                    "guildId": str(gr["guild_id"]),
                    "modChannelId": str(gr["mod_channel_id"] or ""),
                    "modRoleId": str(gr["mod_role_id"] or ""),
                    "targetLanguage": gr["target_language"] or "ru",
                    "deleteMessage": bool(gr["delete_message"]),
                    "llmEnabled": bool(gr["llm_enabled"]),
                    "manualStrikes": bool(gr["manual_strikes"]),
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
        return out

    def _handle_api_action(self, body: dict[str, Any]) -> dict[str, Any]:
        act_type = str(body.get("type") or "").strip()
        db = get_db()
        if act_type == "decide":
            vid = int(body.get("violationId") or 0)
            action = str(body.get("action") or "dismiss").lower()
            dur = int(body.get("durationSeconds") or 0)
            v = db.get_violation(vid)
            if not v:
                return {"ok": False, "error": f"Нарушение #{vid} не найдено"}
            with db._session() as conn:
                conn.execute("DELETE FROM punishments WHERE violation_id = ?", (vid,))
                conn.commit()
            db.add_punishment(
                v["guild_id"],
                v["user_id"],
                violation_id=vid,
                moderator_id=0,
                action=action,
                duration_seconds=dur,
                status="dismissed" if action == "dismiss" else "applied",
            )
            return {"ok": True}
        if act_type == "reset_user":
            gid = int(body.get("guildId") or 0)
            uid = int(body.get("userId") or 0)
            if gid and uid:
                db.reset_user_stats(gid, uid)
            return {"ok": True}
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

