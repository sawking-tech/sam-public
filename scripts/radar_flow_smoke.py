"""Смок и симуляция фильтра потока на записанных треках по их времени (SAM-118).

Строки треков идут в `LiveFlow.observe` как в бою: на каждом тике (раз в TICK_S по времени записи) подаются станции, у которых в этом тике есть отсчёт; секунды без отсчёта
подаются пустым списком, поэтому окно, пропуски, сброс после долгого молчания и свежесть состояния работают те же, что на живом радаре. Данные плана, моделей уровня, предпочтений и привычек
берутся из тех же файлов `data/`, что у радара. Проверяются инварианты (не упало, распределения конечны и суммируются в единицу, шаг короче секунды, окно полное), считаются метрики:
шаг, полнота окна, смещение точки за тик, скачки дальше радиуса локальности, смены вершины списка мест и качели.

Запуск: python scripts/radar_flow_smoke.py [--from "ГГГГ-ММ-ДД ЧЧ:ММ"] [--minutes N] [--seed N] [--window N] [--places] [--truth МЕСТО] [tracks.jsonl ...]; код выхода 1, если инвариант нарушен.
Места устройств из `data/presence_places.json` по умолчанию не подставляются: они описывают сегодняшний день, а не запись; ключ --places включает их. Ключ --truth МЕСТО (из привычек `place_xy`) задаёт правду для неподвижного участка, например ночь на кровати: считаются доля верных вершин и расстояние точки до места."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))        # запуск из любой папки: корень репозитория на пути импорта
from radar import presence_flow_live as pfl                       # noqa: E402
from radar import presence_hub as ph                              # noqa: E402
from radar import presence_rssi as rs                             # noqa: E402
from radar.presence_random import Randomness                      # noqa: E402

DEFAULT_TRACKS = ("data/presence_tracks/tracks-20261010.jsonl", "data/presence_tracks/tracks-20261011.jsonl")
DEFAULT_LAYOUT = "data/home_layout.json"
DEFAULT_PREFS = "data/device_prefs.json"
DEFAULT_HABITS = "data/home_habits.json"
DEFAULT_PLACES = "data/presence_places.json"
DEFAULT_MINUTES = 30            # длина участка по умолчанию: сутки считаются часами, смок должен быть коротким
TICK_S = 1.0                    # тик симуляции: запись держит отсчёт раз в секунду на устройство
JUMP_FACTOR = 1.5               # смещение точки за тик больше радиуса локальности на этот множитель - скачок
STEP_LIMIT_S = 1.0              # шаг фильтра длиннее тика радар не успевает
FULL_WINDOW_MIN_SHARE = 0.8     # доля тиков с полным окном у устройства, которое слышно, не ниже этой (после набора окна)
PINGPONG_S = 5                  # смена вершины с возвратом на прежнее за столько секунд - качели


def read_json(path: str, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_rows(paths: list[str]) -> list[dict]:
    """Строки треков всех файлов по возрастанию времени."""
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            rows += [json.loads(line) for line in f if line.strip()]
    return sorted(rows, key=lambda r: r["t"])


def parse_start(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


def segment(rows: list[dict], start: str | None, minutes: float) -> list[dict]:
    """Участок записи: minutes минут с метки start (ГГГГ-ММ-ДД ЧЧ:ММ), без метки - с середины записи."""
    if not rows:
        return []
    t0 = parse_start(start) if start else rows[len(rows) // 2]["t"]
    return [r for r in rows if t0 <= r["t"] < t0 + minutes * 60]


def build_flow(seed: int, window_slices: int | None, with_places: bool = False) -> pfl.LiveFlow:
    layout, prefs, habits = read_json(DEFAULT_LAYOUT, {}), read_json(DEFAULT_PREFS, {}), read_json(DEFAULT_HABITS, {})
    models = pfl.resolve_models(read_json(pfl.DEFAULT_RADIO_MODEL_FILE, {}))
    flow = pfl.LiveFlow(layout, models, prefs, habits, randomness=Randomness(seed), window_slices=window_slices)
    places = {k.lower(): v for k, v in read_json(DEFAULT_PLACES, {}).items()} if with_places else {}
    flow.bind_places(lambda name: next((place for part, place in places.items() if name and part in name.lower()), None))
    return flow


def simulate(flow: pfl.LiveFlow, rows: list[dict]) -> dict:
    """Тик за тиком по времени записи; собирает длительности шага, полноту окна, смещения точки и вершины списка мест по устройствам."""
    if not rows:
        return {"ticks": 0, "devices": {}, "errors": ["в участке нет строк треков"]}
    stats: dict[str, dict] = {}
    errors, durations = [], []
    start, end = rows[0]["t"], rows[-1]["t"]
    index, tick, previous = 0, start, {}
    while tick <= end:
        bucket = []
        while index < len(rows) and rows[index]["t"] < tick + TICK_S:
            r = rows[index]
            bucket.append(rs.Station(r["id"], r["if"], float(r["db"]), 0, r.get("name")))
            index += 1
        t0 = time.perf_counter()
        try:
            flow.observe(bucket, tick + TICK_S)
            state = flow.state()
        except Exception as e:                                                   # смок для того и нужен: любой сбой в шаге - в отчёт, не в стек
            errors.append(f"{datetime.fromtimestamp(tick):%H:%M:%S}: {type(e).__name__}: {e}")
            break
        durations.append(time.perf_counter() - t0)
        for d in state["devices"]:
            key = f"{d['name']}@{d['iface']}"
            s = stats.setdefault(key, {"ticks": 0, "full": 0, "shifts": [], "tops": [], "points": [], "bad": 0})
            s["ticks"] += 1
            if not all(math.isfinite(v) for v in d["p"]) or abs(sum(d["p"]) - 1.0) > 0.01:
                s["bad"] += 1
            tracked = next((t for t in flow._tracked.values() if t.name == d["name"] and t.iface == d["iface"]), None)
            if tracked is not None and len(tracked.subject.window.history) == tracked.subject.window.size:
                s["full"] += 1
            point = flow.point_of(d["name"], d["iface"])
            if point is not None and key in previous:
                s["shifts"].append(math.hypot(point["x"] - previous[key][0], point["z"] - previous[key][1]))
            if point is not None:
                previous[key] = (point["x"], point["z"])
                s["points"].append((point["x"], point["z"]))
            s["tops"].append(d["places"][0]["place"] if d["places"] else None)
        tick += TICK_S
    return {"ticks": len(durations), "durations": durations, "devices": stats, "errors": errors}


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))] if ordered else 0.0


def swings(tops: list) -> tuple[int, int]:
    """(смены вершины, из них качели: возврат на прежнее за PINGPONG_S тиков)."""
    changes = [i for i in range(1, len(tops)) if tops[i] != tops[i - 1] and tops[i] is not None and tops[i - 1] is not None]
    swung = [i for i in changes if any(i + k < len(tops) and tops[i + k] == tops[i - 1] for k in range(1, PINGPONG_S + 1))]
    return len(changes), len(swung)


def report(result: dict, flow: pfl.LiveFlow, truth: str | None = None) -> list[str]:
    """Строки отчёта и список нарушений инвариантов (вторым элементом)."""
    lines, failures = [f"тиков {result['ticks']}, окно {flow._window_slices} срезов, шаг фильтра (с учётом состояния): макс {max(result.get('durations') or [0]):.3f} с, 95% {percentile(result.get('durations') or [], 0.95):.3f} с"], list(result["errors"])
    if result.get("durations") and max(result["durations"]) > STEP_LIMIT_S:
        failures.append(f"шаг длиннее {STEP_LIMIT_S} с: радар не успевает")
    local = pfl.pm.LOCALITY_M * JUMP_FACTOR
    for key, s in sorted(result["devices"].items()):
        changes, swung = swings(s["tops"])
        jumps = sum(1 for d in s["shifts"] if d > local)
        full_share = s["full"] / s["ticks"] if s["ticks"] else 0.0
        lines.append(f"{key}: тиков с состоянием {s['ticks']}, окно полное {100 * full_share:.0f}%, смещение точки 50% {percentile(s['shifts'], 0.5):.2f} м, 95% {percentile(s['shifts'], 0.95):.2f} м, "
                     f"скачков больше {local:.1f} м: {jumps}, смен вершины {changes}, качелей {swung}, плохих распределений {s['bad']}")
        if truth:
            spot = read_json(DEFAULT_HABITS, {}).get("place_xy", {}).get(truth)
            right = sum(1 for top in s["tops"] if top == truth) / len(s["tops"]) if s["tops"] else 0.0
            far = [math.hypot(x - spot[0], z - spot[1]) for x, z in s["points"]] if spot else []
            lines.append(f"  правда «{truth}»: верная вершина {100 * right:.1f}% тиков, расстояние точки до места 50% {percentile(far, 0.5):.2f} м, 95% {percentile(far, 0.95):.2f} м, максимум {max(far or [0]):.2f} м")
        if s["bad"]:
            failures.append(f"{key}: {s['bad']} распределений не в единицу или не конечны")
        if s["ticks"] > flow._window_slices and full_share < FULL_WINDOW_MIN_SHARE:
            failures.append(f"{key}: окно полное только {100 * full_share:.0f}% тиков (нужно не меньше {100 * FULL_WINDOW_MIN_SHARE:.0f}%)")
    if not result["devices"]:
        failures.append("за участок ни одно устройство не попало в состояние: нет моделей уровня или записи")
    return lines, failures


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("tracks", nargs="*", default=list(DEFAULT_TRACKS))
    parser.add_argument("--from", dest="start", help="начало участка «ГГГГ-ММ-ДД ЧЧ:ММ»; по умолчанию середина записи")
    parser.add_argument("--minutes", type=float, default=DEFAULT_MINUTES)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--window", type=int, default=None, help="срезов в окне; по умолчанию боевое")
    parser.add_argument("--truth", default=None, help="место из привычек, где устройства стояли неподвижно на участке")
    parser.add_argument("--places", action="store_true", help="подставить места устройств из data/presence_places.json")
    args = parser.parse_args(argv)
    rows = segment(read_rows([p for p in args.tracks if Path(p).exists()]), args.start, args.minutes)
    flow = build_flow(args.seed, args.window, args.places)
    lines, failures = report(simulate(flow, rows), flow, args.truth)
    print("\n".join(lines))
    for failure in failures:
        print(f"НАРУШЕНИЕ: {failure}")
    print("смок пройден" if not failures else f"смок не пройден: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
