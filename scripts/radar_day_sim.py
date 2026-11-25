"""Симуляция дня человека по его привычкам (SAM-118): расписание, ходьба по клеткам плана и секундный ряд уровней сигнала по радио-карте.

Расписание. Сутки режутся на слоты `step_min` из файла привычек; место слота берётся из `presence_habits.place_prior` (доля времени в окне, остаток базовым делам). Человек не прыгает между
базовыми местами каждые 15 минут: с вероятностью STAY_PROB он остаётся на прежнем месте, если оно в этом слоте допустимо (допущение, не данные). Короткие места (туалет, ванная) занимают
несколько минут внутри слота, потом человек возвращается. Место «__away__» - человека нет дома, телефон не виден.
Ходьба. Между стоянками путь по клеткам мира, по клетке в секунду; идти он выходит заранее, так что время прихода совпадает с началом стоянки в расписании.
Уровни. Ожидаемый моделью уровень клетки плюс ошибка модели, постоянная для клетки (радио-карта в разных местах ошибается по-разному, как показывают отпечатки), плюс шум секунды.
Выход - в `data/synthetic/` (личный файл, вне git): расписание `.json`, ряд уровней `.jsonl` (секунда, состояние, место, x, z, уровни по диапазонам).
Фильтр. Режим --evaluate прогоняет участки ряда через `LiveFlow.observe` от известной первой точки и считает вершину (место с наибольшей массой среди мест привычек), найденные приходы, отдельно на места кроме стартового, отставание и мерцание на стоянке при разной доле стояния и весе предпочтений.

Запуск: python scripts/radar_day_sim.py --person <ключ> [--date ГГГГ-ММ-ДД] [--seed N] [--evaluate --segments 12:30+60,21:00+60 --stays 0,0.9 --weights 0.1,1 --error-seeds 1,2,3]"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))        # запуск из любой папки: корень репозитория на пути импорта
from radar import presence_flow_live as pfl                       # noqa: E402
from radar import presence_habits as ph                           # noqa: E402
from radar import presence_rssi as rs                             # noqa: E402
from radar.presence_random import Randomness                      # noqa: E402

LAYOUT, HABITS, OUT_DIR, DEVICE_PREFS = "data/home_layout.json", "data/home_habits.json", "data/synthetic", "data/device_prefs.json"
DEVICE_PART = "phone"                     # устройство, которым ходит человек: по нему берутся модели обоих диапазонов
STAY_PROB = 0.7                          # остаться на прежнем месте, если оно допустимо в слоте (допущение)
BRIEF_MIN = {"туалет": (3, 6), "ванная": (5, 10)}     # короткие места: минут на посещение, от и до (допущение)
TEMPORAL_NOISE_DB = 1.0                  # шум секунды: у неподвижного устройства sd 1,07 дБ (SAM-118)
ARRIVAL_HOLD_S = 3                       # отставание: верная точка столько секунд подряд
DAY_S = 86400


@dataclass
class Stay:
    start: int           # секунда суток прихода
    end: int             # секунда суток ухода
    place: str


def choose_place(habits: dict, person: str, when: datetime, previous: str | None, rng: np.random.Generator) -> str:
    prior = ph.place_prior(habits, person, when)
    if previous in prior and prior[previous] > 0 and rng.random() < STAY_PROB:
        return previous
    names = list(prior)
    return str(rng.choice(names, p=np.array([prior[n] for n in names]) / sum(prior.values())))


def day_schedule(habits: dict, person: str, day: datetime, rng: np.random.Generator) -> list[Stay]:
    """Стоянки суток по слотам привычек; соседние слоты на одном месте склеиваются, короткие места вырезаются внутри слота."""
    step = int(habits.get("step_min", ph.DEFAULT_STEP_MIN))
    stays: list[Stay] = []
    base: str | None = None
    for slot in range(0, ph.MINUTES_PER_DAY, step):
        start, end = slot * 60, min(slot + step, ph.MINUTES_PER_DAY) * 60
        place = choose_place(habits, person, day + timedelta(minutes=slot), base, rng)
        pieces = [(start, end, place)]
        if place in BRIEF_MIN and base is not None:                       # короткое посещение внутри слота, остальное на прежнем месте
            low, high = BRIEF_MIN[place]
            length = int(rng.integers(low, high + 1)) * 60
            at = start + int(rng.integers(0, max(1, end - start - length)))
            pieces = [(start, at, base), (at, at + length, place), (at + length, end, base)]
        else:
            base = place
        for s, e, p in pieces:
            if e <= s:
                continue
            if stays and stays[-1].place == p and stays[-1].end == s:
                stays[-1].end = e
            else:
                stays.append(Stay(s, e, p))
    return stays


def trajectory(flow: pfl.LiveFlow, stays: list[Stay], place_xy: dict) -> list[tuple[int | None, str]]:
    """По секундам суток: (клетка или None, состояние: место стоянки или «идёт» или «нет дома»)."""
    out: list[tuple[int | None, str]] = [(None, ph.AWAY)] * DAY_S
    cells = {p: flow.world.cell_at(*xy) for p, xy in place_xy.items()}
    for i, stay in enumerate(stays):
        if stay.place == ph.AWAY or stay.place not in cells:
            continue
        walk: list[int] = []
        nxt = stays[i + 1] if i + 1 < len(stays) else None
        if nxt and nxt.place in cells and nxt.place != stay.place:
            walk = pfl_route(flow, cells[stay.place], cells[nxt.place])[1:-1]
        stand_end = max(stay.start, stay.end - len(walk))
        for sec in range(stay.start, stand_end):
            out[sec] = (cells[stay.place], stay.place)
        for k, cell in enumerate(walk):
            if stand_end + k < DAY_S:
                out[stand_end + k] = (cell, "идёт")
    return out


def pfl_route(flow: pfl.LiveFlow, start: int, goal: int) -> list[int]:
    """Клетки от start до goal: каждый шаг в достижимую за срез клетку, ближайшую по проходам к цели."""
    distances, reach = flow.world.distances(), flow.world.reach(pfl.pm.LOCALITY_M).tocsr()
    path, here = [start], start
    while here != goal and len(path) < 600:
        neighbours = reach[here].indices
        here = int(neighbours[np.argmin(distances[neighbours, goal])])
        path.append(here)
    return path


def level_series(flow: pfl.LiveFlow, models: dict, traj: list, rng: np.random.Generator) -> dict[str, np.ndarray]:
    """Ряд уровней по диапазонам: ожидаемый + ошибка модели клетки (постоянна, sd из подгонки) + шум секунды; вне дома NaN."""
    layout = json.loads(Path(LAYOUT).read_text(encoding="utf-8"))
    series = {}
    for key, fitted in models.items():
        if DEVICE_PART not in key.lower():
            continue
        iface = key.rpartition("@")[2]
        expected = pfl.expected_db(flow.world, layout, fitted)
        error = rng.normal(0.0, fitted.get("leave_one_out_rms_db", 4.0), len(flow.world))          # ошибка карты в каждой клетке своя и не меняется во времени
        levels = np.full(DAY_S, np.nan)
        for sec, (cell, _) in enumerate(traj):
            if cell is not None:
                levels[sec] = expected[cell] + error[cell] + rng.normal(0.0, TEMPORAL_NOISE_DB)
        series[iface] = levels
    return series


def timeline_text(stays: list[Stay]) -> str:
    rows = [f"{s.start // 3600:02d}:{s.start % 3600 // 60:02d}-{s.end // 3600 % 24:02d}:{s.end % 3600 // 60:02d}  {(s.end - s.start) // 60:4d} мин  {s.place}" for s in stays]
    return "\n".join(rows)


def evaluate(flow, models: dict, traj: list, series: dict, iface: str, lo: int, hi: int) -> list[tuple[str, str | None]]:
    """Участок ряда через LiveFlow.observe от известной первой точки; по секундам стоянок (настоящее место, вершина потока). Ходьба и «нет дома» разрывают стоянки (None)."""
    name = next(k for k in models if DEVICE_PART in k.lower() and k.endswith(iface)).rpartition("@")[0]
    located, records = False, []
    for sec in range(lo, hi):
        cell, state = traj[sec]
        level = series[iface][sec]
        if cell is None or np.isnan(level):
            flow.observe([], 1.8e9 + sec)
            records.append((None, None))
            continue
        flow.observe([rs.Station("sim", iface, float(level), 0, name)], 1.8e9 + sec)
        if not located:
            located = flow.locate(name, iface, *flow.world.points[cell])
        records.append((None, None) if state == "идёт" else (state, flow.top_place(name, iface)))
    return records


def stand_runs(records: list[tuple[str, str | None]]) -> list[list[tuple[str, str | None]]]:
    """Стоянки подряд: отрезки одного места без разрыва."""
    runs, current = [], []
    for state, top in records:
        if state is None or (current and current[-1][0] != state):
            if current:
                runs.append(current)
            current = [] if state is None else [(state, top)]
        else:
            current.append((state, top))
    return runs + ([current] if current else [])


def metrics(records: list) -> dict:
    """Вершина; приходы (все и на места, отличные от первой стоянки участка), найденные ARRIVAL_HOLD_S секунд подряд, и отставание найденных; мерцание: доля секунд стоянки,
    где показано не основное место этой стоянки (критерий: не больше 20% на стоянке)."""
    runs = stand_runs(records)
    seconds = [r for run in runs for r in run]
    top1 = sum(top == state for state, top in seconds) / len(seconds) if seconds else 0.0
    first = runs[0][0][0] if runs else None
    arrivals = found = other = other_found = flicker = 0
    lags = []
    for k, run in enumerate(runs):
        tops = [top for _, top in run]
        mode = max(set(tops), key=tops.count)
        flicker += sum(top != mode for top in tops)
        if k == 0:
            continue
        place = run[0][0]
        arrivals += 1
        other += place != first
        hit = next((i for i in range(len(tops) - ARRIVAL_HOLD_S + 1) if all(t == place for t in tops[i:i + ARRIVAL_HOLD_S])), None)
        if hit is not None:
            found += 1
            other_found += place != first
            lags.append(hit)
    return {"top1": top1, "arrivals": arrivals, "found": found, "other": other, "other_found": other_found, "lag": float(np.mean(lags)) if lags else float("nan"),
            "flicker": flicker / len(seconds) if seconds else 0.0}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--person", required=True, help="ключ человека из data/presence_owners.json")
    parser.add_argument("--date", default=None, help="день ГГГГ-ММ-ДД; по умолчанию ближайший понедельник")
    parser.add_argument("--seed", type=int, default=1, help="сид расписания дня")
    parser.add_argument("--error-seeds", default="1", help="сиды ошибки радио-карты через запятую: оценка усредняется по ним, один сид даёт везение или невезение в паре клеток")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--segments", default="12:30+60,21:00+60", help="участки оценки «ЧЧ:ММ+минут» через запятую")
    parser.add_argument("--stays", default="0", help="доли «остался в клетке» через запятую")
    parser.add_argument("--weights", default="0.1", help="веса карты предпочтений телефона через запятую")
    parser.add_argument("--iface", default="phy1-ap0")
    args = parser.parse_args(argv)
    habits = json.loads(Path(HABITS).read_text(encoding="utf-8"))
    day = datetime.strptime(args.date, "%Y-%m-%d") if args.date else datetime(2026, 10, 12)
    rng = Randomness(args.seed).numpy("day-sim")
    models = pfl.resolve_models(json.loads(Path(pfl.DEFAULT_RADIO_MODEL_FILE).read_text(encoding="utf-8")))
    layout = json.loads(Path(LAYOUT).read_text(encoding="utf-8"))
    place_xy = {name: (xy[0], xy[1]) for name, xy in habits["place_xy"].items()}
    make_flow = lambda: pfl.LiveFlow(layout, models, {}, {"place_xy": place_xy}, randomness=Randomness(args.seed))
    flow = make_flow()
    stays = day_schedule(habits, args.person, day, rng)
    traj = trajectory(flow, stays, place_xy)
    error_seeds = [int(x) for x in args.error_seeds.split(",")]
    series = level_series(flow, models, traj, Randomness(error_seeds[0]).numpy("errors"))
    out = Path(OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"day-{args.person}-{day:%Y-%m-%d}-seed{args.seed}"
    (out / f"{stem}.json").write_text(json.dumps([{"from": f"{s.start // 3600:02d}:{s.start % 3600 // 60:02d}", "to": f"{s.end // 3600 % 24:02d}:{s.end % 3600 // 60:02d}", "minutes": (s.end - s.start) // 60, "place": s.place} for s in stays], ensure_ascii=False, indent=1), encoding="utf-8")
    with open(out / f"{stem}.jsonl", "w", encoding="utf-8") as f:
        for sec, (cell, state) in enumerate(traj):
            if cell is None:
                continue
            x, z = (float(v) for v in flow.world.points[cell])
            f.write(json.dumps({"t": sec, "state": state, "x": round(x, 2), "z": round(z, 2), **{f"db_{iface}": round(float(levels[sec]), 1) for iface, levels in series.items()}}, ensure_ascii=False) + "\n")
    print(f"День {args.person} {day:%Y-%m-%d} (сид {args.seed}), стоянок {len(stays)}; файлы {out / stem}.json и .jsonl")
    print(timeline_text(stays))
    if args.evaluate:
        prefs = json.loads(Path(DEVICE_PREFS).read_text(encoding="utf-8")) if Path(DEVICE_PREFS).exists() else {}
        segments = [(int(t[:2]) * 3600 + int(t[3:5]) * 60, int(m) * 60) for t, m in (part.split("+") for part in args.segments.split(","))]
        print(f"\nОценка фильтра: участки {args.segments}, {args.iface}, сиды ошибки карты {args.error_seeds}")
        print(f"{'стояние':>8} {'вес предп.':>10} {'вершина':>8} {'разброс':>8} {'приходы найдены':>16} {'на другие места':>16} {'отставание, с':>14} {'мерцание':>9}")
        for stay in (float(x) for x in args.stays.split(",")):
            for weight in (float(x) for x in args.weights.split(",")):
                per_seed = []
                for es in error_seeds:
                    errors = level_series(flow, models, traj, Randomness(es).numpy("errors"))
                    records = []
                    for lo, length in segments:
                        records += evaluate(pfl.LiveFlow(layout, models, prefs, {"place_xy": place_xy}, randomness=Randomness(args.seed), preference_weight=weight, stay_share=stay),
                                            models, traj, errors, args.iface, lo, lo + length) + [(None, None)]
                    per_seed.append(metrics(records))
                total = {k: sum(m[k] for m in per_seed) for k in ("arrivals", "found", "other", "other_found")}
                lags = [m["lag"] for m in per_seed if m["lag"] == m["lag"]]
                tops = [m["top1"] for m in per_seed]
                print(f"{stay:8.2f} {weight:10.2f} {100 * np.mean(tops):7.1f}% {100 * np.std(tops):7.1f}% {total['found']:7d} из {total['arrivals']:<5d} {total['other_found']:7d} из {total['other']:<5d} "
                      f"{np.mean(lags) if lags else float('nan'):14.1f} {100 * np.mean([m['flicker'] for m in per_seed]):8.1f}%", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
