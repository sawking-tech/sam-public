"""Мир сверху (SAM-118): подошва низкой мебели это клетки пола для телефона и человека, высокая мебель преграда, стены непроходимы, миры одни.
План выдуманный: квартира 6 x 4 м, внутренняя стена на x = 3 с дверью, диван 2 x 0,9 м в левой комнате, шкаф 2,1 м в правой."""
import math

import numpy as np
import pytest

from radar import presence_flow_live as pfl
from radar import presence_rssi as rs
from radar import presence_topview as ptv
from radar import presence_world as pw
from radar.presence_nav import Nav
from radar.presence_random import Randomness

OUTER = [(0, 0, 6, 0), (6, 0, 6, 4), (6, 4, 0, 4), (0, 4, 0, 0)]
SOFA = {"id": "s", "title": "Диван", "kind": "furniture", "x": 0.5, "z": 3.0, "w": 2.0, "d": 0.9, "h": 0.87, "angle": 0.0}
WARDROBE = {"id": "w", "title": "Шкаф", "kind": "furniture", "x": 4.5, "z": 0.3, "w": 1.0, "d": 0.6, "h": 2.1, "angle": 0.0}
NAME, IFACE = "Test-Phone", "phy1-ap0"
MODEL = {f"{NAME.lower()}@{IFACE}": {"l0_db": -40.0, "n": 2.0, "wall_db_per_m": 20.0}}


def wall(x1, z1, x2, z2, t=0.2, h=2.5):
    return {"x1": x1, "z1": z1, "x2": x2, "z2": z2, "t": t, "h": h}


def layout():
    door = {"id": "d", "title": "Дверная рама", "kind": "opening", "x": 2.6, "z": 1.95, "w": 0.8, "d": 0.1, "h": 2.0, "angle": math.pi / 2}
    return {"walls": [wall(*o) for o in OUTER] + [wall(3, 0, 3, 4, t=0.1)], "objects": [door, SOFA, WARDROBE], "router": {"x": 0.5, "z": 0.5, "h": 2.4}, "places": []}


def inside(obj, xz):
    return obj["x"] <= xz[0] <= obj["x"] + obj["w"] and obj["z"] <= xz[1] <= obj["z"] + obj["d"]


@pytest.fixture(scope="module")
def worlds():
    nav = Nav(layout())
    return pw.World(nav, 0.3), pw.World(ptv.TopViewNavigation(nav), 0.3)


@pytest.mark.parametrize("max_h_m, wardrobe_is_floor", [(ptv.TOP_VIEW_MAX_H_M, False), (3.0, True)])
def test_low_furniture_is_floor_and_tall_is_obstacle(worlds, max_h_m, wardrobe_is_floor):
    """Сверху подошва дивана - клетки мира, у ходьбы нет; шкаф выше порога вне мира, при пороге 3 м тоже пол.
    Точка на диване остаётся, точка в стене уходит на пол."""
    walking, _ = worlds
    nav = ptv.TopViewNavigation(Nav(layout()), max_h_m=max_h_m)
    top = pw.World(nav, 0.3)
    assert len(top) > len(walking)
    assert any(inside(SOFA, tuple(p)) for p in top.points) and not any(inside(SOFA, tuple(p)) for p in walking.points)
    assert any(inside(WARDROBE, tuple(p)) for p in top.points) is wardrobe_is_floor
    assert not ptv.is_low({"kind": "opening", "h": 0.1})
    assert inside(SOFA, nav.snap(1.5, 3.4)) and not inside(SOFA, nav.snap(3.0, 1.0))


def test_the_sofa_is_reachable_from_the_floor_and_the_wall_still_blocks(worlds):
    _, top = worlds
    sofa_cell = next(i for i, p in enumerate(top.points) if inside(SOFA, tuple(p)))
    before = top.cell_at(1.5, 2.6)
    assert math.isfinite(top.distances()[before, sofa_cell]) and top.distances()[before, sofa_cell] < 1.5
    nav = ptv.TopViewNavigation(Nav(layout()))
    assert not nav.segment_free((2.8, 0.5), (3.2, 0.5)) and math.isfinite(top.distances()[top.cell_at(1.0, 1.0), top.cell_at(5.0, 3.0)])


