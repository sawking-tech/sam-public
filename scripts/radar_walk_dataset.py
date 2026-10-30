"""Набор проверочных путей радиуса из записанных проходов (SAM-118): ряд уровня устройства и нарезка путей разной длины с известными началом и концом.

Исходные записи и таймлайн лежат в data/ вне git: в них имена мест, время суток и пометки людей. На выходе обезличенный набор: места P01, P02, ...,
время в секундах от начала ряда, устройства «target» и «reference».

Запуск: python scripts/radar_walk_dataset.py data/radar_walk_timeline.json tests/data/radar_walks/walks.json
Формат таймлайна -."""
from __future__ import annotations

import itertools
import json
import statistics
import sys
from datetime import datetime

MIN_CUT_S = 6                  # короче этого путь не режем: в нём нечего сравнивать
STAND_CUT_LENGTHS_S = (10, 20, 40)   # длины окон внутри стоянки: проверка «не двигался»
STAND_CUT_STEP_S = 15          # шаг начала окон внутри стоянки
PEAK_SD_M = 1.0                # положение пика уровня относительно роутера: путь проходит рядом, а не строго под ним
MARK_TIME_SD_S = 5.0            # разброс времени отметки человека: он пишет после события, запаздывание неровное (8-17 с)
PLACE_PREFIX = "P"
ROOM_PREFIX = "R"
PAD_STEPS_S = (5, 10)          # на сколько секунд расширять путь внутрь стоянок на краях: путь с теми же известными концами, но длиннее
OFFSETS_DB = (-3.0, 3.0)       # сдвиги уровня: имитация другого дня, позы телефона, дрейфа калибровки
STAND_GAP_S = 10               # зазор между краем стоянки и началом ходьбы, в котором стоянка ещё считается соседней
CLOCK_FORMAT = "%H:%M:%S"


def clock_to_ts(day: str, clock: str) -> float:
    return datetime.strptime(f"{day} {clock}", f"%Y-%m-%d {CLOCK_FORMAT}").timestamp()


