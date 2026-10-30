"""Инструкции SAM - SAM.md в корне репозитория, как CLAUDE.md у Claude Code: как SAM делает дела, слышит, говорит и
пользуется инструментами.

Формат - gateway/prompts/prompt_md.py: шапка с подстановками по умолчанию (`ключ: значение`, свои значения - в шапке
личного слоя data/SAM.md), дальше разделы `## ИМЯ`. Характер SAM (душа) живёт отдельно, в её хранилище
(gateway/prompts/soul.py); инструменты души этот файл не правят, его правят в репозитории. Файл читается один раз на
процесс: правка доходит до модели после рестарта шлюза. Путь - `SAM_PROMPT_FILE` (относительный - от корня
репозитория), по умолчанию `SAM.md` в корне. Нет файла или раздела - PromptError: одна строка, что не так и что
делать, без стека.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

from .prompt_md import REPO_ROOT, PromptError, PromptFile, parse, resolve

PROMPT_ENV = "SAM_PROMPT_FILE"
DEFAULT_PATH = REPO_ROOT / "SAM.md"
LOG_TAG = "[SAM.md]"


def path() -> Path:
    return resolve(os.getenv(PROMPT_ENV, "").strip() or DEFAULT_PATH)


def read(file: Path) -> PromptFile:
    try:
        return parse(file.read_text(encoding="utf-8"), file.name, LOG_TAG)
    except FileNotFoundError:
        raise PromptError(f"{LOG_TAG} нет файла {file}; верни SAM.md в корень репозитория или укажи путь в {PROMPT_ENV}")
    except (OSError, UnicodeDecodeError) as exc:
        raise PromptError(f"{LOG_TAG} {file} не читается ({exc}); проверь файл и кодировку UTF-8")


@functools.lru_cache(maxsize=1)
def instructions() -> PromptFile:
    return read(path())


def meta() -> dict:
    """Подстановки по умолчанию из шапки SAM.md."""
    return instructions().meta


def names() -> tuple[str, ...]:
    return tuple(instructions().sections)


def section(name: str) -> str:
    try:
        return instructions().sections[name]
    except KeyError:
        raise PromptError(f"{LOG_TAG} нет раздела ## {name} ни в душе, ни в {path()}; верни раздел или поправь сборку "
                          f"промпта")
