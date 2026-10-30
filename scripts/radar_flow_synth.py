"""Синтетическая симуляция потока положения (SAM-118): положение известно по построению, сигнал сочиняется по радио-модели с шумом.

Человек ходит по названным местам: стоит на месте, идёт к следующему кратчайшим путём по клеткам мира (шаг не дальше радиуса локальности), стоит. Уровень каждую секунду это ожидаемый моделью уровень клетки плюс гауссов шум;
секунды можно выбрасывать (пропуски опросов). Поток получает такие же станции, как в бою, через `LiveFlow.observe`. Карта предпочтений телефона нарочно смещена: вес на выбранном «ложном» месте, где человек не стоит,
чтобы видеть, во сколько точности обходится слишком сильное предпочтение. Сравниваются шум сигнала, вес предпочтения и длина окна.

Запуск: python scripts/radar_flow_synth.py [--seed N] [--stand N] [--noise 2,5,8] [--weights 0,0.25,1] [--windows 7,14] [--prior false|true] [--cold]
Поток считается на трек: путь продолжается от известной точки, поэтому по умолчанию первое положение известно потоку (`LiveFlow.locate`). Ключ --cold даёт холодный старт без начальной точки: он показывает, как долго один уровень сигнала не различает места на одном кольце.
Предпочтение false смещено на место, где человек не стоит; true собрано по местам, где он стоит (привычка): так видно вклад предпочтения и когда оно вредит, и когда помогает."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))        # запуск из любой папки: корень репозитория на пути импорта
from radar import presence_flow_live as pfl                       # noqa: E402
from radar import presence_preference as pp                       # noqa: E402
from radar import presence_rssi as rs                             # noqa: E402
from radar.presence_random import Randomness                      # noqa: E402

LAYOUT = "data/home_layout.json"
MODELS = pfl.DEFAULT_RADIO_MODEL_FILE
HABITS = "data/home_habits.json"
DEFAULT_STAND_S = 60            # секунд на месте до следующего перехода
DEFAULT_NOISE_DB = (2.0, 5.0, 8.0)       # разброс шума: ошибка радио-модели 3,6-6,6 дБ по замерам
DEFAULT_WEIGHTS = (0.0, 0.25, 1.0)      # вес ложного предпочтения
DEFAULT_WINDOWS = (7, 14)
PRIOR_SHARE = 0.9               # доля массы предпочтения на выбранных местах
MATCH_RADIUS_M = 1.5            # верная вершина: место не дальше этого от настоящего
START_UTC = 1_800_000_000.0     # начало синтетического времени
DROPOUT_SHARE = 0.1             # доля секунд без опроса
WALK_SEQUENCE = ("кровать в спальне", "кухня", "диван в зале", "стол в кабинете", "ванная", "кровать в спальне")


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def route(flow: pfl.LiveFlow, start: int, goal: int) -> list[int]:
    """Клетки от start до goal: каждый шаг в достижимую за срез клетку, ближайшую по проходам к цели."""
    distances, reach = flow.world.distances(), flow.world.reach(pfl.pm.LOCALITY_M).tocsr()
    path, here = [start], start
    while here != goal and len(path) < 500:
        neighbours = reach[here].indices
        here = int(neighbours[np.argmin(distances[neighbours, goal])])
        path.append(here)
    return path


def trajectory(flow: pfl.LiveFlow, places: dict[str, tuple[float, float]], stand_s: int) -> list[tuple[int, str | None]]:
    """По секундам: (клетка, место, если стоит; None, если идёт)."""
    cells = [(name, flow.world.cell_at(*places[name])) for name in WALK_SEQUENCE if name in places]
    out: list[tuple[int, str | None]] = []
    for (name, cell), (_, next_cell) in zip(cells, cells[1:] + [cells[-1]]):
        out += [(cell, name)] * stand_s
        out += [(c, None) for c in route(flow, cell, next_cell)[1:-1]]
    return out


def prior_field(world, centres: list[tuple[float, float]]) -> np.ndarray:
    """Карта предпочтений: PRIOR_SHARE массы поровну вокруг центров, остальное ровно."""
    near = sum(np.exp(-0.5 * (np.linalg.norm(world.points - np.array(c), axis=1) / 1.5) ** 2) for c in centres)
    return PRIOR_SHARE * near / near.sum() + (1 - PRIOR_SHARE) / len(world)


def simulate(noise_db: float, weight: float, window: int, seed: int, stand_s: int, prior: str = "false", cold: bool = False, sigma: float | None = None, delta_weight: float = 0.0) -> dict:
    layout, habits = load(LAYOUT), load(HABITS)
    places = {name: (xy[0], xy[1]) for name, xy in habits["place_xy"].items()}
    models = pfl.resolve_models(load(MODELS))
    key = next(k for k in models if k.endswith("phy1-ap0"))
    name, iface = key.rsplit("@", 1)
    holder: dict = {}                                             # поле ложного предпочтения: мир есть только у потока, поэтому поле кладётся после его создания
    extra = [pp.Preference(lambda now: holder["field"], weight=weight)] if weight > 0 else []
    flow = pfl.LiveFlow(layout, models, {}, {}, randomness=Randomness(seed), window_slices=window, output_factors=extra, motion_sigma_m=sigma, delta_weight=delta_weight)
    expected = pfl.expected_db(flow.world, layout, models[key])
    false_name = next(p for p in places if p not in WALK_SEQUENCE)
    centres = [places[false_name]] if prior == "false" else [places[p] for p in dict.fromkeys(WALK_SEQUENCE)]
    holder["field"] = prior_field(flow.world, centres)
    rng = np.random.default_rng(seed)
    track = trajectory(flow, places, stand_s)
    top1 = total = 0
    located = False
    errors = []
    for second, (cell, label) in enumerate(track):
        now = START_UTC + second
        if rng.random() < DROPOUT_SHARE:
            flow.observe([], now)
            continue
        level = float(expected[cell] + rng.normal(0.0, noise_db))
        flow.observe([rs.Station("synthetic", iface, level, 0, name)], now)
        if not located and not cold:                                  # первое положение известно: путь продолжается от него
            located = flow.locate(name, iface, *flow.world.points[cell])
        point = flow.point_of(name, iface)
        if label is not None and point is not None and second >= window:
            total += 1
            top1 += np.hypot(point["x"] - places[label][0], point["z"] - places[label][1]) <= MATCH_RADIUS_M
            errors.append(float(np.hypot(point["x"] - places[label][0], point["z"] - places[label][1])))
    return {"top1": top1 / total if total else 0.0, "median_error_m": float(np.median(errors)) if errors else float("nan"), "seconds": total}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--stand", type=int, default=DEFAULT_STAND_S)
    parser.add_argument("--noise", default=",".join(str(n) for n in DEFAULT_NOISE_DB))
    parser.add_argument("--weights", default=",".join(str(w) for w in DEFAULT_WEIGHTS))
    parser.add_argument("--windows", default=",".join(str(w) for w in DEFAULT_WINDOWS))
    parser.add_argument("--prior", choices=("false", "true"), default="false")
    parser.add_argument("--sigmas", default="none", help="инерция перехода, м, через запятую; none - равномерно")
    parser.add_argument("--cold", action="store_true", help="холодный старт: начальная точка потоку неизвестна")
    parser.add_argument("--deltas", default="0", help="веса разности уровня в переходе через запятую; 0 - без разности")
    args = parser.parse_args(argv)
    print(f"предпочтение: {args.prior}, старт: {'холодный' if args.cold else 'с известной точки'}")
    print(f"{'инерция':>8} {'разность':>9} {'шум, дБ':>8} {'вес предпочтения':>26} {'окно':>5} {'точка у места':>14} {'медианная ошибка, м':>20} {'секунд стоянок':>15}")
    for sigma in (None if x == "none" else float(x) for x in args.sigmas.split(",")):
      for delta in (float(x) for x in args.deltas.split(",")):
        for noise in (float(x) for x in args.noise.split(",")):
          for weight in (float(x) for x in args.weights.split(",")):
            for window in (int(x) for x in args.windows.split(",")):
                result = simulate(noise, weight, window, args.seed, args.stand, args.prior, args.cold, sigma, delta)
                print(f"{'равном.' if sigma is None else sigma:>8} {delta:9.2f} {noise:8.1f} {weight:26.2f} {window:5d} {100 * result['top1']:13.1f}% {result['median_error_m']:20.2f} {result['seconds']:15d}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
