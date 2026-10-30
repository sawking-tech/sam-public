"""Список мест с вероятностями (SAM-118): масса вокруг названных мест, сумма не больше единицы, соседние места не делят массу дважды. Мир выдуманный: коридор из шести клеток."""
import numpy as np
import pytest

from radar import presence_report as prp
from radar import presence_world as pw
from tests.fakes_presence import CorridorNavigation

CELLS = 6
STEP_M = 0.3
EVEN = np.full(CELLS, 1 / CELLS)
LEFT_RIGHT = {"left": (0.0, 0.0), "right": (1.5, 0.0)}


@pytest.fixture(scope="module")
def world():
    return pw.World(CorridorNavigation(CELLS), step_m=STEP_M)


@pytest.mark.parametrize("places, radius_m, field, expected", [
    (LEFT_RIGHT, 0.4, np.array([0.4, 0.2, 0.1, 0.05, 0.05, 0.2]), [("left", 0.6), ("right", 0.25)]),    # по убыванию, остальное - «в другом месте»
    ({"a": (0.3, 0.0), "b": (0.6, 0.0)}, 5.0, EVEN, [("b", 4 / 6), ("a", 2 / 6)]),                      # клетка - ближайшему месту, масса не дважды
    (LEFT_RIGHT, 0.1, np.array([1.0, 0, 0, 0, 0, 0]), [("left", 1.0)]),                                 # место без массы опущено
    ({}, None, EVEN, []),                                                                               # мест нет - пустой список
], ids=["by-probability", "nearest-once", "no-mass", "no-places"])
def test_place_report_gives_each_cell_to_the_nearest_place_once(world, places, radius_m, field, expected):
    report = prp.PlaceReport(world, places, radius_m=radius_m) if radius_m is not None else prp.PlaceReport(world, places)
    result = report(field)
    assert [(name, pytest.approx(p)) for name, p in result] == expected
    assert sum(p for _, p in result) <= 1.0 + 1e-9
