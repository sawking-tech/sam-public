"""Карта предпочтений (SAM-118): время выбирает поле, фон оставляет необычные места возможными, вес - степень карты. Мир выдуманный: коридор из четырёх клеток."""
from datetime import datetime

import numpy as np
import pytest

from radar import presence_factors as pf
from radar import presence_preference as pp
from radar import presence_world as pw
from tests.fakes_presence import CorridorNavigation

CELLS = 4
MORNING, EVENING = datetime(2026, 1, 5, 8, 0), datetime(2026, 1, 5, 21, 0)
BY_TIME = {MORNING: np.array([0.7, 0.1, 0.1, 0.1]), EVENING: np.array([0.1, 0.1, 0.1, 0.7])}


@pytest.fixture(scope="module")
def world():
    return pw.World(CorridorNavigation(CELLS), step_m=0.3)


@pytest.mark.parametrize("now", [MORNING, EVENING])
def test_preference_follows_the_time_of_day_and_never_excludes_a_cell(world, now):
    """Утром и вечером своё поле, оно проходит без искажения; ноль в карте клетку не запрещает."""
    context = pf.Context(world=world, now=now)
    result = pf.fuse([pp.Preference(BY_TIME.get)], context)
    assert result == pytest.approx(BY_TIME[now]) and (result > 0).all()
    zero = pp.Preference(lambda t: np.array([1.0, 0.0, 0.0, 0.0])).log_field(context)
    assert np.isfinite(zero).all()


@pytest.mark.parametrize("weight", [1.0, 0.5, 0.0, -0.1])
def test_preference_weight_is_the_power_of_the_map(world, weight):
    """Вес - степень карты: половина веса - половина логарифма, ноль глушит, отрицательный отвергается."""
    field = lambda now: np.array([0.5, 0.25, 0.125, 0.125])
    if weight < 0:
        with pytest.raises(ValueError):
            pp.Preference(field, weight=weight)
        return
    context = pf.Context(world=world, now=MORNING)
    full = pp.Preference(field).log_field(context)
    assert np.allclose(pp.Preference(field, weight=weight).log_field(context), weight * full)
