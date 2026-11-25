"""Радар присутствия по Wi-Fi (SAM-105): кадры источника, состояние по людям, личность поверх облака точек.

Модуль чистый: время приходит параметром, сети и файлов нет (кроме load_layout). Источники (мок сейчас, оценщик CSI
на ESP32 потом) отдают кадр `Frame`: люди с облаком точек в метрах. Хранилище считает возраст каждого человека:
чем он больше, тем сильнее страница рассеивает точки, а не замораживает картинку.

Нагрузка небольшая: чистый Python, без numpy; на человека
POINTS_PER_PERSON точек; кадр один на тик SAM_PRESENCE_TICK_S, без источника цикл спит.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

POINTS_PER_PERSON = 220      # хватает на читаемый силуэт человека; больше - зря нагружает видеокарту и канал
STALE_S = 1.5                # кадр старше - точки начинают рассеиваться
LOST_S = 8.0                 # кадр старше - человек считается потерянным и из состояния уходит
BODY_HEIGHT_M = 1.75         # габарит мок-человека
PET_POINTS = 80              # кот: меньше точек, чем у человека, объём в десять раз меньше
PET_HEIGHT_M = 0.30          # габарит мок-кота
PET_LENGTH_M = 0.50
BODY_RADIUS_M = 0.22         # половина ширины плеч мок-человека
WALK_SPEED_MPS = 0.9         # скорость мок-человека между комнатами
DWELL_S = 6.0                # сколько мок-человек стоит в комнате
MOCK_DT_S = 0.1              # шаг, которым мок смотрит вперёд, чтобы узнать скорость
MOCK_FPS = 5                 # шум меняется с каждым кадром источника (тик 0,2 с)
RADAR_JITTER_M = 0.06        # разброс точек вокруг тела, как у настоящего радара: облако живое и нечёткое, не манекен
SMEAR_S = 0.35               # за это время движущееся тело размазывается позади себя
GAIT_HZ = 1.8                # шагов в секунду при ходьбе
STRIDE_M = 0.16              # размах ноги вперёд-назад
GOLDEN = math.pi * (3 - math.sqrt(5))   # шаг золотого угла: равномерная раскладка точек без случайности

HOUSEHOLD = "household"      # подтверждён известным голосом или устройством
GUEST = "guest"              # человек есть, подтверждения нет, но дома есть свои
UNKNOWN = "unknown"          # человек есть, своих дома нет и подтверждения нет: тревога
LAYOUT_ENV = "SAM_HOME_LAYOUT"
HEAT_CELL_M = 0.25           # клетка карты посещений: меньше шага человека, больше погрешности оценки положения
HEAT_WALKABLE_VISITS = 3.0   # клетка с такой суммой посещений считается проходимой зоной
INTERACT_RADIUS_M = 1.1     # человек ближе к предмету - считается, что он с ним взаимодействует

# Явно демонстрационная планировка: в реальном доме её заменяет файл SAM_HOME_LAYOUT
DEMO_LAYOUT = {
    "name": "demo",
    "rooms": [
        {"id": "demo-living", "title": "demo: гостиная", "x": 0.0, "z": 0.0, "w": 5.0, "d": 4.0},
        {"id": "demo-kitchen", "title": "demo: кухня", "x": 5.0, "z": 0.0, "w": 3.5, "d": 4.0},
        {"id": "demo-bedroom", "title": "demo: спальня", "x": 0.0, "z": 4.0, "w": 4.0, "d": 3.5},
        {"id": "demo-hall", "title": "demo: коридор", "x": 4.0, "z": 4.0, "w": 4.5, "d": 3.5},
    ],
    "objects": [
        {"id": "demo-sofa", "title": "demo: диван", "room": "demo-living", "x": 1.2, "z": 1.0, "w": 2.0, "d": 0.9, "h": 0.8},
        {"id": "demo-tv", "title": "demo: телевизор", "room": "demo-living", "x": 4.6, "z": 1.9, "w": 0.15, "d": 1.2, "h": 0.7},
        {"id": "demo-table", "title": "demo: стол", "room": "demo-kitchen", "x": 6.4, "z": 1.2, "w": 1.2, "d": 1.2, "h": 0.75},
        {"id": "demo-bed", "title": "demo: кровать", "room": "demo-bedroom", "x": 0.8, "z": 4.8, "w": 1.6, "d": 2.0, "h": 0.5},
        {"id": "demo-router", "title": "demo: роутер", "room": "demo-hall", "x": 5.6, "z": 5.4, "w": 0.3, "d": 0.3, "h": 1.4},
    ],
}


@dataclass(frozen=True)
class Person:
    pid: str
    room: str
    points: tuple[tuple[float, float, float], ...]   # x, y, z в метрах, y - вверх
    identity: str = UNKNOWN
    who: str | None = None
    kind: str = "person"                              # person или pet (radar/presence_track.py)


@dataclass(frozen=True)
class Frame:
    ts: float
    source: str
    people: tuple[Person, ...]


@dataclass
class PresenceStore:
    """Последний кадр по каждому человеку и его время; снимок считает возраст и убирает потерянных."""
    stale_s: float = STALE_S
    lost_s: float = LOST_S
    _people: dict[str, tuple[Person, float, str]] = field(default_factory=dict)

    def update(self, frame: Frame) -> None:
        for p in frame.people:
            self._people[p.pid] = (p, frame.ts, frame.source)

    def snapshot(self, now: float) -> dict:
        people = []
        for pid, (p, ts, source) in list(self._people.items()):
            age = max(0.0, now - ts)
            if age > self.lost_s:
                del self._people[pid]
                continue
            people.append({"id": pid, "kind": p.kind, "room": p.room, "identity": p.identity, "who": p.who, "age_s": round(age, 2),
                           "state": "live" if age <= self.stale_s else "stale", "source": source,
                           "points": [[round(c, 3) for c in pt] for pt in p.points]})
        return {"ts": now, "people": people}


def body_points(cx: float, cz: float, n: int = POINTS_PER_PERSON, height: float = BODY_HEIGHT_M,
                radius: float = BODY_RADIUS_M, rnd: random.Random | None = None,
                vel: tuple[float, float] = (0.0, 0.0), phase: float = 0.0) -> tuple[tuple[float, float, float], ...]:
    """Фигура человека: ноги, торс с плечами, голова; точки по золотому углу.

    Без rnd фигура чёткая и неподвижная (для проверок). С rnd получается облако как у радара: каждая точка
    дрожит вокруг тела на RADAR_JITTER_M, при движении часть точек остаётся позади (размазывание по SMEAR_S),
    ноги качаются по фазе шага. Нечёткость задумана: облако не обязано быть чётким."""
    pts = []
    speed = math.hypot(*vel)
    for i in range(n):
        t = (i + 0.5) / n                       # 0 - ступни, 1 - макушка
        if t > 0.86:                            # голова: сужается к макушке
            r = radius * 0.55 * math.sin(math.pi * (t - 0.86) / 0.14)
        elif t > 0.45:                          # торс и плечи
            r = radius * (1.0 - 0.15 * abs(t - 0.72) / 0.27)
        else:                                   # ноги
            r = radius * 0.55
        a = i * GOLDEN
        x, y, z = cx + r * math.cos(a), t * height, cz + r * math.sin(a)
        if rnd is not None:
            if speed > 0.05 and t < 0.45:       # шаг: ноги по очереди вперёд и назад вдоль движения
                swing = math.sin(phase + (i % 2) * math.pi) * STRIDE_M * (0.45 - t) / 0.45
                x, z = x + vel[0] / speed * swing, z + vel[1] / speed * swing
            lag = rnd.random() * SMEAR_S
            x, z = x - vel[0] * lag, z - vel[1] * lag
            x, y, z = (x + rnd.gauss(0, RADAR_JITTER_M), max(0.0, y + rnd.gauss(0, RADAR_JITTER_M)),
                       z + rnd.gauss(0, RADAR_JITTER_M))
        pts.append((x, y, z))
    return tuple(pts)


def room_center(layout: dict, room_id: str) -> tuple[float, float]:
    room = next(r for r in layout["rooms"] if r["id"] == room_id)
    return room["x"] + room["w"] / 2, room["z"] + room["d"] / 2


@dataclass
class Heatmap:
    """Карта посещений: клетка пола - сколько секунд в ней стояли люди. Треки только копятся (по ним видна возможная зона): проходимая зона и планировка проступают сами."""
    cell_m: float = HEAT_CELL_M
    counts: dict[tuple[int, int], float] = field(default_factory=dict)

    def cell_of(self, x: float, z: float) -> tuple[int, int]:
        return math.floor(x / self.cell_m), math.floor(z / self.cell_m)

    def add(self, x: float, z: float, seconds: float = 1.0, limit: float | None = None) -> None:
        """Секунды клетке; limit - предел накопления клетки (лежащий телефон не должен копить часы)."""
        key = self.cell_of(x, z)
        total = self.counts.get(key, 0.0) + seconds
        self.counts[key] = total if limit is None else min(limit, total)

    def decay(self, factor: float, floor: float = 1e-3) -> None:
        """Остывание: все клетки умножаются на factor, остывшее до floor выбрасывается."""
        self.counts = {k: v * factor for k, v in self.counts.items() if v * factor >= floor}

    def moved(self, place) -> "Heatmap":
        """Новая карта, где секунды каждой клетки перенесены в клетку точки place(x, z) от её центра: накопленное за стеной ложится на ближайший пол."""
        out = Heatmap(self.cell_m)
        for (i, j), v in self.counts.items():
            x, z = place((i + 0.5) * self.cell_m, (j + 0.5) * self.cell_m)
            out.add(x, z, v)
        return out

    def merge(self, other: "Heatmap") -> "Heatmap":
        """Новая карта: сумма этой и другой с той же клеткой (иначе другую не сложить)."""
        out = Heatmap(self.cell_m, dict(self.counts))
        if other.cell_m == self.cell_m:
            for k, v in other.counts.items():
                out.counts[k] = out.counts.get(k, 0.0) + v
        return out

    def cells(self, min_count: float = 0.0) -> list[list[float]]:
        """Разреженный список [i, j, секунды] для страницы; пустые клетки не передаются."""
        return [[i, j, round(v, 2)] for (i, j), v in self.counts.items() if v > min_count]

    def walkable(self, min_visits: float = HEAT_WALKABLE_VISITS) -> set[tuple[int, int]]:
        return {k for k, v in self.counts.items() if v >= min_visits}

    def to_json(self) -> str:
        return json.dumps({"cell_m": self.cell_m, "cells": self.cells()}, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "Heatmap":
        data = json.loads(text)
        return cls(data["cell_m"], {(int(i), int(j)): float(v) for i, j, v in data["cells"]})


def heat_from_track(track: list[dict], now: float, half_life_s: float, cell_m: float = HEAT_CELL_M) -> Heatmap:
    """Тепло по треку {t, x, z}: секунды, которые человек провёл в клетках пути, с весом по свежести (вдвое слабее за half_life_s).
    Стоянка на месте даёт секунды в одной клетке, ходьба размазывает время по пройденным клеткам. Чем чаще и свежее ходили, тем горячее."""
    heat = Heatmap(cell_m)
    for a, b in zip(track, track[1:]):
        dt = b["t"] - a["t"]
        if dt <= 0:
            continue
        weight = 0.5 ** (max(0.0, now - b["t"]) / half_life_s)
        steps = max(1, math.ceil(math.dist((a["x"], a["z"]), (b["x"], b["z"])) / (cell_m / 2)))
        for k in range(steps):
            f = (k + 0.5) / steps
            heat.add(a["x"] + (b["x"] - a["x"]) * f, a["z"] + (b["z"] - a["z"]) * f, dt / steps * weight)
    return heat


def centroid(person: Person) -> tuple[float, float]:
    """Центр человека по полу: среднее x и z его облака."""
    n = len(person.points)
    return sum(p[0] for p in person.points) / n, sum(p[2] for p in person.points) / n


def mock_position(layout: dict, t: float) -> tuple[str, float, float]:
    """Мок-человек ходит по кругу комнат: стоит DWELL_S, идёт к следующей со скоростью WALK_SPEED_MPS."""
    ids = [r["id"] for r in layout["rooms"]]
    centers = [room_center(layout, i) for i in ids]
    walks = [math.dist(centers[i], centers[(i + 1) % len(ids)]) / WALK_SPEED_MPS for i in range(len(ids))]
    phase = t % sum(DWELL_S + w for w in walks)
    for i, walk in enumerate(walks):
        if phase < DWELL_S:
            return ids[i], centers[i][0], centers[i][1]
        phase -= DWELL_S
        if phase < walk:
            f = phase / walk
            a, b = centers[i], centers[(i + 1) % len(ids)]
            return ids[i] if f < 0.5 else ids[(i + 1) % len(ids)], a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        phase -= walk
    return ids[0], centers[0][0], centers[0][1]


def cat_points(cx: float, cz: float, n: int = PET_POINTS, height: float = PET_HEIGHT_M,
               length: float = PET_LENGTH_M, rnd: random.Random | None = None) -> tuple[tuple[float, float, float], ...]:
    """Фигура кота: вытянутый по x эллипсоид; без rnd чёткая, с rnd дрожит как облако радара."""
    pts = []
    for i in range(n):
        t = (i + 0.5) / n
        y = t * height
        r = math.sin(math.pi * t)                       # от лап к спине сужается к верху
        a = i * GOLDEN
        x, z = cx + r * length / 2 * math.cos(a), cz + r * length / 4 * math.sin(a)
        if rnd is not None:
            x, y, z = (x + rnd.gauss(0, RADAR_JITTER_M / 2), max(0.0, y + rnd.gauss(0, RADAR_JITTER_M / 2)),
                       z + rnd.gauss(0, RADAR_JITTER_M / 2))
        pts.append((x, y, z))
    return tuple(pts)


def mock_frame(layout: dict, t: float, source: str = "mock", ts: float | None = None) -> Frame:
    """Кадр мока в момент сценария t: человек ходит по комнатам, кот держится рядом; ts - время кадра на часах."""
    room, x, z = mock_position(layout, t)
    _, x2, z2 = mock_position(layout, t + MOCK_DT_S)
    vel = ((x2 - x) / MOCK_DT_S, (z2 - z) / MOCK_DT_S)                         # скорость из положения чуть вперёд
    cat_x, cat_z = x + 0.8 * math.cos(t / 3), z + 0.8 * math.sin(t / 3)       # кот кружит вокруг хозяина
    rnd = random.Random(int(t * MOCK_FPS))                                     # один кадр - один набор шума, повторяемо
    return Frame(t if ts is None else ts, source, (
        Person("p1", room, body_points(x, z, rnd=rnd, vel=vel, phase=t * GAIT_HZ * 2 * math.pi), GUEST, None),
        Person("p2", room, cat_points(cat_x, cat_z, rnd=rnd), GUEST, None)))


def identify(people: Iterable[Person], people_home: Iterable[str], voices_recent: dict[str, str]) -> tuple[Person, ...]:
    """Личность поверх облака: голос, опознанный в комнате, - свой; иначе гость, если дома есть свои, иначе чужой.

    people_home - имена известных людей, чьи телефоны сейчас на роутере; voices_recent - комната -> имя по опознанию
    слуха за последние секунды. Wi-Fi сам личность не определяет, это связка с этими двумя источниками."""
    home = bool(list(people_home))
    out = []
    for p in people:
        if p.room in voices_recent:
            out.append(Person(p.pid, p.room, p.points, HOUSEHOLD, voices_recent[p.room]))
        else:
            out.append(Person(p.pid, p.room, p.points, GUEST if home else UNKNOWN, None))
    return tuple(out)


def interactions(people: Iterable[Person], layout: dict, radius: float = INTERACT_RADIUS_M) -> list[dict]:
    """Связи «человек - предмет»: ближайший предмет в радиусе от центра человека (по полу), по одному на человека."""
    links = []
    for p in people:
        if not p.points:
            continue
        cx, cz = centroid(p)
        best = None
        for o in layout.get("objects", []):
            # расстояние до прямоугольника предмета, а не до его угла: диван длиной 2 м ближе к краю
            dx = max(o["x"] - cx, 0.0, cx - (o["x"] + o["w"]))
            dz = max(o["z"] - cz, 0.0, cz - (o["z"] + o["d"]))
            dist = math.hypot(dx, dz)
            if dist <= radius and (best is None or dist < best[0]):
                best = (dist, o["id"])
        if best:
            links.append({"person": p.pid, "object": best[1], "dist_m": round(best[0], 2)})
    return links


ZONE_LABEL_MAX = 40          # подпись зоны: короткое имя «диван», «кухня», не описание


def validate_zone(data: dict, taken_ids: set[str]) -> dict:
    """Зона, которую человек подписал на карте: прямоугольник в метрах и имя; ValueError - причина одной строкой."""
    label = str(data.get("label") or "").strip()
    if not label or len(label) > ZONE_LABEL_MAX:
        raise ValueError(f"подпись зоны нужна, до {ZONE_LABEL_MAX} знаков: например диван, кровать, коридор, кухня")
    try:
        x, z, w, d = (float(data[k]) for k in ("x", "z", "w", "d"))
    except (KeyError, TypeError, ValueError):
        raise ValueError("зона задаётся числами x, z (угол) и w, d (размер) в метрах") from None
    if w <= 0 or d <= 0:
        raise ValueError("ширина и глубина зоны должны быть больше нуля")
    base = f"zone-{label.lower().replace(' ', '-')}"
    zone_id, n = base, 1
    while zone_id in taken_ids:
        n += 1
        zone_id = f"{base}-{n}"
    return {"id": zone_id, "label": label, "x": x, "z": z, "w": w, "d": d}


def zone_at(zones: Iterable[dict], x: float, z: float) -> str | None:
    """Подпись зоны, в которой стоит точка; из пересекающихся - меньшая по площади (диван внутри гостиной)."""
    inside = [zn for zn in zones if zn["x"] <= x <= zn["x"] + zn["w"] and zn["z"] <= z <= zn["z"] + zn["d"]]
    return min(inside, key=lambda zn: zn["w"] * zn["d"])["label"] if inside else None


# Пока плана квартиры нет, реальный источник получает пустой куб: ни выдуманных комнат, ни предметов
EMPTY_LAYOUT = {
    "name": "empty",
    "rooms": [{"id": "home", "title": "план квартиры не задан", "x": 0.0, "z": 0.0, "w": 8.0, "d": 8.0}],
    "objects": [],
}


def load_layout(path: str | None, fallback: dict = DEMO_LAYOUT) -> dict:
    """Планировка из JSON-файла; нет пути или файла - fallback: демо для мока, пустой куб для реального источника."""
    if not path or not Path(path).is_file():
        return fallback
    layout = json.loads(Path(path).read_text(encoding="utf-8"))
    if not layout.get("rooms"):
        raise ValueError(f"в планировке {path} нет списка rooms")
    return layout
