"""Карта покрытия сети (SAM-118): ожидаемый уровень роутера в каждой клетке плана по многостенной модели, полосы качества и лучшие места.

Данные для слоя страницы радара: сетка уровней и список названных мест плана с уровнем. Модель подогнана по стоянкам одного устройства,
поэтому карта описывает его на этом диапазоне, у других телефонов уровень другой. Модуль чистый: модель и план приходят параметрами."""
from __future__ import annotations

import math

import numpy as np

from .presence_radio_map import WallModel, doorway_openings, expected_levels

CELL_M = 0.25                # шаг сетки как у карты посещений: слои ложатся друг на друга
BANDS = (                    # нижняя граница полосы и её имя; пороги общепринятые: -67 дБм хватает на видео, -70 на устойчивые данные, ниже -80 связь рвётся
    (-60.0, "отлично"),
    (-67.0, "хорошо"),
    (-75.0, "средне"),
    (-82.0, "слабо"),
    (-math.inf, "почти нет"),
)
LEVEL_DECIMALS = 1


def band_of(dbm: float) -> str:
    return next(name for floor, name in BANDS if dbm >= floor)


def _bounds(walls: list[dict]) -> tuple[float, float, float, float]:
    xs = [v for w in walls for v in (w["x1"], w["x2"])]
    zs = [v for w in walls for v in (w["z1"], w["z2"])]
    return min(xs), max(xs), min(zs), max(zs)


def coverage_payload(model: WallModel, layout: dict, cell_m: float = CELL_M) -> dict:
    """Сетка уровней по строкам z, начало ячейки (i0, k0) в клетках, и места плана по убыванию уровня."""
    walls, router = layout["walls"], (layout["router"]["x"], layout["router"]["z"])
    doors = doorway_openings(layout.get("objects") or [])
    x0, x1, z0, z1 = _bounds(walls)
    i0, i1, k0, k1 = math.floor(x0 / cell_m), math.ceil(x1 / cell_m), math.floor(z0 / cell_m), math.ceil(z1 / cell_m)
    xs = (np.arange(i0, i1) + 0.5) * cell_m
    zs = (np.arange(k0, k1) + 0.5) * cell_m
    nodes = np.array([(x, z) for z in zs for x in xs])
    levels = expected_levels(model, nodes, walls, router, doors).reshape(len(zs), len(xs))
    places = []
    for p in layout.get("places") or []:
        dbm = float(expected_levels(model, np.array([[p["x"], p["z"]]]), walls, router, doors)[0])
        places.append({"label": p["label"], "x": p["x"], "z": p["z"], "dbm": round(dbm, LEVEL_DECIMALS), "band": band_of(dbm)})
    return {"cell_m": cell_m, "i0": i0, "k0": k0, "w": len(xs), "h": len(zs), "levels": np.round(levels, LEVEL_DECIMALS).ravel().tolist(),
            "bands": [{"min_db": None if math.isinf(f) else f, "name": n} for f, n in BANDS], "places": sorted(places, key=lambda p: -p["dbm"])}
