"""Инструменты MCP сессии Cortex: подключение через SDK mcp, схемы для модели, реестр для разбора текста, вызов.

Серверы приходят от клиента ACP в session/new. Cortex понимает HTTP-серверы (streamable HTTP); прочие пропускаются
строкой в лог. Одинаковое имя у двух серверов - берётся первый.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Iterable

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ERROR_PREFIX = "Ошибка: "
EMPTY_RESULT = "(пусто)"


def openai_schema(tool: Any) -> dict:
    """Инструмент MCP - описание функции в формате OpenAI tools."""
    return {"type": "function", "function": {
        "name": tool.name,
        "description": tool.description or "",
        "parameters": tool.inputSchema or {"type": "object", "properties": {}},
    }}


def parameter_list(tool: Any) -> list[dict]:
    """Параметры по порядку схемы - для разбора позиционных аргументов из текста (tool_text_parse)."""
    props = (tool.inputSchema or {}).get("properties") or {}
    return [{"name": name} for name in props]


def is_destructive(tool: Any) -> bool:
    annotations = getattr(tool, "annotations", None)
    return bool(annotations and getattr(annotations, "destructiveHint", None))


def result_text(result: Any) -> str:
    """Итог вызова MCP - текст для модели: текстовые части подряд, ошибка - с меткой."""
    parts = [getattr(c, "text", None) for c in (getattr(result, "content", None) or [])]
    text = "\n".join(p for p in parts if p) or EMPTY_RESULT
    return ERROR_PREFIX + text if getattr(result, "isError", False) else text


@dataclass
class _Entry:
    tool: Any
    session: Any


class McpTools:
    """Инструменты всех серверов сессии. Соединения живут в переданном стеке и закрываются вместе с сессией ACP."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def add(self, session: Any, tools: Iterable[Any]) -> None:
        for tool in tools:
            if tool.name in self._entries:
                print(f"[Cortex] инструмент {tool.name} уже есть у другого сервера - пропущен")
                continue
            self._entries[tool.name] = _Entry(tool, session)

    def only(self, names: Iterable[str]) -> "McpTools":
        """Те же соединения, но только названные инструменты: выдача создателя (cortex/acp/rights.py)."""
        keep = set(names)
        subset = McpTools()
        subset._entries = {n: e for n, e in self._entries.items() if n in keep}
        return subset

    @property
    def names(self) -> list[str]:
        return list(self._entries)

    def schemas(self, names: Iterable[str]) -> list[dict]:
        return [openai_schema(self._entries[n].tool) for n in names if n in self._entries]

    def descriptions(self) -> dict[str, str]:
        return {name: e.tool.description or "" for name, e in self._entries.items()}

    def registry(self) -> dict[str, list[dict]]:
        return {name: parameter_list(e.tool) for name, e in self._entries.items()}

    def destructive(self) -> set[str]:
        return {name for name, e in self._entries.items() if is_destructive(e.tool)}

    async def call(self, name: str, args: dict) -> str:
        entry = self._entries.get(name)
        if entry is None:
            return f"{ERROR_PREFIX}нет инструмента {name}"
        return result_text(await entry.session.call_tool(name, args or {}))


async def connect(servers: Iterable[Any], stack: contextlib.AsyncExitStack) -> McpTools:
    """Подключить HTTP-серверы MCP из session/new; упавший сервер - строка в лог, остальные работают."""
    tools = McpTools()
    for server in servers or []:
        url = getattr(server, "url", None)
        if getattr(server, "type", None) != "http" or not url:
            print(f"[Cortex] MCP {getattr(server, 'name', '?')}: поддержан только HTTP - пропущен")
            continue
        headers = {h.name: h.value for h in (getattr(server, "headers", None) or [])}
        try:
            read, write, _ = await stack.enter_async_context(streamablehttp_client(url, headers=headers))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools.add(session, (await session.list_tools()).tools)
        except Exception as e:
            print(f"[Cortex] MCP {server.name} ({url}) не подключён: {e or type(e).__name__}")
    return tools
