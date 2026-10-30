"""Поля шага к роутеру (SAM-118): из каждой клетки пола человек k секунд идёт к роутеру по кратчайшему пути по полу; поле k - куда он пришёл и на сколько изменился ожидаемый уровень.

Поле на k = 1 показывает перемену уровня за секунду ходьбы к роутеру, на k = 14 - за окно потока. Сравнение с шумом уровня носимого телефона говорит, с какого
горизонта движение к роутеру различимо по уровню. Отрицательная разница - модель ждёт падения уровня по пути к роутеру (тень стены). Модуль чистый: мир, уровни
и параметры приходят аргументами."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import presence_level_delta as pld
from . import presence_motion as pm
from . import presence_window as pw_

WALK_SPEED_M_S = pm.LOCALITY_M          # средний шаг 1,39 м/с, тот же, что у радиуса локальности перехода за срез
CARRIED_NOISE_DB = pld.DELTA_SD_DB       # разброс разности уровня носимого телефона за секунду: порог «разница различима»
NO_STEP = -9999                          # у scipy dijkstra так помечена клетка без следующего шага: сама цель или недостижимая


@dataclass(frozen=True)
class WalkField:
    seconds: int
    to: np.ndarray            # клетка, куда пришёл из каждой; без пути к роутеру - она же
    delta_db: np.ndarray      # ожидаемый уровень в конце минус в начале


def walk_ends(dist: np.ndarray, pred: np.ndarray, points: np.ndarray, length_m: float) -> np.ndarray:
    """Где окажется идущий из каждой клетки к цели по дереву путей, пройдя length_m: последняя клетка пути, до которой он дошёл целиком."""
    ends = np.arange(len(dist))
    for start in range(len(dist)):
        if not np.isfinite(dist[start]):
            continue
        cell, walked = start, 0.0
        while pred[cell] != NO_STEP:
            nxt = int(pred[cell])
            step = float(np.hypot(*(points[nxt] - points[cell])))
            if walked + step > length_m:
                break
            walked, cell = walked + step, nxt
        ends[start] = cell
    return ends


def walk_fields(dist: np.ndarray, pred: np.ndarray, points: np.ndarray, expected_db: np.ndarray, seconds: int = pw_.WINDOW_SLICES,
                speed_m_s: float = WALK_SPEED_M_S) -> list[WalkField]:
    """Поля для k = 1..seconds: куда пришёл из каждой клетки за k секунд шага к цели и разница ожидаемого уровня."""
    out = []
    for k in range(1, seconds + 1):
        ends = walk_ends(dist, pred, points, speed_m_s * k)
        out.append(WalkField(k, ends, expected_db[ends] - expected_db))
    return out


def summary(field: WalkField, noise_db: float = CARRIED_NOISE_DB) -> dict:
    """Сводка поля по клеткам, которые сдвинулись: медиана разницы, доля выше шума и доля с падением уровня."""
    moved = field.to != np.arange(len(field.to))
    delta = field.delta_db[moved]
    if not len(delta):
        return {"seconds": field.seconds, "moved": 0, "median_db": 0.0, "above_noise": 0.0, "falling": 0.0}
    return {"seconds": field.seconds, "moved": int(moved.sum()), "median_db": round(float(np.median(delta)), 2),
            "above_noise": round(float(np.mean(delta > noise_db)), 3), "falling": round(float(np.mean(delta < 0)), 3)}
