"""Список мест с вероятностями (SAM-118): выход потока для модели SAM. Поток ничего не сжимает до одного места: у субъекта отдаётся, с какой вероятностью он у каждого названного места.

Клетка мира принадлежит ближайшему названному месту, если оно не дальше радиуса; поэтому вероятности соседних мест не делят одну массу дважды и их сумма не больше единицы,
остаток - вероятность, что субъект не у названных мест. Усреднение за часы и вывод, кто где был и что делал, делает модель SAM по этим спискам. Места приходят данными. Модуль чистый."""
from __future__ import annotations

from typing import Mapping

import numpy as np

from .presence_world import World

PLACE_RADIUS_M = 1.5        # клетка не дальше этого от названного места принадлежит ему: радиус, на котором проверялась верность места на записи


class PlaceReport:
    def __init__(self, world: World, places: Mapping[str, tuple[float, float]], radius_m: float = PLACE_RADIUS_M) -> None:
        self._names = list(places)
        self._owner = np.full(len(world), -1, dtype=int)             # клетка -> номер места, -1: ничьё
        if self._names:
            xy = np.array([places[name] for name in self._names], dtype=float)
            gaps = np.hypot(world.points[:, None, 0] - xy[None, :, 0], world.points[:, None, 1] - xy[None, :, 1])
            nearest = gaps.argmin(axis=1)
            self._owner = np.where(gaps[np.arange(len(world)), nearest] <= radius_m, nearest, -1)

    def name_at(self, cell: int) -> str:
        """Название места, которому принадлежит клетка; клетка вне названных мест - пусто."""
        owner = int(self._owner[cell])
        return self._names[owner] if owner >= 0 else ""

    def __call__(self, distribution: np.ndarray) -> list[tuple[str, float]]:
        """Места с вероятностью по убыванию; места без массы не попадают в список."""
        mass = np.bincount(self._owner[self._owner >= 0], weights=distribution[self._owner >= 0], minlength=len(self._names))
        return sorted(((name, float(p)) for name, p in zip(self._names, mass) if p > 0.0), key=lambda item: -item[1])
