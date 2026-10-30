"""Мир потока положения сверху (SAM-118): один мир для телефона и человека, подошва низкой мебели это клетки пола.

Вид сверху: человек сидит на диване и лежит на кровати, телефон лежит на столе и комоде, и для потока это те же клетки пола, что и проход рядом, со своим
названием зоны и своими предпочтениями. Высокая мебель (шкаф, холодильник, этажерка) остаётся преградой: сверху в ней никого не бывает. Отдельного мира
телефона с «поверхностями» нет: какая мебель стоит в зоне, потоку неважно. Навигация человека для ходьбы на странице (`presence_nav.Nav`) не меняется:
там диван обходят. Модуль чистый: навигация приходит параметром."""
from __future__ import annotations

import numpy as np
from scipy import ndimage

from .presence_nav import AGENT_R_M, Nav, _in_rect

TOP_VIEW_MAX_H_M = 1.2         # мебель ниже этого сверху считается полом: столы 0,75, диваны 0,87, кровать 0,89, комод 0,9; шкафы 2,1 и холодильник 1,8 остаются преградой
TOP_VIEW_MAX_H_ENV = "SAM_PRESENCE_TOP_VIEW_MAX_H_M"


def is_low(obj: dict, max_h_m: float = TOP_VIEW_MAX_H_M) -> bool:
    return obj.get("kind") != "opening" and float(obj.get("h", 0.0)) < max_h_m


def place_zones(place_xy: dict[str, tuple[float, float]], layout: dict, cells_xy: np.ndarray, max_h_m: float = TOP_VIEW_MAX_H_M) -> dict[str, np.ndarray]:
    """Зона каждого места сверху как маска клеток мира: подошва низкого предмета, в который попала точка места (диван, кровать, стол), иначе комната, в которой она лежит;
    точка ни в том, ни в другом (коридор без комнаты) - зоны нет, место остаётся пятном. Предпочтение по зоне ложится на всю подошву, а не на точку и пол вокруг."""
    xs, zs = cells_xy[:, 0], cells_xy[:, 1]
    lows = [o for o in layout.get("objects") or [] if is_low(o, max_h_m)]
    rooms = layout.get("rooms") or []
    zones: dict[str, np.ndarray] = {}
    for place, (x, z) in place_xy.items():
        obj = next((o for o in lows if _in_rect(np.array([x]), np.array([z]), o["x"] + o["w"] / 2, o["z"] + o["d"] / 2, o["w"], o["d"], o.get("angle", 0.0))[0]), None)
        if obj is not None:
            zones[place] = _in_rect(xs, zs, obj["x"] + obj["w"] / 2, obj["z"] + obj["d"] / 2, obj["w"], obj["d"], obj.get("angle", 0.0))
            continue
        room = next((r for r in rooms if r["x"] <= x <= r["x"] + r["w"] and r["z"] <= z <= r["z"] + r["d"]), None)
        if room is not None:
            zones[place] = (xs >= room["x"]) & (xs <= room["x"] + room["w"]) & (zs >= room["z"]) & (zs <= room["z"] + room["d"])
    return zones


class TopViewNavigation:
    def __init__(self, nav: Nav, max_h_m: float = TOP_VIEW_MAX_H_M) -> None:
        self._nav = nav
        footprint = np.zeros_like(nav.passable)
        for o in nav.objects:
            if is_low(o, max_h_m):
                footprint |= _in_rect(nav._xs, nav._zs, o["x"] + o["w"] / 2, o["z"] + o["d"] / 2, o["w"], o["d"], o.get("angle", 0.0))
        self._footprint = footprint & ~nav.exterior & ~nav._wall
        rim = ndimage.binary_dilation(self._footprint, iterations=max(1, round(AGENT_R_M / nav.cell))) & ~nav.blocked      # кромка пола у мебели: у ходьбы тут отступ, сверху это проход к дивану
        self._passable = nav.passable | self._footprint | rim

    def stand_nodes(self, step_m: float) -> list[tuple[float, float]]:
        """Все узлы сетки с шагом step_m, где сверху может быть субъект: пол, подошвы низкой мебели и кромка к ним; не стена, не высокая мебель, не улица."""
        step = max(1, round(step_m / self._nav.cell))
        rows, cols = np.nonzero((self._passable & ~self._nav.exterior)[::step, ::step])
        return [self._nav._point(int(r) * step, int(c) * step) for r, c in zip(rows, cols)]

    def segment_free(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        """Отрезок целиком по проходимому сверху: через стену и высокую мебель нельзя."""
        (r0, c0), (r1, c1) = self._nav._cell(*a), self._nav._cell(*b)
        n = max(abs(r1 - r0), abs(c1 - c0), 1)
        return all(self._passable[round(r0 + (r1 - r0) * k / n), round(c0 + (c1 - c0) * k / n)] for k in range(n + 1))

    def snap(self, x: float, z: float) -> tuple[float, float]:
        """Точка на подошве низкой мебели остаётся на ней; остальное притягивается к полу, как у ходьбы."""
        r, c = self._nav._cell(x, z)
        return self._nav._point(r, c) if self._passable[r, c] and not self._nav.exterior[r, c] else self._nav.snap(x, z)
