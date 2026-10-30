"""Сообщения хода Cortex: короткая рамка агента и текст подсказки от шлюза (ADR-022).

Контекст хода (система SAM, история, память, скиллы) собирает шлюз и присылает одним текстом; Cortex своей
истории не копит. Модуль чистый.
"""
from __future__ import annotations

from typing import Any, Iterable

# Рамка поверх системы SAM из подсказки: где инструменты и чем кончается ход
FRAME = (
    "Ты - исполнитель хода SAM. Ниже весь контекст хода: система SAM, разговор и текущее сообщение. "
    "Действуешь только инструментами из списка; факт в ответе - только из итога инструмента, "
    "не вышло - так и скажи. Нужного инструмента нет среди доступных - найди его tool_search. "
    "Ответ человеку пишешь по правилам системы SAM."
)
IMAGE_MARK = "[image]"   # картинка в подсказке: метка, данные - позже


def prompt_text(blocks: Iterable[Any]) -> str:
    """Текст подсказки ACP: текстовые блоки подряд, картинка - меткой, прочие блоки пропускаются."""
    parts = []
    for block in blocks:
        kind = getattr(block, "type", None)
        if kind == "text":
            parts.append(getattr(block, "text", "") or "")
        elif kind == "image":
            parts.append(IMAGE_MARK)
    return "\n\n".join(p for p in parts if p)


def turn_messages(text: str, frame: str = FRAME) -> list[dict]:
    return [{"role": "system", "content": frame}, {"role": "user", "content": text}]
