"""Личный слой промпта - data/SAM.md поверх SAM.md и души, как ~/.claude/CLAUDE.md поверх CLAUDE.md проекта.

SAM.md и заготовка души лежат в репозитории без имён и быта: на их местах подстановки `{ключ}`, значения по умолчанию
- в шапке SAM.md. Личный файл в data/ (вне git) задаёт свои значения в шапке того же вида и пишет личные инструкции
текстом после неё. Файл читается при каждой сборке промпта, правят его без кода и рестарта. Инструкции встают сразу
после общего промпта основного, голосового и ведущего (до оглавления скиллов и правил хода) и идут как есть: метки
рода и подстановки к ним не применяются. Файла нет или он пуст - значения по умолчанию и раздела нет. Путь -
`SAM_PERSONAL_FILE` (относительный - от корня репозитория), по умолчанию `data/SAM.md`.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from . import instructions
from .prompt_md import REPO_ROOT, PromptError, PromptFile, fill, parse, resolve
from .system import SECTION_BREAK

PERSONAL_ENV = "SAM_PERSONAL_FILE"
DEFAULT_PATH = REPO_ROOT / "data" / "SAM.md"
LOG_TAG = "[Personal]"
# Приоритет как у CLAUDE.md, но безопасность и секреты файл не отменяет: в data/ могут писать и инструменты SAM
HEADER = ("ЛИЧНЫЕ ИНСТРУКЦИИ (личный слой, data/SAM.md). Следуй им; где они расходятся с правилами выше, главнее "
          "они, кроме правил безопасности и секретов.")


_household: Callable[[], str] = lambda: ""   # блок состава дома, источник задаёт корень композиции


def set_household(source: Callable[[], str]) -> None:
    global _household
    _household = source


def personal_path() -> Path:
    return resolve(os.getenv(PERSONAL_ENV, "").strip() or DEFAULT_PATH)


def read_personal(path: Path | None = None, log=print) -> PromptFile:
    """Шапка и текст личного файла; нет файла - пусто, не читается или не разобран - строка в лог и пусто."""
    path = path or personal_path()
    try:
        return parse(path.read_text(encoding="utf-8"), path.name, LOG_TAG)
    except FileNotFoundError:
        return PromptFile()
    except (OSError, UnicodeDecodeError, PromptError) as exc:
        log(f"{LOG_TAG} {path} не читается ({exc}); промпт без личного слоя, проверь файл")
        return PromptFile()


def values(layer: PromptFile | None = None) -> dict:
    """Подстановки: шапка SAM.md, поверх - шапка личного файла."""
    layer = layer if layer is not None else read_personal()
    return {**instructions.meta(), **layer.meta}


def instructions_block(text: str) -> str:
    """Раздел после общего промпта: заголовок и текст; пустой текст - пустой раздел."""
    return SECTION_BREAK + HEADER + "\n" + text if text else ""


def personalize(prompt: str) -> str:
    """Подстановки общего промпта, состав дома и личные инструкции в конце."""
    layer = read_personal()
    home = _household()
    return fill(prompt, values(layer)) + (SECTION_BREAK + home if home else "") + instructions_block(layer.body)
