"""Живой поток положения (SAM-118): опросы станций идут в окно по субъекту, станции без модели пропускаются, после долгого молчания прошлое сбрасывается.
План выдуманный (tests/fakes_presence.flow_layout): квартира 6 x 4 м, внутренняя стена на x = 3 с дверью, роутер в левом углу. Время - числа опросов, не часы."""
import json
import math

import pytest
from fastapi.testclient import TestClient

from radar import presence as pr
from radar import presence_api
from radar import presence_flow_live as pfl
from radar import presence_hub as ph
from radar import presence_rssi as rs
from radar.presence_random import Randomness
from tests.fakes_presence import (FLOW_IFACE as IFACE, FLOW_MODEL as MODEL, FLOW_NAME as NAME, FLOW_PLACES as PLACES, FLOW_START as START,
                                  FLOW_TEST_WINDOW as TEST_WINDOW, flow_layout as layout, isolated_hub_env, write_layout)

OTHER_IFACE = "phy0-ap0"
ROOMS = [{"id": "left", "title": "левая", "x": 0, "z": 0, "w": 3, "d": 4}, {"id": "right", "title": "правая", "x": 3, "z": 0, "w": 3, "d": 4}]     # файлу плана нужен список комнат


def make_flow(models=MODEL, **kwargs):
    return pfl.LiveFlow(layout(), models, {}, {"place_xy": PLACES}, randomness=Randomness(1), window_slices=TEST_WINDOW, **kwargs)


def level_at(flow, xy):
    cell = flow.world.cell_at(*xy)
    return float(pfl.expected_db(flow.world, layout(), MODEL[f"{NAME.lower()}@{IFACE}"])[cell])


def feed(flow, level, seconds, start, name=NAME, inactive_ms=100, per_second=2, iface=IFACE, sid="abc"):
    """Опросы с шагом в полсекунды, как у живого роутера."""
    for k in range(int(seconds * per_second)):
        flow.observe([rs.Station(sid, iface, level, inactive_ms, name)], start + k / per_second)
    return start + seconds


def feed_hub(hub, level, seconds, start):
    for k in range(int(seconds * 2)):
        hub.feed_stations([rs.Station("abc", IFACE, level, 100, NAME)], start + k / 2)
    return start + seconds


def top_place(flow):
    return flow.state()["devices"][0]["places"][0]["place"]


def slices_of(flow):
    return flow.state(detail=True)["devices"][0]["slices"]


def hub_with_flow(flow, **kwargs):
    return ph.PresenceHub(ph.MODE_RSSI, layout(), clock=lambda: START, flow=flow, **kwargs)


@pytest.mark.parametrize("place", ["left", "right"])
def test_flow_puts_a_modelled_station_at_the_place_of_its_level(place):
    flow = make_flow()
    feed(flow, level_at(flow, PLACES[place]), 10, START)
    state = flow.state()
    assert state["enabled"] and len(state["cells"]) == len(flow.world) and top_place(flow) == place
    assert sum(item["p"] for item in state["devices"][0]["places"]) <= 1.0 + 1e-6


def test_flow_ignores_unmodelled_and_silent_stations():
    unmodelled = make_flow(models={})
    feed(unmodelled, -50.0, 5, START)
    assert unmodelled.state()["devices"] == [] and NAME in unmodelled.state()["skipped"]
    silent = make_flow()
    feed(silent, level_at(silent, PLACES["left"]), 5, START, inactive_ms=pfl.FRESH_MS + 1)     # несвежий опрос не считается
    assert silent.state()["devices"] == []


def test_flow_forgets_a_device_after_long_silence():
    # после STALE_S прошлое сброшено: новое место находится сразу
    flow = make_flow()
    end = feed(flow, level_at(flow, PLACES["left"]), 15, START)
    feed(flow, level_at(flow, PLACES["right"]), 12, end + 2 * pfl.STALE_S)
    assert top_place(flow) == "right"
    # замолчавший диапазон уходит из состояния, активный остаётся
    bands = make_flow(models={**MODEL, f"{NAME.lower()}@{OTHER_IFACE}": MODEL[f"{NAME.lower()}@{IFACE}"]})
    level = level_at(bands, PLACES["left"])
    feed(bands, level, 5, START, sid="five")
    feed(bands, level, 5, START + 100, sid="two", iface=OTHER_IFACE)                          # телефон перешёл на 2,4 ГГц
    assert [d["iface"] for d in bands.state()["devices"]] == [OTHER_IFACE]
    # у замолчавшего устройства пятен нет
    gone = make_flow()
    feed(gone, level_at(gone, PLACES["left"]), 12, START)
    gone.observe([], START + 12 + pfl.STALE_S + 1)
    assert gone.spots(NAME) == []


