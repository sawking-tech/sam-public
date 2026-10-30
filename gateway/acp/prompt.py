"""Ход SAM для ACP-агента (ADR-022): сообщения OpenAI - в один текст подсказки, обновления сессии - в куски ответа.

В штатном режиме контекст хода собирает SAM (история, память, скиллы) и отдаёт агенту целиком в новой сессии, поэтому
своя история у агента не копится. Модуль чистый: без процесса и сети.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool result"}
CURRENT_LABEL = "Current message"   # последняя реплика человека - вход этого хода
IMAGE_MARK = "[image]"              # картинка в ходе: агент получает метку, не данные (картинки - позже блоком image)
TOOL_ARGS_CHARS = 400               # аргументы прошлого вызова инструмента в истории: видно, что звали, без простыни

TEXT, THOUGHT, TOOL, OTHER = "text", "thought", "tool", "other"
MESSAGE_CHUNK, THOUGHT_CHUNK = "agent_message_chunk", "agent_thought_chunk"
TOOL_UPDATES = ("tool_call", "tool_call_update")

# Разрешение только разовое: судья SAM решает каждый вызов, «разрешить навсегда» агенту не выдаётся
ALLOW_KINDS = ("allow_once",)
REJECT_KINDS = ("reject_once", "reject_always")


@dataclass(frozen=True)
class Piece:
    """Кусок хода агента: текст ответа, рассуждение, ход инструмента или прочее обновление."""
    kind: str
    text: str = ""


def content_text(content: Any) -> str:
    """Текст сообщения OpenAI: строка или список частей; картинка - меткой."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(part.get("text", ""))
        elif isinstance(part, dict) and part.get("type") in ("image_url", "image"):
            parts.append(IMAGE_MARK)
    return "\n".join(p for p in parts if p)


def _tool_calls_text(calls: Iterable[dict]) -> str:
    lines = []
    for call in calls:
        fn = call.get("function", {})
        args = fn.get("arguments", "")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        lines.append(f"called {fn.get('name', '?')}({args[:TOOL_ARGS_CHARS]})")
    return "\n".join(lines)


def prompt_text(messages: list[dict]) -> str:
    """Один текст хода: системные части сверху, затем разговор по ролям; последняя реплика человека - отдельно."""
    system = [content_text(m.get("content")) for m in messages if m.get("role") == "system"]
    dialog = [m for m in messages if m.get("role") != "system"]
    last_user = max((i for i, m in enumerate(dialog) if m.get("role") == "user"), default=None)

    blocks = [s for s in system if s.strip()]
    lines = []
    for i, m in enumerate(dialog):
        if i == last_user:
            continue
        role = m.get("role", "user")
        text = content_text(m.get("content"))
        if m.get("tool_calls"):
            text = "\n".join(t for t in (text, _tool_calls_text(m["tool_calls"])) if t)
        if text.strip():
            lines.append(f"{ROLE_LABELS.get(role, role)}: {text}")
    if lines:
        blocks.append("\n".join(lines))
    if last_user is not None:
        blocks.append(f"{CURRENT_LABEL}: {content_text(dialog[last_user].get('content'))}")
    return "\n\n".join(blocks)


def piece_of(update: Any) -> Piece:
    """Обновление сессии ACP - кусок хода."""
    kind = getattr(update, "session_update", None)
    if kind in (MESSAGE_CHUNK, THOUGHT_CHUNK):
        text = getattr(getattr(update, "content", None), "text", "") or ""
        return Piece(TEXT if kind == MESSAGE_CHUNK else THOUGHT, text)
    if kind in TOOL_UPDATES:
        title = getattr(update, "title", None) or getattr(update, "tool_call_id", "") or ""
        status = getattr(update, "status", None) or ""
        return Piece(TOOL, f"{title} {status}".strip())
    return Piece(OTHER, str(kind or ""))


def choose_option(options: Iterable[Any], allowed: bool) -> str | None:
    """Ответ на запрос разрешения: id варианта нужного рода (отказ - разовый раньше постоянного); нет такого - None
    (отмена)."""
    kinds = ALLOW_KINDS if allowed else REJECT_KINDS
    by_kind = {getattr(o, "kind", None): getattr(o, "option_id", None) for o in options}
    return next((by_kind[k] for k in kinds if by_kind.get(k)), None)
