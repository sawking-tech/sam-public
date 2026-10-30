"""События цикла агента (шина event_bus) - в обновления сессии ACP.

agent_loop сообщает о ходе через deps.event_bus.emit(тип, данные). Cortex подставляет вместо шины приёмник: текст
модели уходит клиенту по мере прихода, вызов инструмента - началом и итогом, блокировка - отказом. Отображение
событий чистое (to_updates), отправка по порядку - в UpdateSink.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import acp

DELTA, STREAM_END, STREAM_RESET = "assistant_delta", "assistant_stream_end", "assistant_stream_reset"
TOOL_CALL, TOOL_RESULT, GUARD_BLOCK = "tool_call", "tool_result", "guard_block"
IN_PROGRESS, COMPLETED, FAILED = "in_progress", "completed", "failed"
SEGMENT_BREAK = "\n\n"   # между текстами разных вызовов модели: «сейчас проверю» и ответ не слипаются


def to_updates(event: str, payload: dict, started: set[str]) -> list[Any]:
    """Обновления ACP по событию цикла; started - id вызовов, о начале которых клиент уже знает (дополняется)."""
    call_id = payload.get("call_id")
    if event == TOOL_CALL and call_id:
        started.add(call_id)
        return [acp.start_tool_call(call_id, payload.get("name", ""), status=IN_PROGRESS,
                                    raw_input=payload.get("args"))]
    if event == TOOL_RESULT and call_id:
        content = [acp.tool_content(acp.text_block(str(payload.get("result", ""))))]
        return [acp.update_tool_call(call_id, title=payload.get("name") or None, status=COMPLETED, content=content)]
    if event == GUARD_BLOCK and call_id:
        if call_id in started:
            return [acp.update_tool_call(call_id, title=payload.get("tool") or None, status=FAILED)]
        started.add(call_id)
        return [acp.start_tool_call(call_id, payload.get("tool", ""), status=FAILED)]
    return []


class UpdateSink:
    """Шина для agent_loop в ходе Cortex: emit синхронный, отправка клиенту - по порядку в фоновой задаче;
    инъекции в идущий ход - очередь, цикл забирает их через pop_inject перед каждым шагом.

    Помнит текст последнего вызова модели: итог цикла, который не пришёл потоком (форсированный финиш, повтор без
    потока), агент дошлёт отдельно.
    """

    def __init__(self, send: Callable[[Any], Awaitable[None]]) -> None:
        self._send = send
        self._queue: asyncio.Queue = asyncio.Queue()
        self._started: set[str] = set()
        self._segment: list[str] = []
        self.last_segment = ""
        self.sent_text = False
        self._injects: list[str] = []
        self._pump = asyncio.ensure_future(self._run())

    def emit(self, event: str, payload: dict) -> None:
        if event == DELTA:
            self._delta(payload.get("text") or "")
        elif event in (STREAM_END, STREAM_RESET):
            self.last_segment = "".join(self._segment) if event == STREAM_END else ""
            self._segment = []
        else:
            for update in to_updates(event, payload, self._started):
                self._queue.put_nowait(update)

    def inject(self, text: str) -> None:
        if text.strip():
            self._injects.append(text)

    def pop_inject(self, session_id: str) -> str | None:
        return self._injects.pop(0) if self._injects else None

    def say(self, text: str) -> None:
        """Текст ответа помимо потока (итог, который модель не стримила)."""
        self._delta(text)
        self.emit(STREAM_END, {})

    def _delta(self, text: str) -> None:
        if not text:
            return
        if not self._segment and self.sent_text:
            text = SEGMENT_BREAK + text
        self._segment.append(text)
        self.sent_text = True
        self._queue.put_nowait(acp.update_agent_message_text(text))

    async def _run(self) -> None:
        while True:
            update = await self._queue.get()
            try:
                await self._send(update)
            except Exception as e:   # клиент не принял обновление: ход идёт дальше, строка в журнал
                print(f"[Cortex] обновление не отправлено: {e or type(e).__name__}")
            finally:
                self._queue.task_done()

    async def close(self) -> None:
        """Дождаться отправки всего накопленного и остановить отправку."""
        await self._queue.join()
        self._pump.cancel()
