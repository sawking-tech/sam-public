"""Сторона клиента ACP в шлюзе (ADR-022): куски хода по сессиям, разрешения - судье SAM, своё агенту закрыто.

Агент в прокси-режиме действует только через MCP SAM: файлы и терминал клиента ему не открываются (возможности
не объявлены, а вызов отклоняется). Запрос разрешения уходит решателю - судье SAM из корня композиции; ответ только
разовый, «разрешить навсегда» агенту не выдаётся.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from acp import RequestError, RequestPermissionResponse
from acp.schema import AllowedOutcome, DeniedOutcome

from .prompt import Piece, choose_option, piece_of

# Решатель: (название вызова, аргументы) -> можно ли
PermissionResolver = Callable[[str, dict], Awaitable[bool]]
# Канал агента к создателю (SAM-133): параметры расширения _sam/agent_event -> ответ агенту {"result": текст}
AgentEvent = Callable[[dict], Awaitable[dict]]
AGENT_EVENT_METHOD = "_sam/agent_event"   # то же имя у агента: cortex/acp/channels.py
CLOSED_TO_AGENT = "клиент SAM не даёт агенту файлы и терминал: действие только через MCP SAM (ADR-022)"
SELECTED, CANCELLED = "selected", "cancelled"
METHOD_NOT_FOUND = -32601   # код JSON-RPC: метод не поддержан


async def deny_all(title: str, args: dict) -> bool:
    return False


class SamClient:
    """Клиент ACP: обновления сессии - в очередь своего хода, разрешения - решателю."""

    def __init__(self, resolve_permission: PermissionResolver = deny_all, on_agent_event: AgentEvent | None = None) -> None:
        self._resolve = resolve_permission
        self._on_agent_event = on_agent_event
        self._queues: dict[str, asyncio.Queue[Piece]] = {}

    def open(self, session_id: str) -> asyncio.Queue[Piece]:
        queue: asyncio.Queue[Piece] = asyncio.Queue()
        self._queues[session_id] = queue
        return queue

    def close(self, session_id: str) -> None:
        self._queues.pop(session_id, None)

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        queue = self._queues.get(session_id)
        if queue is not None:   # обновление закрытого хода (поздний кусок после отмены) некому показывать
            queue.put_nowait(piece_of(update))

    async def request_permission(self, options: list, session_id: str, tool_call: Any,
                                 **kwargs: Any) -> RequestPermissionResponse:
        title = getattr(tool_call, "title", None) or getattr(tool_call, "tool_call_id", "") or ""
        raw = getattr(tool_call, "raw_input", None)
        args = raw if isinstance(raw, dict) else {"input": raw}
        try:
            allowed = await self._resolve(title, args)
        except Exception as e:   # судья упал - действие не разрешается
            print(f"[ACP] судья разрешения упал: {e} - отказ")
            allowed = False
        option = choose_option(options, allowed)
        outcome = AllowedOutcome(option_id=option, outcome=SELECTED) if option else DeniedOutcome(outcome=CANCELLED)
        return RequestPermissionResponse(outcome=outcome)

    async def _closed(self, *args: Any, **kwargs: Any) -> Any:
        raise RequestError(METHOD_NOT_FOUND, CLOSED_TO_AGENT)

    read_text_file = write_text_file = _closed
    create_terminal = terminal_output = release_terminal = wait_for_terminal_exit = kill_terminal = _closed

    async def ext_method(self, method: str, params: dict) -> dict:
        if method == AGENT_EVENT_METHOD and self._on_agent_event is not None:
            return await self._on_agent_event(params)
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict) -> None:
        return None

    def on_connect(self, conn: Any) -> None:
        return None
