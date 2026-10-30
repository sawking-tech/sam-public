"""Набор для модели положения (SAM-118): метка стоянки, положение на пути, отсев несвежих кадров, шкала тиков с областью и временем стоянки. Данные выдуманные."""
import pytest

from tests.fakes_presence import load_script

ds = load_script("radar_nn_dataset")
T0 = 1_000_000.0
PLACES = [ds.Place("точка A", 1.0, 1.0, 0.3), ds.Place("точка B", 5.0, 1.0, 0.3)]
ROOMS = [{"title": "комната 1", "x": 0.0, "z": 0.0, "w": 3.0, "d": 2.0}, {"title": "комната 2", "x": 3.0, "z": 0.0, "w": 3.0, "d": 2.0}]
CLASSES = {"place_class": {"точка A": "место A"}, "spots": [{"name": "место A", "x": 1.0, "z": 1.0, "r": 0.5}],
           "corridor": [{"name": "у двери", "x": 3.0, "z": 1.0, "r": 0.3}], "class_room": {"место A": "комната 1", "у двери": "комната 1"}}
PHONES = {"PhoneA": "phone-a"}


def frame(t, db, age=0, name="PhoneA-1"):
    return {"t": t, "name": name, "db": db, "if": "phy1-ap0", "in": age, "ln": {"avgch": [db - 1, db + 1], "ack": [db, db], "tx": [100.0, 1, 1], "rx": [50.0, 1, 1]}}


def test_stops_from_marks_with_bounds_refined_by_quiet_phone():
    """Стоянка идёт от прихода до следующей метки и получает точку места; неизвестное место - не стоянка;
    границы уточняются по ровному уровню тихого телефона в обе стороны, шумный ряд и пустой ряд границ не двигают."""
    marks = [{"t": T0, "text": "пришёл: точка A"}, {"t": T0 + 20, "text": "пошёл к точке B"}, {"t": T0 + 30, "text": "пришёл: точка B"}]
    stops = ds.build_stops(marks, PLACES, fast_from=T0 + 1000, last_t=T0 + 35)
    assert [(s.place.name, s.t_start, s.t_end) for s in stops] == [("точка A", T0, T0 + 19), ("точка B", T0 + 30, T0 + 35)]
    assert ds.match_place("пришёл: где-то ещё", PLACES) is None

    stop = ds.Stop(PLACES[0], T0 + 10, T0 + 10, T0 + 40, False, T0 + 41)
    quiet = {int(T0) + s: -70.0 for s in range(0, 5)} | {int(T0) + s: -50.0 for s in range(5, 35)} | {int(T0) + s: -62.0 for s in range(35, 42)}
    noisy = {int(T0) + s: -50.0 + (12, 0, -12)[s % 3] for s in range(0, 42)}          # полоса шире MAX_USEFUL_BAND_DB: границ не решает
    assert ds.refine_bounds(stop, {"a": quiet, "b": noisy}, floor_t=T0) == (T0 + 5, T0 + 35)
    assert ds.refine_bounds(stop, {"a": {}}, floor_t=T0) == (T0 + 10, T0 + 40)      # ряда нет - границы как были


def _fresh_level():
    feats = ds.window_features([frame(T0, -50.0), frame(T0 + 0.2, -52.0), frame(T0 + 0.4, -90.0, age=5000)])
    assert feats["db_median"] == pytest.approx(-51.0) and feats["fresh_share"] == pytest.approx(0.667, abs=1e-3) and feats["ant_diff"] == -2.0


def _point_on_path():
    path = [(0.0, 0.0), (4.0, 0.0), (4.0, 2.0)]
    assert ds.point_at(path, 0.5) == pytest.approx((3.0, 0.0)) and ds.point_at(path, 1.0) == (4.0, 2.0) and ds.point_at(path, -1) == (0.0, 0.0)


def _area_priority():
    assert ds.area_at(CLASSES, ROOMS, 1.2, 1.0) == "место A"                     # ключевое место
    assert ds.area_at(CLASSES, ROOMS, 3.1, 1.0) == "у двери"                     # часть коридора
    assert ds.area_at(CLASSES, ROOMS, 4.5, 1.0) == "комната 2"                   # комната
    assert ds.area_at(CLASSES, ROOMS, 4.5, 2.3) == "комната 2" and ds.area_at(CLASSES, ROOMS, 4.5, 9.0) == ds.OUTSIDE   # ближайшая комната, далеко - вне


def _ticks():
    segments = [ds.Segment("stop:0", T0, T0 + 1.0, "место A"), ds.Segment("leg:0", T0 + 1.0, T0 + 2.0, None, ((1.0, 1.0), (5.0, 1.0)), "transition")]
    out = ds.ticks([frame(T0 + k * 0.1, -50.0 - k) for k in range(20)], PHONES, segments, CLASSES, ROOMS, tick_s=0.5)
    assert [r["area"] for r in out] == ["место A", "место A", "место A", "у двери", None]       # конец шкалы вне отрезков - без метки
    assert [r["standing_s"] for r in out] == [0.0, 0.5, None, None, None] and out[2]["group"] == "leg:0"
    assert out[1]["phone-a"]["db"] == -55.0 and out[1]["room"] == "комната 1" and out[3]["room"] == "комната 1"


def _zone_tick():
    seg = ds.Segment("w3:zone:0", T0, T0 + 10, None, (), "zone", ("место A", "комната 2"))
    label = ds.tick_label(seg, T0 + 3, CLASSES, ROOMS)
    assert label["area_set"] == ["место A", "комната 2"] and label["room_set"] == ["комната 1", "комната 2"]
    assert label["state_known"] is False and label["standing_s"] is None and label["label_quality"] == "zone"


def _zone_entry_cut():
    walk = {"entry_cut_s": 8.0, "zones": [{"zone": "z", "from": "10:00:00", "to": "10:01:00"}, {"zone": "z", "from": "10:02:00", "to": "10:03:00", "started": True}]}
    segs = ds.zone_segments(walk, "2026-10-11", {"z": ["место A"]}, "w3:")
    assert [round(s.t1 - s.t0) for s in segs] == [52, 60] and segs[0].group == "w3:zone:0"


@pytest.mark.parametrize("segment", [_fresh_level, _point_on_path, _area_priority, _ticks, _zone_tick, _zone_entry_cut],
                         ids=["fresh_level", "point_on_path", "area_priority", "ticks", "zone_tick", "zone_entry_cut"])
def test_ticks_carry_fresh_level_area_and_standing_time(segment):
    """Тики набора: уровень только из свежих кадров с долей свежих, точка на пути с постоянной скоростью, область по приоритету
    (ключевое место, коридор, комната, ближайшая комната), время стоянки, у зоны - все её области без состояния, вход зоны обрезан, если старт не отмечен."""
    segment()
