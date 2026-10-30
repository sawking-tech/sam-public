"""Связь «рядом с другим субъектом» (SAM-118): смесь близости и фона, близость по проходам, стена её не пропускает. Мир выдуманный: два ряда клеток
по три через 0,3 м, между рядами стена."""
from datetime import datetime

import numpy as np
import pytest

from radar import presence_factors as pf
from radar import presence_near as pn
from radar import presence_world as pw

NOW = datetime(2026, 1, 5, 12, 0)
STEP_M = 0.3


class TwoRowsNavigation:
    def stand_nodes(self, step_m):
        return [(x * step_m, z) for z in (0.0, 1.0) for x in range(3)]

    def segment_free(self, a, b):
        return (a[1] < 0.5) == (b[1] < 0.5)

    def snap(self, x, z):
        return (x, z)


@pytest.fixture(scope="module")
def context():
    return pf.Context(world=pw.World(TwoRowsNavigation(), step_m=STEP_M), now=NOW)


def first_row_cell(context, x):
    return next(i for i in range(len(context.world)) if context.world.point_of(i) == (x * STEP_M, 0.0))


def other_at(context, x):
    mass = np.zeros(len(context.world))
    mass[first_row_cell(context, x)] = 1.0
    return mass


def _rows(context):
    this_row = [i for i in range(len(context.world)) if context.world.point_of(i)[1] < 0.5]
    other_row = [i for i in range(len(context.world)) if context.world.point_of(i)[1] > 0.5]
    return this_row, other_row


def _silent(context, near):
    return near.log_field(context) is None


def _closer_is_likelier(context, near):
    result = pf.fuse([near], context)
    cells = [first_row_cell(context, x) for x in range(3)]
    return result[cells[0]] > result[cells[1]] > result[cells[2]]


def _even(context, near):
    return pf.fuse([near], context) == pytest.approx([1 / len(context.world)] * len(context.world))


def _wall_keeps_side(context, near):
    result = pf.fuse([near], context)
    this_row, other_row = _rows(context)
    return result[this_row].min() > result[other_row].max()


def _all_possible(context, near):
    return bool((pf.fuse([near], context) > 0).all())


@pytest.mark.parametrize("other_x, kwargs, holds", [
    (None, {}, _silent),                                  # неизвестный другой - фактор молчит
    (0, {"share": 0.9, "reach_m": 0.5}, _closer_is_likelier),    # ближе к другому - вероятнее
    (0, {"share": 0.0}, _even),                           # доля 0 - ровный фон
    (0, {"share": 0.9, "reach_m": 5.0}, _wall_keeps_side),  # стена держит близость на своей стороне даже при большом охвате
    (0, {"share": 0.7, "reach_m": 0.5}, _all_possible),     # фон оставляет клетки за стеной возможными
], ids=["unknown", "closer", "zero-share", "wall", "background"])
def test_near_subject_pulls_towards_the_other_on_its_side_of_the_wall(context, other_x, kwargs, holds):
    other = None if other_x is None else other_at(context, other_x)
    assert holds(context, pn.NearSubject(lambda: other, **kwargs))
