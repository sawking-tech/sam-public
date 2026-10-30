"""Мир потока положения (SAM-118): клетки, где субъект может быть, и расстояния между ними по проходам.

Расстояние между клетками считается по настоящим путям: соседние клетки связаны, только если отрезок между ними свободен, поэтому
оно не идёт сквозь стену и мебель, а две комнаты без двери дают бесконечность. Мир не знает ни о вероятностях, ни о субъектах:
какие места доступны и где преграды, ему говорит навигация через протокол Navigation, у человека, кота и телефона она своя.
Модуль чистый: план приходит внутри навигации, файлов и сети нет."""
from __future__ import annotations

from typing import Protocol

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

WORLD_STEP_M = 0.3            # шаг клеток мира: матрица расстояний растёт как квадрат числа клеток, вдвое мельче шаг - вчетверо больше памяти и времени
LINK_FACTOR = 1.5             # соседние клетки - ближе шага на столько: у квадратной сетки диагональ в 1,41 шага
NEAREST_TRIES = 6             # сколько ближайших клеток перебирать при привязке точки: у стены ближайшая может оказаться по другую её сторону

Point = tuple[float, float]


class Navigation(Protocol):
    """Что мир знает о квартире для одного вида субъекта."""

    def stand_nodes(self, step_m: float) -> list[Point]: ...

    def segment_free(self, a: Point, b: Point) -> bool: ...

    def snap(self, x: float, z: float) -> Point: ...


class World:
    def __init__(self, nav: Navigation, step_m: float = WORLD_STEP_M) -> None:
        points = nav.stand_nodes(step_m)
        if not points:
            raise ValueError("в навигации нет места, где можно быть: мир строить не из чего")
        self._nav = nav
        self.step_m = step_m
        self.points = np.asarray(points, dtype=float)
        self._tree = cKDTree(self.points)
        self._distances: np.ndarray | None = None       # строится при первом обращении: матрица n x n
        self._reach: dict[float, csr_matrix] = {}       # разреженная достижимость за срез, по радиусу

    def __len__(self) -> int:
        return len(self.points)

    def point_of(self, cell: int) -> Point:
        x, z = self.points[cell]
        return float(x), float(z)

    def cell_at(self, x: float, z: float) -> int:
        """Клетка мира для точки: точка сначала привязывается к месту, где можно быть, потом берётся ближайшая клетка, видимая из неё."""
        start = self._nav.snap(x, z)
        _, nearest = self._tree.query(start, k=min(NEAREST_TRIES, len(self)))
        candidates = np.atleast_1d(nearest)
        free = next((int(i) for i in candidates if self._nav.segment_free(start, self.point_of(int(i)))), None)
        return int(candidates[0]) if free is None else free

    def distances(self) -> np.ndarray:
        """Длина кратчайшего пути между клетками в метрах; бесконечность - пути нет."""
        if self._distances is None:
            self._distances = self._build_distances()
        return self._distances

    def reachable(self, a: int, b: int) -> bool:
        return bool(np.isfinite(self.distances()[a, b]))

    def reach(self, radius_m: float) -> csr_matrix:
        """Достижимость за один срез: единица там, где клетки не дальше radius_m и отрезок между ними свободен; клетка достижима из себя."""
        if radius_m not in self._reach:
            n = len(self)
            pairs, _ = self._free_pairs(radius_m)
            rows = np.concatenate([pairs[:, 0], pairs[:, 1], np.arange(n)])
            cols = np.concatenate([pairs[:, 1], pairs[:, 0], np.arange(n)])
            self._reach[radius_m] = csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n))
        return self._reach[radius_m]

    def _free_pairs(self, radius_m: float) -> tuple[np.ndarray, np.ndarray]:
        """Пары клеток не дальше radius_m, между которыми отрезок свободен, и длины отрезков."""
        pairs = np.array(sorted(self._tree.query_pairs(radius_m)), dtype=int).reshape(-1, 2)
        linked = np.array([self._nav.segment_free(self.point_of(int(i)), self.point_of(int(j))) for i, j in pairs], dtype=bool)
        pairs = pairs[linked]
        return pairs, np.hypot(*(self.points[pairs[:, 0]] - self.points[pairs[:, 1]]).T)

    def path_tree(self, target: int) -> tuple[np.ndarray, np.ndarray]:
        """Кратчайшие пути по полу к клетке target: длина пути от каждой клетки в метрах и следующая клетка на пути к target (-9999 у самой target и у недостижимых)."""
        dist, pred = dijkstra(self._graph(), directed=False, indices=target, return_predecessors=True)
        return dist, pred

    def _graph(self) -> csr_matrix:
        n = len(self)
        pairs, lengths = self._free_pairs(self.step_m * LINK_FACTOR)
        return csr_matrix((lengths, (pairs[:, 0], pairs[:, 1])), shape=(n, n))

    def _build_distances(self) -> np.ndarray:
        return dijkstra(self._graph(), directed=False)
