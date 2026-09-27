#!/usr/bin/env python3
"""Moderation Discord bot — entry point (with Pulse & Pterodactyl container support)."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT_DIR = Path(__file__).resolve().parent


def _run_container_pulse_agent() -> None:
    """Легковесный агент внутри Docker-контейнера Pterodactyl (не требует внешних библиотек).
    Позволяет плагину Pulse (DapService.java) видеть активный процесс бота и управлять им."""
    data_dir = ROOT_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    ctrl_file = data_dir / "pulse_control.json"
    try:
        ctrl_file.write_text(json.dumps({"cmd": "start", "ts": int(time.time())}) + "\n", encoding="utf-8")
    except Exception:
        pass

    stop_flag = False

    def _on_sig(_signum, _frame):
        nonlocal stop_flag
        stop_flag = True
        try:
            ctrl_file.write_text(json.dumps({"cmd": "stop", "ts": int(time.time())}) + "\n", encoding="utf-8")
        except Exception:
            pass

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_sig)
            except Exception:
                pass

    while not stop_flag:
        try:
            if ctrl_file.is_file():
                raw = ctrl_file.read_text(encoding="utf-8", errors="ignore")
                if '"stop_ack"' in raw:
                    ctrl_file.unlink(missing_ok=True)
                    break
        except Exception:
            pass
        time.sleep(3.0)


def _ensure_dependencies() -> None:
    """Если бот запущен напрямую (например, кнопкой «Старт» из веб-панели Pulse после докачки с GitHub),
    переключается на .venv или автоматически докачивает зависимости из requirements.txt."""
    try:
        import discord  # noqa: F401
        import dotenv  # noqa: F401
        import httpx  # noqa: F401
        return
    except ImportError:
        pass

    # 1. Проверяем наличие готового .venv
    venv_py = (
        ROOT_DIR / ".venv" / "Scripts" / "python.exe"
        if os.name == "nt"
        else ROOT_DIR / ".venv" / "bin" / "python"
    )
    if venv_py.is_file() and Path(sys.executable).resolve() != venv_py.resolve():
        os.execv(str(venv_py), [str(venv_py), *sys.argv])

    # 2. Пробуем автоматически установить зависимости из requirements.txt
    req_file = ROOT_DIR / "requirements.txt"
    if req_file.is_file():
        print("[BOOTSTRAP] Установка зависимостей Python из requirements.txt...")
        for extra_args in (["--user"], ["--break-system-packages"], []):
            try:
                res = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-q", *extra_args, "-r", str(req_file)],
                    cwd=str(ROOT_DIR),
                    timeout=180,
                )
                if res.returncode == 0:
                    os.execv(sys.executable, [sys.executable, *sys.argv])
            except Exception:
                pass


def main() -> None:
    if "--pulse-agent" in sys.argv:
        _run_container_pulse_agent()
        return

    _ensure_dependencies()

    import config
    from bot.database import get_db
    from bot.logger import get_logger
    from bot.pulse_bridge import setup_pterodactyl_bridge_cli

    log = get_logger("main")

    # Всегда инициализируем схему SQLite (data/modbot.db) и пробрасываем мост в контейнеры Pterodactyl
    # сразу при старте, даже если BOT_TOKEN ещё не заполнен.
    try:
        get_db().init()
    except Exception as e:
        log.warning("Не удалось инициализировать БД при старте: %s", e)

    if "--pulse-sync" in sys.argv:
        idx = sys.argv.index("--pulse-sync")
        custom_dir = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        synced = setup_pterodactyl_bridge_cli(custom_dir)
        if synced:
            print("Синхронизированы папки контейнеров Pterodactyl / Pulse:")
            for sp in synced:
                print(f"  • {sp}")
        else:
            print("Локальные тома Pterodactyl не обнаружены (БД data/modbot.db инициализирована).")
        return

    try:
        synced = setup_pterodactyl_bridge_cli()
        if synced:
            log.info("[PULSE] Автоматически подключены контейнеры Pterodactyl: %s", ", ".join(synced))
    except Exception as e:
        log.debug("[PULSE] Пропуск первичной синхронизации Pterodactyl: %s", e)

    # Если мы запущены внутри контейнера Pterodactyl без BOT_TOKEN, а на хосте VPS работает основной бот,
    # переходим в режим контейнерного агента для веб-панели Pulse.
    hb_file = ROOT_DIR / "data" / "pulse_heartbeat.json"
    if not config.BOT_TOKEN and hb_file.is_file():
        try:
            if time.time() - hb_file.stat().st_mtime < 60:
                log.info("[PULSE] Обнаружен активный процесс бота на VPS — запуск контейнерного агента Pulse.")
                _run_container_pulse_agent()
                return
        except Exception:
            pass

    if not config.BOT_TOKEN:
        log.error("BOT_TOKEN не задан в .env! Выполните: pmx config")
        raise SystemExit("BOT_TOKEN не задан в .env!")

    from bot.client import ModBot
    bot = ModBot()

    async def run():
        loop = asyncio.get_running_loop()
        stop_triggered = False

        async def _graceful_shutdown(sig_name: str):
            nonlocal stop_triggered
            if stop_triggered:
                return
            stop_triggered = True
            log.info("Получен сигнал %s — корректная остановка бота...", sig_name)
            await bot.close()

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    loop.add_signal_handler(
                        sig, lambda s=sig_name: asyncio.create_task(_graceful_shutdown(s))
                    )
                except (NotImplementedError, RuntimeError):
                    pass

        try:
            log.info("Запуск Discord-бота модерации...")
            await bot.start(config.BOT_TOKEN)
        except KeyboardInterrupt:
            await bot.close()
        finally:
            if not bot.is_closed():
                await bot.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Бот остановлен.")


if __name__ == "__main__":
    main()
