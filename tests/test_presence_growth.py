"""Рост уверенности по секундам окна (SAM-118): пик, доля массы у точки и площадь 90% по полю каждой секунды.
Мир выдуманный: один ряд из десяти клеток через 0,3 м, путь между клетками равен расстоянию по ряду."""
import numpy as np
import pytest

from radar import presence_growth as pg
from radar import presence_world as pw

STEP_M = 0.3
CELLS = 10


class RowNavigation:
    def stand_nodes(self, step_m):
        return [(x * step_m, 0.0) for x in range(CELLS)]

    def segment_free(self, a, b):
        return True

    def snap(self, x, z):
        return (x, z)


def _sharp(cell):
    field = np.zeros(CELLS)
    field[cell] = 1.0
    return field


@pytest.fixture(scope="module")
def world():
    return pw.World(RowNavigation(), step_m=STEP_M)


@pytest.mark.parametrize("field, point, radius_m, expected", [
    # ровное поле: низкий пик, девять клеток из десяти набирают 0,9 массы
    (np.full(CELLS, 1.0), 3, None, {"peak": 0.1, "area_m2": 9 * STEP_M ** 2}),
    # острое поле: вся масса в одной клетке
    (_sharp(3), 3, None, {"peak": 1.0, "area_m2": STEP_M ** 2}),
    # у точки - клетка точки и два соседа; запас на float у границы радиуса
    (np.full(CELLS, 1.0), 5, STEP_M * 1.1, {"mass_near": 0.3}),
    # без точки массы у точки нет
    (np.full(CELLS, 1.0), None, None, {"mass_near": 0.0}),
    # поле нормируется
    (np.full(CELLS, 5.0), 0, None, {"peak": 0.1}),
    # пустое поле - нули
    (np.zeros(CELLS), 0, None, {"peak": 0.0, "mass_near": 0.0, "area_m2": 0.0}),
])
def test_second_growth_measures_peak_mass_near_and_area(world, field, point, radius_m, expected):
    kwargs = {} if radius_m is None else {"radius_m": radius_m}
    growth = pg.second_growth(world, field, point, **kwargs)
    for name, value in expected.items():
        assert getattr(growth, name) == pytest.approx(value), name


def test_window_growth_sharpens_as_field_narrows(world):
    fields = [np.full(CELLS, 1.0), np.array([1.0] * 5 + [0.0] * 5), _sharp(3)]
    growth = pg.window_growth(world, fields, [3, 3, 3])
    assert [g.peak for g in growth] == sorted(g.peak for g in growth)
    assert [g.mass_near for g in growth] == sorted(g.mass_near for g in growth)
    assert [g.area_m2 for g in growth] == sorted((g.area_m2 for g in growth), reverse=True)
