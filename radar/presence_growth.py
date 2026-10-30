"""Рост уверенности по секундам окна (SAM-118): для поля каждой секунды три числа - пик, доля массы у точки секунды и площадь, в которой лежит 90% массы.

Поле секунды k это распределение среза k по законным путям первых k + 1 секунд; у последней секунды это итоговое распределение. Ясный сигнал сужает поле: пик растёт,
масса стягивается к точке, площадь 90% падает. Модуль чистый: мир, поля и точки приходят параметрами."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .presence_world import World

MASS_RADIUS_M = 1.0     # «у точки»: клетки не дальше метра пути от точки секунды; метр это примерно шаг человека за срез
COVERAGE = 0.9          # доля массы, площадь которой считается: девяносто процентов


@dataclass(frozen=True)
class SecondGrowth:
    peak: float             # наибольшая вероятность одной клетки поля
    mass_near: float        # доля массы поля в радиусе MASS_RADIUS_M пути от точки секунды; без точки - 0
    area_m2: float          # площадь наименьшего набора клеток, где лежит COVERAGE массы


def second_growth(world: World, field: np.ndarray, point: int | None, radius_m: float = MASS_RADIUS_M, coverage: float = COVERAGE) -> SecondGrowth:
    """Метрики одного поля. Поле нормируется здесь: пустое поле (сумма ноль) даёт нули."""
    total = float(field.sum())
    if total <= 0.0:
        return SecondGrowth(0.0, 0.0, 0.0)
    shares = field / total
    near = 0.0 if point is None else float(shares[world.distances()[point] <= radius_m].sum())
    cumulative = np.cumsum(np.sort(shares)[::-1])
    cells = int(np.searchsorted(cumulative, coverage - 1e-12) + 1)         # допуск: накопленная сумма ровно 0,9 не должна брать лишнюю клетку из-за округления
    return SecondGrowth(float(shares.max()), near, cells * world.step_m ** 2)


def window_growth(world: World, fields: Sequence[np.ndarray], points: Sequence[int | None]) -> list[SecondGrowth]:
    """Метрики каждой секунды окна по полям и точкам секунд, в том же порядке."""
    return [second_growth(world, field, point) for field, point in zip(fields, points)]
