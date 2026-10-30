"""Проигрыш потока на записи (SAM-118): площадь 90%, метрики по секундам стоянок и выбор модели.
Мир выдуманный: коридор из четырёх клеток через 4 м, так что в радиус «рядом с местом» попадает только своя клетка."""
import numpy as np
import pytest

from radar import presence_cloud as pc
from radar import presence_subject as ps
from radar import presence_world as pw
from tests.fakes_presence import CorridorNavigation, load_script

rfr = load_script("radar_flow_replay")
STEP_M = 4.0
AREA_STEP_M = 0.3
CANDIDATES = {"start": (0.0, 0.0), "end": (12.0, 0.0)}        # два названных места на концах коридора
WORLD = pw.World(CorridorNavigation(4), step_m=STEP_M)


def estimate(distribution, centre=0, places=()):
    return ps.Estimate(np.asarray(distribution, dtype=float), [pc.Lobe((centre,), 1.0, centre)], list(places))


AT_START, AT_END = estimate([0.7, 0.1, 0.1, 0.1], 0), estimate([0.1, 0.1, 0.1, 0.7], 3)


def truth(seconds):
    return {s: CANDIDATES["start"] for s in seconds}


def _score(series, seconds, key):
    return rfr.score(series, truth(seconds), WORLD, CANDIDATES)[key]


def _calibration():
    right, wrong = [("start", 0.9)], [("end", 0.9)]
    series = {10: estimate([1.0, 0, 0, 0], places=right), 11: estimate([1.0, 0, 0, 0], places=right),
              12: estimate([1.0, 0, 0, 0], places=right), 13: estimate([1.0, 0, 0, 0], places=wrong)}
    return rfr.calibration(series, truth(range(10, 14)), CANDIDATES)[-1][1:]


@pytest.mark.parametrize("actual, expected", [
    (lambda: rfr.area_m2(np.array([0.5, 0.3, 0.1, 0.1]), AREA_STEP_M, share=0.85), 3 * AREA_STEP_M ** 2),     # 0,5 + 0,3 + 0,1 покрывают 0,85
    (lambda: rfr.area_m2(np.array([0.6, 0.35, 0.03, 0.02]), AREA_STEP_M), 2 * AREA_STEP_M ** 2),
    (lambda: rfr.area_m2(np.array([1.0, 0.0, 0.0, 0.0]), AREA_STEP_M), AREA_STEP_M ** 2),
    (lambda: _score({10: AT_START, 11: AT_END}, [10, 11], "top1"), 0.5),           # вторая секунда: масса у end, верное место вторым
    (lambda: _score({10: AT_START, 11: AT_END}, [10, 11], "top3"), 1.0),
    (lambda: _score({10: AT_START, 11: AT_END}, [10, 11], "mass"), (0.7 + 0.1) / 2),   # вокруг места только клетка 0
    (lambda: _score({s: AT_START for s in (10, 11, 12)}, [10, 11, 12], "steady"), 1.0),
    (lambda: _score({10: AT_START, 11: AT_END, 12: AT_START}, [10, 11, 12], "steady"), 0.0),
    (lambda: _score(dict(zip(range(10, 16), [AT_START, AT_END] * 3)), range(10, 16), "swing"), 0.8),   # из пяти смен возврат у четырёх
    (lambda: _score(dict(zip(range(10, 16), [AT_START] * 3 + [AT_END] * 3)), range(10, 16), "swing"), 0.0),
    (lambda: _score({10: estimate([1.0, 0, 0, 0])}, [10, 11], "secs"), 1),            # секунда без оценки пропущена
    (_calibration, (4, 0.75)),                                                           # верхняя вероятность 0,9 верна в трёх из четырёх
], ids=["area_85", "area_90", "area_point", "top1", "top3", "mass", "steady_hold", "steady_flip", "swing_back", "swing_move", "skip", "calibration"])
def test_replay_score_metrics(actual, expected):
    assert actual() == pytest.approx(expected)


def test_replay_picks_model_by_device_and_band_or_exits():
    models = {"s24-ultra@phy1-ap0": {"n": 1}, "s24-ultra@phy0-ap0": {"n": 2}, "redmi@phy1-ap0": {"n": 3}}
    assert rfr.pick_model(models, "s24", "phy0-ap0") == {"n": 2}
    with pytest.raises(SystemExit):
        rfr.pick_model(models, "pixel", "phy1-ap0")
