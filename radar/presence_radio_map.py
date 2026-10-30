"""Ожидаемый уровень сигнала по плану (SAM-118): затухание по расстоянию и по толщине стен на прямой от роутера до точки.

Уровень = L0 - 10 n lg d - a * (сумма толщин пересечённых стен). Чистое расстояние без стен требовало показателя n=5,8 на шести стоянках набора проходок (стены прятались
в дальность), со стенами n=2 и отклонение «без одного места» 1,4 дБ вместо 5,7. Подгонка по долгим стоянкам с известным местом; отражения не моделируются.
Модуль чистый: стены, роутер и уровни приходят параметрами."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FREE_SPACE_N = 2.0           # показатель затухания в свободном пространстве: стены считаются отдельно, поэтому он не подбирается
ROUTER_HEIGHT_M = 2.4        # высота роутера по плану; телефон на высоте около метра
PHONE_HEIGHT_M = 1.0
MIN_CONTROLS = 3             # меньше контрольных точек: L0, затухание стен и n не определить
SOFT_SHADOW_M = 0.3          # боковой сдвиг луча для размытия тени: порядка половины длины волны вокруг края стены на 5 ГГц с запасом на мелкую геометрию плана
SOFT_RAYS = ((0.25, -1.0), (0.5, 0.0), (0.25, 1.0))   # вес и сдвиг в долях SOFT_SHADOW_M для боковых лучей
OPENING_DEPTH_TOL_M = 0.15   # проём в плане тоньше стены и сдвинут относительно её оси: допуск по глубине при проверке пересечения
DOORWAY_TITLES = ("Дверь", "Дверная")
NEAR_REF_M = 1.5             # расстояние пика при проходе у роутера: коридор 1,4 м с роутером посередине и высота 1,4 м над телефоном дают не больше 1,6 м
NEAR_BREAK_M = 3.0           # ближе этого действует надбавка ближнего поля; дальше стоят стоянки (3,7-6,8 м), по ним подогнана остальная модель
NEAR_CAP = 1.5               # во сколько раз надбавка может вырасти внутри NEAR_REF_M: вплотную к роутеру уровень не уходит в бесконечность


@dataclass(frozen=True)
class WallModel:
    l0_db: float             # уровень на 1 м
    n: float                 # показатель затухания по расстоянию
    wall_db_per_m: float     # потеря за метр толщины пересечённых стен
    near_gain_db: float = 0.0   # надбавка ближнего поля на расстоянии NEAR_REF_M, плавно уходит к NEAR_BREAK_M

    def predict(self, dist_m: np.ndarray, wall_thickness_m: np.ndarray) -> np.ndarray:
        base = self.l0_db - 10.0 * self.n * np.log10(dist_m) - self.wall_db_per_m * wall_thickness_m
        reach = np.clip(np.log(NEAR_BREAK_M / dist_m) / np.log(NEAR_BREAK_M / NEAR_REF_M), 0.0, NEAR_CAP)
        return base + self.near_gain_db * reach


def distance3d(xy: np.ndarray, router: tuple[float, float], router_h: float = ROUTER_HEIGHT_M, phone_h: float = PHONE_HEIGHT_M) -> np.ndarray:
    """Расстояние от роутера до точек по прямой в трёх измерениях, метры."""
    xy = np.atleast_2d(xy)
    flat = np.hypot(xy[:, 0] - router[0], xy[:, 1] - router[1])
    return np.hypot(flat, router_h - phone_h)


def doorway_openings(objects: list[dict]) -> list[dict]:
    """Проёмы дверей в стенах: рама и открытая дверь пропускают сигнал без потерь; окна остаются стеной."""
    return [o for o in objects if o.get("kind") == "opening" and str(o.get("title", "")).startswith(DOORWAY_TITLES) and "окно" not in str(o.get("title", "")).lower()]


def _in_opening(openings: list[dict], x: np.ndarray, z: np.ndarray) -> np.ndarray:
    """Точки (x, z) внутри проёмов; проём - повёрнутый прямоугольник плана с центром в (x + w/2, z + d/2)."""
    inside = np.zeros(len(x), dtype=bool)
    for o in openings:
        dx, dz = x - (o["x"] + o["w"] / 2), z - (o["z"] + o["d"] / 2)
        c, s = np.cos(o.get("angle", 0.0)), np.sin(o.get("angle", 0.0))
        inside |= (np.abs(dx * c - dz * s) <= o["w"] / 2) & (np.abs(dx * s + dz * c) <= o["d"] / 2 + OPENING_DEPTH_TOL_M)
    return inside


def _hard_thickness(walls: list[dict], router: tuple[float, float], points: np.ndarray, openings: list[dict]) -> np.ndarray:
    total = np.zeros(len(points))
    px, pz = router
    for w in walls:
        ax, az, bx, bz = w["x1"], w["z1"], w["x2"], w["z2"]
        side_ab_p = (bz - az) * (px - ax) - (bx - ax) * (pz - az)
        side_ab_q = (bz - az) * (points[:, 0] - ax) - (bx - ax) * (points[:, 1] - az)
        side_pq_a = (points[:, 1] - pz) * (ax - px) - (points[:, 0] - px) * (az - pz)
        side_pq_b = (points[:, 1] - pz) * (bx - px) - (points[:, 0] - px) * (bz - pz)
        crossed = (side_ab_p * side_ab_q < 0) & (side_pq_a * side_pq_b < 0)
        if openings and crossed.any():
            along = side_ab_p / np.where(crossed, side_ab_p - side_ab_q, 1.0)               # доля пути от роутера до точки пересечения со стеной
            crossed &= ~_in_opening(openings, px + along * (points[:, 0] - px), pz + along * (points[:, 1] - pz))
        total += w["t"] * crossed
    return total


def wall_thickness_on_path(walls: list[dict], router: tuple[float, float], points: np.ndarray, openings: list[dict] = (), soft_m: float = SOFT_SHADOW_M) -> np.ndarray:
    """Сумма толщин стен на прямой от роутера до каждой точки, без проёмов; стена - словарь x1, z1, x2, z2, t.

    Тень стены размыта по боковым лучам на soft_m: волна огибает край стены, поэтому у границы тени уровень меняется плавно, а не скачком."""
    points = np.atleast_2d(points)
    if soft_m <= 0:
        return _hard_thickness(walls, router, points, list(openings))
    ray = points - np.asarray(router)
    length = np.maximum(np.hypot(ray[:, 0], ray[:, 1]), 1e-9)
    side = np.column_stack([-ray[:, 1], ray[:, 0]]) / length[:, None] * soft_m
    return sum(weight * _hard_thickness(walls, router, points + shift * side, list(openings)) for weight, shift in SOFT_RAYS)


def fit_wall_model(dist_m: np.ndarray, wall_thickness_m: np.ndarray, levels_db: np.ndarray, n: float = FREE_SPACE_N,
                   peak_levels_db: np.ndarray | list[float] = ()) -> WallModel:
    """Подгонка L0 и потерь стены наименьшими квадратами при заданном показателе n; пики прохода у роутера (на расстоянии NEAR_REF_M, без стен) дают надбавку ближнего поля."""
    if len(dist_m) < MIN_CONTROLS:
        raise ValueError(f"нужно не меньше {MIN_CONTROLS} контрольных точек, дано {len(dist_m)}")
    target = levels_db + 10.0 * n * np.log10(dist_m)
    (l0, wall), *_ = np.linalg.lstsq(np.column_stack([np.ones_like(target), -wall_thickness_m]), target, rcond=None)
    model = WallModel(float(l0), n, float(wall))
    if len(peak_levels_db):
        gain = float(np.median(peak_levels_db) - model.predict(np.array([NEAR_REF_M]), np.zeros(1))[0])
        model = WallModel(model.l0_db, n, model.wall_db_per_m, max(gain, 0.0))
    return model


def expected_levels(model: WallModel, nodes_xy: np.ndarray, walls: list[dict], router: tuple[float, float], openings: list[dict] = ()) -> np.ndarray:
    """Ожидаемый уровень в каждой точке плана."""
    return model.predict(distance3d(nodes_xy, router), wall_thickness_on_path(walls, router, nodes_xy, openings))


def crosses_wall(walls: list[dict], a: tuple[float, float], b: tuple[float, float], openings: list[dict] = ()) -> bool:
    """Пересекает ли прямой отрезок a-b стену плана; проёмы дверей свободны, размытия тени нет (для проверки перемещения нужна точная геометрия)."""
    return bool(_hard_thickness(walls, a, np.array([b], dtype=float), list(openings))[0] > 0)
