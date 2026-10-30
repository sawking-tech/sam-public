"""Модель по метке сложности агента: источник роутера из SAM_AGENT_LLM_<МЕТКА> (без зависимостей, его берёт и Cortex)."""
from __future__ import annotations

import os

# Модель метки - источник роутера (lmstudio, zai, gemini...) из SAM_AGENT_LLM_<МЕТКА>; пусто - выбор роутера
LEVEL_ENV_PREFIX = "SAM_AGENT_LLM_"


LEVEL_SEPARATOR = ":"   # "openrouter:z-ai/glm-5.3": источник, затем id модели (в id двоеточие допустимо, делим по первому)


def level_source(level: str) -> str:
    """Значение метки из конфига: источник или источник:модель; пусто - выбор роутера по умолчанию."""
    return os.getenv(f"{LEVEL_ENV_PREFIX}{level.upper()}", "").strip()


def split_level(value: str) -> tuple[str, str]:
    """(источник в нижнем регистре, id модели или пустая строка) из значения метки."""
    source, _, model = value.partition(LEVEL_SEPARATOR)
    return source.strip().lower(), model.strip()
