"""Где кто словами (SAM-105): комната, зона и мебель рядом, взгляд, отбор по имени, ручное место, «со слов», маршруты радара,
автопилот, реальный режим, тепло, стоянка и журнал. Планы выдуманные; часы хаба внедрены, цикл тиков идёт без задержки. Ручки SAM
и MCP-переходник - tests/test_presence_sam_api.py."""
import asyncio
import json
import logging
import math
import random

import pytest
from fastapi.testclient import TestClient

from radar import presence as pr
from radar import presence_api
from radar import presence_autopilot as ap
from radar import presence_hub as ph
from radar import presence_override as po
from radar import presence_routine as routine
from radar import presence_where as pw
from radar.presence_nav import Nav
from radar.presence_random import Randomness
from tests.fakes_presence import two_room_layout

TICK_WAIT_S = 30.0          # предохранитель от зависания цикла; тик идёт без задержки и приходит сразу
LAYOUT = {"rooms": [{"id": "r1", "title": "спальня", "x": 0, "z": 0, "w": 5, "d": 3},
                    {"id": "r2", "title": "зал", "x": 5, "z": 0, "w": 4, "d": 3}],
          "objects": [{"title": "Кровать", "x": 1, "z": 0.5, "w": 1.6, "d": 2.0},
                      {"title": "Диван", "x": 6, "z": 1, "w": 2, "d": 1}]}
ZONES = [{"id": "zone-кровать", "label": "кровать", "x": 0.5, "z": 0.2, "w": 2.5, "d": 2.5}]
IDENT = [{"person": "second", "x": 1.8, "z": 1.5, "pose": "lie", "confidence": 0.7, "sources": ["wifi"]},
         {"person": "first", "x": 7.0, "z": 1.5, "pose": "sit", "confidence": 0.4, "sources": ["wifi"]}]
TELL_LAYOUT = {"name": "t", "rooms": LAYOUT["rooms"], "objects": LAYOUT["objects"],
               "places": [{"label": "Диван в зале", "x": 6.5, "z": 1.5}], "people": {"first": {"name": "Первая"}, "second": {"name": "Вторая"}}}


def hub_at(layout, now=10.0, **kw) -> ph.PresenceHub:
    return ph.PresenceHub(ph.MODE_RSSI, {"name": "t", **layout}, clock=lambda: now, tick_s=0.0, **kw)


def tick(hub: ph.PresenceHub) -> str:
    """Один кадр цикла хаба для слушателя: автопилот, сохранения и рассылка, как у живого радара."""
    async def one():
        queue = hub.add_client()
        task = asyncio.create_task(hub.run())
        try:
            return await asyncio.wait_for(queue.get(), TICK_WAIT_S)
        finally:
            task.cancel()
            hub.remove_client(queue)
    return asyncio.run(one())


@pytest.fixture
def radar_log(caplog):
    """Журнал радара через caplog: файловый обработчик presence_log не ставится, уровень логгера возвращается после теста."""
    caplog.set_level(logging.INFO, logger="sam.radar")
    return caplog


# --- где кто словами ---

def test_where_names_room_zone_near_and_filters_by_name():
    """Словами: комната, зона, мебель рядом, поза и уверенность; отбор по части имени; точка вне комнат без комнаты; без положения - пустое место."""
    rows = {r["person"]: r for r in pw.where(IDENT, LAYOUT, ZONES)}
    assert (rows["second"]["room"], rows["second"]["zone"], rows["second"]["near"]) == ("спальня", "кровать", ["Кровать"])
    assert (rows["first"]["room"], rows["first"]["zone"], rows["first"]["near"]) == ("зал", None, ["Диван"])
    assert rows["second"]["pose"] == "lie" and rows["second"]["confidence"] == 0.7
    assert [r["person"] for r in pw.where(IDENT, LAYOUT, ZONES, " SEC ")] == ["second"]
    assert pw.room_at(LAYOUT["rooms"], 20.0, 20.0) is None
    nowhere = pw.where([{"person": "second", "x": None, "z": None, "confidence": 0.35, "sources": ["wifi"]}], {"rooms": [], "objects": []}, [])
    assert nowhere == [{"person": "second", "room": None, "zone": None, "near": [], "pose": None, "confidence": 0.35, "sources": ["wifi"], "x": None, "z": None}]


