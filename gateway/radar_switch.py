"""Выключатель радара присутствия: `SAM_RADAR`, по умолчанию выключен.

Выключенный радар не видят ни модель, ни система: нет проверок надзора и перезапуска, встроенного приложения, навигации,
инструментов MCP, скилла, workflow и подачи свидетельств. Код радара остаётся и работает как обычно, когда флаг включён.
Этот модуль только отвечает, включён ли радар; каждое место, где радар виден, спрашивает здесь.

Запуск `python -m gateway.radar_switch` - проверка для скриптов: код выхода 0, если включён, иначе одна строка, что сделать.
"""
from __future__ import annotations

import sys
from typing import Mapping

from .env import env_bool

ENV = "SAM_RADAR"
DEFAULT = False
# Одно имя у компонента стенда, сервера MCP, скилла и категории workflow радара
COMPONENT = "radar"
OFF_LINE = f"радар выключен: {ENV}=1 в .env включает"


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    return env_bool(ENV, DEFAULT, environ=environ)


def main() -> None:
    from dotenv import load_dotenv

    from .core_paths import REPO
    load_dotenv(REPO / ".env")
    if not enabled():
        sys.exit(OFF_LINE)


if __name__ == "__main__":
    main()
