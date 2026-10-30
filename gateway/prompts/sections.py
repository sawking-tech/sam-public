"""Раздел промпта по имени: характер - из души SAM (gateway/prompts/soul.py), остальное - из инструкций SAM.md
(gateway/prompts/instructions.py). Модули сборки (system.py, voice.py, lead.py) берут разделы отсюда и не знают, где
какой лежит."""
from __future__ import annotations

from . import instructions, soul


def section(name: str) -> str:
    text = soul.soul().sections.get(name)
    return text if text is not None else instructions.section(name)