TABLE = {"title": "Стол", "kind": "furniture", "x": 2.0, "z": 0.8, "w": 1.0, "d": 0.4}       # центр (2,5; 1,0)


@pytest.mark.parametrize("x, face, seen", [(1.0, 0.0, "Стол"), (1.0, math.pi, None), (-5.0, 0.0, None), (1.0, None, None)],
                         ids=["facing", "back", "far", "no_gaze"])
def test_facing_names_the_object_in_the_cone_of_sight(x, face, seen):
    """Предмет в конусе взгляда называется; спиной, далеко или без взгляда - None."""
    assert pw.facing({"person": "a", "x": x, "z": 1.0, "pose": "stand", "face": face}, [TABLE]) == seen


def test_where_route_answers_and_refuses_when_off():
    hub = hub_at(LAYOUT, zones=ZONES)
    hub.set_person("second", 0.7, "кровать")
    body = TestClient(presence_api.create_app(hub)).get("/sam/presence/where", params={"person": "second"}).json()
    assert body["people"][0]["room"] == "спальня" and body["people"][0]["zone"] == "кровать"
    off = TestClient(presence_api.create_app(ph.PresenceHub(ph.MODE_OFF, pr.DEMO_LAYOUT)))
    assert off.get("/sam/presence/where").status_code == 409


# --- ручное место ---

SPOTS = {"коридор": {"x": 6.6, "z": 6.2, "pose": "stand"}, "pc": {"x": 2.0, "z": 1.0, "pose": "sit"}}
BASE = [{"person": "second", "confidence": 0.3, "x": 1.0, "z": 1.0, "pose": "lie", "face": 3.0, "sources": ["wifi"]}]


@pytest.mark.parametrize("rows, overrides, expected", [
    (BASE, {"second": {"confidence": 0.9, "place": "коридор"}},
     [{"confidence": 0.9, "x": 6.6, "z": 6.2, "pose": "stand", "sources": ["debug", "wifi"]}]),
    (BASE, {"second": {"confidence": 0.9, "place": "pc"}}, [{"x": 2.0, "z": 1.0, "face": None}]),      # прежний взгляд сброшен: хаб достроит по окружению
    (BASE, {"second": {"confidence": 0.0, "place": None}}, []),                                       # ноль убирает
    ([], {"first": {"confidence": 0.5, "place": "коридор"}}, [{"person": "first", "x": 6.6}]),         # нового ставит в место
    ([], {"first": {"confidence": 0.5, "place": None}}, []),                                          # ставить некуда
], ids=["move", "face_reset", "zero", "new_in_place", "new_nowhere"])
def test_manual_person_override(rows, overrides, expected):
    """Ручная вероятность ставит уверенность и переносит в названное место, сбрасывает взгляд, ноль удаляет."""
    got = po.apply(rows, overrides, SPOTS)
    assert [{k: r[k] for k in e} for r, e in zip(got, expected)] == expected and len(got) == len(expected)


def test_person_api_sets_clears_and_refuses():
    hub = hub_at({"rooms": LAYOUT["rooms"], "objects": [], "places": [{"label": "Коридор", "x": 6.6, "z": 6.2}]})
    client = TestClient(presence_api.create_app(hub))
    assert "коридор" in hub.place_points() and "коридор" in hub.hello()["place_keys"]
    ok = client.post("/sam/presence/person", json={"person": "First", "confidence": 0.8, "place": "Коридор"})
    assert ok.status_code == 200 and hub.identities(10.0)[0]["person"] == "first"
    assert client.post("/sam/presence/person", json={"person": "first", "confidence": 2}).status_code == 422
    assert client.delete("/sam/presence/person/first").status_code == 200
    assert client.delete("/sam/presence/person/first").status_code == 404


# --- места ---

