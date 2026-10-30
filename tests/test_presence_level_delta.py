"""Разность уровня как свидетельство о переходе (SAM-118): пара клеток одного кольца различается по рисунку подхода, стояние даёт нули, матрица своя у среза.
Мир выдуманный: коридор из пяти клеток с шагом 0,3 м, ожидаемый уровень падает на 3 дБ на клетку; роутер слева."""
from datetime import datetime

import numpy as np
import pytest

from radar import presence_factors as pf
from radar import presence_level_delta as pld
from radar import presence_motion as pm
from radar import presence_window as pw_
from radar import presence_world as pw

NOW = datetime(2026, 1, 5, 12, 0)
STEP_M = 0.3
RADIUS_M = 0.35          # соседи через одну клетку: шаг на клетку или стояние
EXPECTED_DB = np.array([-40.0, -43.0, -46.0, -49.0, -52.0])
QUIET_SD_DB = 1.4        # разброс разности у тихого (неподвижного) устройства: тесты проверяют механизм, а не шум носимого телефона (5,5 по умолчанию)
NEGLIGIBLE = 1e-100


class CorridorNavigation:
    def stand_nodes(self, step_m):
        return [(x * step_m, 0.0) for x in range(len(EXPECTED_DB))]

    def segment_free(self, a, b):
        return True

    def snap(self, x, z):
        return (x, z)


class Feed:
    def __init__(self):
        self.field = None

    def log_field(self, context):
        return self.field


@pytest.fixture(scope="module")
def corridor():
    return pw.World(CorridorNavigation(), step_m=STEP_M)


def make(corridor, size=4, certainty=0.0, sd_db=QUIET_SD_DB, weight=pld.DELTA_WEIGHT):
    holder = {"level": None}
    delta = pld.LevelDelta(lambda: holder["level"], EXPECTED_DB, sd_db=sd_db, weight=weight)
    motion = pm.Motion(locality_m=RADIUS_M, jump_share=0.0, pairs=[delta])
    feed = Feed()
    feed.field = np.zeros(len(corridor))                                     # кольцо молчит: виден только рисунок разностей
    return pw_.Window(corridor, motion, [feed], size=size, start_certainty=certainty), holder, delta


def run(window, holder, levels):
    result = None
    for level in levels:
        holder["level"] = level
        result = window.step(NOW)
    return result


def _first_slice_and_gap(corridor):
    delta = pld.LevelDelta(lambda: -46.0, EXPECTED_DB)
    silent = pld.LevelDelta(lambda: None, EXPECTED_DB)
    # первый срез без разности, второй даёт ноль; молчащий уровень - разрыв, пары нет
    return delta.observe(NOW) is None and delta.observe(NOW) == pytest.approx(0.0) and silent.observe(NOW) is None


def _reset_forgets(corridor):
    delta = pld.LevelDelta(lambda: -46.0, EXPECTED_DB)
    delta.observe(NOW)
    delta.reset()
    return delta.observe(NOW) is None


def _window_reset_forgets(corridor):
    window, holder, delta = make(corridor, size=3)
    run(window, holder, [-52.0, -49.0])
    window.reset()
    holder["level"] = -46.0
    return delta.observe(NOW) is None


def _pair_by_direction(corridor):
    delta = pld.LevelDelta(lambda: None, EXPECTED_DB, weight=1.0)
    rows, cols = np.array([2, 2, 2]), np.array([1, 2, 3])                     # из клетки 2: к роутеру (+3 дБ), стоять (0), от роутера (-3 дБ)
    towards = delta.log_pairs(3.0, rows, cols)
    return (towards.argmax() == 0 and towards[0] == pytest.approx(0.0)
            and delta.log_pairs(0.0, rows, cols).argmax() == 1 and delta.log_pairs(None, rows, cols) is None)


def _bad_parameters_refused(corridor):
    for bad in ({"sd_db": 0.0}, {"weight": 0.0}):
        with pytest.raises(ValueError, match="положительные"):
            pld.LevelDelta(lambda: None, EXPECTED_DB, **bad)
    return True


@pytest.mark.parametrize("holds", [_first_slice_and_gap, _reset_forgets, _window_reset_forgets, _pair_by_direction, _bad_parameters_refused],
                         ids=["first-and-gap", "reset", "window-reset", "pair-direction", "bad-parameters"])
def test_level_delta_pair_evidence_follows_measured_change(corridor, holds):
    assert holds(corridor)


