"""Набор для обучения модели положения по уровню Wi-Fi (SAM-118): окна сырого уровня двух телефонов с меткой «где был человек» по отметкам проходки.

Три файла на выходе:
- stops: окна стоянок; конец стоянки - следующая отметка, начало уточнено назад по ровному ряду (человек пишет «пришёл» уже стоя). Метка точная: точка плана.
- transitions: окна медленных переходов между стоянками; метка - положение на кратчайшем пути по полу при ровной скорости. Если между стоянками был
  непомеченный заход (две отметки «пошёл» подряд), метка ненадёжна и помечается uncertain.
- fast: окна быстрых проходок по готовой нарезке (переход, промежуточные точки, начало и конец).

Четвёртый файл ticks - непрерывная шкала по TICK_S (около 5 Гц) для последовательной модели: на тике последний кадр каждого телефона
и метки: область (место стоянки или область точки на пути: ключевое место, участок коридора, комната - таблица классов в data/) и сколько секунд
человек уже стоит (null - идёт). Три состояния «идёт / только что остановился / стоит давно» выводятся из этого при обучении с любым порогом.

Окно - window_s секунд на телефон: медиана и разброс уровня по свежим кадрам (кадр моложе FRESH_MS), доля свежих, разница двух антенн, уровень подтверждений,
скорости передачи и приёма. Имена устройств и мест берутся из таблицы точек в data/, в набор телефоны попадают как phone-a/phone-b, места - как в таблице.

Запуск: python scripts/radar_nn_dataset.py --places data/walks/nn/places-2026-10-11.json --marks data/walks/walk-2026-10-11.jsonl \\
        --raw data/presence_tracks/raw-20261011.jsonl --walks data/walks/nn/walks-2026-10-11.json --classes data/walks/nn/classes-2026-10-11.json \\
        --out data/walks/nn --tag 2026-10-11 [--window-s 1]"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

FRESH_MS = 1000                 # кадр старше - уровень устаревший (телефон молчит), в медиану не идёт
ARRIVE_MARK = "пришёл:"
LEAVE_MARK = "пошёл"
END_MARK = "конец"              # отметка «...; конец проходки»: после неё стоянка длится до последнего отсчёта, но не дольше LAST_STOP_S
LAST_STOP_S = 10.0
ARRIVE_BACK_MAX_S = 12.0        # насколько раньше отметки «пришёл» может начаться стоянка: отметки чата отстают на 5-10 с
CORE_S = 10.0                   # ядро стоянки после отметки «пришёл»: человек точно стоит, по нему считаются центр и полоса уровня
MIN_BAND_DB = 3.0               # полоса ровного ряда не уже этого: у тихого телефона разброс на стоянке 1,6-3 дБ
BAND_MADS = 3.0                 # полоса - столько робастных разбросов (MAD * 1,48) телефона в ядре: у шумного телефона (7 дБ) полоса шире
MAD_TO_SD = 1.48
MAX_USEFUL_BAND_DB = 6.0        # телефон с полосой шире на стоянке границ не различает (в полосу попадает и ходьба): границу ищут только тихие
LEAVE_BACK_MAX_S = 12.0         # уход ищется не раньше стольких секунд до отметки «пошёл»: отметки чата отстают на 5-10 с
LEAVE_OUT_S = 2                 # столько секунд подряд вне полосы - человек ушёл; одиночный выброс стоянку не рвёт
LEAVE_GUARD_S = 1.0             # конец стоянки без уточнения - на секунду раньше отметки «пошёл»
MIN_STOP_S = 2.0                # стоянка короче - в набор не идёт
CLOCK = "%H:%M:%S"
NO_STEP = -9999
TICK_S = 0.2                    # шаг шкалы последовательной модели: роутер отдаёт около пяти кругов в секунду
OUTSIDE = "вне плана"
DOORWAY_REACH_M = 0.5           # точка пути в проёме между прямоугольниками комнат отходит ближайшей комнате не дальше этого


@dataclass(frozen=True)
class Place:
    name: str
    x: float
    z: float
    sd_m: float


@dataclass
class Stop:
    place: Place
    t_mark: float
    t_start: float
    t_end: float
    fast: bool
    t_next: float | None = None                         # время следующей отметки: от неё считается сдвиг найденного конца


def clock_ts(day: str, clock: str) -> float:
    return datetime.strptime(f"{day} {clock}", f"%Y-%m-%d {CLOCK}").timestamp()


def match_place(text: str, places: Sequence[Place]) -> Place | None:
    """Первая точка таблицы, чьё имя - подстрока текста отметки после «пришёл:»."""
    body = text.split(ARRIVE_MARK, 1)[1] if ARRIVE_MARK in text else text
    return next((p for p in places if p.name.lower() in body.lower()), None)


def build_stops(marks: Sequence[dict], places: Sequence[Place], fast_from: float, last_t: float) -> list[Stop]:
    """Стоянки по отметкам: от «пришёл» до следующей отметки; точки нет в таблице - стоянка пропускается."""
    out = []
    for i, m in enumerate(marks):
        if not m["text"].startswith(ARRIVE_MARK):
            continue
        place = match_place(m["text"], places)
        if place is None:
            continue
        end = marks[i + 1]["t"] - LEAVE_GUARD_S if i + 1 < len(marks) else min(last_t, m["t"] + LAST_STOP_S)
        out.append(Stop(place, m["t"], m["t"], end, m["t"] >= fast_from, marks[i + 1]["t"] if i + 1 < len(marks) else None))
    return out


def _bands(stop: Stop, series: dict[str, dict[int, float]]) -> dict[str, tuple[float, float]]:
    """Центр и полуширина полосы ровного ряда каждого телефона по ядру стоянки; телефон без трёх секунд в ядре не участвует."""
    out = {}
    for phone, levels in series.items():
        core = [v for s, v in levels.items() if stop.t_mark <= s <= min(stop.t_end, stop.t_mark + CORE_S)]
        if len(core) >= 3:
            centre = statistics.median(core)
            mad = statistics.median([abs(v - centre) for v in core])
            out[phone] = (centre, max(MIN_BAND_DB, BAND_MADS * MAD_TO_SD * mad))
    useful = {p: b for p, b in out.items() if b[1] <= MAX_USEFUL_BAND_DB}
    return useful or out


def _flat(second: int, series: dict[str, dict[int, float]], bands: dict[str, tuple[float, float]]) -> bool | None:
    """Секунда в полосе у всех телефонов, у которых есть отсчёт; отсчётов нет ни у кого - None."""
    seen = [abs(series[p][second] - c) <= w for p, (c, w) in bands.items() if second in series[p]]
    return all(seen) if seen else None


def refine_bounds(stop: Stop, series: dict[str, dict[int, float]], floor_t: float) -> tuple[float, float]:
    """Границы стоянки по ровному ряду: начало назад от «пришёл», пока ряд в полосе (не дальше ARRIVE_BACK_MAX_S и не раньше floor_t);
    конец - первая из LEAVE_OUT_S секунд подряд вне полосы, иначе прежний конец. Полосы нет (мало отсчётов) - границы как были."""
    bands = _bands(stop, series)
    if not bands:
        return stop.t_start, stop.t_end
    start, s = stop.t_mark, int(stop.t_mark) - 1
    while s >= max(floor_t, stop.t_mark - ARRIVE_BACK_MAX_S) and _flat(s, series, bands):
        start, s = float(s), s - 1
    end, out = stop.t_end, 0
    first = int(max(stop.t_mark, (stop.t_next or stop.t_end) - LEAVE_BACK_MAX_S)) + 1
    for s in range(first, int(stop.t_end) + 1):
        flat = _flat(s, series, bands)
        out = out + 1 if flat is False else 0
        if out >= LEAVE_OUT_S:
            end = float(s - LEAVE_OUT_S + 1)
            break
    return start, max(end, start)


def second_levels(rows: Iterable[dict], phone_part: str) -> dict[int, float]:
    """Медиана свежих кадров телефона по целым секундам: ряд для уточнения концов стоянок."""
    bins: dict[int, list[float]] = {}
    for r in rows:
        if phone_part in (r.get("name") or "") and r.get("in", 0) < FRESH_MS:
            bins.setdefault(int(r["t"]), []).append(r["db"])
    return {s: statistics.median(v) for s, v in bins.items()}


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def window_features(rows: Sequence[dict]) -> dict:
    """Признаки окна одного телефона: уровень по свежим кадрам и признаки линка; диапазон - интерфейс большинства свежих кадров."""
    fresh = [r for r in rows if r.get("in", 0) < FRESH_MS]
    db = [r["db"] for r in fresh]
    links = [r["ln"] for r in fresh if r.get("ln")]
    pair = lambda key, i: [ln[key][i] for ln in links if ln.get(key) and len(ln[key]) > i and ln[key][i] is not None]
    ants = [a - b for a, b in zip(pair("avgch", 0), pair("avgch", 1))]
    iface = statistics.mode([r["if"] for r in fresh]) if fresh else None
    return {"iface": iface, "n": len(rows), "fresh_share": round(len(fresh) / len(rows), 3) if rows else 0.0,
            "db_median": _median(db), "db_sd": round(statistics.pstdev(db), 2) if len(db) > 1 else None,
            "db_min": min(db) if db else None, "db_max": max(db) if db else None, "ant_diff": _median(ants),
            "ack0": _median(pair("ack", 0)), "ack1": _median(pair("ack", 1)), "tx_rate": _median(pair("tx", 0)), "rx_rate": _median(pair("rx", 0))}


def windows(rows: Sequence[dict], phones: dict[str, str], t0: float, t1: float, window_s: float) -> Iterable[tuple[float, str, dict]]:
    """Окна [t, t + window_s) на отрезке [t0, t1) по каждому телефону: (начало окна, псевдоним телефона, признаки); телефон без кадров в окне пропускается."""
    t = t0
    while t + window_s <= t1 + 1e-9:
        inside = [r for r in rows if t <= r["t"] < t + window_s]
        for part, alias in phones.items():
            mine = [r for r in inside if part in (r.get("name") or "")]
            if mine:
                yield t, alias, window_features(mine)
        t += window_s


def path_length(points: Sequence[tuple[float, float]]) -> list[float]:
    """Накопленная длина ломаной от первой точки."""
    acc = [0.0]
    for a, b in zip(points, points[1:]):
        acc.append(acc[-1] + math.dist(a, b))
    return acc


def point_at(points: Sequence[tuple[float, float]], share: float) -> tuple[float, float]:
    """Точка на ломаной на доле share её длины (0 - начало, 1 - конец)."""
    acc = path_length(points)
    if acc[-1] == 0:
        return points[0]
    target = min(max(share, 0.0), 1.0) * acc[-1]
    for i in range(1, len(points)):
        if acc[i] >= target:
            k = (target - acc[i - 1]) / (acc[i] - acc[i - 1]) if acc[i] > acc[i - 1] else 0.0
            return (points[i - 1][0] + k * (points[i][0] - points[i - 1][0]), points[i - 1][1] + k * (points[i][1] - points[i - 1][1]))
    return points[-1]


def room_at(rooms: Sequence[dict], x: float, z: float) -> str | None:
    return next((r["title"] for r in rooms if r["x"] <= x <= r["x"] + r["w"] and r["z"] <= z <= r["z"] + r["d"]), None)


def area_at(classes: dict, rooms: Sequence[dict], x: float, z: float) -> str:
    """Область точки: ключевое место, иначе участок коридора (круги из таблицы классов), иначе комната плана."""
    for key in ("spots", "corridor"):
        hit = next((a["name"] for a in classes.get(key) or [] if math.dist((a["x"], a["z"]), (x, z)) <= a["r"]), None)
        if hit:
            return hit
    return room_at(rooms, x, z) or nearest_room(rooms, x, z) or OUTSIDE


def nearest_room(rooms: Sequence[dict], x: float, z: float, reach_m: float = DOORWAY_REACH_M) -> str | None:
    """Комната, ближайшая к точке вне прямоугольников (дверной проём, толщина стены), если не дальше reach_m."""
    gap = lambda r: math.hypot(max(r["x"] - x, 0.0, x - r["x"] - r["w"]), max(r["z"] - z, 0.0, z - r["z"] - r["d"]))
    best = min(rooms, key=gap, default=None)
    return best["title"] if best is not None and gap(best) <= reach_m else None


def area_room(classes: dict, rooms: Sequence[dict], area: str) -> str:
    """Комната области для метрики: из таблицы классов, иначе сама область, если это комната плана."""
    return (classes.get("class_room") or {}).get(area) or (area if any(r["title"] == area for r in rooms) else OUTSIDE)


@dataclass(frozen=True)
class Segment:
    """Отрезок шкалы с меткой: стоянка (точка известна) или ход по маршруту."""
    group: str
    t0: float
    t1: float
    stop_area: str | None = None                       # область стоянки; None - ход
    path: tuple = ()
    quality: str = "stop"
    zone_areas: tuple = ()                              # зона: набор её областей, верна любая; состояние в зоне неизвестно


def tick_label(seg: Segment, t: float, classes: dict, rooms: Sequence[dict]) -> dict:
    """Метки тика: область и её набор (у зоны - все области зоны), комната и набор комнат, секунд стоянки (None - идёт), известно ли состояние, группа."""
    if seg.zone_areas:
        areas = list(seg.zone_areas)
        return {"group": seg.group, "area": areas[0], "room": area_room(classes, rooms, areas[0]), "area_set": areas,
                "room_set": sorted({area_room(classes, rooms, a) for a in areas}), "standing_s": None, "state_known": False, "label_quality": "zone"}
    if seg.stop_area is not None:
        area, since = seg.stop_area, round(t - seg.t0, 2)
    else:
        x, z = point_at(seg.path, (t - seg.t0) / (seg.t1 - seg.t0) if seg.t1 > seg.t0 else 1.0)
        area, since = area_at(classes, rooms, x, z), None
    room = area_room(classes, rooms, area)
    return {"group": seg.group, "area": area, "room": room, "area_set": [area], "room_set": [room], "standing_s": since, "state_known": True, "label_quality": seg.quality}


def phone_state(rows: Sequence[dict]) -> dict | None:
    """Последний кадр телефона в окне тика: уровень, свежесть и признаки линка; кадра нет - None."""
    if not rows:
        return None
    r = rows[-1]
    ln = r.get("ln") or {}

    def two(key, i):
        values = ln.get(key) or []
        return values[i] if len(values) > i else None
    a0, a1 = two("avgch", 0), two("avgch", 1)
    return {"db": r["db"], "fresh": r.get("in", 0) < FRESH_MS, "age_ms": r.get("in", 0), "if": r["if"],
            "ant_diff": a0 - a1 if a0 is not None and a1 is not None else None, "ack0": two("ack", 0), "ack1": two("ack", 1),
            "tx_rate": two("tx", 0), "rx_rate": two("rx", 0)}


def ticks(rows: Sequence[dict], phones: dict, segments: Sequence[Segment], classes: dict, rooms: Sequence[dict], tick_s: float = TICK_S) -> list[dict]:
    """Шкала по tick_s от начала первого отрезка до конца последнего: последний кадр каждого телефона за тик и метки отрезка, накрывающего тик (нет такого - null)."""
    if not segments:
        return []
    ordered = sorted(rows, key=lambda r: r["t"])
    t, end, i, out, recent = min(s.t0 for s in segments), max(s.t1 for s in segments), 0, [], []
    while t <= end + 1e-9:
        while i < len(ordered) and ordered[i]["t"] <= t:
            recent.append(ordered[i])
            i += 1
        recent = [r for r in recent if r["t"] > t - tick_s]
        seg = next((s for s in segments if s.t0 <= t < s.t1), None)
        row = {"t": round(t, 2), **{alias: phone_state([r for r in recent if part in (r.get("name") or "")]) for part, alias in phones.items()}}
        row.update(tick_label(seg, t, classes, rooms) if seg else {"group": None, "area": None, "room": None, "area_set": None, "room_set": None,
                                                                     "standing_s": None, "state_known": False, "label_quality": None})
        out.append(row)
        t += tick_s
    return out


def label_row(t: float, alias: str, feats: dict, x: float, z: float, sd_m: float, quality: str, rooms: Sequence[dict], **extra) -> dict:
    return {"t": round(t, 2), "phone": alias, **feats, "x": round(x, 2), "z": round(z, 2), "sd_m": round(sd_m, 2), "label_quality": quality, **extra}


Router = Callable[[Sequence[tuple[float, float]]], list[tuple[float, float]]]


def moving_rows(rows, phones, path, t0, t1, window_s, rooms, classes, quality, **extra) -> list[dict]:
    """Окна перехода: положение на маршруте по полу при ровной скорости и область этой точки; разброс - четверть длины перехода, не меньше 0,5 м."""
    sd = max(0.5, path_length(path)[-1] / 4)
    out = []
    for t, alias, feats in windows(rows, phones, t0, t1, window_s):
        x, z = point_at(path, ((t + window_s / 2) - t0) / (t1 - t0) if t1 > t0 else 1.0)
        area = area_at(classes, rooms, x, z)
        out.append(label_row(t, alias, feats, x, z, sd, quality, rooms, area=area, room=area_room(classes, rooms, area), standing_s=None, **extra))
    return out


def stop_area(classes: dict, place: Place) -> str:
    return (classes.get("place_class") or {}).get(place.name, place.name)


def stop_rows(rows, phones, stops: Sequence[Stop], window_s, rooms, classes) -> list[dict]:
    out = []
    for i, s in enumerate(stops):
        if s.t_end - s.t_start < MIN_STOP_S:
            continue
        area = stop_area(classes, s.place)
        for t, alias, feats in windows(rows, phones, s.t_start, s.t_end, window_s):
            out.append(label_row(t, alias, feats, s.place.x, s.place.z, s.place.sd_m, "stop", rooms, stop=i, place=s.place.name, fast=s.fast,
                                 area=area, room=area_room(classes, rooms, area), standing_s=round(t - s.t_start, 2)))
    return out


def slow_legs(stops: Sequence[Stop], marks: Sequence[dict], route: Router) -> list[tuple]:
    """Переходы между соседними медленными стоянками: (номер, откуда, куда, маршрут, качество); две и больше отметки «пошёл» между ними - непомеченный заход, uncertain."""
    out = []
    for i, (a, b) in enumerate(zip(stops, stops[1:])):
        if a.fast or b.fast or b.t_start <= a.t_end:
            continue
        leaves = [m for m in marks if a.t_mark < m["t"] < b.t_mark and m["text"].startswith(LEAVE_MARK)]
        out.append((i, a, b, route([(a.place.x, a.place.z), (b.place.x, b.place.z)]), "transition" if len(leaves) <= 1 else "transition_uncertain"))
    return out


def fast_legs(segments: Sequence[dict], route: Router) -> list[tuple]:
    """Быстрые проходки по нарезке: маршрут через начало, промежуточные точки и конец из её словаря points."""
    return [(i, seg, route([tuple(seg["points"][n]) for n in [seg["from"], *seg.get("via", []), seg["to"]]])) for i, seg in enumerate(segments)]


def world_router(layout: dict) -> Router:
    """Маршрут по клеткам мира потока (кратчайший путь по полу) через все точки по очереди."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from radar import presence_topview as ptv
    from radar import presence_world as pw
    from radar.presence_flow_live import Nav
    world = pw.World(ptv.TopViewNavigation(Nav(layout), ptv.TOP_VIEW_MAX_H_M), pw.WORLD_STEP_M)

    def leg(a, b) -> list[tuple[float, float]]:
        target, cell = world.cell_at(*b), world.cell_at(*a)
        _, pred = world.path_tree(target)
        cells = [cell]
        while pred[cells[-1]] != NO_STEP and len(cells) < len(world):
            cells.append(int(pred[cells[-1]]))
        return [a, *[tuple(map(float, world.points[c])) for c in cells], b]

    def route(points):
        out = [points[0]]
        for a, b in zip(points, points[1:]):
            out += leg(a, b)[1:]
        return out
    return route


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def zone_segments(walk: dict, day: str, zone_areas: dict, prefix: str) -> list[Segment]:
    """Зоны проходки: отрезок «дальше зона ... стоп» с отрезанным переходом в начале (entry_cut_s), если «начали» не было."""
    cut = float(walk.get("entry_cut_s", 0.0))
    out = []
    for i, z in enumerate(walk.get("zones") or []):
        t0 = clock_ts(day, z["from"]) + (0.0 if z.get("started") else cut)
        t1 = clock_ts(day, z["to"])
        if t1 - t0 >= MIN_STOP_S:
            out.append(Segment(f"{prefix}zone:{i}", t0, t1, None, (), "zone", tuple(zone_areas[z["zone"]])))
    return out