def test_flow_window_keeps_one_slice_per_second_through_gaps():
    # секунды без свежего опроса тоже срезы: окно полное и без сброса
    flow = make_flow()
    end = feed(flow, level_at(flow, PLACES["left"]), TEST_WINDOW + 3, START)
    for k in range(1, 6):                                                            # станция замолчала на 5 секунд: опросов нет вообще
        flow.observe([], end + k)
    assert len(slices_of(flow)) == TEST_WINDOW
    feed(flow, level_at(flow, PLACES["left"]), 2, end + 6)                           # снова слышно: окно то же, не пустое
    assert len(slices_of(flow)) == TEST_WINDOW
    # четыре опроса в секунду - один срез на секунду
    dense = make_flow()
    feed(dense, level_at(dense, PLACES["left"]), 3, START, per_second=4)
    assert len(slices_of(dense)) == 3


def test_flow_route_serves_state_or_off():
    flow = make_flow()
    hub = hub_with_flow(flow)
    feed_hub(hub, level_at(flow, PLACES["left"]), 10, START)
    state = TestClient(presence_api.create_app(hub)).get("/sam/presence/flow").json()
    assert state["enabled"] and state["devices"][0]["places"][0]["place"] == "left"
    assert TestClient(presence_api.create_app(hub_with_flow(None))).get("/sam/presence/flow").json() == {"enabled": False}


def built_hub(tmp_path, plan, **env):
    """Хаб из окружения, как на стенде: план и модели уровня в tmp_path, остальные файлы радара тоже там."""
    models = tmp_path / "radio_model.json"
    models.write_text(json.dumps({"models": MODEL}), encoding="utf-8")
    return ph.build(isolated_hub_env(tmp_path, **{ph.FLAG_ENV: ph.MODE_RSSI, pr.LAYOUT_ENV: str(write_layout(tmp_path, {"rooms": ROOMS, **plan})),
                                                  pfl.RADIO_MODEL_ENV: str(models), **env}))


@pytest.mark.parametrize("env, plan, slices", [
    ({}, layout(), None),                                                             # без флага потока нет
    ({ph.FLOW_ENV: "1"}, {**layout(), "walls": []}, None),                            # битый план (нет стен): радар без потока
    ({ph.FLOW_ENV: "1", pfl.WINDOW_SLICES_ENV: "9"}, layout(), 9),                    # размер окна из окружения
    ({ph.FLOW_ENV: "1"}, layout(), pfl.pw_.WINDOW_SLICES),                            # по умолчанию - измеренное окно
], ids=["no-flag", "bad-plan", "window-env", "window-default"])
def test_hub_builds_flow_from_env(tmp_path, capsys, env, plan, slices):
    hub = built_hub(tmp_path, plan, **env)
    client = TestClient(presence_api.create_app(hub))
    if slices is None:
        assert client.get("/sam/presence/flow").json() == {"enabled": False}
        if env:
            assert "поток положения не собран" in capsys.readouterr().out
        return
    feed_hub(hub, -50.0, slices + 2, START)
    [device] = hub.flow.state(detail=True)["devices"]
    assert len(device["slices"]) == slices


def probability_of(flow, place):
    return next(item["p"] for item in flow.state()["devices"][0]["places"] if item["place"] == place)


def test_known_place_of_device_raises_its_probability():
    plain, placed = make_flow(), make_flow()
    hub = hub_with_flow(placed)
    hub.devices.places["test"] = "left"                                               # карта мест хаба: устройство посажено данными
    level = level_at(plain, PLACES["left"])
    feed(plain, level, 10, START)
    feed_hub(hub, level, 10, START)
    assert probability_of(placed, "left") > probability_of(plain, "left")


