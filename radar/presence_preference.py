"""Карта предпочтений как фактор потока положения (SAM-118): где субъект бывает чаще в это время, независимо от уровня сигнала.

Поле по клеткам строит композиция (для человека `presence_habits.cell_field` по его привычкам, для телефона и кота своя карта мест) и отдаёт функцией
от времени; ровный фон внутри поля уже есть, поэтому необычное место не обнуляется. Фактор только берёт логарифм. Он не знает, сколько раз его позовут:
предпочтение входит в распределение один раз (на начало пути или на выход окна), на каждом срезе оно учлось бы столько раз, сколько срезов в окне.
Модуль чистый."""
from __future__ import annotations

from datetime import datetime
from typing import Callable

import numpy as np

from .presence_factors import LOG_FLOOR, Context


class Preference:
    def __init__(self, field_at: Callable[[datetime], np.ndarray | None], weight: float = 1.0) -> None:
        """weight - степень, в которую возводится поле: 1 - как есть, 0 - предпочтение молчит, между ними сигнал перевешивает. Вес подбирается проигрышем записи (SAM-118)."""
        if weight < 0.0:
            raise ValueError(f"вес предпочтения {weight} меньше нуля")
        self._field_at, self._weight = field_at, weight

    def log_field(self, context: Context) -> np.ndarray | None:
        """Логарифм предпочтения по клеткам на момент контекста; карты на это время нет - фактор молчит."""
        field = self._field_at(context.now)
        if field is None:
            return None
        return self._weight * np.log(np.maximum(np.asarray(field, dtype=float), LOG_FLOOR))
