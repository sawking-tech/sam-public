"""Выбор путей для слоя объяснения (SAM-118): из пучка выборки берутся самые разные.

Пучок из ста путей на картинке слипается в один толстый жгут. Десять самых широких показывают размах допустимых путей: что окно ещё считает законным по краям.
Модуль чистый: клетки мира и пути приходят массивами."""
from __future__ import annotations

import numpy as np


def widest(points: np.ndarray, paths: np.ndarray, n: int) -> np.ndarray:
    """n самых разных путей пучка (путей x срезов, номера клеток). Первый путь дальше всех от среднего пути, каждый следующий дальше всех от уже взятых.
    Расстояние между путями - среднее по срезам евклидово расстояние между их клетками. Путей не больше n - пучок возвращается целиком."""
    if len(paths) <= n:
        return paths
    xy = points[paths]                                                # путей x срезов x 2
    distance = lambda reference: np.linalg.norm(xy - reference, axis=2).mean(axis=1)
    chosen = [int(np.argmax(distance(xy.mean(axis=0))))]
    nearest = distance(xy[chosen[0]])
    while len(chosen) < n:
        nearest[chosen] = -1.0                                       # взятые путь не повторяется
        chosen.append(int(np.argmax(nearest)))
        nearest = np.minimum(nearest, distance(xy[chosen[-1]]))
    return paths[chosen]
