"""Перемотка потока (SAM-118): состояние на секунду записи совпадает с живым, кэш по метке, маршрут с at. План выдуманный: квартира 6 x 4 м со стеной и дверью, роутер слева."""
import math

from fastapi.testclient import TestClient

from radar import presence_api
from radar import presence_flow_live as pfl
from radar import presence_hub as ph
from radar import presence_rewind as prw
from radar import presence_rssi as rs
from radar import presence_tracks as pt
from radar.presence_random import Randomness

OUTER = [(0, 0, 6, 0), (6, 0, 6, 4), (6, 4, 0, 4), (0, 4, 0, 0)]
NAME, IFACE, SID = "Test-Phone", "phy1-ap0", "abc"
PLACES = {"left": [1.0, 1.0], "right": [5.0, 3.0]}
MODEL = {f"{NAME.lower()}@{IFACE}": {"l0_db": -40.0, "n": 2.0, "wall_db_per_m": 20.0}}
START = 1_000_000.0
WINDOW = 7


def wall(x1, z1, x2, z2, t=0.2, h=2.5):
    return {"x1": x1, "z1": z1, "x2": x2, "z2": z2, "t": t, "h": h}


def layout():
    door = {"id": "d", "title": "Дверная рама", "kind": "opening", "x": 2.6, "z": 1.95, "w": 0.8, "d": 0.1, "h": 2.0, "angle": math.pi / 2}
    return {"walls": [wall(*o) for o in OUTER] + [wall(3, 0, 3, 4, t=0.1)], "objects": [door], "router": {"x": 0.5, "z": 0.5, "h": 2.4}, "places": []}


def make_flow():
    return pfl.LiveFlow(layout(), MODEL, {}, {"place_xy": PLACES}, randomness=Randomness(1), window_slices=WINDOW)


def level_at(flow, xy):
    return float(pfl.expected_db(flow.world, layout(), MODEL[f"{NAME.lower()}@{IFACE}"])[flow.world.cell_at(*xy)])


def recorded_walk(tmp_path):
    """Треки: 40 с слева, потом 40 с справа, отсчёт раз в секунду; живой поток кормится теми же станциями."""
    log, live = pt.TrackLog(tmp_path), make_flow()
    left, right = level_at(live, PLACES["left"]), level_at(live, PLACES["right"])
    for k in range(80):
        station = rs.Station(SID, IFACE, left if k < 40 else right, 100, NAME)
        log.record([station], START + k)
        live.observe([station], START + k)
    return log, live


def distance(a, b):
    return sum(abs(x - y) for x, y in zip(a, b))


def argmax(p):
    return max(range(len(p)), key=p.__getitem__)


def place_mass(device, name):
    return next(item["p"] for item in device["places"] if item["place"] == name)


def test_rewound_state_matches_the_live_flow_at_that_second(tmp_path):
    """Перемотка по записанным трекам совпадает с живым потоком (вершина и масса у места, где телефон), другая секунда даёт другое состояние,
    пустые секунды тоже двигают окно, холодная сборка и свежий двойник равны. Перемотка кормится минутой до метки, живой поток шёл с начала:
    начало окна помнит чуть больше, поэтому сравнение с запасом, а не до бита."""
    log, live = recorded_walk(tmp_path)
    rewind = prw.Rewind(make_flow, log)
    at = START + 79
    now, then = rewind.state(at)["devices"][0], live.state()["devices"][0]
    assert argmax(now["p"]) == argmax(then["p"]) and abs(place_mass(now, "right") - place_mass(then, "right")) < 0.05
    assert distance(rewind.state(START + 30)["devices"][0]["p"], then["p"]) > 0.5                 # другая секунда записи - другое состояние
    assert rewind.state(at)["at"] == int(at) and rewind.span() == (START, START + 79)

    one_row = pt.TrackLog(tmp_path / "one")
    (tmp_path / "one").mkdir()
    one_row.record([rs.Station(SID, IFACE, level_at(live, PLACES["left"]), 100, NAME)], START)
    rows = one_row.read_range(START - 5, START + 5)
    at_row = prw.replay(make_flow(), rows, START, span_s=0).state()["devices"][0]["p"]
    later = prw.replay(make_flow(), rows, START + 5, span_s=10).state()["devices"][0]["p"]
    assert distance(at_row, later) > 0.01                                                         # отсчёт один, а пустые секунды после него - тоже срезы

    twin = live.fresh()
    assert twin.world is live.world and twin.floor_world is live.floor_world and twin.state()["devices"] == []
    assert live.state()["devices"]                                                                # живой поток не тронут
    assert prw.Rewind(live.fresh, log).state(START + 50)["devices"][0]["p"] == prw.Rewind(make_flow, log).state(START + 50)["devices"][0]["p"]


def test_rewind_builds_a_second_once_and_bounds_the_cache(tmp_path):
    """Секунда собирается один раз (та же целая секунда - из кэша), кэш ограничен: вытесненная секунда собирается заново."""
    log, _ = recorded_walk(tmp_path)
    built = []
    rewind = prw.Rewind(lambda: built.append(1) or make_flow(), log, cache_size=2)
    rewind.state(START + 50), rewind.state(START + 50.7), rewind.state(START + 51), rewind.state(START + 52)
    assert len(built) == 3
    rewind.state(START + 52)
    assert len(built) == 3
    rewind.state(START + 50)
    assert len(built) == 4


def test_flow_route_serves_the_past_by_at_and_reports_the_span(tmp_path):
    """`/sam/presence/flow?at=` отдаёт прошлое, без `at` - настоящее, `/rewind/span` - границы записи; без потока - enabled=false."""
    log, live = recorded_walk(tmp_path)
    hub = ph.PresenceHub(ph.MODE_RSSI, layout(), clock=lambda: START, flow=live, rewind=prw.Rewind(make_flow, log), tracks=log)
    client = TestClient(presence_api.create_app(hub))
    past = client.get(f"/sam/presence/flow?detail=1&second=all&at={START + 30}").json()
    assert past["at"] == int(START + 30) and past["devices"][0]["slices"] and "at" not in client.get("/sam/presence/flow").json()
    now = client.get("/sam/presence/flow").json()["devices"][0]["p"]
    assert distance(past["devices"][0]["p"], now) > 0.5                                           # прошлое другое
    assert argmax(client.get(f"/sam/presence/flow?at={START + 79}").json()["devices"][0]["p"]) == argmax(now)     # настоящее то же, живой поток не тронут
    assert client.get("/sam/presence/rewind/span").json() == {"enabled": True, "from": START, "to": START + 79}
    plain = ph.PresenceHub(ph.MODE_RSSI, layout(), clock=lambda: START, flow=live)
    assert TestClient(presence_api.create_app(plain)).get("/sam/presence/rewind/span").json() == {"enabled": False}
    assert prw.Rewind(lambda: None, pt.TrackLog(tmp_path)).state(START) == {"enabled": False, "at": int(START)}
