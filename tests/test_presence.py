"""Радар присутствия (SAM-105): облако точек человека, возраст кадра, личность, планировка, связи, тепло, зоны."""
import json
import random

import pytest

from radar import presence as pr

LAYOUT = pr.DEMO_LAYOUT


def test_mock_mode_person_walks_room_to_room_as_a_fresh_noisy_cloud():
    first = pr.mock_position(LAYOUT, 1.0)
    assert first[0] == "demo-living"
    assert pr.room_center(LAYOUT, "demo-living") == (first[1], first[2])
    assert pr.mock_position(LAYOUT, pr.DWELL_S + 1.0)[1:] != (first[1], first[2])   # уже вышла из гостиной
    assert pr.mock_position(LAYOUT, 1001.0)[0] in {r["id"] for r in LAYOUT["rooms"]}   # цикл не выходит за комнаты
    f1, f2 = pr.mock_frame(LAYOUT, 3.0), pr.mock_frame(LAYOUT, 3.2)
    assert f1.people[0].points != f2.people[0].points                  # каждый тик новое облако
    assert pr.mock_frame(LAYOUT, 3.0).people[0].points == f1.people[0].points   # тот же тик повторяем


def test_person_goes_live_stale_then_lost_and_a_new_frame_revives():
    store = pr.PresenceStore()
    store.update(pr.mock_frame(LAYOUT, 100.0))
    live = store.snapshot(100.5)["people"][0]
    stale = store.snapshot(100.0 + pr.STALE_S + 1)["people"][0]
    assert live["state"] == "live" and live["age_s"] == 0.5
    assert stale["state"] == "stale" and stale["age_s"] > pr.STALE_S
    assert store.snapshot(100.0 + pr.LOST_S + 1)["people"] == []
    assert store.snapshot(100.0 + pr.LOST_S + 2)["people"] == []      # потерянный не возвращается сам
    store.update(pr.mock_frame(LAYOUT, 100.0 + pr.LOST_S + 3))
    assert store.snapshot(100.0 + pr.LOST_S + 3.2)["people"][0]["state"] == "live"


def test_body_cloud_has_human_size_noise_and_walking_smear():
    crisp = pr.body_points(2.0, 3.0)
    xs, ys = [p[0] for p in crisp], [p[1] for p in crisp]
    assert len(crisp) == pr.POINTS_PER_PERSON
    assert 0 < min(ys) < 0.05 and 1.7 < max(ys) <= pr.BODY_HEIGHT_M
    assert max(xs) - min(xs) <= 2 * pr.BODY_RADIUS_M + 1e-9           # ширина плеч около 0,44 м
    assert abs((max(xs) + min(xs)) / 2 - 2.0) < 0.05                  # фигура стоит в заданной точке
    a = pr.body_points(2.0, 3.0, rnd=random.Random(1))
    assert a != crisp and a != pr.body_points(2.0, 3.0, rnd=random.Random(2))
    assert a == pr.body_points(2.0, 3.0, rnd=random.Random(1))         # шум повторяем по сиду
    assert all(p[1] >= 0 for p in a)                                  # ниже пола точек нет
    assert max(p[0] for p in a) - min(p[0] for p in a) > max(xs) - min(xs)
    still = pr.body_points(0, 0, rnd=random.Random(3))
    walking = pr.body_points(0, 0, rnd=random.Random(3), vel=(1.0, 0.0), phase=1.0)
    legs = lambda pts: [p[0] for p in pts if p[1] < 0.45 * pr.BODY_HEIGHT_M]
    assert sum(legs(walking)) / len(legs(walking)) < sum(legs(still)) / len(legs(still))
    assert sum(p[0] for p in walking) < sum(p[0] for p in still)      # размазано назад против движения


@pytest.mark.parametrize("voices, by_room, identity, who", [
    (["resident-a"], {"kitchen": "resident-a"}, pr.HOUSEHOLD, "resident-a"),
    (["resident-b"], {}, pr.GUEST, None),
    ([], {}, pr.UNKNOWN, None),
])
def test_identity_is_household_by_voice_else_guest_or_unknown(voices, by_room, identity, who):
    got = pr.identify([pr.Person("p1", "kitchen", pr.body_points(1, 1))], voices, by_room)[0]
    assert got.identity == identity
    if who:
        assert got.who == who