def test_zero_delta_keeps_a_standing_mass_in_its_cell(corridor):
    """Один переход из клетки 2 при разности нуль: без свидетельства масса делится поровну на стоять, влево и вправо; с ним шаг на 3 дБ поперёк узора штрафуется сам."""
    delta = pld.LevelDelta(lambda: None, EXPECTED_DB, sd_db=QUIET_SD_DB)
    motion, plain = pm.Motion(locality_m=RADIUS_M, jump_share=0.0, pairs=[delta]), pm.Motion(locality_m=RADIUS_M, jump_share=0.0)
    start = np.zeros(len(corridor))
    start[2] = 1.0
    held = np.exp(motion.log_field(pf.Context(world=corridor, now=NOW, previous=start, observed=(0.0,))))
    spread = np.exp(plain.log_field(pf.Context(world=corridor, now=NOW, previous=start)))
    assert spread[2] == pytest.approx(1 / 3) and held[2] > 0.8 and held.sum() == pytest.approx(1.0)
    assert held[1] == pytest.approx(held[3])                                  # штраф симметричен: вверх и вниз по уровню одинаково далеко от нуля


def test_window_tells_approach_direction_by_level_deltas(corridor):
    """Два пути приходят на один уровень -46 дБ: подход с -52 (снизу, от клетки 4) и с -40 (сверху, от клетки 0). Кольцо молчит: только разности говорят, откуда пришли."""
    window_up, holder_up, _ = make(corridor, size=3)
    up = run(window_up, holder_up, [-52.0, -49.0, -46.0])
    window_down, holder_down, _ = make(corridor, size=3)
    down = run(window_down, holder_down, [-40.0, -43.0, -46.0])
    paths_up = window_up.sample_paths(400, rng=np.random.default_rng(1))
    paths_down = window_down.sample_paths(400, rng=np.random.default_rng(1))
    assert np.mean(paths_up[:, 0] > paths_up[:, -1]) > 0.8                     # пришли с дальних клеток
    assert np.mean(paths_down[:, 0] < paths_down[:, -1]) > 0.8                 # пришли с ближних
    assert up.sum() == pytest.approx(1.0) and down.sum() == pytest.approx(1.0)


def transitions(window, holder, levels):
    """Что говорит переход каждому срезу после первого: публичный transition окна после каждого шага."""
    out = []
    for level in levels:
        holder["level"] = level
        window.step(NOW)
        out.append(window.transition)
    return out[1:]


def test_window_matrix_per_slice_matches_sampled_paths(corridor):
    # разности +3 и 0: у каждого среза свой переход
    window, holder, _ = make(corridor, size=3)
    moved, held = transitions(window, holder, [-52.0, -49.0, -49.0])
    assert not np.allclose(moved, held)
    # сглаживание сходится с выборкой путей
    gammas = window.smoothed()
    paths = window.sample_paths(6000, rng=np.random.default_rng(2))
    for k, gamma in enumerate(gammas):
        assert np.bincount(paths[:, k], minlength=len(corridor)) / 6000 == pytest.approx(gamma, abs=0.03)
    # молчащий уровень оставляет базовую матрицу: переходы те же, что у окна без разностей
    silent, silent_holder, _ = make(corridor, size=3)
    feed = Feed()
    feed.field = np.zeros(len(corridor))
    plain = pw_.Window(corridor, pm.Motion(locality_m=RADIUS_M, jump_share=0.0), [feed], size=3, start_certainty=0.0)
    levels = [None, -46.0, None]
    for got, base in zip(transitions(silent, silent_holder, levels), transitions(plain, {"level": None}, levels)):
        assert np.allclose(got, base)


GRID_POINTS = np.array([(x * STEP_M, z * STEP_M) for z in range(3) for x in range(3)])     # квадрат 3 x 3, клетка 4 в середине
GRID_DB = -40.0 - 10.0 * GRID_POINTS[:, 0]                                                 # уровень падает с x: 3 дБ на клетку, роутер слева


@pytest.mark.parametrize("levels, points, cell, neighbours, expected", [
    # уровень растёт к меньшему x: к роутеру, 10 дБ/м
    (GRID_DB, GRID_POINTS, 4, np.arange(9), (-1.0, 0.0, 10.0)),
    # без плоскости - ноль: нет соседей, один сосед, соседи на одной прямой
    (GRID_DB, GRID_POINTS, 4, np.array([4]), (0.0, 0.0, 0.0)),
    (GRID_DB, GRID_POINTS, 4, np.array([4, 5]), (0.0, 0.0, 0.0)),
    (EXPECTED_DB, np.array([(x * STEP_M, 0.0) for x in range(len(EXPECTED_DB))]), 2, np.array([1, 2, 3]), (0.0, 0.0, 0.0)),
], ids=["towards-router", "alone", "one-neighbour", "collinear"])
def test_ascent_points_to_router_or_is_zero_without_a_plane(levels, points, cell, neighbours, expected):
    assert pld.ascent(levels, points, cell, neighbours) == pytest.approx(expected)
