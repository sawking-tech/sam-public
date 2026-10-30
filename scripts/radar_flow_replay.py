"""Проигрыш записи треков через поток положения (SAM-118): уровни устройства идут в окно путей, оценка сравнивается с местом стоянки по таймлайну проходок.

Для каждой пары «длина окна, уверенность в начале» считает по секундам стоянок: долю верных вершин и троек среди названных мест, массу распределения вокруг
настоящего места и площадь, в которой лежит 90% массы. Окно в один срез это решение по одному текущему уровню, остальные окна показывают, что даёт путь.
Запись содержит отсчёт раз в секунду, поэтому срез здесь секунда, а не 0,5 с живого опроса. Радиальная модель подогнана по тем же стоянкам, цифры оптимистичны.

Запуск: python scripts/radar_flow_replay.py [baselines|start|sweep N...|inertia|delta|topview] [tracks.jsonl] [timeline.json] [layout.json] [radio_model.json]"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))        # запуск из любой папки: корень репозитория на пути импорта
from radar import presence_flow_live as pfl                      # noqa: E402
from radar import presence_level_delta as pld                    # noqa: E402
from radar import presence_motion as pm                          # noqa: E402
from radar import presence_preference as pp                      # noqa: E402
from radar import presence_report as prp                          # noqa: E402
from radar.presence_factors import Context                       # noqa: E402
from radar import presence_rings as pr                           # noqa: E402
from radar import presence_subject as ps                         # noqa: E402
from radar import presence_topview as ptv                        # noqa: E402
from radar import presence_window as pw_                         # noqa: E402
from radar import presence_world as pw                           # noqa: E402
from radar.presence_nav import Nav                               # noqa: E402
from radar.presence_radio_map import WallModel, doorway_openings, expected_levels   # noqa: E402
from radar_replay import DEFAULT_PREFS, DEVICE_PART, IFACE, clock_to_ts, load, load_optional, read_levels   # noqa: E402

DEFAULT_TRACKS = "data/presence_tracks/tracks-20261010.jsonl"
DEFAULT_TIMELINE = "data/radar_walk_timeline.json"
DEFAULT_LAYOUT = "data/home_layout.json"
DEFAULT_MODEL = "data/presence_radio_model.json"
DEFAULT_FINGERPRINTS = "data/presence_fingerprints.json"
DEFAULT_CONTROLS = "data/radio_controls.json"
DEFAULT_HABITS = "data/home_habits.json"             # места привычек с координатами: из них строится поле предпочтений телефона
SLICE_S = 1.0                  # срез проигрыша: запись держит отсчёт раз в секунду
MEAN_SPEED_MPS = 1.39          # средний шаг человека, 5 км/ч: из него радиус локальности за срез
WINDOWS_S = (1, 3, 7, 14, 28)  # длины окна в срезах; 1 - решение по одному уровню
RING_WEIGHTS = (0.05, 0.1, 0.3, 1.0)       # веса среза колец для подбора: 0,05 из потока по умолчанию, 0,3 прежний вес блока 3 с
CERTAINTIES = (0.0, 0.5, 1.0)  # уверенность в начале окна
START_WINDOWS = (7, 60)        # окна режима start: прежнее и минутное
START_CERTAINTIES = (0.5, 0.9, 0.95)       # доля прошлого в начале окна
START_MIN_SHARES = (0.0, 0.1, 0.3)         # отсечение слабых клеток начала: доля максимума распределения выпавшей секунды
MOTION_CASES = ((None, 0.0), (0.35, 0.0), (None, 0.5), (None, 0.8), (None, 0.9), (None, 0.95))    # режим inertia: (инерция, м; доля стояния)
DELTA_CASES = ((0.0, pld.DELTA_SD_DB), (1.0, 1.0), (1.0, 1.4), (1.0, 2.0), (1.0, 3.0), (1.0, pld.DELTA_SD_DB), (1.0, 8.0), (0.5, pld.DELTA_SD_DB), (0.3, pld.DELTA_SD_DB))   # режим delta: (вес разности уровня; разброс, дБ); первая строка - без разности, 1,4 - разброс неподвижного устройства, большой разброс сходится к первой строке
PREFERENCE_WEIGHTS = (1.0, 0.5, 0.25, 0.1)   # режим baselines: вес карты предпочтений телефона
SWEEP_CASES = ((0.5, 0.0), (0.95, 0.3))    # режим sweep: прежний старт и боевой, по длинам окна
MATCH_RADIUS_M = 1.5           # пятно рядом с настоящим местом, если его центр не дальше этого
PINGPONG_S = 5                 # смена вершины с возвратом на прежнее место за столько секунд - качели, а не переход
ARRIVAL_HOLD_S = 3             # отставание после прихода: вершина верна столько секунд подряд
EDGE_S = 3                     # секунд по краям стоянки не считаются: отметка человека запаздывает
WARMUP_S = 60                  # секунд проигрыша до первой стоянки, чтобы окно заполнилось
MASS_SHARE = 0.9               # доля массы для площади области
SAMPLES = 100                  # выборок на шаг, как в потоке
SEED = 1
WINDOW_LIVE = 14        # боевое окно: режимы baselines и inertia считают на нём
WINDOW_DEFAULT = 7     # окно проигрыша для калибровки: прежний размер, чтобы цифры сравнивались; боевое окно задаёт SAM_PRESENCE_WINDOW_SLICES


def pick_model(models: dict, device_part: str, iface: str) -> dict:
    """Подогнанная модель для устройства прогона и диапазона записи."""
    for key, fitted in models.items():
        if device_part in key.lower() and key.endswith(f"@{iface}"):
            return fitted
    raise SystemExit(f"в {DEFAULT_MODEL} нет модели для устройства «{device_part}» на {iface}: подгони её скриптом radar_radio_fit.py")


def expected_db(world: pw.World, layout: dict, fitted: dict) -> np.ndarray:
    model = WallModel(fitted["l0_db"], fitted["n"], fitted["wall_db_per_m"], fitted.get("near_gain_db", 0.0))
    router = (layout["router"]["x"], layout["router"]["z"])
    return expected_levels(model, world.points, layout["walls"], router, doorway_openings(layout["objects"]))


def truth_places(timeline: dict) -> dict[int, tuple[float, float]]:
    """Секунда стоянки (края отрезаны) -> координаты настоящего места."""
    out = {}
    for stand in timeline["stands"]:
        spot = timeline["places"][stand["place"]]
        a, b = clock_to_ts(timeline["date"], stand["from"]), clock_to_ts(timeline["date"], stand["to"])
        for sec in range(int(a) + EDGE_S, int(b) - EDGE_S + 1):
            out[sec] = (spot["x"], spot["z"])
    return out


def cells_near(world: pw.World, xy: tuple[float, float]) -> np.ndarray:
    return np.nonzero(np.hypot(world.points[:, 0] - xy[0], world.points[:, 1] - xy[1]) <= MATCH_RADIUS_M)[0]


def arrival_lags(series: dict[int, ps.Estimate], timeline: dict, world: pw.World, candidates: dict[str, tuple[float, float]]) -> float:
    """Среднее отставание после прихода на место: секунд от начала стоянки до первой секунды, с которой вершина верна ARRIVAL_HOLD_S секунд подряд; не нашла - вся длина стоянки. Первая стоянка не считается: прихода к ней нет."""
    names = list(candidates)
    around = [cells_near(world, candidates[name]) for name in names]
    lags = []
    for stand in timeline["stands"][1:]:
        spot = timeline["places"][stand["place"]]
        a, b = int(clock_to_ts(timeline["date"], stand["from"])), int(clock_to_ts(timeline["date"], stand["to"]))
        right = {}
        for sec in range(a, b + 1):
            if sec in series:
                best = names[int(np.argmax([series[sec].distribution[cells].sum() for cells in around]))]
                right[sec] = bool(np.hypot(candidates[best][0] - spot["x"], candidates[best][1] - spot["z"]) <= MATCH_RADIUS_M)
        first = next((sec for sec in range(a, b - ARRIVAL_HOLD_S + 2) if all(right.get(sec + k) for k in range(ARRIVAL_HOLD_S))), None)
        lags.append((b if first is None else first) - a)
    return sum(lags) / len(lags) if lags else 0.0


def area_m2(distribution: np.ndarray, step_m: float, share: float = MASS_SHARE) -> float:
    """Площадь самой малой области, где лежит доля share массы: клетки по убыванию вероятности."""
    ordered = np.sort(distribution)[::-1]
    return float((np.searchsorted(np.cumsum(ordered), share) + 1) * step_m ** 2)


def score(series: dict[int, ps.Estimate], truth: dict[int, tuple[float, float]], world: pw.World, candidates: dict[str, tuple[float, float]]) -> dict[str, float]:
    """Метрики по секундам стоянок. Вершина и тройка: места из candidates по массе вокруг них, верно, если место не дальше MATCH_RADIUS_M от настоящего (как
    выбор из названных мест у прежнего радара). Устойчивость: доля секунд, где вершина не сменилась с прошлой секунды; качели: доля смен, после которых вершина вернулась на прежнее место за PINGPONG_S секунд (хаотичное мигание, в отличие от перехода). Масса вокруг места, площадь 90% и доля массы у равномерного распределения для сравнения."""
    secs = [s for s in sorted(truth) if s in series]
    names = list(candidates)
    around = [cells_near(world, candidates[name]) for name in names]
    near_truth = {xy: cells_near(world, xy) for xy in set(truth.values())}
    top1, top3, mass, areas, even, picked = [], [], [], [], [], {}
    for s in secs:
        p = series[s].distribution
        order = np.argsort([-p[cells].sum() for cells in around])
        right = [bool(np.hypot(candidates[names[i]][0] - truth[s][0], candidates[names[i]][1] - truth[s][1]) <= MATCH_RADIUS_M) for i in order]
        picked[s] = names[order[0]]
        top1.append(right[0])
        top3.append(any(right[:3]))
        mass.append(float(p[near_truth[truth[s]]].sum()))
        areas.append(area_m2(p, world.step_m))
        even.append(len(near_truth[truth[s]]) / len(world))
    pairs = [s for s in secs if s - 1 in picked]
    steady = sum(picked[s] == picked[s - 1] for s in pairs) / len(pairs) if pairs else 1.0        # доля секунд без смены вершины: мигание метки у неподвижного
    changes = [s for s in pairs if picked[s] != picked[s - 1]]
    swings = [s for s in changes if any(picked.get(s + k) == picked[s - 1] for k in range(1, PINGPONG_S + 1))]       # смена и возврат на прежнее место за PINGPONG_S секунд
    swing_share = len(swings) / len(changes) if changes else 0.0
    n = len(secs)
    return {"steady": steady, "swing": swing_share, "top1": sum(top1) / n, "top3": sum(top3) / n, "mass": sum(mass) / n, "area": sum(areas) / n, "even": sum(even) / n, "secs": float(n)}


def calibration(series: dict[int, ps.Estimate], truth: dict[int, tuple[float, float]], candidates: dict[str, tuple[float, float]],
                edges=(0.0, 0.2, 0.4, 0.6, 0.8, 1.01)) -> list[tuple[str, int, float]]:
    """Честность списка мест: по вероятности верхнего места сколько секунд и как часто оно верно (рядом с настоящим); у честного списка доля близка к самой вероятности."""
    rows = []
    for s in sorted(truth):
        places = series[s].places if s in series else []
        if places:
            name, p = places[0]
            rows.append((p, bool(np.hypot(candidates[name][0] - truth[s][0], candidates[name][1] - truth[s][1]) <= MATCH_RADIUS_M)))
    out = []
    for lo, hi in zip(edges, edges[1:]):
        picks = [right for p, right in rows if lo <= p < hi]
        out.append((f"{lo:.1f}-{min(hi, 1.0):.1f}", len(picks), sum(picks) / len(picks) if picks else float("nan")))
    return out


def phone_preference(prefs: dict, place_xy: dict, world: pw.World, weight: float = 1.0, layout: dict | None = None) -> pp.Preference:
    """Карта предпочтений телефона проигрыша: та же, что у живого потока (`presence_flow_live.phone_preference`), с планом - по зонам сверху."""
    return pfl.phone_preference(prefs, DEVICE_PART, place_xy, world, weight, layout)


def run(world: pw.World, expected: np.ndarray, levels: dict, t0: int, t1: int, window_slices: int, certainty: float, output=(), ring_weight: float = pr.SLICE_WEIGHT, start_min_share: float = 0.0, motion_sigma: float | None = None, stay_share: float = 0.0, report: prp.PlaceReport | None = None,
        delta_weight: float = 0.0, delta_sd_db: float = pld.DELTA_SD_DB) -> dict[int, ps.Estimate]:
    """Один прогон: секунда -> оценка; нет уровня в эту секунду - свидетельство молчит, окно всё равно сдвигается. delta_weight больше нуля подключает разность уровня к переходу."""
    current: dict[str, float | None] = {"level": None}
    rings = pr.Rings(lambda: current["level"], expected, weight=ring_weight)
    pairs = [pld.LevelDelta(lambda: current["level"], expected, sd_db=delta_sd_db, weight=delta_weight)] if delta_weight > 0 else []
    motion = pm.Motion(locality_m=MEAN_SPEED_MPS * SLICE_S, sigma_m=motion_sigma, stay_share=stay_share, pairs=pairs)
    window = pw_.Window(world, motion, [rings], size=window_slices, start_certainty=certainty, start_min_share=start_min_share)
    subject = ps.Subject("phone", window, output, samples=SAMPLES, rng=np.random.default_rng(SEED), report=report)
    out = {}
    for sec in range(t0, t1 + 1):
        current["level"] = levels[sec][1] if sec in levels else None
        out[sec] = subject.step(datetime.fromtimestamp(sec))
    return out


def point_estimate(world: pw.World, xy: tuple[float, float]) -> ps.Estimate:
    """Оценка «стою здесь»: масса поровну на клетках вокруг места, для базовых методов, у которых нет распределения."""
    cells = cells_near(world, xy)
    distribution = np.zeros(len(world))
    distribution[cells] = 1.0 / len(cells)
    return ps.Estimate(distribution, [])


def baseline_series(kind: str, world: pw.World, levels: dict, t0: int, t1: int, expected: np.ndarray, candidates: dict, fingerprints: dict, preference_field: np.ndarray) -> dict[int, ps.Estimate]:
    """Базовые методы без окна и без фильтра. prior: всегда самое предпочитаемое место, сигнал не смотрится. fingerprint: место с ближайшим средним уровнем отпечатка телефона на этом диапазоне. model: клетка, где ожидаемый
    радио-моделью уровень ближе всего к измеренному, один срез."""
    out = {}
    best_prior = preference_field / preference_field.sum()
    for sec in range(t0, t1 + 1):
        if kind == "prior":
            out[sec] = ps.Estimate(best_prior, [])
        elif sec in levels:
            level = levels[sec][1]
            if kind == "fingerprint":
                name = min(fingerprints, key=lambda place: abs(fingerprints[place] - level))
                out[sec] = point_estimate(world, candidates[name])
            elif kind == "model":
                out[sec] = ps.Estimate(_normalised_gauss(expected, level), [])
    return out


def _normalised_gauss(expected: np.ndarray, level: float, sd_db: float = pr.RING_SD_DB) -> np.ndarray:
    weights = np.exp(-0.5 * ((level - expected) / sd_db) ** 2)
    return weights / weights.sum()


def fingerprint_means(fingerprints: dict, key: str, candidates: dict) -> dict[str, float]:
    """Средний уровень отпечатка по местам, у которых известны координаты: место -> дБ."""
    lowered = {name.lower(): name for name in candidates}
    return {lowered[place.lower()]: float(np.mean([r if isinstance(r, (int, float)) else r[0] for r in rows])) for place, rows in fingerprints.get(key, {}).items() if place.lower() in lowered}


def main(argv: list[str]) -> int:
    start_mode = bool(argv) and argv[0] in ("start", "sweep")          # python scripts/radar_flow_replay.py start: сравнение стартов окна; sweep 5 7 10 ...: кривая по длине окна
    sweep = start_mode and argv[0] == "sweep"
    inertia = bool(argv) and argv[0] == "inertia"          # python scripts/radar_flow_replay.py inertia: инерция перехода при боевом старте
    baselines = bool(argv) and argv[0] == "baselines"          # python scripts/radar_flow_replay.py baselines: фильтр против простых методов
    delta = bool(argv) and argv[0] == "delta"              # python scripts/radar_flow_replay.py delta: разность уровня как свидетельство о переходе, вес и разброс
    topview = bool(argv) and argv[0] == "topview"          # python scripts/radar_flow_replay.py topview: мир сверху (подошвы низкой мебели это пол) против мира ходьбы
    argv = argv[1:] if (start_mode or baselines or inertia or delta or topview) else argv
    windows = tuple(int(a) for a in argv if a.isdigit()) or START_WINDOWS      # python scripts/radar_flow_replay.py start 14 30: окна режима start из аргументов
    argv = [a for a in argv if not a.isdigit()]
    if not DEVICE_PART:
        print("Не задано устройство прогона: добавь SAM_RADAR_REPLAY_DEVICE=часть имени в .env или окружение")
        return 2
    tracks, timeline_path, layout_path, model_path = (argv + [DEFAULT_TRACKS, DEFAULT_TIMELINE, DEFAULT_LAYOUT, DEFAULT_MODEL][len(argv):])[:4]
    timeline, layout = load(timeline_path), load(layout_path)
    world = pw.World(ptv.TopViewNavigation(Nav(layout)))              # боевой мир: сверху, подошвы низкой мебели это пол
    expected = expected_db(world, layout, pick_model(pfl.resolve_models(load(model_path)), DEVICE_PART, IFACE))
    t0, t1 = int(clock_to_ts(timeline["date"], timeline["from"])) - WARMUP_S, int(clock_to_ts(timeline["date"], timeline["to"]))
    levels = read_levels(tracks, DEVICE_PART, IFACE, t0, t1)
    truth = truth_places(timeline)
    print(f"клеток мира {len(world)}, секунд уровня {len(levels)}, секунд стоянок {len(truth)}, радиус локальности {MEAN_SPEED_MPS * SLICE_S:.2f} м за срез")
    habits = load(DEFAULT_HABITS)
    place_xy = {name: (xy[0], xy[1]) for name, xy in habits["place_xy"].items()}
    candidates = {**place_xy, **{name: (spot["x"], spot["z"]) for name, spot in timeline["places"].items()}}       # названные места привычек и стоянок прогона
    preference = phone_preference(load_optional(DEFAULT_PREFS), place_xy, world, layout=layout)
    print(f"{'окно, срезов':>13} {'уверенность':>12} {'предпочтения':>13} {'вершина':>9} {'тройка':>8} {'масса у места':>14} {'площадь 90%, м2':>16}")
    if topview:
        world_w = pw.World(Nav(layout))
        expected_w = expected_db(world_w, layout, pick_model(pfl.resolve_models(load(model_path)), DEVICE_PART, IFACE))
        print(f"{'мир':>28} {'клеток':>7} {'вершина':>8} {'тройка':>8} {'масса у места':>14} {'площадь 90%, м2':>16} {'устойчивость':>13} {'качели':>7} {'отставание, с':>14}")
        for label, w, e in (("ходьба (без мебели)", world_w, expected_w), ("сверху (низкая мебель - пол)", world, expected)):
            series = run(w, e, levels, t0, t1, WINDOW_LIVE, 0.95, (), start_min_share=0.3)
            result = score(series, truth, w, candidates)
            print(f"{label:>28} {len(w):7d} {100 * result['top1']:7.1f}% {100 * result['top3']:7.1f}% {100 * result['mass']:13.1f}% {result['area']:16.1f} {100 * result['steady']:12.1f}% {100 * result['swing']:6.1f}% {arrival_lags(series, timeline, w, candidates):14.1f}", flush=True)
        return 0
    if delta:
        print(f"{'вес разности':>13} {'разброс, дБ':>12} {'вершина':>8} {'тройка':>8} {'масса у места':>14} {'площадь 90%, м2':>16} {'устойчивость':>13} {'качели':>7} {'отставание, с':>14}")
        for weight, sd in DELTA_CASES:
            series = run(world, expected, levels, t0, t1, WINDOW_LIVE, 0.95, (), start_min_share=0.3, delta_weight=weight, delta_sd_db=sd)
            result = score(series, truth, world, candidates)
            print(f"{weight:13.2f} {sd:12.2f} {100 * result['top1']:7.1f}% {100 * result['top3']:7.1f}% {100 * result['mass']:13.1f}% {result['area']:16.1f} {100 * result['steady']:12.1f}% {100 * result['swing']:6.1f}% {arrival_lags(series, timeline, world, candidates):14.1f}", flush=True)
        return 0
    if inertia:
        print(f"{'инерция, м':>11} {'стояние':>8} {'вершина':>8} {'тройка':>8} {'масса у места':>14} {'площадь 90%, м2':>16} {'устойчивость':>13} {'качели':>7} {'отставание, с':>14}")
        for sigma, stay in MOTION_CASES:
            series = run(world, expected, levels, t0, t1, WINDOW_LIVE, 0.95, (), start_min_share=0.3, motion_sigma=sigma, stay_share=stay)
            result = score(series, truth, world, candidates)
            print(f"{'равномерно' if sigma is None else f'{sigma:g}':>11} {stay:8.2f} {100 * result['top1']:7.1f}% {100 * result['top3']:7.1f}% {100 * result['mass']:13.1f}% {result['area']:16.1f} {100 * result['steady']:12.1f}% {100 * result['swing']:6.1f}% {arrival_lags(series, timeline, world, candidates):14.1f}", flush=True)
        return 0
    if baselines:
        fingerprints = load(DEFAULT_FINGERPRINTS)
        control_xy = {name: (spot["x"], spot["z"]) for name, spot in load(DEFAULT_CONTROLS).items()}
        key = next((k for k in fingerprints if DEVICE_PART in k.lower() and k.endswith(f"@{IFACE}")), None)
        means = fingerprint_means(fingerprints, key, control_xy) if key else {}
        field = np.exp(preference.log_field(Context(world=world, now=datetime.fromtimestamp(t0))))
        print(f"{'метод':<42} {'вершина':>8} {'тройка':>8} {'масса у места':>14} {'площадь 90%, м2':>16} {'устойчивость':>13} {'качели':>7} {'отставание, с':>14}")
        names = list(candidates)
        around = {n: cells_near(world, candidates[n]) for n in names}
        methods = [
            ("только предпочтения (сигнал не смотрится)", baseline_series("prior", world, levels, t0, t1, expected, candidates, means, field)),
            ("ближайший отпечаток (в выборке, оптимистично)", baseline_series("fingerprint", world, levels, t0, t1, expected, {**candidates, **control_xy}, means, field)),
            ("только модель уровня, один срез", baseline_series("model", world, levels, t0, t1, expected, candidates, means, field)),
            ("фильтр 14, прежний старт, без предпочтений", run(world, expected, levels, t0, t1, 14, 0.5, ())),
            ("фильтр 14, боевой старт, без предпочтений", run(world, expected, levels, t0, t1, 14, 0.95, (), start_min_share=0.3)),
            ("фильтр 14, боевой старт, разность уровня в переходе", run(world, expected, levels, t0, t1, 14, 0.95, (), start_min_share=0.3, delta_weight=pld.DELTA_WEIGHT)),      # в бою выключена: строка показывает, во что обходится
            *[(f"фильтр 14, боевой старт, предпочтения с весом {w:g}", run(world, expected, levels, t0, t1, 14, 0.95, (phone_preference(load_optional(DEFAULT_PREFS), place_xy, world, w, layout),), start_min_share=0.3)) for w in PREFERENCE_WEIGHTS],
        ]
        for label, series in methods:
            result = score(series, truth, world, candidates)
            print(f"{label:<42} {100 * result['top1']:7.1f}% {100 * result['top3']:7.1f}% {100 * result['mass']:13.1f}% {result['area']:16.1f} {100 * result['steady']:12.1f}% {100 * result['swing']:6.1f}% {arrival_lags(series, timeline, world, candidates):14.1f}", flush=True)
        print(f"случайный выбор из {len(names)} мест: вершина {100 / len(names):.1f}%")
        return 0
    if start_mode:
        print(f"{'окно':>5} {'прошлое':>8} {'отсечение':>10} {'вершина':>9} {'тройка':>8} {'масса у места':>14} {'площадь 90%, м2':>16} {'устойчивость':>13} {'качели':>7} {'отставание, с':>14}")
        cases = SWEEP_CASES if sweep else [(c, s) for c in START_CERTAINTIES for s in START_MIN_SHARES]
        for window_slices in windows:
            for certainty, share in cases:
                series = run(world, expected, levels, t0, t1, window_slices, certainty, (), start_min_share=share)
                result = score(series, truth, world, candidates)
                print(f"{window_slices:5d} {certainty:8.2f} {share:10.2f} {100 * result['top1']:8.1f}% {100 * result['top3']:7.1f}% {100 * result['mass']:13.1f}% {result['area']:16.1f} {100 * result['steady']:12.1f}% {100 * result['swing']:6.1f}% {arrival_lags(series, timeline, world, candidates):14.1f}", flush=True)
        return 0
    even = None
    for window_slices in WINDOWS_S:
        for certainty in ((0.0,) if window_slices == 1 else CERTAINTIES):
            for label, output in (("нет", ()), ("есть", (preference,))):
                result = score(run(world, expected, levels, t0, t1, window_slices, certainty, output), truth, world, candidates)
                even = result["even"]
                print(f"{window_slices:13d} {certainty:12.1f} {label:>13} {100 * result['top1']:8.1f}% {100 * result['top3']:7.1f}% {100 * result['mass']:13.1f}% {result['area']:16.1f}")
    print(f"для сравнения: масса вокруг места при ровном распределении {100 * even:.1f}%")
    series = run(world, expected, levels, t0, t1, WINDOW_DEFAULT, 0.5, report=prp.PlaceReport(world, candidates))
    print("калибровка списка мест (верхняя вероятность: секунд, доля верных):")
    for name, n, share in calibration(series, truth, candidates):
        print(f"  {name}: {n:4d} с, {100 * share:5.1f}%")
    print("подбор веса среза колец (окно 14 срезов):")
    print(f"{'вес':>6} {'уверенность':>12} {'предпочтения':>13} {'вершина':>9} {'тройка':>8} {'площадь 90%, м2':>16}")
    for weight in RING_WEIGHTS:
        for certainty in CERTAINTIES:
            for label, output in (("нет", ()), ("есть", (preference,))):
                result = score(run(world, expected, levels, t0, t1, 14, certainty, output, ring_weight=weight), truth, world, candidates)
                print(f"{weight:6.2f} {certainty:12.1f} {label:>13} {100 * result['top1']:8.1f}% {100 * result['top3']:7.1f}% {result['area']:16.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
