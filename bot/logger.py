"""Централизованное логирование бота с ротацией файлов и выводом в консоль."""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import config

_CONFIGURED = False


def get_logger(name: str = "modbot") -> logging.Logger:
    global _CONFIGURED
    root = logging.getLogger("modbot")
    if not _CONFIGURED:
        _CONFIGURED = True
        level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        root.setLevel(level)

        fmt = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Консольный обработчик (UTF-8)
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(fmt)
        root.addHandler(stream_handler)

        # Файловый обработчик с ротацией (до 5 МБ × 3 файла)
        log_path = getattr(config, "LOG_PATH", "data/modbot.log")
        try:
            log_dir = os.path.dirname(log_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except Exception as e:
            root.warning("Не удалось инициализировать файловый логгер (%s): %s", log_path, e)

    if name == "modbot" or name.startswith("modbot."):
        return logging.getLogger(name)
    return logging.getLogger(f"modbot.{name}")
