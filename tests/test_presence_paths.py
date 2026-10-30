"""Выбор самых широких путей пучка для слоя (SAM-118). Мир выдуманный: клетки в ряд, клетка i лежит в точке (i, 0)."""
import numpy as np
import pytest

from radar import presence_paths as pp

POINTS = np.array([[float(i), 0.0] for i in range(10)])
CROWD = [[4, 4, 5], [4, 5, 5], [5, 5, 4], [5, 4, 4], [4, 4, 4], [5, 5, 5]]


@pytest.mark.parametrize("paths, n, must_have", [
    # пучок не длиннее n возвращается целиком
    ([[1, 2], [3, 4]], 5, [[1, 2], [3, 4]]),
    # самые широкие - крайние, а не толпа в середине
    (CROWD + [[0, 0, 0], [9, 9, 9]], 3, [[0, 0, 0], [9, 9, 9]]),
    # одинаковые пути не мешают набрать n без повторов одного и того же
    ([[3, 3, 3]] * 6, 3, [[3, 3, 3]]),
])
def test_widest_paths_pick_extremes_without_repeats(paths, n, must_have):
    picked = pp.widest(POINTS, np.array(paths), n).tolist()
    assert len(picked) == min(n, len(paths))
    assert all(path in picked for path in must_have)