def test_place_points_list_rooms_zones_furniture_places_with_poses_on_free_floor():
    """Места: комнаты, зоны, мебель и названные места с позами, мелочь не место; стоячее место в твёрдом предмете выносится на пол перед ним;
    маршрут мест называет SAM комнаты, места и людей."""
    layout = {"rooms": [{"id": "r", "title": "спальня", "x": 0, "z": 0, "w": 4, "d": 3}],
              "objects": [{"title": "Кровать (примерно)", "kind": "furniture", "x": 1, "z": 0.5, "w": 1.6, "d": 2.0},
                          {"title": "Диван", "kind": "furniture", "x": 6, "z": 1, "w": 2, "d": 1}, {"title": "Лампа", "kind": "furniture", "x": 0, "z": 0, "w": 0.2, "d": 0.2}],
              "places": [{"label": "Коридор", "x": 6.6, "z": 6.2}]}
    hub = hub_at(layout, zones=[{"id": "zone-кухня-стол", "label": "Кухня-стол", "x": 2, "z": 2, "w": 1, "d": 1}])
    points = hub.place_points()
    assert {"спальня", "кровать", "диван", "кухня-стол", "коридор"} <= set(points) and "лампа" not in points
    assert points["кровать"]["pose"] == "lie" and points["диван"]["pose"] == "sit" and points["спальня"]["pose"] == "stand"
    assert (points["кухня-стол"]["x"], points["кухня-стол"]["z"]) == (2.5, 2.5) and "кровать" in hub.hello()["place_keys"]

    fridge = {"id": "f", "title": "Холодильник", "kind": "furniture", "x": 4.6, "z": 1.0, "w": 0.7, "d": 0.7, "h": 1.8, "angle": 0.0, "solid": True}
    plan = two_room_layout()
    plan["objects"] = [o for o in plan["objects"] if o["id"] == "door"] + [fridge]
    spot = hub_at(plan, zones=[{"id": "zone-холодильник", "label": "Холодильник", "x": 4.6, "z": 1.0, "w": 0.7, "d": 0.7}]).place_points()["холодильник"]
    assert not (4.6 <= spot["x"] <= 5.3 and 1.0 <= spot["z"] <= 1.7) and abs(spot["x"] - 4.95) < 1.0 and abs(spot["z"] - 1.35) < 1.0   # рядом, не внутри

    body = TestClient(presence_api.create_app(hub_at(TELL_LAYOUT, channels=("wifi", "told")))).get("/sam/presence/places").json()
    names = {p["name"]: p for p in body["places"]}
    assert body["rooms"] == ["спальня", "зал"] and body["people"] == {"first": "Первая", "second": "Вторая"} and body["told"] is True
    assert names["диван в зале"]["room"] == "зал" and names["кровать"]["pose"] == "lie"


# --- «со слов» ---

@pytest.mark.parametrize("channels, message, status, said", [
    (("wifi", "told"), {"who": "nobody", "place": "зал"}, 422, None),                                  # человека нет в разметке
    (("wifi", "told"), {"who": "Вторая", "place": "диван у окна"}, 422, "похожие: диван"),             # места нет, подсказка похожих
    (("wifi", "told"), {"who": "кот", "place": "зал"}, 422, None),
    (("wifi", "told"), {"who": "second", "place": "зал", "pose": "fly"}, 422, None),
    (("wifi",), {"who": "second", "place": "зал"}, 200, "SAM_PRESENCE_CHANNELS"),                     # канал выключен: не применено
], ids=["unknown_person", "unknown_place", "cat", "bad_pose", "channel_off"])
def test_told_message_rejects_or_is_not_applied(channels, message, status, said):
    client = TestClient(presence_api.create_app(hub_at(TELL_LAYOUT, channels=channels)))
    got = client.post("/sam/presence/message", json={"channel": "told", **message})
    assert got.status_code == status
    if status == 200:
        assert got.json()["applied"] is False and said in got.json()["detail"]
    elif said:
        assert said in got.json()["detail"]


def test_told_message_moves_the_person_to_the_seat_with_the_pose():
    hub = hub_at(TELL_LAYOUT, channels=("wifi", "told"))
    ok = TestClient(presence_api.create_app(hub)).post("/sam/presence/message", json={"channel": "told", "who": "Вторая", "place": "Диван в зале", "pose": "lie"})
    assert ok.json() == {"ok": True, "applied": True}
    told = [e for e in hub.messages.evidence(10.0) if e.person == "second"][0]
    seat = hub.place_points()["диван в зале"]
    assert (told.x, told.z, told.pose) == (seat["x"], seat["z"], "lie")             # место встаёт на сиденье дивана под ним, поза - со слов


