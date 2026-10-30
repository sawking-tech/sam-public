"""Лог радара (SAM-105): события и сбои одной строкой в файл с ротацией, чтобы по нему можно было разобрать, что случилось.

Пишутся: маршруты и приход людей, ручные вероятности, зоны, медленные тики, упавший тик, ошибки страницы. Файл и уровень -
SAM_RADAR_LOG_FILE и SAM_RADAR_LOG_LEVEL. Настраивается сервисом один раз; библиотечный код только берёт логгер `sam.radar`.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

FILE_ENV = "SAM_RADAR_LOG_FILE"
DEFAULT_FILE = "logs/radar-events.log"
LEVEL_ENV = "SAM_RADAR_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"
MAX_BYTES = 1_000_000          # файл до мегабайта, три прошлых хранятся: радар живёт неделями
BACKUPS = 3
FORMAT = "%(asctime)s %(levelname)s %(message)s"


def setup(environ=os.environ) -> logging.Logger:
    """Обработчик файла на логгере `sam.radar`; повторный вызов не плодит обработчики."""
    logger = logging.getLogger("sam.radar")
    logger.setLevel(getattr(logging, (environ.get(LEVEL_ENV) or DEFAULT_LEVEL).upper(), logging.INFO))
    path = Path(environ.get(FILE_ENV) or DEFAULT_FILE)
    if not any(isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == path.resolve() for h in logger.handlers):
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        handler.setFormatter(logging.Formatter(FORMAT))
        logger.addHandler(handler)
    return logger
