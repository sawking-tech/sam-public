"""Факторы потока положения (SAM-118): всё, что говорит, где субъект может быть, - фактор одного вида, и поток складывает их одинаково.

Фактор отдаёт поле по клеткам мира: логарифм того, насколько клетка подходит под этот признак, или None, если признак молчит.
Карта предпочтений, переход от прошлой точки и внешний признак (уровень сигнала, голос, камера, состояние техники, чужие слова)
различаются тем, что именно они считают, а не интерфейсом: новый признак подключается новым классом без правки сводящей функции.
Вес и срок годности признака лежат в нём самом. Нулевая вероятность клетки - минус бесконечность в поле, и ставит её тот признак,
который точно знает (выключенный компьютер, стена). Модуль чистый: время и случайность приходят параметрами."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

import numpy as np

from .presence_world import World

LOG_FLOOR = 1e-300       # вероятность не меньше этого перед логарифмом: нуль дал бы минус бесконечность и исключил бы клетку насовсем; исключает только тот, кто точно знает


@dataclass(frozen=True)
class Context:
    """Что фактор может знать о моменте: мир, время, прошлый шаг этого же субъекта и что срез измерил о переходе в него."""
    world: World
    now: datetime
    previous: np.ndarray | None = None       # распределение по клеткам на прошлом шаге; None - шага не было
    elapsed_s: float = 0.0                   # сколько секунд прошло с прошлого шага
    observed: tuple | None = None            # наблюдения парных факторов перехода в этот срез, по одному на фактор (`Transition.observe`); None - парных факторов нет


class Factor(Protocol):
    def log_field(self, context: Context) -> np.ndarray | None: ...


class PairFactor(Protocol):
    """Свидетельство о переходе между клетками: что измерено на срезе (`observe`) и логарифм правдоподобия этого измерения для пары клеток «из строки в столбец» (`log_pairs`).
    Наблюдение хранит окно по срезам, поэтому фактор помнит только то, что нужно для следующего наблюдения (прошлый уровень), и забывает это по `reset`."""
    def observe(self, now: datetime) -> object | None: ...
    def log_pairs(self, observation: object, rows: np.ndarray, cols: np.ndarray) -> np.ndarray | None: ...
    def reset(self) -> None: ...


class Transition(Protocol):
    """Переход между срезами для окна путей: матрица зависит от наблюдений среза, в который переходим."""
    def observe(self, now: datetime) -> tuple | None: ...
    def log_field(self, context: Context) -> np.ndarray | None: ...
    def into_matrix(self, world: World, observed: tuple | None = None) -> np.ndarray: ...
    def reset(self) -> None: ...


def fuse(factors: Sequence[Factor], context: Context) -> np.ndarray:
    """Распределение по клеткам: произведение полей всех факторов, которые не молчат; сумма единица. Никто не говорит - ровное."""
    n = len(context.world)
    total = np.zeros(n)
    for factor in factors:
        field = factor.log_field(context)
        if field is not None:
            total += _checked(field, n, factor)
    top = total.max()
    if not np.isfinite(top):
        raise ValueError("факторы исключили все клетки: проверь признаки с нулевой вероятностью")
    weights = np.exp(total - top)
    return weights / weights.sum()


def _checked(field: np.ndarray, n: int, factor: Factor) -> np.ndarray:
    if field.shape != (n,):
        raise ValueError(f"фактор {type(factor).__name__} вернул поле формы {field.shape}, а клеток мира {n}")
    return field
