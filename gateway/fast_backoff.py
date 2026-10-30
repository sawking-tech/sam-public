"""Откат быстрого голоса по ошибкам (SAM-168): чем больше поправок основной за окно, тем дольше отвечает основная.

Поправка (amend_reply, в том числе DROP) отмечается по сессии. Срок отката после поправки - BASE_S * FACTOR^(n-1), где
n - поправок за последние WINDOW_S, не больше MAX_S; без новых поправок откат кончается сам. Часы приходят параметром:
логика проверяется без времени.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable

from .env import env_float

# Окно счёта поправок: в живом разговоре реплика раз в 5-20 с, пять минут - 15-60 реплик; не замерено
WINDOW_S = env_float("SAM_FAST_BACKOFF_WINDOW_S", 300.0)
BASE_S = env_float("SAM_FAST_BACKOFF_BASE_S", 60.0)      # первая поправка - минута основной
FACTOR = env_float("SAM_FAST_BACKOFF_FACTOR", 2.0)       # каждая следующая за окно удваивает срок
MAX_S = env_float("SAM_FAST_BACKOFF_MAX_S", 1800.0)      # потолок - полчаса


def duration(corrections: int, base_s: float = BASE_S, factor: float = FACTOR, max_s: float = MAX_S) -> float:
    """Срок отката после n-й поправки за окно; 0 поправок - 0."""
    return 0.0 if corrections <= 0 else min(max_s, base_s * factor ** (corrections - 1))


class FastBackoff:
    def __init__(self, window_s: float = WINDOW_S, clock: Callable[[], float] = time.monotonic,
                 length: Callable[[int], float] = duration) -> None:
        self._window, self._clock, self._length = window_s, clock, length
        self._marks: dict[str, deque] = {}
        self._until: dict[str, float] = {}

    def note(self, session: str) -> tuple[int, float]:
        """Поправка в сессии: (поправок за окно, срок отката в секундах)."""
        now = self._clock()
        marks = self._marks.setdefault(session, deque())
        marks.append(now)
        while marks and now - marks[0] > self._window:
            marks.popleft()
        length = self._length(len(marks))
        self._until[session] = now + length
        return len(marks), length

    def remaining(self, session: str) -> float:
        """Сколько секунд отката осталось; 0 - быстрая снова отвечает."""
        return max(0.0, self._until.get(session, 0.0) - self._clock())

    def status(self) -> dict[str, int]:
        """Для /health: сессии в откате и сколько секунд осталось."""
        return {s: int(left) for s in list(self._until) if (left := self.remaining(s)) > 0}
