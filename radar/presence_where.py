"""«Где кто» словами (SAM-105): комната, подписанная зона и ближайшая мебель для каждого опознанного человека.

Свидетельства радара дают точку x, z; SAM и инструменты MCP читают место словами, а не числами. Модуль чистый:
планировка, зоны и свидетельства приходят аргументами, сети и часов здесь нет.
"""
from __future__ import annotations

import math

from .presence import zone_at
from .presence_messages import NEAR_M, objects_near

NEAR_LIMIT = 3             # сколько предметов рядом называть: дальше список перестаёт помогать


def room_at(rooms: list[dict], x: float, z: float) -> str | None:
    """Название комнаты, в чей прямоугольник попала точка; вне комнат (коридор, улица) - None."""
    inside = [r for r in rooms if r["x"] <= x <= r["x"] + r["w"] and r["z"] <= z <= r["z"] + r["d"]]
    return min(inside, key=lambda r: r["w"] * r["d"]).get("title") if inside else None


def where(identities: list[dict], layout: dict, zones: list[dict], person: str = "") -> list[dict]:
    """Место каждого человека; `person` - часть имени для отбора (пусто - все)."""
    wanted = person.strip().lower()
    rows = []
    for ident in identities:
        if wanted and wanted not in str(ident["person"]).lower():
            continue
        x, z = ident["x"], ident["z"]
        if x is None or z is None:                                   # положения нет (человек известен только по устройству): место пустое, строка остаётся
            rows.append({"person": ident["person"], "room": None, "zone": None, "near": [], "pose": ident.get("pose"), "confidence": ident.get("confidence"),
                         "sources": ident.get("sources", []), "x": None, "z": None})
            continue
        rows.append({
            "person": ident["person"],
            "room": room_at(layout.get("rooms") or [], x, z),
            "zone": zone_at(zones, x, z),
            "near": objects_near(x, z, layout.get("objects") or [], NEAR_M)[:NEAR_LIMIT],
            "pose": ident.get("pose"),
            "confidence": ident.get("confidence"),
            "sources": ident.get("sources", []),
            "x": x, "z": z,
        })
    return rows


FACING_RANGE_M = 2.0
FACING_CONE_RAD = math.radians(40)


def facing(row: dict, objects: list[dict]) -> str | None:
    """На что смотрит человек: ближайший предмет плана в конусе взгляда не дальше FACING_RANGE_M; нет взгляда или предмета - None."""
    if row.get("face") is None or row.get("x") is None:
        return None
    best = None
    for o in objects:
        if o.get("kind") == "opening" or not o.get("title"):
            continue
        dx, dz = o["x"] + o["w"] / 2 - row["x"], o["z"] + o["d"] / 2 - row["z"]
        dist = math.hypot(dx, dz)
        if dist == 0 or dist > FACING_RANGE_M:
            continue
        bearing = math.atan2(dz, dx)
        off = abs(math.atan2(math.sin(bearing - row["face"]), math.cos(bearing - row["face"])))
        if off <= FACING_CONE_RAD and (best is None or dist < best[0]):
            best = (dist, o["title"])
    return best[1] if best else None
