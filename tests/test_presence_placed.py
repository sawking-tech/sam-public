"""Место, заданное данными (SAM-118): пятно вокруг указанного места, фон оставляет унесённое устройство возможным.
Здесь же общий контракт факторов места: без входа фактор молчит. Мир выдуманный: коридор из шести клеток."""
from datetime import datetime

import numpy as np
import pytest

from radar import presence_factors as pf
from radar import presence_placed as pl
from radar import presence_preference as pp
from radar import presence_rings as pr
from radar import presence_world as pw
from tests.fakes_presence import CorridorNavigation

CELLS = 6
STEP_M = 0.3
NOW = datetime(2026, 1, 5, 12, 0)
PLACES = {"start": (0.0, 0.0), "end": (1.5, 0.0)}


@pytest.fixture(scope="module")
def context():
    return pf.Context(world=pw.World(CorridorNavigation(CELLS), step_m=STEP_M), now=NOW)


def distribution(context, place, **kwargs):
    return pf.fuse([pl.Placed(lambda: place, PLACES, **kwargs)], context)


@pytest.mark.parametrize("factor", [
    pl.Placed(lambda: None, PLACES),                         # места нет
    pl.Placed(lambda: "somewhere", PLACES),                  # место неизвестно
    pp.Preference(lambda now: None),                         # нет карты на это время
    pr.Rings(lambda: None, np.zeros(CELLS)),                 # нет измерения
], ids=["placed-none", "placed-unknown", "preference-no-map", "rings-no-measurement"])
def test_factor_without_input_is_silent(context, factor):
    """Фактор без входа возвращает None и не меняет слияние."""
    assert factor.log_field(context) is None
    assert pf.fuse([factor], context) == pytest.approx([1 / CELLS] * CELLS)


@pytest.mark.parametrize("place, share, radius_m", [("end", None, 0.4), ("start", 0.9, 0.2), ("start", 0.0, None)])
def test_placed_field_peaks_at_the_named_place_and_keeps_a_background(context, place, share, radius_m):
    """Масса собирается у названного места, фон оставляет все клетки возможными, share=0 - ровный фон."""
    kwargs = {k: v for k, v in (("share", share), ("radius_m", radius_m)) if v is not None}
    result = distribution(context, place, **kwargs)
    assert (result > 0).all()
    if share == 0.0:
        assert result == pytest.approx([1 / CELLS] * CELLS)
    else:
        peak = 0 if place == "start" else CELLS - 1
        assert result.argmax() == peak and result[peak] > 3 * result[CELLS - 1 - peak]