def load_series(path: str, name_prefix: str, iface: str, t0: float, t1: float) -> list[float | None]:
    """Уровень по секундам от int(t0) до int(t1): медиана отсчётов секунды, None - отсчётов не было."""
    per_second: dict[int, list[float]] = {}
    with open(path, encoding="utf8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (row.get("name") or "").lower().startswith(name_prefix) and row.get("if") == iface and t0 <= row["t"] <= t1:
                per_second.setdefault(int(row["t"]), []).append(row["db"])
    return [statistics.median(per_second[s]) if s in per_second else None for s in range(int(t0), int(t1) + 1)]


def place_ids(timeline: dict) -> dict[str, str]:
    """Имя места -> P01, P02 по порядку первого появления в стоянках и ходьбе."""
    order: list[str] = []
    for item in timeline["stands"]:
        order.append(item["place"])
    for leg in timeline["legs"]:
        order += [leg["from"], leg["to"]]
    ids: dict[str, str] = {}
    for name in order:
        ids.setdefault(name, f"{PLACE_PREFIX}{len(ids) + 1:02d}")
    return ids


def end_point(ids: dict[str, str], places: dict, name: str, kind: str, sd_m: float = 0.0) -> dict:
    return {"place": ids[name], "kind": kind, "x": places[name]["x"], "z": places[name]["z"], "sd_m": sd_m}


def peak_time(series: list[float | None], a: int, b: int) -> int:
    """Секунда наибольшего уровня на отрезке [a, b]."""
    return max((s for s in range(a, b + 1) if series[s] is not None), key=lambda s: series[s])


def cuts_for_leg(leg: dict, rel: dict, series: list[float | None], ids: dict[str, str], places: dict, router: dict) -> list[dict]:
    """Пути одной ходьбы: целиком и, если есть пик у роутера, до пика и после него."""
    a, b = rel["start"], rel["end"]
    start = end_point(ids, places, leg["from"], "stand")
    finish = end_point(ids, places, leg["to"], "stand")
    base = {"pace": leg["pace"], "moving": True, "leg": rel["index"]}
    out = [{**base, "t0": a, "t1": b, "start": start, "end": finish}]
    if leg.get("router_pass"):
        peak = peak_time(series, a, b)
        mid = {"place": None, "kind": "router_peak", "x": router["x"], "z": router["z"], "sd_m": PEAK_SD_M}
        out += [{**base, "t0": a, "t1": peak, "start": start, "end": mid}, {**base, "t0": peak, "t1": b, "start": mid, "end": finish}]
    return [c for c in out if c["t1"] - c["t0"] >= MIN_CUT_S]


def landmark_point(landmark: dict, lag_s: float) -> dict:
    """Ориентир внутри ходьбы: положение приблизительное, время события - время отметки минус запаздывание."""
    return {"place": None, "kind": "landmark", "x": landmark["x"], "z": landmark["z"], "sd_m": landmark["sd_m"],
            "t_sd_s": MARK_TIME_SD_S if landmark.get("lag_s", lag_s) else 1.0}


def cuts_for_landmarks(leg: dict, rel: dict, rel_clock, lag_s: float, ids: dict[str, str], places: dict) -> list[dict]:
    """Все пары точек [начало ходьбы, ориентиры по времени, конец ходьбы]: пути разной длины с разным началом, приблизительные."""
    marks = sorted(((rel_clock(m["clock"]) - int(m.get("lag_s", lag_s)), landmark_point(m, lag_s)) for m in leg.get("landmarks", [])), key=lambda x: x[0])
    points = [(rel["start"], end_point(ids, places, leg["from"], "stand"))] + marks + [(rel["end"], end_point(ids, places, leg["to"], "stand"))]
    out = []
    for (t0, start), (t1, end) in itertools.combinations(points, 2):
        if t1 - t0 >= MIN_CUT_S and (start["kind"] == "landmark" or end["kind"] == "landmark"):
            out.append({"t0": t0, "t1": t1, "pace": leg["pace"], "moving": True, "exact": False, "leg": rel["index"], "start": start, "end": end})
    return out


def cuts_for_chains(legs_rel: list[dict], stands_rel: list[dict], ids: dict[str, str], places: dict) -> list[dict]:
    """Склейки подряд идущих ходьб вместе со стоянкой между ними: путь с паузой, начало и конец известны."""
    out = []
    for first, second in zip(legs_rel, legs_rel[1:]):
        if first["leg"]["to"] != second["leg"]["from"]:
            continue
        out.append({"t0": first["start"], "t1": second["end"], "pace": first["leg"]["pace"], "moving": True, "chain": True,
                    "start": end_point(ids, places, first["leg"]["from"], "stand"), "end": end_point(ids, places, second["leg"]["to"], "stand")})
    return out


def cuts_for_stands(stands_rel: list[dict], ids: dict[str, str], places: dict) -> list[dict]:
    """Окна разной длины внутри стоянок: человек не двигался, положение известно."""
    out = []
    for stand in stands_rel:
        for length in STAND_CUT_LENGTHS_S:
            for t0 in range(stand["a"], stand["b"] - length + 1, STAND_CUT_STEP_S):
                point = end_point(ids, places, stand["item"]["place"], "stand")
                out.append({"t0": t0, "t1": t0 + length, "pace": None, "moving": False, "start": point, "end": point})
    return out


def room_ids(layout: dict | None) -> dict[str, dict]:
    """Комнаты плана: R01, R02, ... с прямоугольниками (имена комнат в набор не идут)."""
    return {f"{ROOM_PREFIX}{i + 1:02d}": {"x": r["x"], "z": r["z"], "w": r["w"], "d": r["d"]} for i, r in enumerate((layout or {}).get("rooms") or [])}


def room_of(rooms: dict[str, dict], x: float, z: float) -> str | None:
    return next((rid for rid, r in rooms.items() if r["x"] <= x <= r["x"] + r["w"] and r["z"] <= z <= r["z"] + r["d"]), None)


def second_labels(seconds: int, stands_rel: list[dict], legs_rel: list[dict], ids: dict[str, str]) -> dict[str, list]:
    """По секундам: стоянка (место) или ходьба (номер ходьбы); где положение неизвестно - None."""
    kind: list[str | None] = [None] * seconds
    place: list[str | None] = [None] * seconds
    leg: list[int | None] = [None] * seconds
    for s in stands_rel:
        for t in range(s["a"], min(seconds, s["b"] + 1)):
            kind[t], place[t] = "stand", ids[s["item"]["place"]]
    for item in legs_rel:
        for t in range(item["start"], min(seconds, item["end"] + 1)):
            kind[t], leg[t] = "walk", item["index"]
    return {"kind": kind, "place": place, "leg": leg}


def neighbour_stand(stands: list[dict], t: int, before: bool) -> dict | None:
    """Стоянка, которая кончилась не раньше STAND_GAP_S до секунды t (before) или началась не позже STAND_GAP_S после неё: между стоянкой и ходьбой есть зазор."""
    if before:
        near = [s for s in stands if s["t1"] <= t and t - s["t1"] <= STAND_GAP_S]
        return max(near, key=lambda s: s["t1"], default=None)
    near = [s for s in stands if s["t0"] >= t and s["t0"] - t <= STAND_GAP_S]
    return min(near, key=lambda s: s["t0"], default=None)


def is_stand_to_stand(cut: dict) -> bool:
    return cut["moving"] and cut["exact"] and cut["start"]["kind"] == "stand" and cut["end"]["kind"] == "stand" and not cut.get("chain")


def padded(cuts: list[dict], stands: list[dict]) -> list[dict]:
    """Точные пути между стоянками, начатые раньше и законченные позже: захватывают конец прежней стоянки и начало следующей; концы известны те же."""
    out = []
    for cut in filter(is_stand_to_stand, cuts):
        before, after = neighbour_stand(stands, cut["t0"], True), neighbour_stand(stands, cut["t1"], False)
        if not before or not after:
            continue
        for a, b in itertools.product((0,) + PAD_STEPS_S, repeat=2):
            t0, t1 = before["t1"] - a, after["t0"] + b
            if t0 >= before["t0"] and t1 <= after["t1"] and (t0, t1) != (cut["t0"], cut["t1"]):
                out.append({**cut, "t0": t0, "t1": t1, "length_s": t1 - t0, "augment": "pad", "parent": cut["id"]})
    return out


def level_shifted(cuts: list[dict]) -> list[dict]:
    """Целые ходьбы со сдвигом уровня: потребитель прибавляет offset_db к ряду."""
    return [{**c, "augment": "offset", "offset_db": d, "parent": c["id"]} for c in filter(is_stand_to_stand, cuts) for d in OFFSETS_DB]


def reversed_cuts(cuts: list[dict]) -> list[dict]:
    """Развороты целых ходьб: гипотеза, что профиль уровня при обратном проходе зеркален; помечены, обучать на них без проверки нельзя."""
    return [{**c, "start": c["end"], "end": c["start"], "augment": "reverse", "parent": c["id"], "hypothesis": True} for c in filter(is_stand_to_stand, cuts)]


def stand_median(series: list[float | None], stand: dict) -> float:
    return statistics.median(v for v in series[stand["t0"]: stand["t1"] + 1] if v is not None)


def splice_of(first: dict, second: dict, offset: float, places: dict, indexes: tuple[int, int]) -> dict:
    return {"augment": "splice", "moving": True, "exact": True, "hypothesis": False, "pace": first["pace"], "legs": list(indexes),
            "parts": [{"t0": first["t0"], "t1": first["t1"], "offset_db": 0.0}, {"t0": second["t0"], "t1": second["t1"], "offset_db": round(offset, 1)}],
            "length_s": (first["t1"] - first["t0"]) + (second["t1"] - second["t0"]),
            "start": {"place": first["from"], "kind": "stand", **places[first["from"]], "sd_m": 0.0},
            "end": {"place": second["to"], "kind": "stand", **places[second["to"]], "sd_m": 0.0}}


def spliced(legs: list[dict], stands: list[dict], series: list[float | None], places: dict) -> list[dict]:
    """Склейки двух ходьб, которые встречаются на одном месте, но шли не подряд: второй кусок выравнивается по уровню стоянки на стыке."""
    out = []
    for (i, first), (j, second) in itertools.permutations(enumerate(legs), 2):
        if j == i + 1 or first["to"] != second["from"]:
            continue
        end_stand, start_stand = neighbour_stand(stands, first["t1"], False), neighbour_stand(stands, second["t0"], True)
        if end_stand and start_stand:
            out.append(splice_of(first, second, stand_median(series, end_stand) - stand_median(series, start_stand), places, (i, j)))
    return out


def augmented(cuts: list[dict], legs: list[dict], stands: list[dict], series: list[float | None], places: dict) -> list[dict]:
    items = padded(cuts, stands) + level_shifted(cuts) + reversed_cuts(cuts) + spliced(legs, stands, series, places)
    for number, item in enumerate(items, 1):
        item["id"] = f"A{number:03d}"
    return items


def collect_cuts(timeline: dict, legs_rel: list[dict], stands_rel: list[dict], series: list[float | None], rel, ids: dict[str, str], places: dict) -> list[dict]:
    cuts: list[dict] = []
    for item in legs_rel:
        cuts += cuts_for_leg(item["leg"], item, series, ids, places, timeline["router"])
        cuts += cuts_for_landmarks(item["leg"], item, rel, timeline.get("mark_lag_s", 0), ids, places)
    return cuts + cuts_for_chains(legs_rel, stands_rel, ids, places) + cuts_for_stands(stands_rel, ids, places)


def numbered(cuts: list[dict]) -> list[dict]:
    """Идентификаторы C001... по времени начала; по умолчанию путь точный."""
    for number, cut in enumerate(sorted(cuts, key=lambda c: (c["t0"], c["t1"])), 1):
        cut["id"] = f"C{number:03d}"
        cut["length_s"] = cut["t1"] - cut["t0"]
        cut.setdefault("exact", True)
    return sorted(cuts, key=lambda c: c["id"])


def build(timeline: dict) -> dict:
    day = timeline["date"]
    t0, t1 = clock_to_ts(day, timeline["from"]), clock_to_ts(day, timeline["to"])
    rel = lambda clock: int(clock_to_ts(day, clock) - int(t0))
    target = load_series(timeline["tracks_file"], timeline["target"], timeline["iface"], t0, t1)
    reference = load_series(timeline["tracks_file"], timeline["reference"], timeline["iface"], t0, t1)
    ids, places = place_ids(timeline), timeline["places"]
    rooms = room_ids(json.load(open(timeline["layout"], encoding="utf8")) if timeline.get("layout") else None)
    stands_rel = [{"item": s, "a": rel(s["from"]), "b": rel(s["to"])} for s in timeline["stands"]]
    legs_rel = [{"leg": leg, "start": rel(leg["start"]), "end": rel(leg["end"]), "index": i} for i, leg in enumerate(timeline["legs"])]
    cuts = numbered(collect_cuts(timeline, legs_rel, stands_rel, target, rel, ids, places))
    stands = [{"place": ids[s["item"]["place"]], "t0": s["a"], "t1": s["b"]} for s in stands_rel]
    legs = [{"from": ids[i["leg"]["from"]], "to": ids[i["leg"]["to"]], "t0": i["start"], "t1": i["end"], "pace": i["leg"]["pace"]} for i in legs_rel]
    place_rows = {ids[name]: {"x": p["x"], "z": p["z"], "room": room_of(rooms, p["x"], p["z"])} for name, p in places.items() if name in ids}
    return {"meta": {"rate_hz": 1, "band": timeline["band"], "seconds": len(target), "target": "target", "reference": "reference", "units": "dBm, m"},
            "places": place_rows, "rooms": rooms, "router": timeline["router"], "series": {"target": target, "reference": reference},
            "labels": second_labels(len(target), stands_rel, legs_rel, ids), "stands": stands, "legs": legs, "cuts": cuts,
            "augmented": augmented(cuts, legs, stands, target, place_rows)}


def main() -> int:
    timeline = json.load(open(sys.argv[1], encoding="utf8"))
    dataset = build(timeline)
    with open(sys.argv[2], "w", encoding="utf8") as f:
        json.dump(dataset, f, ensure_ascii=False, separators=(",", ":"))
    print(f"путей {len(dataset['cuts'])}, секунд {dataset['meta']['seconds']}, мест {len(dataset['places'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