def test_the_live_flow_has_one_world_for_the_phone_and_the_person_and_both_may_sit_on_the_sofa():
    flow = pfl.LiveFlow(layout(), MODEL, {}, {"place_xy": {}}, randomness=Randomness(1), window_slices=7, owners={"test": "owner-a"})
    assert flow.world is flow.floor_world and any(inside(SOFA, tuple(p)) for p in flow.world.points)
    sofa_cell = next(i for i, p in enumerate(flow.world.points) if inside(SOFA, tuple(p)))
    level = float(pfl.expected_db(flow.world, layout(), MODEL[f"{NAME.lower()}@{IFACE}"])[sofa_cell])
    for k in range(24):
        flow.observe([rs.Station("abc", IFACE, level, 100, NAME)], 1_000_000.0 + k / 2)
    flow.locate(NAME, IFACE, *flow.world.points[sofa_cell])
    for k in range(24, 40):
        flow.observe([rs.Station("abc", IFACE, level, 100, NAME)], 1_000_000.0 + k / 2)
    state = flow.state()
    sofa_mass = sum(p for i, p in enumerate(state["devices"][0]["p"]) if inside(SOFA, tuple(flow.world.points[i])))
    assert sofa_mass > 0.1 and len(state["people"][0]["p"]) == len(flow.world)
    phone, person = flow.point_of(NAME, IFACE), flow.person_spot("owner-a")[0]
    assert math.hypot(person[0] - phone["x"], person[1] - phone["z"]) < 1.6


def test_place_zones_are_the_footprint_of_the_low_object_or_the_room_under_the_place_point(worlds):
    _, top = worlds
    plan = {**layout(), "rooms": [{"title": "левая", "x": 0, "z": 0, "w": 3, "d": 4}, {"title": "правая", "x": 3, "z": 0, "w": 3, "d": 4}]}
    zones = ptv.place_zones({"диван": (1.5, 3.4), "кухня": (4.5, 3.0), "шкаф": (5.0, 0.5), "улица": (9.0, 9.0)}, plan, top.points)
    assert all(inside(SOFA, tuple(top.points[i])) for i in np.nonzero(zones["диван"])[0]) and zones["диван"].sum() > 10
    assert zones["кухня"].sum() > zones["диван"].sum() and all(top.points[i][0] >= 3 for i in np.nonzero(zones["кухня"])[0])      # комната справа от стены
    assert "улица" not in zones and zones["шкаф"].sum() > 0 and all(top.points[i][0] >= 3 for i in np.nonzero(zones["шкаф"])[0])   # шкаф высокий: зона его комнаты


def test_the_phone_preference_by_zones_concentrates_the_mass_on_the_sofa_footprint():
    from datetime import datetime
    from radar.presence_factors import Context
    plan = layout()
    prefs = {"default": {"диван": 1.0}}
    flow = pfl.LiveFlow(plan, MODEL, prefs, {"place_xy": {"диван": [1.5, 3.4]}}, randomness=Randomness(1), window_slices=7, preference_weight=1.0)
    factor = pfl.phone_preference(prefs, "test", {"диван": (1.5, 3.4)}, flow.world, 1.0, plan)
    field = np.exp(factor.log_field(Context(world=flow.world, now=datetime(2026, 1, 5, 12, 0))))
    field = field / field.sum()
    on_sofa = np.array([inside(SOFA, tuple(p)) for p in flow.world.points])
    assert field[on_sofa].sum() > 0.6 and on_sofa.sum() < len(flow.world) / 5                       # больше половины массы на 1/5 клеток и меньше
