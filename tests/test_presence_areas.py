"""Вместимость комнат (SAM-105): лишние ждут у входа, остаётся пришедший раньше."""
import pytest

from radar import presence_areas as pa

ROOMS = [{"id": "k", "title": "кухня", "x": 9.0, "z": 5.0, "w": 3.0, "d": 3.0}]
DOOR = {"title": "Дверь, открытая", "kind": "opening", "x": 8.65, "z": 5.5, "w": 0.7, "d": 0.1, "angle": 0.0}      # центр (9.0; 5.55) на западной стене кухни
WINDOW = {"title": "Окно", "kind": "opening", "x": 11.0, "z": 5.0, "w": 1.0, "d": 0.1, "angle": 0.0}


def _row(person, x, z, **extra):
    return {"person": person, "x": x, "z": z, "pose": "sit", **extra}


def _by(rows):
    return {r["person"]: r for r in rows}


def test_room_over_capacity_latecomer_waits_at_the_door_until_a_place_frees():
    areas = pa.Areas([{"room": "кухня", "max": 2}], ROOMS, [DOOR, WINDOW])
    assert areas.apply([_row("a", 10.0, 6.0)], 100.0)[0]["x"] == 10.0                  # один - место есть
    got = _by(areas.apply([_row("a", 10.0, 6.0), _row("b", 10.5, 6.5), _row("c", 11.0, 7.0)], 110.0))
    assert got["a"]["x"] == 10.0 and got["b"]["x"] == 10.5                             # двое помещаются
    assert abs(got["c"]["x"] - 8.6) < 0.1 and abs(got["c"]["z"] - 5.55) < 0.2 and got["c"]["pose"] == "stand"   # третий у двери, снаружи

    single = pa.Areas([{"room": "кухня", "max": 1}], ROOMS, [DOOR, WINDOW])
    single.apply([_row("b", 10.0, 6.0)], 100.0)                                        # b пришёл первым
    got = _by(single.apply([_row("a", 10.0, 6.0), _row("b", 10.2, 6.0)], 120.0))
    assert got["b"]["x"] == 10.2 and got["a"]["x"] != 10.0                             # a позже и ждёт, хотя имя раньше
    assert single.apply([_row("a", 10.0, 6.0, walking=True), _row("b", 10.2, 6.0)], 130.0)[0]["x"] == 10.0   # идущий проходит

    toilet = pa.Areas([{"label": "туалет", "x": 0.0, "z": 0.0, "w": 1.0, "d": 1.0, "max": 1}], [], [DOOR])
    toilet.apply([_row("a", 0.5, 0.5)], 100.0)
    assert toilet.apply([_row("a", 0.5, 0.5), _row("b", 0.6, 0.6)], 101.0)[1]["x"] != 0.6
    toilet.apply([_row("a", 5.0, 5.0), _row("b", 0.6, 0.6)], 102.0)                    # a вышел
    assert toilet.apply([_row("a", 5.0, 5.0), _row("b", 0.6, 0.6)], 103.0)[1]["x"] == 0.6   # место свободно


def _door_centre_inside():
    """Дверь, центр модели которой внутри комнаты: второй ждёт снаружи, со стороны коридора."""
    areas = pa.Areas([{"label": "туалет", "x": 7.3, "z": 6.8, "w": 1.0, "d": 1.7, "max": 1}], [],
                     [{"title": "Дверь, открытая", "kind": "opening", "x": 7.37, "z": 6.77, "w": 0.7, "d": 0.52}])
    waiting = areas.apply([_row("a", 7.8, 7.5), _row("b", 7.9, 8.0)], 100.0)[1]
    assert not (7.3 <= waiting["x"] <= 8.3 and 6.8 <= waiting["z"] <= 8.5)
    assert 6.0 < waiting["z"] < 6.8 and 7.3 < waiting["x"] < 8.3


def _bathroom_slots():
    """При слотах двое встают по местам: первый у двери внутри, второй в центре зоны предмета."""
    area = [{"label": "ванная", "x": 0.0, "z": 0.0, "w": 2.0, "d": 2.0, "max": 2, "slots": ["вход", "стиралка"]}]
    door = {"title": "Дверь", "kind": "opening", "x": 0.65, "z": -0.3, "w": 0.7, "d": 0.1}        # центр (1.0; -0.25) над северной стеной
    zones = [{"label": "стиралка", "x": 1.4, "z": 1.4, "w": 0.4, "d": 0.4}]
    got = _by(pa.Areas(area, [], [door]).apply([_row("a", 0.5, 0.5), _row("b", 0.6, 1.0)], 100.0, zones, []))
    assert abs(got["a"]["x"] - 1.0) < 0.01 and 0.2 < got["a"]["z"] < 0.5
    assert got["b"]["x"] == pytest.approx(1.6) and got["b"]["z"] == pytest.approx(1.6)
    assert got["a"]["pose"] == got["b"]["pose"] == "stand"


@pytest.mark.parametrize("case", [_door_centre_inside, _bathroom_slots], ids=["door_centre_inside", "bathroom_slots"])
def test_waiting_places_are_outside_the_door_or_at_named_slots(case):
    case()
