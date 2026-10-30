"""Переход между соседними срезами как фактор потока положения (SAM-118): правило локальности и стены.

Точка следующего среза обязана быть близко к точке прошлого: клетка достижима из клетки, если расстояние между ними не больше радиуса R и отрезок
между ними свободен, то есть не пересекает стену и мебель. Масса клетки делится поровну между достижимыми из неё клетками. Срезы фиксированные
(одно измерение роутера), время и скорость в расчёте не участвуют. Малая доля массы, jump_share, остаётся на «где угодно»: после ошибочной оценки
сильное измерение должно мочь вернуть распределение. Радиус и доля - свои у каждого субъекта и приходят в конструктор.

Парные факторы (`PairFactor`, например разность уровня) дают свидетельство о самом переходе: вес пары клеток умножается на правдоподобие того, что срез
измерил о переходе в него, без перенормировки, иначе свидетельство стало бы приором направления. Матрица поэтому своя у каждого среза: окно хранит
наблюдения по срезам и передаёт их через `Context.observed`. Модуль чистый."""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

import numpy as np
from scipy.sparse import csr_matrix, identity

from .presence_factors import LOG_FLOOR, Context, PairFactor
from .presence_world import World

LOCALITY_M = 1.4         # радиус локальности за срез 1 с: средний шаг 1,39 м/с (5 км/ч); до подбора по проходкам
JUMP_SHARE = 0.0         # доля массы, которая за срез может оказаться где угодно: ноль, невозможные пути не нужны; проигрыш с 0,02 даёт то же (устойчивость 89%, тройка 50%); восстановление после ошибки - на границе окна


class Motion:
    def __init__(self, locality_m: float = LOCALITY_M, jump_share: float = JUMP_SHARE, jump_field: np.ndarray | None = None, sigma_m: float | None = None,
                 stay_share: float = 0.0, pairs: Sequence[PairFactor] = ()) -> None:
        """sigma_m - инерция: вес перехода на расстояние d по проходам равен exp(-d^2 / 2 sigma^2); None - равномерно по радиусу, как раньше.
        stay_share - доля «остался в клетке»: человек либо стоит, либо идёт с обычной скоростью, поэтому стояние предпочитается, а шаг в радиусе не штрафуется по длине.
        pairs - свидетельства о переходе: их наблюдения окно собирает на срезе (`observe`) и отдаёт обратно в `Context.observed`."""
        if sigma_m is not None and sigma_m <= 0:
            raise ValueError(f"инерция {sigma_m} м: нужна положительная длина или None")
        if not 0.0 <= stay_share < 1.0:
            raise ValueError(f"доля стояния {stay_share}: нужна от нуля до единицы, единица запретила бы ходьбу")
        self._locality_m, self._jump_share, self._jump_field, self._sigma_m, self._stay_share = locality_m, jump_share, jump_field, sigma_m, stay_share
        self._pairs = tuple(pairs)
        self._dense: tuple[World, np.ndarray] | None = None       # плотная матрица переходов без наблюдений для последнего мира
        self._forward: tuple[World, csr_matrix] | None = None     # разреженные веса переходов «из строки в столбец» без наблюдений, по строкам сумма единица

    def observe(self, now: datetime) -> tuple | None:
        """Наблюдения парных факторов на этом срезе, по одному на фактор; факторов нет - None, и матрица среза равна общей."""
        return tuple(factor.observe(now) for factor in self._pairs) if self._pairs else None

    def reset(self) -> None:
        """Парные факторы забывают прошлый срез: после долгого молчания разность с ним ничего не значит."""
        for factor in self._pairs:
            factor.reset()

    def _base_weights(self, world: World) -> csr_matrix:
        """Вероятность перехода из клетки (строка) в клетку (столбец) за срез: достижимые клетки, с инерцией вес падает с расстоянием; сумма по строке единица."""
        if self._forward is None or self._forward[0] is not world:
            reach = world.reach(self._locality_m).tocoo()
            weights = np.ones(len(reach.data)) if self._sigma_m is None else np.exp(-0.5 * (world.distances()[reach.row, reach.col] / self._sigma_m) ** 2)
            matrix = csr_matrix((weights, (reach.row, reach.col)), shape=reach.shape)
            forward = csr_matrix(matrix.multiply(1.0 / matrix.sum(axis=1)))
            if self._stay_share > 0.0:
                forward = csr_matrix((1.0 - self._stay_share) * forward + self._stay_share * identity(forward.shape[0], format="csr"))
            self._forward = (world, forward)
        return self._forward[1]

    def _forward_weights(self, world: World, observed: tuple | None = None) -> csr_matrix:
        """Веса переходов среза: общие веса, умноженные на правдоподобие наблюдений среза для каждой пары клеток; наблюдений нет или все молчат - общие."""
        base = self._base_weights(world)
        if observed is None:
            return base
        coo = base.tocoo()
        total = None
        for factor, observation in zip(self._pairs, observed):
            log_pairs = factor.log_pairs(observation, coo.row, coo.col)
            if log_pairs is not None:
                total = log_pairs if total is None else total + log_pairs
        if total is None:
            return base
        return csr_matrix((coo.data * np.exp(total - total.max()), (coo.row, coo.col)), shape=base.shape)       # общий множитель не меняет распределения; без него веса уходили бы в нуль

    def into_matrix(self, world: World, observed: tuple | None = None) -> np.ndarray:
        """Матрица M, где M[c', c] - вес перехода из клетки c в c' за один срез (тот же, по которому считается log_field); строки нужны обратной выборке путей.
        Без наблюдений плотная матрица строится один раз на мир; с наблюдениями - на каждый вызов, окно хранит её по срезам само."""
        if observed is not None:
            return self._dense_from(world, self._forward_weights(world, observed))
        if self._dense is None or self._dense[0] is not world:
            self._dense = (world, self._dense_from(world, self._base_weights(world)))
        return self._dense[1]

    def _dense_from(self, world: World, forward: csr_matrix) -> np.ndarray:
        background = 1.0 / len(world) if self._jump_field is None else self._jump_field / self._jump_field.sum()
        return ((1.0 - self._jump_share) * forward.toarray() + self._jump_share * background).T.copy()

    def into(self, world: World, cell: int) -> np.ndarray:
        """Вероятность перехода в клетку cell из каждой клетки за один срез без наблюдений: строка матрицы into_matrix."""
        return self.into_matrix(world)[cell]

    def log_field(self, context: Context) -> np.ndarray | None:
        """Логарифм вероятности клетки после перехода с учётом наблюдений среза; прошлого шага нет - переход молчит."""
        if context.previous is None:
            return None
        moved = self._forward_weights(context.world, context.observed).T @ context.previous       # масса клетки уходит в достижимые из неё клетки по весам перехода и правдоподобию наблюдений
        background = np.full(len(moved), 1.0 / len(moved)) if self._jump_field is None else self._jump_field / self._jump_field.sum()
        return np.log(np.maximum((1.0 - self._jump_share) * moved / moved.sum() + self._jump_share * background, LOG_FLOOR))
