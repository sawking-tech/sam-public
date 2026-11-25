"""Вместимость комнат (SAM-105): у области есть предел людей, остальные ждут у входа.

Область и её максимум -
данные планировки (`areas`): комната по названию или прямоугольник. Одна механика: в области остаются те, кто пришёл
раньше, лишние ставятся у входа - снаружи области у ближайшей двери. Кто в пути, область не занимает.
Необязательные слоты области (`slots`: «вход», подпись зоны, слово из названия предмета): первый в комнате встаёт в первый слот, второй во второй.
"""
from __future__ import annotations

import math

DOOR_WORDS = ("дверь", "дверная")
ENTRANCE_OUT_M = 0.4         # ждущий у входа стоит на шаг снаружи области у её границы рядом с дверью: «уже у входа»
ENTRANCE_IN_M = 0.35         # слот «вход» внутри области: на шаг от двери внутрь
ENTRANCE_SLOT = "вход"       # имя слота у двери; остальные слоты - подписанные зоны или предметы плана по слову в названии


def _door_points(objects: list[dict]) -> list[tuple[float, float]]:
    return [(o["x"] + o["w"] / 2, o["z"] + o["d"] / 2) for o in objects
            if o.get("kind") == "opening" and any(w in (o.get("title") or "").lower() for w in DOOR_WORDS)]


def _entrance(rect: tuple[float, float, float, float], door: tuple[float, float], outward: bool = True) -> tuple[float, float]:
    """Точка у границы прямоугольника рядом с дверью: снаружи (ждать) или внутри (слот «вход»). У открытой двери центр модели может
    лежать внутри комнаты: тогда берётся ближайшая к нему сторона."""
    x, z, w, d = rect
    qx, qz = min(max(door[0], x), x + w), min(max(door[1], z), z + d)
    nx, nz = door[0] - qx, door[1] - qz
    if nx == 0 and nz == 0:                                   # дверь внутри или на границе: к ближайшей стороне
        edges = [(door[0] - x, (-1.0, 0.0)), (x + w - door[0], (1.0, 0.0)), (door[1] - z, (0.0, -1.0)), (z + d - door[1], (0.0, 1.0))]
        gap, (nx, nz) = min(edges, key=lambda e: e[0])
        qx, qz = door[0] + nx * gap, door[1] + nz * gap
    length = math.hypot(nx, nz)
    step = ENTRANCE_OUT_M if outward else -ENTRANCE_IN_M
    return qx + nx / length * step, qz + nz / length * step


class Areas:
    def __init__(self, areas: list[dict], rooms: list[dict], objects: list[dict]) -> None:
        doors, self.areas = _door_points(objects), []
        for a in areas:
            room = next((r for r in rooms if r.get("title") == a.get("room")), None) if a.get("room") else None
            rect = room or a
            x, z, w, d = (rect.get(k) for k in ("x", "z", "w", "d"))
            if None in (x, z, w, d):
                continue
            cx, cz = x + w / 2, z + d / 2
            door = min(doors, key=lambda p: math.dist(p, (cx, cz)), default=(cx, cz))
            entrance, inner = _entrance((x, z, w, d), door), _entrance((x, z, w, d), door, outward=False)
            self.areas.append({"name": a.get("label") or a.get("room"), "rect": (x, z, w, d), "max": int(a["max"]), "entrance": entrance, "inner": inner, "slots": list(a.get("slots") or [])})
        self._since: dict[tuple[str, str], float] = {}      # (область, человек) -> когда вошёл: кто раньше, тот остаётся

    def apply(self, rows: list[dict], now: float, zones: list[dict] | None = None, objects: list[dict] | None = None) -> list[dict]:
        out = [dict(r) for r in rows]
        for area in self.areas:
            x, z, w, d = area["rect"]
            inside = [r for r in out if r.get("x") is not None and not r.get("walking") and x <= r["x"] <= x + w and z <= r["z"] <= z + d]
            ids = {r["person"] for r in inside}
            for key in [k for k in self._since if k[0] == area["name"] and k[1] not in ids]:
                del self._since[key]                              # вышел: при следующем входе очередь занимает заново
            for r in inside:
                self._since.setdefault((area["name"], r["person"]), now)
            inside.sort(key=lambda r: (self._since[(area["name"], r["person"])], str(r["person"])))
            for r, slot in zip(inside[:area["max"]], area["slots"]):
                point = self._slot_point(slot, area, zones or [], objects or [])
                if point:
                    r["x"], r["z"], r["pose"] = point[0], point[1], "stand"
            for r in inside[area["max"]:]:
                r["x"], r["z"], r["pose"] = area["entrance"][0], area["entrance"][1], "stand"
        return out

    @staticmethod
    def _slot_point(slot: str, area: dict, zones: list[dict], objects: list[dict]) -> tuple[float, float] | None:
        """Где встать на слоте: «вход» - у двери внутри области, иначе центр зоны с такой подписью, иначе центр предмета со словом в названии."""
        name = slot.lower()
        if name == ENTRANCE_SLOT:
            return area["inner"]
        zone = next((zn for zn in zones if str(zn.get("label", "")).lower() == name), None)
        if zone:
            return zone["x"] + zone["w"] / 2, zone["z"] + zone["d"] / 2
        obj = next((o for o in objects if o.get("kind") != "opening" and name in (o.get("title") or "").lower()), None)
        return (obj["x"] + obj["w"] / 2, obj["z"] + obj["d"] / 2) if obj else None
