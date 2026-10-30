"""Факторы потока положения (SAM-118): сводящая функция складывает поля любых факторов одинаково. Мир выдуманный: четыре клетки в ряд."""
from datetime import datetime

import numpy as np
import pytest

from radar import presence_factors as pf
from radar import presence_world as pw

CELLS = 4
NOW = datetime(2026, 1, 5, 12, 0)
EVEN = [0.25] * CELLS


class RowNavigation:
    def stand_nodes(self, step_m):
        return [(x * step_m, 0.0) for x in range(CELLS)]

    def segment_free(self, a, b):
        return True

    def snap(self, x, z):
        return (x, z)


class Field:
    """Фактор с готовым полем: вес в логарифмах уже внутри; None - фактор молчит."""

    def __init__(self, field):
        self.field = None if field is None else np.asarray(field, dtype=float)

    def log_field(self, context):
        return self.field


@pytest.fixture(scope="module")
def context():
    return pf.Context(world=pw.World(RowNavigation(), step_m=0.3), now=NOW)


@pytest.mark.parametrize("fields, expected", [
    ([], EVEN),                                                                     # нет факторов - ровно
    ([None], EVEN),
    ([np.log([1, 2, 3, 4]), np.log([4, 3, 2, 1])], np.array([4, 6, 6, 4]) / 20),     # поля перемножаются
    ([np.log([1, 1, 2, 4]), None], np.array([1, 1, 2, 4]) / 8),                      # молчащий не меняет
    ([np.log([1, 9, 1, 1]), [0.0, -np.inf, 0.0, 0.0]], np.array([1, 0, 1, 1]) / 3),  # -inf исключает клетку
    ([[-2000.0, -2001.0, -2002.0, -2003.0]], np.exp(-np.arange(4.0)) / np.exp(-np.arange(4.0)).sum()),   # очень малые не теряются
], ids=["none", "silent", "multiply", "silent_with_other", "excluded", "underflow"])
def test_fuse_multiplies_fields_ignores_silent_ones_and_keeps_excluded_cells(context, fields, expected):
    result = pf.fuse([Field(f) for f in fields], context)
    assert result == pytest.approx(expected) and result.sum() == pytest.approx(1.0)


@pytest.mark.parametrize("field, message", [([-np.inf] * CELLS, "исключили все клетки"), ([0.0, 0.0], "Field")], ids=["all_excluded", "wrong_shape"])
def test_fuse_refuses_all_excluded_or_wrong_shape_with_one_line(context, field, message):
    with pytest.raises(ValueError, match=message):
        pf.fuse([Field(field)], context)
