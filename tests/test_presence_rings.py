"""Кольцо уровня (SAM-118): клетки с ожидаемым уровнем рядом с измеренным вероятнее; вес среза задаёт резкость, sd - толщину. Мир выдуманный:
коридор из пяти клеток, ожидаемый уровень падает на 3 дБ на клетку. Молчание без измерения - в общем контракте факторов (test_presence_placed.py)."""
from datetime import datetime

import numpy as np
import pytest

from radar import presence_factors as pf
from radar import presence_rings as pr
from radar import presence_world as pw
from tests.fakes_presence import CorridorNavigation

NOW = datetime(2026, 1, 5, 12, 0)
CELLS = 5
EXPECTED_DB = np.array([-40.0, -43.0, -46.0, -49.0, -52.0])


@pytest.fixture(scope="module")
def context():
    return pf.Context(world=pw.World(CorridorNavigation(CELLS), step_m=0.3), now=NOW)


def fused(context, *levels, weight=pr.SLICE_WEIGHT, sd_db=pr.RING_SD_DB):
    return pf.fuse([pr.Rings(lambda level=level: level, EXPECTED_DB, sd_db=sd_db, weight=weight) for level in levels], context)


@pytest.mark.parametrize("level, likeliest", [(-46.0, {2}), (-49.5, {3, 4})])
def test_rings_put_the_mass_where_the_expected_level_matches_the_measured_one(context, level, likeliest):
    """Пик в клетке с измеренным уровнем."""
    assert fused(context, level, weight=1.0).argmax() in likeliest


def test_rings_split_the_mass_evenly_between_two_cells(context):
    result = fused(context, -44.5, weight=1.0)
    assert result[1] == pytest.approx(result[2])


@pytest.mark.parametrize("blunt, sharp", [
    (dict(levels=1, weight=0.05), dict(levels=1, weight=1.0)),                     # вес делает кольцо резче
    (dict(levels=1), dict(levels=20)),                                             # срезы перемножаются и сужают область
    (dict(levels=1, weight=1.0, sd_db=10.0), dict(levels=1, weight=1.0, sd_db=2.0)),    # большее sd - кольцо толще
], ids=["weight", "slices", "sd"])
def test_rings_sharpen_with_weight_and_slices_and_thicken_with_sd(context, blunt, sharp):
    """У резкого кольца больше массы в клетке измеренного уровня и меньше в дальней."""
    def run(levels, **kw):
        return fused(context, *[-46.0] * levels, **kw)
    b, s = run(**blunt), run(**sharp)
    assert s[2] > b[2] and s[0] < b[0]