def build_walk(walk: dict, cfg: dict, day: str, raw_all, marks_all, places, phones, route, rooms, classes, window_s: float) -> dict:
    """Набор одной проходки: стоянки, переходы, быстрая часть, зоны и шкала тиков; группы с приставкой номера проходки."""
    w0, w1 = clock_ts(day, walk["from"]), clock_ts(day, walk["to"])
    prefix = f"{walk['id']}:"
    skip = {clock_ts(day, s) for s in cfg.get("skip_marks") or []}
    marks = [m for m in marks_all if w0 <= m["t"] <= w1 and int(m["t"]) not in {int(s) for s in skip}]
    raw = [r for r in raw_all if w0 <= r["t"] <= w1]
    if not raw:
        return {}
    stops, slow, fast, zones = [], [], [], []
    if walk["kind"] == "stops":
        fast_from = clock_ts(day, walk["fast_from"]) if walk.get("fast_from") else math.inf
        stops = build_stops(marks, places, fast_from, raw[-1]["t"])
        series = {p: second_levels(raw, p) for p in phones}
        for prev, s in zip([None, *stops], stops):
            s.t_start, s.t_end = refine_bounds(s, series, prev.t_end if prev else w0)
        slow = slow_legs(stops, marks, route)
        if walk.get("fast"):
            fast = fast_legs(json.loads(Path(walk["fast"]).read_text(encoding="utf-8"))["segments"], route)
    else:
        zones = zone_segments(walk, day, cfg["zone_areas"], prefix)
    tag = {"walk": walk["id"]}
    parts = {"stops": [{**r, **tag} for r in stop_rows(raw, phones, stops, window_s, rooms, classes)],
             "transitions": [{**r, **tag} for i, a, b, path, q in slow for r in moving_rows(raw, phones, path, a.t_end, b.t_start, window_s, rooms, classes, q,
                                                                                         leg=i, leg_from=a.place.name, leg_to=b.place.name)],
             "fast": [{**r, **tag} for i, seg, path in fast for r in moving_rows(raw, phones, path, seg["t_start"], seg["t_end"], window_s, rooms, classes, "fast",
                                                                                leg=i, leg_from=seg["from"], leg_to=seg["to"])],
             "zones": [{"t": round(z.t0, 2), "t_end": round(z.t1, 2), "group": z.group, "areas": list(z.zone_areas), **tag} for z in zones]}
    segments = ([Segment(f"{prefix}stop:{i}", s.t_start, s.t_end, stop_area(classes, s.place)) for i, s in enumerate(stops) if s.t_end - s.t_start >= MIN_STOP_S]
                + [Segment(f"{prefix}leg:{i}", a.t_end, b.t_start, None, tuple(path), q) for i, a, b, path, q in slow]
                + [Segment(f"{prefix}fast:{i}", seg["t_start"], seg["t_end"], None, tuple(path), "fast") for i, seg, path in fast] + zones)
    parts["ticks"] = [{**r, **tag} for r in ticks(raw, phones, segments, classes, rooms)]
    detail = [{"walk": walk["id"], "place": s.place.name, "mark": datetime.fromtimestamp(s.t_mark).strftime(CLOCK), "t_start": s.t_start, "t_end": s.t_end,
               "start": datetime.fromtimestamp(s.t_start).strftime(CLOCK), "end": datetime.fromtimestamp(s.t_end).strftime(CLOCK),
               "arrive_lag_s": round(s.t_mark - s.t_start, 1), "leave_lag_s": round(s.t_next - s.t_end, 1) if s.t_next else None,
               "seconds": round(s.t_end - s.t_start, 1), "x": s.place.x, "z": s.place.z, "fast": s.fast} for s in stops]
    meta = ([{"group": f"{prefix}stop:{i}", "walk": walk["id"], "kind": "stop", "place": s.place.name, "t0": s.t_start, "t1": s.t_end}
             for i, s in enumerate(stops) if s.t_end - s.t_start >= MIN_STOP_S]
            + [{"group": f"{prefix}leg:{i}", "walk": walk["id"], "kind": "leg", "from": a.place.name, "to": b.place.name, "quality": q, "t0": a.t_end, "t1": b.t_start}
               for i, a, b, path, q in slow]
            + [{"group": f"{prefix}fast:{i}", "walk": walk["id"], "kind": "leg", "from": seg["from"], "to": seg["to"], "quality": "fast", "t0": seg["t_start"], "t1": seg["t_end"]}
               for i, seg, path in fast]
            + [{"group": z.group, "walk": walk["id"], "kind": "zone", "areas": list(z.zone_areas), "t0": z.t0, "t1": z.t1} for z in zones])
    return {"parts": parts, "detail": detail, "segments": meta}


