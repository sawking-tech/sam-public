"""Разность уровня как свидетельство о переходе (SAM-118): измеренное изменение уровня за срез против разности ожидаемых уровней пары клеток.

Одна точка измерения не различает места одного кольца (0,5-1,1 дБ между ожидаемыми уровнями при ошибке карты 5 дБ). Их различает рисунок
подхода: последовательность разностей уровня от секунды к секунде dL_t, у пути-кандидата своя последовательность E(c_t) - E(c_t-1). Ошибка карты в месте
постоянна во времени, поэтому в разности соседних секунд она сокращается, и остаётся шум измерения около 1 дБ. Вес перехода c1 -> c2 умножается на
гауссиану разности: при dL около нуля шаг поперёк градиента штрафуется сам, стоящий человек даёт узор из нулей. Уровень кольца (`presence_rings`) остаётся
якорем. На синтетике с шумом 2 дБ фактор поднимает точку у места с 2,9% до 26,9%; у носимого телефона разность шумит на 5,5 дБ при полезной разности
0,3-3 дБ за шаг, и на размеченной прогулке фактор только вредит (вершина 65% -> 2% при разбросе 1,4, 53% при 5), поэтому в живом потоке он выключен,
пока уровень не станет тише (`scripts/radar_flow_replay.py delta`). Модуль чистый: уровень приходит функцией, как у кольца."""
from __future__ import annotations

from datetime import datetime
from typing import Callable

import numpy as np

DELTA_SD_DB = 5.5        # разброс разности уровней соседних секунд у носимого телефона: на стоянках размеченной прогулки sd 4,5-6,4 дБ (5 ГГц), на переходах 6,1; у неподвижного устройства было бы 1,5 (замер: sd 1,07)
DELTA_WEIGHT = 1.0       # вес свидетельства разности: единица, в отличие от кольца (0,3): соседние разности независимы, их шум не повторяется от секунды к секунде


class LevelDelta:
    def __init__(self, level_db: Callable[[], float | None], expected_db: np.ndarray, sd_db: float = DELTA_SD_DB, weight: float = DELTA_WEIGHT) -> None:
        if sd_db <= 0 or weight <= 0:
            raise ValueError(f"разность уровня с разбросом {sd_db} дБ и весом {weight}: нужны положительные; выключается отсутствием фактора, а не нулём")
        self._level, self._expected_db, self._sd_db, self._weight = level_db, np.asarray(expected_db, dtype=float), sd_db, weight
        self._previous: float | None = None          # уровень прошлого среза; None - его не было или он молчал

    def observe(self, now: datetime) -> float | None:
        """Разность уровня этого среза и прошлого; одного из них нет - None, и прошлым становится нынешний."""
        level = self._level()
        delta = None if level is None or self._previous is None else level - self._previous
        self._previous = level
        return delta

    def log_pairs(self, observation: object, rows: np.ndarray, cols: np.ndarray) -> np.ndarray | None:
        """Логарифм правдоподобия измеренной разности для переходов из клеток rows в клетки cols; разности нет - молчит."""
        if observation is None:
            return None
        expected = self._expected_db[cols] - self._expected_db[rows]
        return -0.5 * self._weight * ((float(observation) - expected) / self._sd_db) ** 2

    def reset(self) -> None:
        self._previous = None


def ascent(expected_db: np.ndarray, points: np.ndarray, cell: int, neighbours: np.ndarray) -> tuple[float, float, float]:
    """Направление роста ожидаемого уровня в клетке: единичный вектор (dx, dz) и крутизна в дБ/м по наименьшим квадратам плоскости по соседним клеткам. Стрелка к роутеру
    на слое: уровень растёт к нему. Соседей меньше двух или они на одной прямой - крутизна нуль и вектор нулевой."""
    others = neighbours[neighbours != cell]
    if len(others) < 2:
        return 0.0, 0.0, 0.0
    offsets = points[others] - points[cell]
    rise = expected_db[others] - expected_db[cell]
    gradient, _, rank, _ = np.linalg.lstsq(offsets, rise, rcond=None)
    slope = float(np.hypot(*gradient))
    if rank < 2 or slope == 0.0:
        return 0.0, 0.0, 0.0
    return float(gradient[0] / slope), float(gradient[1] / slope), slope