def test_flow_detail_for_chosen_second_and_device():
    # точка до окна появляется, только когда окно полное: это первая секунда прошлого шага
    filling = make_flow()
    feed(filling, level_at(filling, PLACES["left"]), TEST_WINDOW - 1, START)
    assert filling.state(detail=True)["devices"][0]["before_point"] is None
    feed(filling, level_at(filling, PLACES["left"]), 4, START + TEST_WINDOW)
    before = filling.state(detail=True)["devices"][0]["before_point"]
    assert before is not None and {"cell", "x", "z"} <= set(before)
    flow = make_flow()
    feed(flow, level_at(flow, PLACES["right"]), 12, START)
    n, last = len(flow.world), TEST_WINDOW - 1
    plain, detailed = flow.state()["devices"][0], flow.state(detail=True, second=pfl.ALL_SECONDS)["devices"][0]
    # срезы слоя с частями, переход, выход, пути и точки
    assert "slices" not in plain and len(detailed["slices"]) == TEST_WINDOW
    assert "rings" in detailed["slices"][-1]["parts"]
    assert detailed["transition"] is not None and detailed["output"].keys() <= {"preference", "placed"}
    assert len(detailed["paths"]) == pfl.VIEW_PATHS and all(0 <= c < n for path in detailed["paths"] for c in path)
    assert all(len(sl["upto"]) == k + 1 and all(len(g) == n and abs(sum(g) - 1) < 0.01 for g in sl["upto"]) for k, sl in enumerate(detailed["slices"]))
    assert detailed["point"] is not None and detailed["previous_point"] is not None
    # у каждой секунды свои пути, кончающиеся на ней
    assert all(len(sl["paths"]) == pfl.VIEW_PATHS and all(len(path) == k + 1 and all(0 <= c < n for c in path) for path in sl["paths"]) for k, sl in enumerate(detailed["slices"]))
    # рост по каждой секунде; у последней поле - итоговое распределение
    default = flow.state(detail=True)["devices"][0]
    assert all(0 < sl["growth"]["peak"] <= 1 and 0 <= sl["growth"]["mass_near"] <= 1 and sl["growth"]["area_m2"] > 0 for sl in default["slices"])
    assert default["slices"][-1]["growth"]["peak"] == pytest.approx(max(default["p"]), abs=1e-3)
    # законные пути и пути окна - только у выбранной секунды, поля, точки и рост - у всех
    assert [bool(sl["upto"]) for sl in default["slices"]] == [k == last for k in range(TEST_WINDOW)]
    assert [bool(sl["paths"]) for sl in default["slices"]] == [k == last for k in range(TEST_WINDOW)]
    chosen = flow.state(detail=True, second=2)["devices"][0]["slices"]
    assert [bool(sl["upto"]) for sl in chosen] == [k == 2 for k in range(TEST_WINDOW)] and len(chosen[2]["upto"]) == 3 and len(chosen[2]["paths"][0]) == 3
    assert all(sl["growth"] and sl["point"] is not None for sl in chosen)
    # подъём к роутеру в точке: роутер слева (x 0,5), точка справа - уровень растёт к меньшему x
    ascent = default["ascent"]
    assert math.hypot(ascent["dx"], ascent["dz"]) == pytest.approx(1.0, abs=1e-3) and ascent["db_per_m"] > 0 and ascent["dx"] < 0
    # детальный вид - только у выбранного устройства
    two = make_flow()
    for sid in ("one", "two"):
        feed(two, level_at(two, PLACES["left"]), 10, START, sid=sid)
    assert [("slices" in d) for d in two.state(detail=True, only=f"one|{IFACE}")["devices"]] == [True, False]


def test_owner_follows_phone_and_disappears_after_silence():
    owners = {"test": "p1"}
    flow = make_flow(owners=owners)
    hub = hub_with_flow(flow, owners=owners)
    end = feed_hub(hub, level_at(flow, PLACES["left"]), 12, START)
    state = flow.state(detail=True)
    person = state["people"][0]
    # у хозяина субъект человека с тем же составом записи, что у телефона, рядом с телефоном
    assert person["id"] == "person:p1" and person["iface"] == pfl.PERSON_IFACE and person["name"] == "p1" and sum(person["p"]) == pytest.approx(1.0, abs=0.01)
    assert person["slices"] and person["point"] is not None and person["ascent"] is None
    phone, at = state["devices"][0]["point"], person["point"]
    assert math.hypot(phone["x"] - at["x"], phone["z"] - at["z"]) <= 1.5
    # хаб ставит хозяина туда, где его субъект
    [row] = hub.identities(end)
    assert row["person"] == "p1" and math.hypot(row["x"] - at["x"], row["z"] - at["z"]) < 1.0
    # полминуты тишины - человек стоит, после STALE_S - уходит
    flow.observe([], end + 30)
    assert flow.state()["people"] and flow.person_spot("p1")
    flow.observe([], end + pfl.STALE_S + 5)
    assert flow.state()["people"] == [] and flow.person_spot("p1") == []
    # без хозяина в файле людей нет ни в потоке, ни у хаба
    nobody = make_flow()
    lone = hub_with_flow(nobody, owners={})
    feed_hub(lone, level_at(nobody, PLACES["left"]), 12, START)
    assert nobody.state()["people"] == [] and lone.identities(START + 12) == []


