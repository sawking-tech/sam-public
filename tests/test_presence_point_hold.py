"""Липкость пятна точки (SAM-118): пятно, где стоит точка, уступает другому только при кратном перевесе веса несколько тиков подряд; без прошлой точки и вне пятен - обычный выбор.
Мир выдуманный: коридор из десяти клеток с шагом 1 м (длиннее склейки пятен 3,6 м), два пятна по краям."""
from datetime import datetime

import numpy as np
import pytest

from radar import presence_cloud as pc
from radar import presence_motion as pm
from radar import presence_subject as ps
from radar import presence_window as pw_
from radar import presence_world as pw
from tests.fakes_presence import CorridorNavigation

NOW = datetime(2026, 1, 5, 12, 0)
CELLS = 10
STEP_M = 1.0


@pytest.fixture(scope="module")
def corridor():
    return pw.World(CorridorNavigation(CELLS), step_m=STEP_M)


def lobes(left_share: float) -> list[pc.Lobe]:
    return sorted([pc.Lobe((0, 1), left_share, 0), pc.Lobe((8, 9), 1.0 - left_share, 9)], key=lambda lobe: -lobe.share)


LEFT_CELLS, RIGHT_CELLS, MIDDLE_CELLS = frozenset((0, 1)), frozenset((8, 9)), frozenset((5, 6))
MIDDLE_CHALLENGER = [pc.Lobe((0, 1), 0.05, 0), pc.Lobe((5, 6), 0.95, 5)]


@pytest.mark.parametrize("ticks, previous, hold, centres, final", [
    ([lobes(0.3)], None, pc.Hold(), [9], pc.Hold(RIGHT_CELLS)),                                        # без прошлой точки - тяжелейшее пятно
    ([lobes(0.45)], 0, pc.Hold(LEFT_CELLS), [0], pc.Hold(LEFT_CELLS)),                                  # 0,55 против 0,45 с близостью: перевеса вдвое нет, держится
    ([lobes(0.05)] * 3, 0, pc.Hold(LEFT_CELLS), [0, 0, 9], pc.Hold(RIGHT_CELLS)),                       # претендент берёт точку после серии из трёх тиков
    ([lobes(0.05)], 0, pc.Hold(LEFT_CELLS), [0], pc.Hold(LEFT_CELLS, RIGHT_CELLS, 1)),                  # первый тик серии
    ([lobes(0.05), lobes(0.6)], 0, pc.Hold(LEFT_CELLS), [0, 0], pc.Hold(LEFT_CELLS)),                   # претендент пропал: счёт сброшен
    ([MIDDLE_CHALLENGER], 0, pc.Hold(LEFT_CELLS, RIGHT_CELLS, 2), [0], pc.Hold(LEFT_CELLS, MIDDLE_CELLS, 1)),   # другой претендент: счёт с единицы
    ([lobes(0.3)], 4, pc.Hold(), [9], None),                                                            # далёкая точка без памяти - обычный выбор (choose_lobe)
    ([lobes(0.3)], 4, pc.Hold(LEFT_CELLS), [0], None),                                                  # с памятью далёкое пятно своё: точка ещё идёт к нему шагом
    ([lobes(0.05)], 2, pc.Hold(), [0], pc.Hold(LEFT_CELLS, RIGHT_CELLS, 1)),                            # пятно в метре от точки - своё, хоть точка не в его клетках
    ([[]], 4, pc.Hold(), [None], pc.Hold()),                                                            # пятен нет - нет выбора
], ids=["no-previous", "below-ratio", "streak-wins", "streak-starts", "challenger-gone", "challenger-changes",
        "far-no-memory", "far-with-memory", "nearest-in-reach", "no-lobes"])
def test_point_keeps_its_lobe_until_a_challenger_wins_for_several_ticks(corridor, ticks, previous, hold, centres, final):
    """Пятно, где стоит точка, уступает претенденту только при перевесе вдвое три тика подряд."""
    got = []
    for tick in ticks:
        lobe, hold = pc.hold_lobe(tick, corridor.distances(), previous, hold, ratio=2.0, ticks=3)
        got.append(lobe.centre if lobe else None)
    assert got == centres
    if final is not None:
        assert hold == final


class Feed:
    """Поле свидетельства, которое тест задаёт по тикам: вход фактора вместо измерения роутера."""

    def __init__(self):
        self.field = None

    def log_field(self, context):
        return self.field


def test_subject_point_ignores_mode_flicker_and_walks_to_a_lasting_move(corridor):
    """Масса делится между левым и правым краем 82 на 18 и перевес меняет сторону раз в тик: без липкости точка летала бы по 4 м, с ней стоит; устойчивый перенос массы точку переводит.
    Пятно, где стоит точка, должно существовать: если его масса исчезла совсем, держаться нечему, и точка идёт за весом."""
    feed = Feed()
    window = pw_.Window(corridor, pm.Motion(locality_m=1.1, jump_share=0.0), [feed], size=1, start_certainty=0.0)
    subject = ps.Subject("phone", window, samples=200, rng=np.random.default_rng(3), hold_ratio=2.0, hold_ticks=3, step_m=1.1)
    left, right = np.full(CELLS, -30.0), np.full(CELLS, -30.0)
    left[:2], left[8:], right[8:], right[:2] = 0.0, -1.5, 0.0, -1.5
    points = []
    for k in range(8):
        feed.field = left if k % 2 == 0 else right
        points.append(subject.step(NOW).point)
    assert points[0] in (0, 1) and all(p in (0, 1) for p in points)          # перескоки моды раз в тик точку не двигают
    for _ in range(12):
        feed.field = right
        points.append(subject.step(NOW).point)
    assert points[-1] in (8, 9) and points[9] in (0, 1)                       # перенос массы на три тика подряд переводит точку
    assert all(abs(a - b) <= 1.1 for a, b in zip(points, points[1:]))         # и она идёт шагом, не дальше 1,1 м за тик
    g = corridor.distances()
    assert pc.step_towards(None, 9, g, 1.4) == 9 and pc.step_towards(8, 9, g, 1.4) == 9      # без прошлой точки и в шаге от цели - сразу в цель
    walk, cell = [], 0
    for _ in range(12):
        cell = pc.step_towards(cell, 9, g, 1.4)
        walk.append(cell)
    assert walk[:3] == [1, 2, 3] and walk[-1] == 9                                          # к далёкой цели - по клетке за шаг
