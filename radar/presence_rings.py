"""Кольцо уровня как фактор потока положения (SAM-118): измеренный уровень сигнала против ожидаемого по клеткам мира.

Одно измерение даёт кольцо вокруг роутера: клетки, где ожидаемый уровень близок к измеренному, получают высокий вес, толщина кольца - ошибка радио-карты.
Ожидаемые уровни по клеткам считает композиция (`presence_radio_map.expected_levels` по точкам мира), фактор их только сравнивает. Уровень приходит
функцией: на каждом срезе окна поток отдаёт уровень этого среза, нет измерения - фактор молчит. Вес среза меньше единицы: соседние отсчёты почти
повторяют друг друга, и без веса кольца перемножались бы как независимые. Модуль чистый."""
from __future__ import annotations

from typing import Callable

import numpy as np

from .presence_factors import Context

RING_SD_DB = 5.0         # разброс уровня в клетке вместе с шумом: ошибка радио-карты по местам 4,9-7,6 дБ, при 4,5 дБ области на долгих стоянках были слишком узкими
SLICE_WEIGHT = 0.3       # вес среза 1 с, подобран проигрышем записи: 0,1 слабо (вершина 19%, тройка 35%), 1,0 мерцает (устойчивость 84%)


class Rings:
    def __init__(self, level_db: Callable[[], float | None], expected_db: np.ndarray, sd_db: float = RING_SD_DB, weight: float = SLICE_WEIGHT) -> None:
        self._level, self._expected_db, self._sd_db, self._weight = level_db, np.asarray(expected_db, dtype=float), sd_db, weight

    def log_field(self, context: Context) -> np.ndarray | None:
        """Логарифм правдоподобия уровня в каждой клетке; измерения нет - фактор молчит."""
        level = self._level()
        if level is None:
            return None
        return -0.5 * self._weight * ((level - self._expected_db) / self._sd_db) ** 2
