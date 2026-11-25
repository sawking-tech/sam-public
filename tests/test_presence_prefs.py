"""Карта предпочтений устройства (SAM-118): вес места, личная карта над общей и нормирование."""
from datetime import datetime

import pytest

from radar import presence_prefs as pp

PREFS = {"default": {"кровать в спальне": 1.0, "туалет": 0.1, "кухня": 0.5}, "beta": {"кресло": 1.0, "кровать в спальне": 2.0}}


@pytest.mark.parametrize("posterior, key, expected", [
    ({"туалет": 0.5, "кровать в спальне": 0.5}, "alpha@phy1-ap0", {"кровать в спальне": 10 / 11, "туалет": 1 / 11}),   # ровный уровень уступает предпочтению
    ({"туалет": 0.5, "кровать в спальне": 0.5}, "beta-max-13@phy1-ap0", {"кровать в спальне": 20 / 21, "туалет": 1 / 21}),   # личная таблица над общей
    ({"балкон": 0.5, "кровать в спальне": 0.5}, "alpha@phy1-ap0", {"балкон": 0.5, "кровать в спальне": 0.5}),   # неизвестное место нейтрально
    ({"туалет": 0.95, "кровать в спальне": 0.05}, "alpha@phy1-ap0", None),                                # сильный уровень побеждает
    ({"туалет": 0.5, "кровать в спальне": 0.5}, None, {"туалет": 0.5, "кровать в спальне": 0.5}),            # без таблиц - как есть
])
def test_device_place_weight_takes_personal_over_default_and_loses_to_a_strong_level(posterior, key, expected):
    """Личная таблица над общей, неизвестное место нейтрально, ровный уровень уступает предпочтению, сильный уровень побеждает, итог нормирован."""
    result = pp.apply(posterior, PREFS if key else {}, key or "alpha")
    assert sum(result.values()) == pytest.approx(1.0)
    if expected is None:
        assert result["туалет"] > 0.6
    else:
        assert result == pytest.approx(expected)
    assert pp.apply({}, PREFS, "alpha") == {}


# Названия предметов плана приходят из Sweet Home 3D с заглавной буквы, названия мест у отпечатков и привычек строчные: сравнение идёт через place_key.
TABLE, BED, TOILET = "Стол", "Кровать", "Унитаз (примерно)"
KITCHEN, OFFICE, BEDROOM, TOILET_PLACE, HALL = (pp.place_key(name) for name in ("Кухня", "Кабинет", "Кровать", "Туалет", "Коридор"))
SIZE_M = 1.0
SLEEP_AT, DINNER_AT, NOON_AT = (datetime(2026, 10, 12, h, m) for h, m in ((3, 0), (20, 30), (12, 0)))
OUTLET_BOOST = 1.6
OWNER = "owner"


def piece(title: str, x: float, z: float, w: float = SIZE_M, d: float = SIZE_M) -> dict:
    return {"kind": "furniture", "title": title, "x": x, "z": z, "w": w, "d": d}


def centre(item: dict) -> tuple[float, float]:
    return item["x"] + item["w"] / 2, item["z"] + item["d"] / 2


