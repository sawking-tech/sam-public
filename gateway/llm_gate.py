"""Очередь к местам вызова LLM с приоритетом (SAM-84).

Мест LLM_CONCURRENCY, как у прежнего семафора. Освободившееся место отдаётся ждущему с
наименьшим приоритетом, при равном - тому, кто встал раньше. Голосовой ход берёт
ближайшее свободное место раньше воркеров, сжатия, автозаписи, проверки ответа и heartbeat;
отдельного резерва мест нет: на одной локальной GPU лишний параллельный вызов замедляет всех.

Приоритет ставит вызывающий код блоком priority_scope; вне блока - PRIORITY_DEFAULT, и порядок
остальных вызовов прежний: первым пришёл - первым получил место.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import heapq
import itertools
from typing import Iterator

PRIORITY_VOICE = 0      # голосовой ход: человек ждёт звука
PRIORITY_DEFAULT = 10   # всё остальное: воркеры, сжатие, автозапись, проверки, heartbeat

_priority: contextvars.ContextVar[int] = contextvars.ContextVar("llm_priority", default=PRIORITY_DEFAULT)


@contextlib.contextmanager
def priority_scope(priority: int) -> Iterator[None]:
    """Вызовы LLM внутри блока встают в очередь с этим приоритетом."""
    token = _priority.set(priority)
    try:
        yield
    finally:
        _priority.reset(token)


def current_priority() -> int:
    return _priority.get()


class PriorityGate:
    """Семафор, который отдаёт место по приоритету ждущего, а не по порядку прихода."""

    def __init__(self, slots: int):
        self._capacity = slots
        self._free = slots
        self._waiters: list[tuple[int, int, asyncio.Future]] = []
        self._seq = itertools.count()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def free(self) -> int:
        """Свободных мест сейчас: проверка ответа решает по ним, сразу ей идти или пачкой (chatter)."""
        return self._free

    @property
    def waiting(self) -> int:
        return sum(1 for *_, fut in self._waiters if not fut.done())

    async def acquire(self, priority: int | None = None) -> None:
        if self._free > 0 and not self.waiting:
            self._free -= 1
            return
        prio = current_priority() if priority is None else priority
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (prio, next(self._seq), fut))
        try:
            await fut
        except asyncio.CancelledError:
            # место уже передано, а ждущий снят - вернуть его следующему
            if fut.done() and not fut.cancelled():
                self.release()
            else:
                fut.cancel()
            raise

    def release(self) -> None:
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)
                return
        self._free += 1

    async def __aenter__(self) -> "PriorityGate":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()
