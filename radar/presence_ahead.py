"""Препятствие впереди идущего (SAM-105): человек, стена или предмет на пути не дают показывать шаговую позу.

Шаг запрещён в некоторых местах: у входов в комнаты, в ванной, или если впереди препятствие (человек, стена). Модуль чистый:
проверка свободного пути приходит функцией free(a, b), положения других людей - строками.
"""
from __future__ import annotations

import math

REACH_M = 0.6                       # на этом расстоянии впереди должно быть свободно, чтобы шагнуть
PERSON_RANGE_M = 1.0                # человек ближе этого в конусе взгляда мешает шагу
PERSON_CONE_RAD = math.radians(35)


def mark_blocked(rows: list[dict], free) -> list[dict]:
    """Идущим с препятствием впереди ставит blocked_ahead: страница в этом случае показывает обычную стойку, а не шаг."""
    out = []
    for row in rows:
        if not row.get("walking") or row.get("face") is None or row.get("x") is None:
            out.append(row)
            continue
        x, z, face = row["x"], row["z"], row["face"]
        ahead = (x + math.cos(face) * REACH_M, z + math.sin(face) * REACH_M)
        blocked = not free((x, z), ahead) or any(_person_in_front(row, other) for other in rows if other is not row)
        out.append({**row, "blocked_ahead": True} if blocked else row)
    return out


def _person_in_front(row: dict, other: dict) -> bool:
    if other.get("x") is None:
        return False
    dx, dz = other["x"] - row["x"], other["z"] - row["z"]
    dist = math.hypot(dx, dz)
    if dist == 0 or dist > PERSON_RANGE_M:
        return dist == 0
    bearing = math.atan2(dz, dx)
    return abs(math.atan2(math.sin(bearing - row["face"]), math.cos(bearing - row["face"]))) <= PERSON_CONE_RAD
