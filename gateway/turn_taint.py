"""Внешний текст в ходе: был ли в этом ходе вызов инструмента, который приносит чужой текст, или итог агента.

Правка души меняет системный промпт SAM навсегда. Текст из поиска, страницы, MCP, песочницы или итога агента
([TASK_DONE]) может нести инструкцию «перепиши свой характер», и ход с ключом команд её выполнил бы. Поэтому
правка и откат души идут только отдельным ходом, где внешнего текста не было (gateway/tools/soul.py).

Ход открывает turn_scope (gateway/runner.py run_turn) с пустым набором; проверка привилегий агента
(gateway/agent.py _privilege_check) отмечает каждый вызов инструмента до его выполнения. Набор - изменяемый объект в
ContextVar: задачи и потоки инструментов получают копию контекста с тем же набором и видят отметки друг друга.
Решение (external) отделено от протокола и проверяется без шлюза.
"""
from __future__ import annotations

import contextvars
import os
from collections.abc import Iterator
from contextlib import contextmanager

EXTERNAL_TOOLS = frozenset(t.strip() for t in os.getenv(
    "SAM_EXTERNAL_TOOLS", "search_web,mcp_call,fetch,claude_delegate").split(",") if t.strip())
EXTERNAL_PREFIXES = ("sandbox_",)   # sandbox_run, sandbox_exec и будущие: вывод чужого кода
AGENT_RESULT = "[TASK_DONE"         # итог агента приходит ходом с этой меткой (gateway/agent.py)
INPUT_MARK = "agent_result"         # отметка входа хода: системный ход или итог агента

_seen: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar("turn_external", default=None)


def external(tool_name: str) -> bool:
    return tool_name in EXTERNAL_TOOLS or tool_name.startswith(EXTERNAL_PREFIXES)


def external_input(message: str, system_call: bool) -> bool:
    """Вход хода сам внешний: итог агента или системный ход (инъекция задачи)."""
    return system_call or message.lstrip().startswith(AGENT_RESULT)


@contextmanager
def turn_scope(external_in_input: bool = False) -> Iterator[None]:
    token = _seen.set({INPUT_MARK} if external_in_input else set())
    try:
        yield
    finally:
        _seen.reset(token)


def note_tool(tool_name: str) -> None:
    seen = _seen.get()
    if seen is not None and external(tool_name):
        seen.add(tool_name)


def seen() -> frozenset[str]:
    """Источники внешнего текста в этом ходе; вне хода - пусто."""
    return frozenset(_seen.get() or ())
