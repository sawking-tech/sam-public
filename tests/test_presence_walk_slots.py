"""Слоты проходок (SAM-118): окна места по привычкам, допуск времени в цепочках, сопоставление точек проходки местам привычек;
скрипт scripts/radar_walk_slots.py собирает то же из файлов."""
import json

from radar import presence_walk_slots as ws
from tests.fakes_presence import load_script

HABITS = {
    "step_min": 15,
    "place_xy": {"bed": [1.0, 1.0], "toilet": [8.0, 8.0], "kitchen": [11.0, 8.0]},
    "people": {
        "a": [
            {"id": "sleep", "share": 0.9, "places": {"bed": 1.0}, "when": [{"days": "all", "from": "01:00", "to": "08:30"}]},
            {"id": "wake", "share": 0.4, "places": {"toilet": 1.0}, "when": [{"days": "all", "from": "08:30", "to": "09:00"}]},
            {"id": "lunch", "share": 0.5, "places": {"kitchen": 1.0}, "when": [{"days": "weekdays", "from": "13:00", "to": "14:00"}]},
        ]
    },
}


def test_place_windows_follow_activity_hours_and_merge_days():
    """Окно места - только часы его занятия, дни с одинаковым окном сливаются, туалет во сне окна не получает;
    начало цепочки требует правдоподобия каждого места с допуском в шаг; ход относится к занятиям, включающим место назначения."""
    assert [(w.days, w.start, w.end) for w in ws.place_windows(HABITS, "a", "bed")] == [(ws.ph.WEEKDAYS, "01:00", "08:30")]
    assert [(w.days, w.start, w.end) for w in ws.place_windows(HABITS, "a", "kitchen")] == [(ws.ph.WEEKDAYS[:5], "13:00", "14:00")]
    assert [(w.start, w.end) for w in ws.place_windows(HABITS, "a", "toilet")] == [("08:30", "09:00")]

    starts = ws.chain_starts(HABITS, "a", [("bed", 0), ("toilet", 120)])
    assert starts and starts[0].clock == "08:15"
    assert ws.chain_starts(HABITS, "a", [("bed", 0), ("kitchen", 60)]) == []

    assert [(c.activity, c.share) for c in ws.classify_leg(HABITS, "a", "toilet")] == [("wake", 0.4)]
    assert ws.classify_leg(HABITS, "a", "kitchen")[0].when == ("weekdays 13:00-14:00",)
    assert ws.classify_leg(HABITS, "a", "nowhere") == []


def _habits_two_people() -> dict:
    sleep = {"id": "sleep", "share": 0.9, "places": {"bed": 1.0}, "when": [{"days": "all", "from": "00:00", "to": "08:00"}]}
    work = {"id": "work", "base": True, "places": {"desk": 1.0}, "when": [{"days": "all", "from": "08:00", "to": "24:00"}]}
    return {"step_min": 15, "place_xy": {"bed": [1.0, 1.0], "desk": [8.0, 8.0]}, "people": {"first": [sleep, work], "second": [work]}}


def _walks() -> dict:
    places = {"P01": {"x": 1.0, "z": 1.0, "room": "R01"}, "P02": {"x": 8.0, "z": 8.0, "room": "R01"}}
    return {"rooms": {"R01": {"x": 0.0, "z": 0.0, "w": 10.0, "d": 10.0}}, "places": places,
            "stands": [{"place": "P01", "t0": 0, "t1": 30}, {"place": "P02", "t0": 60, "t1": 90}],
            "legs": [{"from": "P01", "to": "P02", "t0": 30, "t1": 60}]}


def test_label_walk_file_matches_stands_and_chains_for_carrier_or_everyone(tmp_path):
    """Точка проходки берёт ближайшее место привычек в радиусе, иначе единственное место своей комнаты; у человека окна по стоянкам
    и цепочки только там, где сопоставлены все стоянки; скрипт по файлам размечает носителя или всех, если носитель не задан."""
    rooms = [ws.Room(0, 0, 5, 3), ws.Room(6, 6, 6, 3)]
    matcher = ws.HabitPlaceMatcher({k: tuple(v) for k, v in HABITS["place_xy"].items()}, rooms)
    assert matcher.match((8.2, 8.1)) == "toilet" and matcher.match((0.3, 2.5)) == "bed"
    assert matcher.match((3.0, 8.0)) is None and matcher.match((6.5, 6.5)) is None

    stands = [{"place": "P01", "t0": 0, "t1": 30}, {"place": "P02", "t0": 100, "t1": 130}, {"place": "P03", "t0": 200, "t1": 230}]
    out = ws.label_walk(HABITS, stands, matcher, {"P01": (1.0, 1.0), "P02": (8.0, 8.0), "P03": (3.0, 8.0)}, ["a"])
    assert out["matched"] == {"P01": "bed", "P02": "toilet", "P03": None}
    assert set(out["persons"]["a"]["stands"]) == {"P01", "P02"}
    assert [(c["from"], c["to"]) for c in out["persons"]["a"]["chains"]] == [("P01", "P02")]

    walks_file, habits_file = tmp_path / "walks.json", tmp_path / "habits.json"
    walks_file.write_text(json.dumps(_walks()), encoding="utf8")
    habits_file.write_text(json.dumps(_habits_two_people()), encoding="utf8")
    script = load_script("radar_walk_slots")
    one = script.label_file(str(walks_file), str(habits_file), "first")
    assert list(one["persons"]) == ["first"] and one["matched"] == {"P01": "bed", "P02": "desk"}
    assert list(script.label_file(str(walks_file), str(habits_file))["persons"]) == ["first", "second"]
