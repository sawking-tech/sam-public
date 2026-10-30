"""Поводы своей инициативы SAM (SAM-49): проверяет код, реплику делает основная модель.

Повод - ворота лёгкого workflow стенда (SAM-96, `workflows/late-night.yml`): расписание и вызов основной
модели - у планировщика `gateway/workflows.py`. Отдельного вызова LLM мимо основной модели здесь больше нет
(основная модель и есть модель шлюза). Образец - heartbeat openclaw раз в 30 минут с молчанием
по умолчанию (матрица агентов 8 окт).

Повод v1 один - поздний час: с LATE_FROM_H до LATE_TO_H, а человек говорил в последние ACTIVE_S.
Поводы - чистые функции (время, активность) -> текст повода или пусто, проверяются без сети.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable
from .env import env_float, env_int

LATE_FROM_H = env_int("SAM_LATE_FROM_H", 2)                 # SAM-49: с 02:00 - пора спать
LATE_TO_H = env_int("SAM_LATE_TO_H", 6)
ACTIVE_S = env_float("SAM_ACTIVE_S", 600.0)                   # человек не спит: говорил за 10 минут


def in_hours(hour: int, from_h: int, to_h: int) -> bool:
    """Час в окне [from_h, to_h); окно может переходить через полночь (23-6). Равные границы - пустое окно."""
    if from_h <= to_h:
        return from_h <= hour < to_h
    return hour >= from_h or hour < to_h


def late_night(now: datetime, since_human_s: float | None) -> str:
    """Повод «поздний час» с точным временем (10 окт в 02:00 модель сказала «три часа ночи»); нет повода - ''."""
    if not in_hours(now.hour, LATE_FROM_H, LATE_TO_H) or since_human_s is None or since_human_s > ACTIVE_S:
        return ""
    return f"Сейчас ровно {now:%H:%M}. Человек не спит: говорил {int(since_human_s // 60)} мин назад."


class HumanActivity:
    """Когда человек говорил последний раз: голосовой ход отмечает, повод читает."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: float | None = None

    def note(self) -> None:
        self._last = self._clock()

    def since_s(self) -> float | None:
        return None if self._last is None else self._clock() - self._last
