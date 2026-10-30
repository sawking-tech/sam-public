"""Сглаживание уровня сигнала по времени (SAM-118): медиана по короткому окну гасит одиночные провалы и всплески (рука, тело, переотражения),
чтобы они не принимались за шаг. Модуль чистый: время и отсчёты приходят параметрами."""
from __future__ import annotations

import math
import statistics
from collections import deque

WINDOW_S = 5.0     # окно медианы: на шаге человека уровень меняется за несколько секунд, одиночный провал короче
STILL_WINDOW_S = 20.0   # окно, по которому устройство считается лежащим: уровень ровный
STILL_SD_DB = 3.0       # разброс уровня в окне не больше этого - телефон лежит (на шаге человека уровень гуляет на 8-15 дБ)
STILL_MIN_SAMPLES = 10  # меньше отсчётов в окне - судить рано
KEEP_MAX = 64      # отсчётов в буфере на устройство: при опросе около раза в секунду окно с запасом


class LevelSmoother:
    """Скользящая медиана уровня одного устройства."""

    def __init__(self, window_s: float = WINDOW_S) -> None:
        self.window_s = window_s
        self._samples: deque[tuple[float, float]] = deque(maxlen=KEEP_MAX)

    def add(self, observed_at: float, db: float) -> float:
        """Записать отсчёт (тот же момент второй раз не добавляется) и вернуть медиану за окно, оканчивающееся этим моментом."""
        if not self._samples or observed_at > self._samples[-1][0]:
            self._samples.append((observed_at, db))
        return self.median(observed_at)

    def is_still(self, now: float, window_s: float = STILL_WINDOW_S, max_sd_db: float = STILL_SD_DB) -> bool:
        """Уровень ровный в последние window_s секунд: устройство лежит, а не идёт в руке."""
        levels = [db for _, db in self.recent(now, window_s)]
        return len(levels) >= STILL_MIN_SAMPLES and statistics.pstdev(levels) <= max_sd_db

    def recent(self, now: float, window_s: float) -> list[tuple[float, float]]:
        """Отсчёты (время, уровень) за последние window_s секунд по порядку времени."""
        return [(t, db) for t, db in self._samples if now - t <= window_s]

    def median(self, now: float) -> float:
        window = [db for t, db in self._samples if now - t <= self.window_s]
        return statistics.median(window) if window else math.nan
