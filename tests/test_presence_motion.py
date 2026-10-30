"""Переход между срезами (SAM-118): клетка достижима, если отрезок свободен и не длиннее радиуса; через стену масса не идёт. Миры выдуманные: коридор
из 41 клетки с шагом 0,3 м и два ряда по три клетки в метре друг от друга, между рядами стена."""
from datetime import datetime

import numpy as np
import pytest

from radar import presence_factors as pf
from radar import presence_motion as pm
from radar import presence_world as pw

NOW = datetime(2026, 1, 5, 12, 0)
STEP_M = 0.3
CORRIDOR_CELLS = 41
NEGLIGIBLE = 1e-100         # меньше этого масса считается нулём


class CorridorNavigation:
    def stand_nodes(self, step_m):
        return [(x * step_m, 0.0) for x in range(CORRIDOR_CELLS)]

    def segment_free(self, a, b):
        return True

    def snap(self, x, z):
        return (x, z)


class TwoRowsNavigation:
    def stand_nodes(self, step_m):
        return [(x * step_m, z) for z in (0.0, 1.0) for x in range(3)]

    def segment_free(self, a, b):
        return (a[1] < 0.5) == (b[1] < 0.5)

    def snap(self, x, z):
        return (x, z)


@pytest.fixture(scope="module")
def corridor():
    return pw.World(CorridorNavigation(), step_m=STEP_M)


@pytest.fixture(scope="module")
def two_rows():
    return pw.World(TwoRowsNavigation(), step_m=STEP_M)


def after_one_slice(motion, world, previous):
    return np.exp(motion.log_field(pf.Context(world=world, now=NOW, previous=previous)))


def support(p):
    """Клетки с заметной массой: нуль в потоке представлен полом логарифма 1e-300, а не точным нулём."""
    return p > NEGLIGIBLE


def at_cell(world, cell):
    start = np.zeros(len(world))
    start[cell] = 1.0
    return start


CENTRE = CORRIDOR_CELLS // 2


def _silent_without_past(world, motion):
    return motion.log_field(pf.Context(world=world, now=NOW)) is None


def _within_radius(world, motion):
    result = after_one_slice(motion, world, at_cell(world, CENTRE))
    near = [i for i in range(len(world)) if abs(world.points[i, 0] - world.points[CENTRE, 0]) <= 1.0 + 1e-9]
    return result[near].sum() == pytest.approx(1.0) and result.sum() == pytest.approx(1.0) and support(result)[CENTRE] and not support(result)[0]


def _reach(world, locality_m):
    return support(after_one_slice(pm.Motion(locality_m=locality_m, jump_share=0.0), world, at_cell(world, CENTRE))).sum()


def _wider_reaches_farther(world, motion):
    return _reach(world, 2.0) > _reach(world, 0.5)


def _wall_stops(world, motion):
    """Ряды в метре друг от друга, радиус полтора метра, но отрезок между рядами пересекает стену."""
    result = after_one_slice(motion, world, at_cell(world, 0))
    other_row = [i for i in range(len(world)) if world.point_of(i)[1] > 0.5]
    return not support(result)[other_row].any() and result.sum() == pytest.approx(1.0)


def _jump_everywhere(world, motion):
    result = after_one_slice(motion, world, at_cell(world, 0))
    return bool(support(result).all()) and result.sum() == pytest.approx(1.0)


def _conserved_over_many_slices(world, motion):
    mass = at_cell(world, CENTRE)
    for _ in range(14):
        mass = after_one_slice(motion, world, mass)
    return mass.sum() == pytest.approx(1.0)


@pytest.mark.parametrize("world_name, motion, holds", [
    ("corridor", pm.Motion(), _silent_without_past),                           # нет прошлого - молчит
    ("corridor", pm.Motion(locality_m=1.0, jump_share=0.0), _within_radius),   # масса не дальше радиуса
    ("corridor", None, _wider_reaches_farther),                                # шире радиус - дальше
    ("two_rows", pm.Motion(locality_m=1.5, jump_share=0.0), _wall_stops),     # стена держит даже внутри радиуса
    ("corridor", pm.Motion(locality_m=0.5, jump_share=0.2), _jump_everywhere),  # доля прыжка оставляет массу везде
    ("corridor", pm.Motion(), _conserved_over_many_slices),                    # масса сохраняется за окно
], ids=["silent", "radius", "wider", "wall", "jump", "conserved"])
def test_motion_spreads_mass_within_radius_and_not_through_walls(request, world_name, motion, holds):
    world = request.getfixturevalue(world_name)
    assert holds(world, motion)


def test_motion_into_matches_forward_transition(corridor, two_rows):
    motion = pm.Motion(locality_m=0.7, jump_share=0.0)
    forward = np.exp(motion.log_field(pf.Context(world=corridor, now=NOW, previous=at_cell(corridor, 10))))
    assert forward[11] == pytest.approx(motion.into(corridor, 11)[10], abs=1e-9)
    assert motion.into(corridor, 11)[40] == 0.0                         # далеко: перехода нет
    # по умолчанию прыжка нет и за стену столбец не доходит, с долей прыжка - доходит везде
    assert (pm.Motion(locality_m=1.5).into(two_rows, 3)[:3] == 0).all()
    assert (pm.Motion(locality_m=1.5, jump_share=0.1).into(two_rows, 3) > 0).all()
