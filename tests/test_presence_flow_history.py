"""История потока положения (SAM-118): строка на субъекта в секунду, файлы суток со сроком, чтение по уверенности, тепловая карта, запись из хаба и маршруты. Данные выдуманные."""
import math
from datetime import datetime

from fastapi.testclient import TestClient

from radar import presence_api
from radar import presence_flow_history as pfh
from radar import presence_flow_live as pfl
from radar import presence_hub as ph
from radar import presence_rssi as rs
from radar.presence_random import Randomness

DAY = datetime(2026, 10, 11, 12, 0).timestamp()
OUTER = [(0, 0, 6, 0), (6, 0, 6, 4), (6, 4, 0, 4), (0, 4, 0, 0)]
NAME, IFACE = "Test-Phone", "phy1-ap0"
MODEL = {f"{NAME.lower()}@{IFACE}": {"l0_db": -40.0, "n": 2.0, "wall_db_per_m": 20.0}}
OWNER = "resident-a"
PERSON_ID = f"person:{OWNER}|person"


def subject(sid="abc", iface="phy1-ap0", name="S24-Test", x=1.0, z=2.0, places=(("кухня", 0.62), ("коридор", 0.2))):
    return {"id": sid, "iface": iface, "name": name, "point": {"cell": 3, "x": x, "z": z}, "places": [{"place": p, "p": q} for p, q in places]}


def wall(x1, z1, x2, z2, t=0.2, h=2.5):
    return {"x1": x1, "z1": z1, "x2": x2, "z2": z2, "t": t, "h": h}


def layout():
    door = {"id": "d", "title": "Дверная рама", "kind": "opening", "x": 2.6, "z": 1.95, "w": 0.8, "d": 0.1, "h": 2.0, "angle": math.pi / 2}
    return {"walls": [wall(*o) for o in OUTER] + [wall(3, 0, 3, 4, t=0.1)], "objects": [door], "router": {"x": 0.5, "z": 0.5, "h": 2.4}, "places": []}


def test_hub_records_the_flow_once_a_second_and_routes_serve_rows_and_heat(tmp_path):
    flow = pfl.LiveFlow(layout(), MODEL, {}, {"place_xy": {"left": [1.0, 1.0]}}, randomness=Randomness(1), window_slices=7, owners={"test": OWNER})
    history = pfh.FlowHistory(tmp_path)
    hub = ph.PresenceHub(ph.MODE_RSSI, layout(), clock=lambda: DAY, flow=flow, flow_history=history)
    level = float(pfl.expected_db(flow.world, layout(), MODEL[f"{NAME.lower()}@{IFACE}"])[flow.world.cell_at(1.0, 1.0)])
    for k in range(20):                                                                       # опрос раз в полсекунды
        hub.feed_stations([rs.Station("abc", IFACE, level, 100, NAME)], DAY + k / 2)
    rows = history.read_range(DAY, DAY + 20)
    assert {r["id"] for r in rows} == {f"abc|{IFACE}", PERSON_ID} and len(rows) >= 16          # по строке в секунду на телефон и человека, не на опрос
    assert all(len(r["places"]) <= pfh.PLACES_KEPT and "sure" in r and "x" in r for r in rows)
    client = TestClient(presence_api.create_app(hub))
    served = client.get(f"/sam/presence/flow/history?since={DAY}&until={DAY + 20}&subject={OWNER}").json()["rows"]
    assert served and all(r["id"] == PERSON_ID for r in served)                                # отбор по части имени субъекта
    heat = client.get(f"/sam/presence/flow/heat?since={DAY}&until={DAY + 20}&min_sure=0").json()
    assert heat["cell_m"] == pfh.HEAT_CELL_M and sum(heat["cells"].values()) == len(rows)
    sure = client.get(f"/sam/presence/flow/heat?since={DAY}&until={DAY + 20}&min_sure=0.5").json()["cells"]
    assert sum(sure.values()) == len([r for r in rows if r["sure"] >= 0.5])                    # тепло только из уверенных строк

    own = pfh.FlowHistory(tmp_path / "own")                                                    # запись и чтение по времени, субъекту и уверенности
    person = subject(f"person:{OWNER}", pfl.PERSON_IFACE, OWNER, 1.2, 2.1, (("кухня", 0.3),))
    many = subject(places=tuple((f"м{i}", 0.5 - i * 0.05) for i in range(8)))
    assert own.record([many, person, {"id": "a", "iface": "b", "point": None, "places": []}], DAY) == 2   # субъект без точки строки не даёт
    assert own.record([subject()], DAY + 0.5) == 0                                             # слишком скоро
    assert own.record([subject(x=1.3)], DAY + 1.0) == 1
    got = own.read_range(DAY, DAY + 5)
    assert [r["x"] for r in got] == [1.0, 1.2, 1.3] and got[0]["id"] == "abc|phy1-ap0" and len(got[0]["places"]) == pfh.PLACES_KEPT
    assert got[0]["sure"] == 0.5 and [r["id"] for r in own.read_range(DAY, DAY + 5, subject=OWNER)] == [PERSON_ID]
    assert len(own.read_range(DAY, DAY + 5, min_sure=0.5)) == 2                                # порог 0,5 включительно, человек с 0,3 - нет
    assert pfh.FlowHistory(None).record([subject()], DAY) == 0 and pfh.FlowHistory(None).read_range(0, 1) == []


def test_old_days_are_pruned_by_keep_days(tmp_path):
    history = pfh.FlowHistory(tmp_path, keep_days=2)
    for d in range(4):
        history.record([subject()], DAY + d * 86400)
    assert sorted(p.name for p in tmp_path.glob("flow-*.jsonl")) == ["flow-20261013.jsonl", "flow-20261014.jsonl"]
    assert pfh.stale_days(["flow-20261001.jsonl", "flow-20261011.jsonl", "other.txt"], datetime(2026, 10, 11), 5) == ["flow-20261001.jsonl"]       # хранятся 7-11 октября
    assert pfh.stale_days(["flow-20261001.jsonl"], datetime(2026, 10, 11), 14) == []                                                                 # 1 октября ещё в двух неделях