def test_layout_reads_a_file_or_falls_back_to_the_demo_and_refuses_an_empty_one(tmp_path):
    assert pr.load_layout(None) is pr.DEMO_LAYOUT
    assert pr.load_layout(str(tmp_path / "nope.json")) is pr.DEMO_LAYOUT
    f = tmp_path / "home.json"
    f.write_text(json.dumps({"name": "home", "rooms": [{"id": "a", "x": 0, "z": 0, "w": 1, "d": 1}]}), encoding="utf-8")
    assert pr.load_layout(str(f))["name"] == "home"
    f.write_text(json.dumps({"name": "empty", "rooms": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        pr.load_layout(str(f))


@pytest.mark.parametrize("x, z, radius, expected", [
    (2.0, 2.2, None, "demo-sofa"),       # у дивана (он 1.2..3.2 по x, 1.0..1.9 по z)
    (4.4, 1.5, None, "demo-tv"),         # ближайший из двух предметов
    (2.5, 3.9, 0.5, None),               # далеко от всего
])
def test_person_links_to_the_nearest_object_within_reach(x, z, radius, expected):
    kw = {} if radius is None else {"radius": radius}
    links = pr.interactions([pr.Person("p1", "demo-living", pr.body_points(x, z))], LAYOUT, **kw)
    if expected is None:
        assert links == []
    else:
        assert links[0]["person"] == "p1" and links[0]["object"] == expected
    if expected == "demo-sofa":
        assert links[0]["dist_m"] == pytest.approx(0.3, abs=0.05)


def test_heat_accumulates_seconds_survives_json_and_moves_with_the_plan():
    heat = pr.Heatmap(cell_m=0.5)
    for _ in range(4):
        heat.add(1.1, 2.1, 1.0)                                       # одна клетка, 4 секунды
    heat.add(3.0, 3.0, 0.5)
    assert heat.counts[(2, 4)] == 4.0
    assert heat.walkable(3.0) == {(2, 4)}
    assert [2, 4, 4.0] in heat.cells()
    assert pr.Heatmap.from_json(heat.to_json()).counts == heat.counts
    moved = heat.moved(lambda x, z: (0.1, 0.1) if x > 2 else (x, z))
    assert sum(moved.counts.values()) == pytest.approx(4.5)
    assert moved.counts[moved.cell_of(0.1, 0.1)] == 0.5 and moved.counts[moved.cell_of(1.1, 2.1)] == 4.0


@pytest.mark.parametrize("data", [
    {"label": "", "x": 0, "z": 0, "w": 1, "d": 1},
    {"label": "x" * (pr.ZONE_LABEL_MAX + 1), "x": 0, "z": 0, "w": 1, "d": 1},
    {"label": "кухня", "x": 0, "z": 0},
    {"label": "кухня", "x": 0, "z": 0, "w": 0, "d": 1},
])
def test_zone_is_validated_named_uniquely_and_found_by_point(data):
    zone = pr.validate_zone({"label": " Диван ", "x": "1", "z": 2, "w": 2, "d": 1}, set())
    assert zone == {"id": "zone-диван", "label": "Диван", "x": 1.0, "z": 2.0, "w": 2.0, "d": 1.0}
    assert pr.validate_zone({"label": "диван", "x": 0, "z": 0, "w": 1, "d": 1}, {"zone-диван"})["id"] == "zone-диван-2"
    with pytest.raises(ValueError):
        pr.validate_zone(data, set())
    zones = [{"label": "гостиная", "x": 0, "z": 0, "w": 5, "d": 4}, {"label": "диван", "x": 1, "z": 1, "w": 2, "d": 1}]
    assert pr.zone_at(zones, 1.5, 1.5) == "диван"                      # меньшая зона внутри большей
    assert pr.zone_at(zones, 4.0, 3.0) == "гостиная"
    assert pr.zone_at(zones, 9.0, 9.0) is None