def test_locked_zone_survives_delete_until_unlocked():
    client = TestClient(presence_api.create_app(hub_at(pr.EMPTY_LAYOUT)))
    made = client.post("/sam/presence/zone", json={"label": "рабочая зона", "x": 0, "z": 0, "w": 1, "d": 1, "locked": True}).json()
    assert made["locked"] is True
    denied = client.delete("/sam/presence/zone/" + made["id"])
    assert denied.status_code == 423 and "замок" in denied.json()["detail"]
    assert client.post(f"/sam/presence/zone/{made['id']}/lock", json={"locked": False}).json() == {"ok": True, "locked": False}
    assert client.delete("/sam/presence/zone/" + made["id"]).status_code == 200
    assert client.post("/sam/presence/zone/нет/lock", json={"locked": True}).status_code == 404


# --- автопилот и реальный режим ---

def autopilot_layout() -> dict:
    """Кухня со всеми местами стартовых сцен быта по умолчанию и двумя жильцами a и b."""
    labels = {"pc-a": (1.0, 1.0), "pc-b": (2.0, 1.0), "Газовая плита": (3.0, 3.0), "Кухня": (4.0, 2.0)}
    return {"rooms": [{"id": "r", "title": "спальня", "x": 0, "z": 0, "w": 6, "d": 4}],
            "objects": [{"title": "Кровать", "kind": "furniture", "x": 4, "z": 0.2, "w": 1.6, "d": 1.6}],
            "places": [{"label": k, "x": x, "z": z} for k, (x, z) in labels.items()], "people": {"a": {}, "b": {}}}


@pytest.fixture
def default_scenes(monkeypatch, tmp_path):
    """Быт без файла сцен стенда: встроенные сцены."""
    monkeypatch.setenv(routine.ROUTINE_FILE_ENV, str(tmp_path / "no-routine.json"))


def test_autopilot_random_mode_plans_varied_combinations_on_a_period_and_off_clears(default_scenes):
    """План случайного автопилота даёт всех вместе, парой и врозь; хаб применяет его только включённым, раз в период, выключение снимает назначенное;
    маршрут автопилота включает, сообщает и принимает длину рывка ходьбы от страницы."""
    seen = set()
    for seed in range(60):
        got = ap.plan(["a", "b", "c"], ["кровать", "диван", "кухня", "спальня"], ["кухня", "спальня"], random.Random(seed))
        assert set(got) == {"a", "b", "c"}
        seen.add(len(set(got.values())))
    assert {1, 2, 3} <= seen

    now = [10.0]
    hub = ph.PresenceHub(ph.MODE_RSSI, {"name": "t", **autopilot_layout()}, clock=lambda: now[0], tick_s=0.0)
    tick(hub)
    assert hub.overrides == {}                                                 # выключен - ничего не назначает
    hub.set_autopilot(True, 5.0, "random")
    tick(hub)
    first = dict(hub.overrides)
    assert set(first) == {"a", "b"}
    now[0] = 12.0
    tick(hub)
    assert hub.overrides == first                                              # период 5 с ещё не прошёл
    hub.set_autopilot(False)
    assert hub.overrides == {}

    client = TestClient(presence_api.create_app(hub_at({"rooms": autopilot_layout()["rooms"], "people": {"a": {}}})))
    assert client.post("/sam/presence/autopilot", json={"on": True, "period_s": 9}).json() == {"on": True, "period_s": 9.0, "mode": "routine"}      # быт - по умолчанию
    assert client.get("/sam/presence/autopilot").json()["on"] is True
    assert client.post("/sam/presence/tuning", json={"walk_burst_s": 0.6}).json()["tuning"]["walk_burst_s"] == 0.6


def test_autopilot_routine_starts_guestless_resumes_and_restarts_only_on_mode_change(default_scenes):
    """Быт начинается со случайной сцены без гостя; страница, повторяющая тот же режим, его не перезапускает; после паузы сцена та же
    и места ставятся заново; смена режима начинает с начала."""
    starts = {name: {p: place for p, place in routine.DEFAULT_SCENES[name]["assign"].items() if p in ("a", "b")} for name in routine.START_SCENES}
    begun = set()
    for seed in range(12):
        hub = hub_at(autopilot_layout(), randomness=Randomness(seed))
        assert hub.set_autopilot(True)["mode"] == "routine"
        tick(hub)
        placed = {p: o["place"] for p, o in hub.overrides.items()}
        assert placed in list(starts.values())
        begun.add(json.dumps(placed, sort_keys=True))
    assert len(begun) > 1                                                      # старт дня разный

    for person in list(hub.overrides):
        hub.clear_person(person)
    hub.set_autopilot(True, 9.0, mode="routine")                               # ползунок периода: страница шлёт тот же режим
    tick(hub)
    assert hub.overrides == {}                                                 # быт не перезапущен: сцена идёт, новых назначений нет
    hub.set_autopilot(False)
    hub.set_autopilot(True, mode="routine")                                    # включили снова: та же сцена, места заново
    tick(hub)
    assert {p: o["place"] for p, o in hub.overrides.items()} == placed
    assert hub.set_autopilot(True, mode="random")["mode"] == "random"
    tick(hub)
    assert set(hub.overrides) == {"a", "b"}                                    # случайные места вместо сцены