def test_derived_place_weight_is_surface_times_occupancy_boosted_by_sockets():
    """Выведенный вес: поверхность на время людей, у унитаза и коридора поверхности нет, розетка поднимает место (там телефон заряжается)."""
    bed, table, toilet = piece(BED, 4.0, 0.0, 1.8, 2.0), piece(TABLE, 10.0, 6.0), piece(TOILET, 7.5, 7.8, 0.4, 0.7)
    places = {BEDROOM: centre(bed), KITCHEN: centre(table), TOILET_PLACE: centre(toilet), HALL: (6.6, 5.0)}
    occupancy = {BEDROOM: 9.0, KITCHEN: 1.0, TOILET_PLACE: 0.5, HALL: 1.0}
    weights = pp.derive(places, [bed, table, toilet], occupancy)
    assert weights[BEDROOM] == 1.0 and 0.2 < weights[KITCHEN] < 0.5                        # стол на кухне: бывают меньше, но не втрое
    assert weights[TOILET_PLACE] < weights[KITCHEN] and weights[HALL] < weights[KITCHEN]    # унитаз не поверхность, у коридора нет поверхности
    assert all(w >= pp.MIN_WEIGHT for w in weights.values())
    assert pp.has_surface([bed, table, toilet], *centre(table)) and not pp.has_surface([bed, table, toilet], *centre(toilet))
    kitchen_table, office_table = piece(TABLE, 0.0, 0.0), piece(TABLE, 5.0, 0.0)
    places = {KITCHEN: centre(kitchen_table), OFFICE: centre(office_table)}
    equal = {KITCHEN: 1.0, OFFICE: 1.0}
    plain = pp.derive(places, [kitchen_table, office_table], equal)
    boosted = pp.derive(places, [kitchen_table, office_table], equal, {KITCHEN: OUTLET_BOOST})
    assert plain[KITCHEN] == plain[OFFICE] == 1.0 and boosted[KITCHEN] == 1.0 and boosted[OFFICE] == pytest.approx(1 / OUTLET_BOOST, abs=0.001)


def sleeper_habits() -> dict:
    sleep = {"id": "sleep", "share": 0.95, "places": {BEDROOM: 1.0}, "when": [{"days": "all", "from": "01:00", "to": "09:00"}]}
    dinner = {"id": "dinner", "share": 0.8, "places": {KITCHEN: 1.0}, "when": [{"days": "all", "from": "20:00", "to": "21:00"}]}
    work = {"id": "work", "base": True, "places": {OFFICE: 1.0}, "when": [{"days": "all", "from": "09:00", "to": "20:00"}]}
    return {"step_min": 15, "people": {OWNER: [sleep, dinner, work]}}


def test_device_prefs_follow_the_owners_habit_slot():
    """Ночью телефон хозяина тянет к кровати, за ужином к кухне; таблица на слот считается раз, ручные веса держатся, без привычек - файл."""
    habits = sleeper_habits()
    night, dinner = pp.slot_occupancy(habits, None, OWNER, SLEEP_AT), pp.slot_occupancy(habits, None, OWNER, DINNER_AT)
    assert max(night, key=night.get) == BEDROOM and max(dinner, key=dinner.get) == KITCHEN
    assert pp.slot_occupancy(habits, None, OWNER, NOON_AT).get(BEDROOM, 0.0) < 0.05          # днём кровать не любимое место
    table, bed = piece(TABLE, 0.0, 0.0), piece(BED, 5.0, 0.0)
    places = {KITCHEN: centre(table), BEDROOM: centre(bed)}
    content = {pp.PrefsKey.DEFAULT: {KITCHEN: 0.5}, pp.PrefsKey.FIXED: {KITCHEN: 0.03}, "beta": {BEDROOM: 2.0}, pp.PrefsKey.NOTE: "пояснение"}
    prefs = pp.DevicePrefs(content, sleeper_habits(), None, [table, bed], places, lambda key: OWNER)
    night = prefs.table("beta@phy1", SLEEP_AT.timestamp())
    assert night[pp.PrefsKey.DEFAULT][KITCHEN] == 0.03 and night[pp.PrefsKey.DEFAULT][BEDROOM] == 1.0 and night["beta"] == {BEDROOM: 2.0}
    assert prefs.table("beta@phy1", SLEEP_AT.timestamp()) is night                              # тот же слот: считается один раз
    assert pp.PrefsKey.NOTE not in night
    bare = pp.DevicePrefs(content, {}, None, [], {}, lambda key: None)
    assert bare.table("alpha", 0.0)[pp.PrefsKey.DEFAULT] == {KITCHEN: 0.5}
