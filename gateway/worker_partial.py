"""Частичный итог агента (SAM-212): пул шагов кончился раньше итога.

Создатель получает не служебную строку цикла, а что агент успел (свежие итоги инструментов и последний статус), на
чём встал и почему остановился. Модуль чистый: разбирает историю воркера, без сети и шины.
"""
from __future__ import annotations

import json

from .env import env_int

STOPPED = "[ШАГИ КОНЧИЛИСЬ] Итог неполный: агент истратил пул шагов."
UNANSWERED = "Просьба о шагах (request_steps) осталась без ответа создателя."
STATUS_LINE = "Последний статус: {text}"
STOPPED_AT = "Встал на: {call}"
RESULT_LINE = "{name}: {text}"
LAST_TEXT = "Последний ответ: {text}"
ANSWER_MARK = "ОТВЕТ:"   # итог через report_status: в частичном итоге его нет, иначе итог был бы полным

# Свежие итоги говорят, где агент встал; три помещаются в итог задачи (RESULT_PREVIEW) вместе со статусом
LAST_RESULTS = env_int("SAM_AGENT_PARTIAL_RESULTS", 3)
RESULT_CHARS = 160   # начало итога инструмента: путь файла, код выхода, первая строка ошибки
ARGS_CHARS = 120     # аргументы вызова, на котором агент встал
# Связь с создателем, а не работа: их итоги («ok», «вопрос отправлен») о сделанном не говорят
CHANNEL_TOOLS = frozenset({"report_status", "ask_creator", "estimate_steps", "request_steps", "grant_steps",
                           "send_to_agent"})
STATUS_TOOL = "report_status"


def _calls(messages: list) -> list[tuple[str, str, dict]]:
    """Вызовы инструментов по порядку: (id, имя, аргументы)."""
    found = []
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                args = {}
            found.append((tc.get("id") or "", fn.get("name") or "", args if isinstance(args, dict) else {}))
    return found


def _last_status(calls: list[tuple[str, str, dict]]) -> str:
    for _, name, args in reversed(calls):
        text = str(args.get("message") or "")
        if name == STATUS_TOOL and text and not text.startswith(ANSWER_MARK):
            return text
    return ""


def _work_results(messages: list, names: dict[str, str]) -> list[tuple[str, str]]:
    """Итоги рабочих инструментов: (имя, начало итога), старые первыми."""
    return [(names[m.get("tool_call_id")], str(m.get("content") or "")[:RESULT_CHARS])
            for m in messages
            if m.get("role") == "tool" and names.get(m.get("tool_call_id"), STATUS_TOOL) not in CHANNEL_TOOLS]


def _last_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip():
            return m["content"].strip()[:RESULT_CHARS]
    return ""


def partial_result(messages: list, *, unanswered: bool = False, limit: int = 500) -> str:
    """Итог агента без итога: причина, последний статус, вызов, на котором встал, и свежие итоги инструментов (новые
    первыми: обрезка по limit срезает старые)."""
    calls = _calls(messages)
    lines = [STOPPED + (" " + UNANSWERED if unanswered else "")]
    if status := _last_status(calls):
        lines.append(STATUS_LINE.format(text=status))
    work = [(name, args) for _, name, args in calls if name not in CHANNEL_TOOLS]
    if work:
        name, args = work[-1]
        lines.append(STOPPED_AT.format(call=f"{name}({json.dumps(args, ensure_ascii=False)[:ARGS_CHARS]})"))
    results = _work_results(messages, {cid: name for cid, name, _ in calls})[-LAST_RESULTS:]
    lines.extend(RESULT_LINE.format(name=name, text=text) for name, text in reversed(results))
    if not results and (text := _last_text(messages)):
        lines.append(LAST_TEXT.format(text=text))
    return "\n".join(lines)[:limit]
