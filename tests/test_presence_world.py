"""Мир потока положения (SAM-118): клетки на проходимом месте, расстояния по проходам, привязка точки. План выдуманный:
квартира 6 x 4 м, внутренняя стена на x = 3 с дверью на z = 2; вторая планировка - та же стена без двери."""
import math

import numpy as np
import pytest

from radar import presence_nav as nav
from radar import presence_world as pw

OUTER = [(0, 0, 6, 0), (6, 0, 6, 4), (6, 4, 0, 4), (0, 4, 0, 0)]
LEFT, RIGHT = (1.0, 0.6), (5.0, 0.6)


def wall(x1, z1, x2, z2, t=0.2, h=2.5):
    return {"x1": x1, "z1": z1, "x2": x2, "z2": z2, "t": t, "h": h}


def door(cx, cz, width=0.8, angle=math.pi / 2):
    return {"id": "d", "title": "Дверная рама", "kind": "opening", "x": cx - width / 2, "z": cz - 0.05, "w": width, "d": 0.1, "h": 2.0, "angle": angle}


def layout(with_door=True):
    walls = [wall(*o) for o in OUTER] + [wall(3, 0, 3, 4, t=0.1)]
    return {"walls": walls, "objects": [door(3, 2.0)] if with_door else []}


@pytest.fixture(scope="module")
def apartment():
    return nav.Nav(layout())


@pytest.fixture(scope="module")
def world(apartment):
    return pw.World(apartment)


def euclid(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class StripNavigation:
    """Два ряда клеток по три в ряд, между рядами стена: ни точки, ни отрезки через неё не проходят."""

    def stand_nodes(self, step_m):
        return [(x * step_m, z) for z in (0.0, 1.0) for x in range(3)]

    def segment_free(self, a, b):
        return (a[1] < 0.5) == (b[1] < 0.5)

    def snap(self, x, z):
        return (x, z)


def test_world_cells_on_free_floor_and_point_snaps_to_own_side(apartment, world):
    """Клетки только на свободном полу внутри квартиры, точка в стене уходит на свою сторону, навигация без места отвергнута."""
    assert len(world) > 100
    for cell in range(len(world)):
        x, z = world.point_of(cell)
        assert apartment.segment_free((x, z), (x, z)) and not apartment.is_exterior(x, z)
    assert world.point_of(world.cell_at(2.95, 1.0))[0] < 3.0
    assert world.point_of(world.cell_at(3.05, 1.0))[0] > 3.0

    class Nothing(StripNavigation):
        def stand_nodes(self, step_m):
            return []

    with pytest.raises(ValueError, match="нет места"):
        pw.World(Nothing())


def test_distance_goes_through_door_and_sealed_rooms_unreachable(world):
    """В комнате расстояние почти прямое, через стену - через дверь, без двери комнаты не связаны; мир знает только протокол навигации."""
    a, b = world.cell_at(*LEFT), world.cell_at(*RIGHT)
    assert euclid(LEFT, RIGHT) + 0.4 < world.distances()[a, b] < 2 * euclid(LEFT, (3.0, 2.0)) * 1.1
    near, far = (1.0, 0.6), (2.0, 3.0)
    assert euclid(near, far) * 0.95 < world.distances()[world.cell_at(*near), world.cell_at(*far)] < euclid(near, far) * 1.12

    sealed = pw.World(nav.Nav(layout(with_door=False)))
    a, b = sealed.cell_at(*LEFT), sealed.cell_at(*RIGHT)
    assert not sealed.reachable(a, b) and np.isinf(sealed.distances()[a, b])

    strip = pw.World(StripNavigation(), step_m=0.3)
    row_a, row_b = strip.cell_at(0.0, 0.0), strip.cell_at(0.3, 0.0)
    assert strip.distances()[row_a, row_b] == pytest.approx(0.3)
    assert not strip.reachable(row_a, strip.cell_at(0.0, 1.0))


def test_reach_respects_radius_and_walls():
    """Достижимость симметрична, включает саму клетку, ограничена радиусом и не идёт через стену."""
    rows = pw.World(StripNavigation(), step_m=0.3)
    reach = rows.reach(1.5).toarray()
    assert (reach == reach.T).all() and (np.diag(reach) == 1).all()
    first, across = rows.cell_at(0.0, 0.0), rows.cell_at(0.0, 1.0)
    assert reach[first, rows.cell_at(0.6, 0.0)] == 1 and reach[first, across] == 0
    assert rows.reach(0.2).toarray().sum() == len(rows)