def test_switching_the_autopilot_off_drops_unfinished_walks():
    """Выключенный автопилот обрывает незавершённые ходы: поставленный туда, где стоит, человек стоит там, а не продолжает идти к старой цели."""
    hub = hub_at(two_room_layout({"a": {}}))
    hub.set_person("a", 0.9, "угол спальни")
    start = hub.identities(10.0)[0]
    hub.set_person("a", 0.9, "угол зала")
    assert hub.identities(10.0)[0].get("walking") is True                      # ход в разгаре
    hub.set_autopilot(True)
    hub.set_autopilot(False)
    assert hub.overrides == {}
    hub.set_person("a", 0.9, "угол спальни")                                   # без сброса ход к залу шёл бы дальше: цель сменилась только что
    row = hub.identities(10.5)[0]
    assert "walking" not in row and (row["x"], row["z"]) == (start["x"], start["z"])


def test_real_mode_clears_simulation_and_restores_on_exit_via_api():
    """Реальный режим снимает автопилот и ручные места, включает все каналы и подробный журнал; выход возвращает каналы и отладку; маршрут переключает и сообщает каналы."""
    layout = {"rooms": [{"id": "r", "title": "кухня", "x": 0, "z": 0, "w": 6, "d": 4}], "people": {"a": {}}}
    hub = hub_at(layout, channels=("wifi",))
    hub.set_autopilot(True)
    hub.set_person("a", 0.9, "кухня")
    assert "told" not in hub.enabled_channels and len(hub.channels) == 1
    assert hub.set_real(True) is True
    assert not hub.autopilot_on and hub.overrides == {} and hub.debug
    assert {"wifi", "messages", "time", "pc", "told"} <= hub.enabled_channels and len(hub.channels) == 4
    assert hub.set_real(True) is True                                          # повтор ничего не ломает
    hub.set_real(False)
    assert hub.enabled_channels == frozenset(("wifi",)) and len(hub.channels) == 1 and not hub.debug

    client = TestClient(presence_api.create_app(hub_at(layout)))
    assert client.get("/sam/presence/real").json()["on"] is False
    on = client.post("/sam/presence/real", json={"on": True}).json()
    assert on["on"] is True and "told" in on["channels"] and client.get("/sam/presence/real").json()["on"] is True
    assert client.post("/sam/presence/real", json={"on": False}).json()["on"] is False


# --- тепло, шаги, стоянка ---

def test_heat_and_steps_accumulate_on_floor_and_clear_via_api(tmp_path):
    """Пребывание копится по уверенности, шаги - только у идущего, пауза между кадрами не рисует пребывание; тепло за стеной при старте
    ложится на пол; очистка карты пустит её и на диске и скажет, сколько было клеток."""
    now = [100.0]
    hub = ph.PresenceHub(ph.MODE_RSSI, two_room_layout({"a": {}, "b": {}}), clock=lambda: now[0])
    hub.set_person("a", 1.0, "угол спальни")
    hub.set_person("b", 0.5, "угол зала")
    hub.step(100.0)
    hub.set_person("a", 1.0, "вход")                                           # a пошёл, b остаётся
    now[0] = 100.5
    rows = {r["person"]: r for r in hub.step(100.5)["identities"]}
    assert rows["a"].get("walking") is True and not rows["b"].get("walking")
    walked, still = hub.heat.cell_of(rows["a"]["x"], rows["a"]["z"]), hub.heat.cell_of(rows["b"]["x"], rows["b"]["z"])
    assert hub.heat.counts[walked] == pytest.approx(0.5, abs=0.01) and hub.heat.counts[still] == pytest.approx(0.25, abs=0.01)
    assert hub.steps.counts.get(walked, 0.0) == pytest.approx(0.5, abs=0.01) and still not in hub.steps.counts
    before = sum(hub.heat.counts.values())
    now[0] = 130.0
    hub.step(130.0)
    assert sum(hub.heat.counts.values()) - before <= 2 * ph.PRESENCE_DT_CAP_S + 0.01        # пауза в тридцать секунд - не больше одного кадра на человека

    old = pr.Heatmap()
    old.add(2.1, -0.6, 50.0)                                                   # за внешней стеной: сюда копилось «за устройством»
    plan = two_room_layout()
    nav = Nav(plan)
    moved = ph.PresenceHub(ph.MODE_RSSI, plan, clock=lambda: 10.0, heat=old, heat_file=tmp_path / "heat.json")
    assert sum(moved.heat.counts.values()) == pytest.approx(50.0)               # секунды целы
    assert not any(nav.is_exterior((i + 0.5) * moved.heat.cell_m, (k + 0.5) * moved.heat.cell_m) for i, k in moved.heat.counts)    # и все на полу
    moved.heat.add(1.0, 1.0, 5.0)
    client = TestClient(presence_api.create_app(moved))
    cells = len(moved.heat.counts)
    assert client.delete("/sam/presence/heat").json() == {"cleared": cells}
    assert moved.heat.counts == {} and json.loads((tmp_path / "heat.json").read_text(encoding="utf-8")).get("counts") in (None, [], {})
    assert client.delete("/sam/presence/heat").json() == {"cleared": 0}


