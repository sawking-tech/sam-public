"""Поля шага к роутеру (SAM-118): путь к роутеру идёт по полу в обход стены, разница уровня растёт с секундами, сводка считает долю выше шума."""
import numpy as np
import pytest

from radar import presence_walk_fields as pwf
from radar import presence_world as pw

STEP = 0.5


class WallWithDoor:
    """Сетка 5 x 4 клеток по 0,5 м; между рядами z < 1 и z >= 1 стена, проход только у x = 2."""

    def stand_nodes(self, step_m):
        return [(x * STEP, z * STEP) for z in range(4) for x in range(5)]

    def segment_free(self, a, b):
        crosses = (a[1] < 0.75) != (b[1] < 0.75)
        return not crosses or (a[0] >= 1.75 and b[0] >= 1.75)

    def snap(self, x, z):
        return (x, z)


def test_walk_field_goes_round_wall_grows_with_seconds_and_summarises():
    """Путь к роутеру идёт по полу через проход, за метр ещё по эту сторону стены, длинный кончается у роутера;
    на поле, растущем к роутеру, разница уровня растёт с секундами; сводка считает сдвинутые клетки, долю выше шума и падающих."""
    world = pw.World(WallWithDoor(), step_m=STEP)
    router, start = world.cell_at(0.0, 1.5), world.cell_at(0.0, 0.5)
    dist, pred = world.path_tree(router)
    assert dist[start] > 3.0                                                       # прямо 1 м, по полу через проход у x = 2
    assert world.points[pwf.walk_ends(dist, pred, world.points, 1.0)[start]][1] < 0.75
    assert pwf.walk_ends(dist, pred, world.points, 100.0)[start] == router

    expected = -40.0 - 4.0 * np.where(np.isfinite(dist), dist, 0.0)                # уровень падает на 4 дБ на метр пути от роутера
    fields = pwf.walk_fields(dist, pred, world.points, expected, seconds=3, speed_m_s=1.0)
    far = world.cell_at(2.0, 0.0)
    assert [f.seconds for f in fields] == [1, 2, 3]
    assert 0 < fields[0].delta_db[far] < fields[1].delta_db[far] < fields[2].delta_db[far]
    assert fields[0].delta_db[router] == 0.0 and fields[0].to[router] == router

    field = pwf.WalkField(2, np.array([1, 1, 2, 0]), np.array([6.0, 0.0, 0.0, -1.0]))
    assert pwf.summary(field, noise_db=5.5) == {"seconds": 2, "moved": 2, "median_db": pytest.approx(2.5), "above_noise": 0.5, "falling": 0.5}
    assert pwf.summary(pwf.WalkField(1, np.array([0]), np.array([0.0])))["moved"] == 0
