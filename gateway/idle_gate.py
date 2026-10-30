"""Ворота простоя (SAM-62, ADR-015): фоновая работа с памятью идёт, когда N секунд нет хода.

Ход (текстовый и голосовой) отмечает активность: пока он идёт, ворота закрыты; тишина считается с его
конца. Фоновое сжатие ждёт открытых ворот, чтобы не делить GPU с живым ответом. Одни ворота на процесс:
их же ждут автозапись фактов (SAM-63) и консолидация (SAM-22).

Логика без сети и звука: часы приходят параметром.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from typing import Awaitable, Callable, Iterator
from .env import env_float

# Сколько секунд тишины - пауза. Не замерено: в записях разговоров реплики идут через 5-20 с
# (замер на записи разговоров), 30 с тишины - разговор скорее закончился
IDLE_S = env_float("SAM_IDLE_S", 30.0)
POLL_S = 1.0


class IdleGate:
    def __init__(self, idle_s: float = IDLE_S, clock: Callable[[], float] = time.monotonic,
                 poll_s: float = POLL_S, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 timer: Callable[[], float] | None = None) -> None:
        """sleep и timer - ожидание опроса и часы предела ожидания; по умолчанию asyncio.sleep и часы цикла."""
        self._idle_s, self._clock, self._poll_s = idle_s, clock, poll_s
        self._sleep, self._timer = sleep, timer
        self._last: float | None = None
        self._active = 0

    def touch(self) -> None:
        self._last = self._clock()

    @contextmanager
    def activity(self) -> Iterator[None]:
        """Ход: ворота закрыты всё время хода и ещё idle_s после него."""
        self._active += 1
        self.touch()
        try:
            yield
        finally:
            self._active -= 1
            self.touch()

    def turns(self) -> int:
        """Сколько ходов идёт сейчас (перезапуск шлюза ждёт их конца, gateway/restart.py)."""
        return self._active

    def is_idle(self) -> bool:
        if self._active:
            return False
        return self._last is None or self._clock() - self._last >= self._idle_s

    async def wait_idle(self, poll_s: float | None = None, max_s: float | None = None) -> bool:
        """Ждать паузы. max_s - предел ожидания: вышел - False, ждущий идёт без паузы (SAM-97).
        Предел считается по timer (по умолчанию часы цикла), а не по clock ворот."""
        timer = self._timer or asyncio.get_running_loop().time
        deadline = None if max_s is None else timer() + max_s
        while not self.is_idle():
            if deadline is not None and timer() >= deadline:
                return False
            await self._sleep(poll_s or self._poll_s)
        return True


gate = IdleGate()