def test_stay_report_says_stretch_after_an_hour_and_resets_on_move():
    """Час на одном месте - пора размяться с обоснованием; ушёл дальше радиуса - отсчёт заново; маршрут отдаёт отчёт."""
    layout = {"name": "t", "rooms": [{"id": "r", "title": "к", "x": 0, "z": 0, "w": 3, "d": 4}, {"id": "s", "title": "з", "x": 3, "z": 0, "w": 3, "d": 4}],
              "people": {"a": {}}}
    now = [0.0]
    hub = ph.PresenceHub(ph.MODE_RSSI, layout, clock=lambda: now[0])
    hub.set_person("a", 0.8, "к")
    for t in range(0, int(ph.STAY_ALERT_S) + 61, 60):
        now[0] = float(t)
        hub.step(float(t))
    report = hub.stay_report()[0]
    assert report["person"] == "a" and report["minutes"] >= 60 and report["stretch"] is True and report["room"] == "к"
    assert report["still_share"] > 0.9 and "в основном сидел или лежал" in report["reason"] and "разминку" in report["say"]
    hub.set_person("a", 0.8, "з")                                              # в другой комнате, дальше радиуса
    now[0] += 60.0
    hub.step(now[0])
    again = hub.stay_report()[0]
    assert again["minutes"] < 1 and again["stretch"] is False
    assert TestClient(presence_api.create_app(hub)).get("/sam/presence/stay").json()["people"][0]["person"] == "a"


# --- журнал радара ---

def test_radar_log_gets_events_and_survives_a_failing_tick(radar_log):
    """Ручная вероятность пишется в журнал; упавший тик - строка со стеком, а цикл шлёт следующий кадр."""
    hub = hub_at(LAYOUT)
    hub.set_person("second", 0.8, None)
    real_step, calls = hub.step, []

    def failing_once(now=None):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("сбой тика для теста")
        return real_step(now)

    hub.step = failing_once                                                    # сбой внутри тика: граница, которую цикл обязан пережить
    assert '"type":"frame"' in tick(hub)
    assert "ручная вероятность: second" in radar_log.text and "тик радара упал" in radar_log.text and "сбой тика для теста" in radar_log.text


def test_page_errors_are_written_to_the_log_with_a_rate_limit(radar_log):
    """Ошибка страницы - строка журнала; лавина режется лимитом в минуту, через минуту снова пишется."""
    now = [1000.0]
    client = TestClient(presence_api.create_app(hub_at(pr.EMPTY_LAYOUT), clock=lambda: now[0]))
    post = lambda message: client.post("/sam/presence/client-log", json={"kind": "error", "message": message, "extra": "presence.html:101"}).json()["ok"]
    answers = [post(f"e{i}") for i in range(presence_api.CLIENT_LOG_PER_MINUTE + 5)]
    assert answers == [True] * presence_api.CLIENT_LOG_PER_MINUTE + [False] * 5
    now[0] += 61.0
    assert post("x is not a function") is True
    assert "x is not a function" in radar_log.text and "e34" not in radar_log.text