def main() -> None:
    ap = argparse.ArgumentParser(description="набор окон уровня с метками положения по проходкам дня")
    ap.add_argument("--places", type=Path, required=True)
    ap.add_argument("--marks", type=Path, required=True)
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--walks", type=Path, required=True, help="описание проходок дня: интервалы, быстрая часть, зоны (data/walks/nn/walks-<дата>.json)")
    ap.add_argument("--layout", type=Path, default=Path("data/home_layout.json"))
    ap.add_argument("--classes", type=Path, required=True, help="таблица классов: области мест, ключевые места, участки коридора")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--tag", required=True, help="дата проходок ГГГГ-ММ-ДД: часть имён файлов и день для времени отметок")
    ap.add_argument("--window-s", type=float, default=1.0)
    args = ap.parse_args()

    table = json.loads(args.places.read_text(encoding="utf-8"))
    places = [Place(p["match"], p["x"], p["z"], p["sd_m"]) for p in table["places"]]
    phones = table["phones"]
    cfg = json.loads(args.walks.read_text(encoding="utf-8"))
    marks = read_jsonl(args.marks)
    layout = json.loads(args.layout.read_text(encoding="utf-8"))
    raw = [r for r in read_jsonl(args.raw) if any(p in (r.get("name") or "") for p in phones)]
    route = world_router(layout)
    rooms = layout.get("rooms") or []
    classes = json.loads(args.classes.read_text(encoding="utf-8"))
    built = [b for b in (build_walk(w, cfg, args.tag, raw, marks, places, phones, route, rooms, classes, args.window_s) for w in cfg["walks"]) if b]
    if not built:
        sys.exit(f"нет отсчётов телефонов {list(phones)} в интервалах проходок {args.walks}: проверь --raw и описание проходок")
    parts = {name: [r for b in built for r in b["parts"][name]] for name in built[0]["parts"]}
    for name, rows in parts.items():
        write_jsonl(args.out / f"{name}-{args.tag}.jsonl", rows)
    summary = {name: {"rows": len(rows), "by_walk": {w: sum(r.get("walk") == w for r in rows) for w in sorted({r.get("walk") for r in rows})},
                      "by_quality": {str(q): sum(r.get("label_quality") == q for r in rows) for q in sorted({str(r.get("label_quality")) for r in rows})}}
               for name, rows in parts.items()}
    summary["stops_detail"] = [d for b in built for d in b["detail"]]
    summary["segments"] = [s for b in built for s in b["segments"]]
    lags = [d["arrive_lag_s"] for d in summary["stops_detail"]], [d["leave_lag_s"] for d in summary["stops_detail"] if d["leave_lag_s"] is not None]
    summary["mark_lag_s"] = {name: {"median": round(statistics.median(v), 1), "min": min(v), "max": max(v)} for name, v in zip(("arrive", "leave"), lags) if v}
    (args.out / f"summary-{args.tag}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("stops_detail", "segments")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
