#!/usr/bin/env python3
"""Moderation Discord bot — entry point."""
import asyncio
import signal

import config
from bot.client import ModBot
from bot.logger import get_logger


def main():
    log = get_logger("main")
    if not config.BOT_TOKEN:
        log.error("BOT_TOKEN не задан в .env!")
        raise SystemExit("BOT_TOKEN не задан в .env!")

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
                    # На Windows add_signal_handler не поддерживается — работает except KeyboardInterrupt
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

