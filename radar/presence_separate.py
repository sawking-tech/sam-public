"""Люди не накладываются друг на друга (SAM-105): пара ближе MIN_SEP_M раздвигается поровну вдоль линии между ними.

«у столов вокруг того, кто сидит». Одно отталкивание центров: двое стоящих сходятся ближе (STAND_SEP_M), сидящий или
лежащий остаётся на месте, остальные встают вокруг него. Порядок пар и направление при совпадении точек заданы именами,
результат не дрожит от кадра к кадру. Чистая функция: хранилища и времени нет.
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Callable

MIN_SEP_M = 0.55       # ближе этого не подходят к сидящему или лежащему: ширина плеч и немного воздуха
WALK_SEP_M = 0.8       # идущие держат дистанцию больше стоящих: на ходу рядом не толпятся
STAND_SEP_M = 0.35     # двое стоящих могут стоять совсем рядом
FIXED_POSES = ('sit', 'lie')   # сидящего и лежащего не сдвигают: вокруг него встают другие
FRONT_R_M = 1.0        # перед лицом сидящего, ближе этого, не стоят: он смотрит туда (монитор, комната)
FRONT_HALF_RAD = math.radians(50)      # половина угла зоны перед лицом
FRONT_EXIT_RAD = math.radians(65)      # из зоны выходят чуть за её границу: сбоку или к плечу, не на самую кромку
SEARCH_R_M = 1.0       # в каком радиусе от упёршейся цели искать свободный узел сетки
PASSES = 4             # кругов разведения: трое в одной точке расходятся за два-три круга


Reach = Callable[[tuple[float, float], tuple[float, float]], bool]      # можно ли дойти из первой точки во вторую по свободному месту
Nodes = Callable[[float, float, float], list[tuple[float, float]]]     # свободные узлы сетки в круге (x, z, радиус)


def _angle(a: float, b: float) -> float:
    return math.atan2(math.sin(a - b), math.cos(a - b))


def _front(sitter: dict, x: float, z: float) -> bool:
    """Точка в зоне перед лицом сидящего (взгляд известен): стоять там нельзя, лицо закрывается."""
    face = sitter.get("face")
    if sitter.get("pose") != "sit" or face is None or sitter.get("x") is None:
        return False
    dx, dz = x - sitter["x"], z - sitter["z"]
    dist = math.hypot(dx, dz)
    return 0.0 < dist <= FRONT_R_M and abs(_angle(math.atan2(dz, dx), face)) <= FRONT_HALF_RAD


def _leave_front(sitter: dict, target: tuple[float, float]) -> tuple[float, float]:
    """Точка выходит из зоны перед лицом вбок, в ту сторону, где она ближе к кромке: тот же радиус вокруг сидящего, поворот."""
    dx, dz = target[0] - sitter["x"], target[1] - sitter["z"]
    side = 1.0 if _angle(math.atan2(dz, dx), sitter["face"]) >= 0 else -1.0
    ang, radius = sitter["face"] + side * FRONT_EXIT_RAD, max(math.hypot(dx, dz), MIN_SEP_M)
    return (sitter["x"] + math.cos(ang) * radius, sitter["z"] + math.sin(ang) * radius)


def _clear_of_fronts(mover: dict, target: tuple[float, float], rows: dict) -> tuple[float, float]:
    for sitter in rows.values():
        if sitter is not mover and _front(sitter, *target):
            target = _leave_front(sitter, target)
    return target


def _place(row: dict, target: tuple[float, float], origin: tuple[float, float], need: float, rows: dict, free: Reach, nodes: Nodes, gap: float) -> tuple[float, float]:
    """Куда встать, если прямой толчок упёрся в препятствие: ближайший к цели свободный узел сетки не ближе need к соседу; нет такого - остаться."""
    src = (row["x"], row["z"])
    stuck = not free(src, src)                                               # стоит в непроходимом месте (у стула в зазоре стола): выбирается на свободное, а не остаётся
    reach = (lambda a, b: free(b, b)) if stuck else free
    sitters = [o for o in rows.values() if o is not row]
    if reach(src, target) and not any(_front(o, *target) for o in sitters):
        return target
    others = [(o["x"], o["z"]) for o in rows.values() if o is not row and o.get("x") is not None]
    fits = [n for n in nodes(target[0], target[1], SEARCH_R_M)
            if math.hypot(n[0] - origin[0], n[1] - origin[1]) >= need - 1e-6 and all(math.hypot(n[0] - o[0], n[1] - o[1]) >= gap for o in others) and not any(_front(o, *n) for o in sitters) and reach(src, n)]
    return min(fits, key=lambda n: (math.hypot(n[0] - target[0], n[1] - target[1]), n)) if fits else src


def _leave_in_front(sitter: dict, other: dict, rows: dict, free: Reach | None, nodes: Nodes | None, gap: float) -> bool:
    """other стоит перед лицом sitter: уходит вбок или за спину (идущих не трогаем: их путь строит ходьба). True - сдвинут."""
    if other.get("pose") in FIXED_POSES or other.get("walking") or not _front(sitter, other["x"], other["z"]):
        return False
    target = _clear_of_fronts(other, _leave_front(sitter, (other["x"], other["z"])), rows)
    if free and nodes:
        target = _place(other, target, (sitter["x"], sitter["z"]), MIN_SEP_M, rows, free, nodes, gap)
    if target == (other["x"], other["z"]):
        return False
    other["x"], other["z"] = target
    return True


def separate(rows: list[dict], min_sep: float = MIN_SEP_M, stand_sep: float = STAND_SEP_M, free: Reach | None = None, nodes: Nodes | None = None) -> list[dict]:
    out = {r["person"]: dict(r) for r in rows}
    ids = sorted(p for p, r in out.items() if r.get("x") is not None and r.get("z") is not None)
    for _ in range(PASSES):
        moved = False
        for a, b in combinations(ids, 2):
            ra, rb = out[a], out[b]
            dx, dz = rb["x"] - ra["x"], rb["z"] - ra["z"]
            d = math.hypot(dx, dz)
            fixed_a, fixed_b = ra.get("pose") in FIXED_POSES, rb.get("pose") in FIXED_POSES
            if ra.get("hug") == b or rb.get("hug") == a:
                continue                                                     # обнимаются: разводить их не нужно
            if _leave_in_front(ra, rb, out, free, nodes, stand_sep) or _leave_in_front(rb, ra, out, free, nodes, stand_sep):
                moved = True
                continue
            need = stand_sep if not (fixed_a or fixed_b) else min_sep
            if ra.get("walking") or rb.get("walking"):
                need = max(need, WALK_SEP_M)
            if d >= need:
                continue
            ux, uz = (dx / d, dz / d) if d > 1e-6 else (1.0, 0.0)           # совпали точно: первый по имени уходит влево
            push = need - d
            share_a = 0.0 if fixed_a and not fixed_b else (1.0 if fixed_b and not fixed_a else 0.5)    # двигается тот, кто не сидит и не лежит
            pos_a, pos_b = (ra["x"], ra["z"]), (rb["x"], rb["z"])
            new_a = (pos_a[0] - ux * push * share_a, pos_a[1] - uz * push * share_a)
            new_b = (pos_b[0] + ux * push * (1 - share_a), pos_b[1] + uz * push * (1 - share_a))
            new_a = _clear_of_fronts(ra, new_a, out) if share_a > 0 else new_a        # и толчок не уводит в зону перед чьим-то лицом
            new_b = _clear_of_fronts(rb, new_b, out) if share_a < 1 else new_b
            if free and nodes:                                               # толчок в стену или мебель не принимается: ближайший свободный узел сетки
                new_a = _place(ra, new_a, pos_b, need, out, free, nodes, stand_sep) if share_a > 0 else new_a
                new_b = _place(rb, new_b, pos_a, need, out, free, nodes, stand_sep) if share_a < 1 else new_b
                if 0 < share_a < 1 and new_a == pos_a:                       # зажатый не уступает: всё расстояние берёт тот, кто подошёл
                    new_b = _place(rb, (pos_b[0] + ux * push, pos_b[1] + uz * push), pos_a, need, out, free, nodes, stand_sep)
                elif 0 < share_a < 1 and new_b == pos_b:
                    new_a = _place(ra, (pos_a[0] - ux * push, pos_a[1] - uz * push), pos_b, need, out, free, nodes, stand_sep)
            ra["x"], ra["z"], rb["x"], rb["z"] = new_a[0], new_a[1], new_b[0], new_b[1]
            moved = True
        if not moved:
            break
    return [out[r["person"]] for r in rows]
