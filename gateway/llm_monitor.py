"""Что уходит в LLM - монитору: событие llm_request перед каждой попыткой вызова.

llm_client перед отправкой поставщику зовёт publish; модуль собирает событие (сессия и роль
из token_usage.usage_scope, источник, модель, сообщения, имена tools) и отдаёт приёмнику.
Приёмник подключает корень композиции (gateway/agent.py: event_bus.emit). Без приёмника
событие не собирается; сбой приёмника печатается одной строкой и не роняет вызов LLM.
"""
from __future__ import annotations

from typing import Any, Callable

from . import token_usage
from .env import env_int
from .llm_router import ROUTE_FAST, ROUTE_KEY

EVENT_TYPE = "llm_request"
NO_LIMIT = 0
# Предел символов на сообщение: системный промпт основной модели - десятки тысяч символов,
# а монитор держит каждое событие в DOM. 0 - без обрезки
MAX_CHARS = env_int("SAM_MONITOR_LLM_MAX_CHARS", 4000)
# Вход быстрой модели голоса (SAM-56) и есть предмет отладки - по умолчанию целиком
FAST_MAX_CHARS = env_int("SAM_MONITOR_LLM_FAST_MAX_CHARS", NO_LIMIT)
TRUNCATED_MARK = "\n…[обрезано, всего {total} симв.]"
IMAGE_PART = "[image]"
LOG_PREFIX = "[LLMMonitor]"

_sink: Callable[[dict], None] | None = None


def set_sink(sink: Callable[[dict], None] | None) -> None:
    """Куда отдавать события; None - не собирать."""
    global _sink
    _sink = sink


def _field(obj: Any, name: str) -> Any:
    """Поле сообщения: словарь из кода SAM или объект ответа поставщика в истории хода."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _text(content: Any) -> str:
    """Содержимое в текст: части мультимодального сообщения склеиваются, картинка - пометкой."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if _field(part, "type") == "text":
                parts.append(_field(part, "text") or "")
            else:
                parts.append(IMAGE_PART)
        return "\n".join(parts)
    return str(content)


def _truncate(text: str, limit: int) -> str:
    if limit <= NO_LIMIT or len(text) <= limit:
        return text
    return text[:limit] + TRUNCATED_MARK.format(total=len(text))


def _tool_call_names(message: Any) -> list[str]:
    return [_field(_field(tc, "function"), "name") or "" for tc in _field(message, "tool_calls") or []]


def _message(message: Any, limit: int) -> dict:
    out = {"role": _field(message, "role") or "", "content": _truncate(_text(_field(message, "content")), limit)}
    if names := _tool_call_names(message):
        out["tool_calls"] = names
    if name := _field(message, "name"):
        out["name"] = name
    return out


def _tool_name(tool: Any) -> str:
    """Имя tool: схема OpenAI ({function: {name}}) или плоская ({name})."""
    return _field(_field(tool, "function"), "name") or _field(tool, "name") or ""


def is_fast(llm_config: dict) -> bool:
    return llm_config.get(ROUTE_KEY) == ROUTE_FAST


def build_event(llm_config: dict, request: dict,
                max_chars: int = MAX_CHARS, fast_max_chars: int = FAST_MAX_CHARS) -> dict:
    """Событие одного запроса: request - аргументы вызова поставщика (messages, tools)."""
    session_id, role = token_usage.current_scope()
    fast = is_fast(llm_config)
    limit = fast_max_chars if fast else max_chars
    messages = [_message(m, limit) for m in request.get("messages") or []]
    return {
        "session": session_id,
        "role": role,
        "fast": fast,
        "source": llm_config.get("source", ""),
        "model": request.get("model") or llm_config.get("model", ""),
        "messages": messages,
        "tools": [_tool_name(t) for t in request.get("tools") or []],
    }


def publish(llm_config: dict, request: dict) -> None:
    """Отдать событие приёмнику; сбой сборки или приёмника - строка в консоль, вызов идёт дальше."""
    if _sink is None:
        return
    try:
        _sink(build_event(llm_config, request))
    except Exception as e:
        print(f"{LOG_PREFIX} событие {EVENT_TYPE} не отправлено: {e}")