def test_device_spots_are_lobes_and_point_spot():
    flow = make_flow()
    hub = hub_with_flow(flow, owners={"test": "p2"})                                  # хозяин у хаба, у потока субъекта человека нет
    end = feed_hub(hub, level_at(flow, PLACES["left"]), 12, START)
    # пятна - лепестки бросков с долями по убыванию; лепестки берут клетки не слабее 15% наибольшей
    spots = flow.spots(NAME)
    assert spots and 0.5 < sum(share for _, _, share, _ in spots) <= 1.0 + 1e-6 and spots == sorted(spots, key=lambda spot: -spot[2])
    assert flow.spots("unknown") == [] and flow.spots(NAME, OTHER_IFACE) == [] and flow.spots(NAME, IFACE) == spots
    # точечное пятно одно - в точке потока, с долей её лепестка
    [spot] = flow.point_spot(NAME)
    point = flow.point_of(NAME)
    assert (spot[0], spot[1]) == (point["x"], point["z"]) and 0 < spot[2] <= 1 and spot[3] == point["place"]
    assert flow.point_spot("unknown") == []
    # хаб ставит хозяина по точечному пятну устройства
    [row] = hub.identities(end)
    assert row["person"] == "p2" and math.hypot(row["x"] - spot[0], row["z"] - spot[1]) < 1.0


@pytest.mark.parametrize("models, name, iface, expected", [
    # модель диапазона со сдвигом устройства становится моделью каждого устройства
    ({"models": {"phy1-ap0": {"iface": "phy1-ap0", "band": "5 ГГц", "l0_db": -45.0, "n": 3.0, "wall_db_per_m": 30.0}},
      "device_offsets_db": {"a@phy1-ap0": 0.0, "b@phy1-ap0": -2.0, "c@phy0-ap0": 0.0}}, "B", "phy1-ap0", {"l0_db": -47.0, "n": 3.0}),
    # у диапазона нет модели: устройство пропускается и попадёт в skipped
    ({"models": {"phy1-ap0": {"iface": "phy1-ap0", "l0_db": -45.0, "n": 3.0, "wall_db_per_m": 30.0}},
      "device_offsets_db": {"c@phy0-ap0": 0.0}}, "C", "phy0-ap0", None),
    # старый файл по устройствам проходит как есть
    ({"models": {"a@phy1-ap0": {"l0_db": -40.0, "n": 2.0, "wall_db_per_m": 20.0}}}, "A", "phy1-ap0", {"l0_db": -40.0, "n": 2.0}),
    # новый телефон берёт модель той же модели телефона на своём диапазоне
    ({"models": {"redmi-note-13-pro-5g@phy1-ap0": {"l0_db": -40.0}, "s24-ultra@phy1-ap0": {"l0_db": -38.0}, "redmi-note-13-pro-5g@phy0-ap0": {"l0_db": -45.0}}},
     "Redmi-Note-13-Pro", "phy1-ap0", {"l0_db": -40.0}),
    ({"models": {"s24-ultra@phy1-ap0": {"l0_db": -38.0}}}, "S24-Ultra", "phy1-ap0", {"l0_db": -38.0}),
    ({"models": {"s24-ultra@phy1-ap0": {"l0_db": -38.0}}}, "Pixel", "phy2-ap0", None),
    ({"models": {"s24-ultra@phy1-ap0": {"l0_db": -38.0}}}, None, "phy1-ap0", None),
], ids=["band-shift", "band-without-model", "old-file", "same-model", "other-model", "unknown", "no-name"])
def test_level_model_resolved_per_device_and_band(models, name, iface, expected):
    model = pfl.model_for(pfl.resolve_models(models), name, iface)
    if expected is None:
        assert model is None
    else:
        assert {k: model[k] for k in expected} == expected
