"""Хаб радара (SAM-105): флаг, выключенный радар как no-op, снимок, API и сокет, тепло, зоны, места и настройка детектора.
Часы и пауза цикла внедрены, файлы радара - в tmp_path."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from radar import presence as pr
from radar import presence_api
from radar import presence_hub as ph
from radar import presence_rssi as rs
from tests.fakes_presence import LoopDone, TickSleep, isolated_hub_env

NOW = 100.0
QUIET_POLLS, JITTER_POLLS, JITTER_DB = 20, 10, 2.0      # сначала тихо - база встаёт на ноль, потом сигнал дрожит на 2 дБ


def _hub(mode=ph.MODE_MOCK, **kwargs):
    return ph.PresenceHub(mode, pr.DEMO_LAYOUT, 0.01, clock=lambda: NOW, **kwargs)


@pytest.fixture
def api():
    """Приложение радара над подставленным хабом: `api(hub)` даёт клиента."""
    clients = []

    def make(hub):
        c = TestClient(presence_api.create_app(hub))
        clients.append(c)
        return c
    yield make
    for c in clients:
        c.close()


def test_hub_mode_from_env(capsys):
    assert ph.mode_from_env({}) == ph.MODE_OFF
    assert ph.mode_from_env({ph.FLAG_ENV: " MOCK "}) == ph.MODE_MOCK
    assert ph.mode_from_env({ph.FLAG_ENV: "csi"}) == ph.MODE_OFF
    assert "csi" in capsys.readouterr().out


def test_off_radar_is_a_noop_for_api_socket_and_files(api, tmp_path):
    hub = ph.build(isolated_hub_env(tmp_path))                      # флага нет - радар выключен, не зависит от SAM_PRESENCE в живом .env
    assert not hub.enabled and hub.step() is None
    assert hub.snapshot() == {"enabled": False, "mode": "off"}
    assert hub.hello() == {"type": "hello", "enabled": False, "mode": "off", "layout": None}
    asyncio.run(asyncio.wait_for(hub.run(), 0.5))                    # возвращается сразу, цикла нет
    hub.save_heat()
    assert list(tmp_path.iterdir()) == []                            # ни одного файла не прочитано и не создано
    client = api(hub)
    assert client.get("/sam/presence").json() == {"layout": None, "enabled": False, "mode": "off"}
    with client.websocket_connect("/ws/presence") as ws:
        assert json.loads(ws.receive_text())["enabled"] is False


def test_mock_radar_serves_layout_and_frames_over_api_and_socket(api):
    hub = _hub()
    snap = hub.step(NOW + 1)
    assert snap["enabled"] and snap["mode"] == "mock" and isinstance(snap["links"], list)
    person = snap["people"][0]
    assert len(person["points"]) == pr.POINTS_PER_PERSON and person["state"] == "live"
    assert hub.hello()["layout"]["rooms"]
    client = api(hub)
    body = client.get("/sam/presence").json()
    assert body["enabled"] and body["layout"]["rooms"]
    with client.websocket_connect("/ws/presence") as ws:
        assert json.loads(ws.receive_text())["layout"]["rooms"]


def run_loop(hub):
    """Цикл хаба до исчерпания фальшивой паузы."""
    async def scenario():
        with pytest.raises(LoopDone):
            await hub.run()
    asyncio.run(scenario())


def test_loop_streams_only_fresh_frames_to_listeners():
    # пока клиентов нет, кадры не считаются
    idle_sleep = TickSleep(5)
    idle = _hub(sleep=idle_sleep)
    run_loop(idle)
    assert idle_sleep.calls == 5 and idle.snapshot()["people"] == []
    # слушатель получает кадры с людьми; отставший, не читающий клиент держит только последние кадры
    now, queues, ticks = [NOW], [], ph.CLIENT_QUEUE * 3

    def before_tick(n):
        now[0] = NOW + n                                              # кадр n помечен временем NOW + n
        if n == 0:
            queues.append(hub.add_client())
    hub = ph.PresenceHub(ph.MODE_MOCK, pr.DEMO_LAYOUT, 0.01, clock=lambda: now[0], sleep=TickSleep(ticks, before=before_tick))
    run_loop(hub)
    [q] = queues
    frames = [json.loads(q.get_nowait()) for _ in range(q.qsize())]
    assert 0 < len(frames) <= ph.CLIENT_QUEUE and all(f["type"] == "frame" and f["people"] for f in frames)
    assert frames[-1]["ts"] == NOW + ticks - 1                        # последний кадр - свежий, старые выброшены
    hub.remove_client(q)


def test_heat_accumulates_persists_and_survives_bad_file(tmp_path, capsys):
    # шаги копят тепло; карта уходит с первым кадром и в hello
    hub = _hub()
    assert hub.step(NOW + 1)["heat"]["cells"]
    assert "heat" not in hub.step(NOW + 1.2)
    assert hub.heat.counts and hub.hello()["heat"]["cell_m"] == pr.HEAT_CELL_M
    # сохраняется и читается обратно, битый файл - заново с одной строкой
    path = tmp_path / "heat.json"
    saved = _hub(heat_file=path)
    saved.heat.add(1.0, 1.0, 3.0)
    saved.save_heat()
    assert ph.load_heat(path).counts == saved.heat.counts
    path.write_text("не json", encoding="utf-8")
    assert ph.load_heat(path).counts == {}
    assert "не прочитана" in capsys.readouterr().out
    # путь, куда не записать, - одна строка в лог, без исключения
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    _hub(heat_file=blocker / "heat.json").save_heat()
    assert "не сохранена" in capsys.readouterr().out


def test_zones_added_named_and_removed_via_hub_and_api(api, tmp_path):
    zones_file = tmp_path / "zones.json"
    hub = _hub(zones_file=zones_file)
    cx, cz = pr.room_center(pr.DEMO_LAYOUT, "demo-living")             # мок в t=0 стоит в центре гостиной
    hub.add_zone({"label": "диван", "x": cx - 1, "z": cz - 1, "w": 2, "d": 2})
    assert json.loads(zones_file.read_text(encoding="utf-8"))[0]["label"] == "диван"
    assert hub.step(NOW)["people"][0]["zone"] == "диван"
    assert hub.hello()["zones"][0]["id"] == "zone-диван"
    assert hub.remove_zone("zone-диван") and not hub.remove_zone("zone-диван")
    assert json.loads(zones_file.read_text(encoding="utf-8")) == []
    # то же через API: выключенный радар - 409, плохая зона - 422 с причиной, повторное удаление - 404
    zone = {"label": "шкаф", "x": 0, "z": 0, "w": 1, "d": 1}
    assert api(_hub(ph.MODE_OFF)).post("/sam/presence/zone", json=zone).status_code == 409
    client = api(hub)
    added = client.post("/sam/presence/zone", json=zone)
    assert added.status_code == 200 and added.json()["id"] == "zone-шкаф"
    bad = client.post("/sam/presence/zone", json={**zone, "label": ""})
    assert bad.status_code == 422 and "подпись" in bad.json()["detail"]
    assert client.delete("/sam/presence/zone/zone-шкаф").status_code == 200
    assert client.delete("/sam/presence/zone/zone-шкаф").status_code == 404


def test_device_places_set_saved_and_read_via_api(api, tmp_path):
    hub = ph.PresenceHub(ph.MODE_RSSI, pr.EMPTY_LAYOUT, clock=lambda: 10.0, places_file=tmp_path / "places.json")
    client = api(hub)
    ok = client.post("/sam/presence/place", json={"name": " S24 ", "place": "рабочий стол"})
    assert ok.status_code == 200 and ok.json()["places"] == {"s24": "рабочий стол"}
    assert json.loads((tmp_path / "places.json").read_text(encoding="utf-8")) == {"s24": "рабочий стол"}
    assert client.post("/sam/presence/place", json={"name": "s24", "place": ""}).json()["places"] == {}
    assert client.post("/sam/presence/place", json={"name": " ", "place": "x"}).status_code == 422
    assert api(_hub(ph.MODE_OFF)).post("/sam/presence/place", json={"name": "s24", "place": "x"}).status_code == 409


@pytest.mark.parametrize("mode, layout, heat_saved", [
    (ph.MODE_RSSI, pr.EMPTY_LAYOUT, True),              # реальный источник без плана - пустой куб, тепло пишется на диск
    (ph.MODE_MOCK, pr.DEMO_LAYOUT, False),              # мок - демо, треки выдуманного человека не копятся
])
def test_build_picks_layout_and_persistence_by_source(tmp_path, mode, layout, heat_saved):
    hub = ph.build(isolated_hub_env(tmp_path, **{ph.FLAG_ENV: mode}))
    assert hub.layout is layout
    hub.heat.add(1.0, 1.0, 3.0)
    hub.save_heat()
    assert (tmp_path / ph.HEAT_FILE_ENV.lower()).exists() is heat_saved


def test_service_serves_health_page_and_message(api):
    client = api(_hub(ph.MODE_RSSI))
    assert client.get("/health").json() == {"status": "ok", "enabled": True, "mode": "rssi"}
    assert client.get("/presence").status_code == 200 and client.get("/").status_code == 200
    assert client.get("/static/presence/figure.js").status_code == 200
    assert client.get("/static/vendor/three/build/three.module.js").status_code == 200    # three.js - в пакете радара
    assert client.get("/static/shell/sam.css").status_code == 200       # каркас - из шлюза (SAM-198)
    assert client.post("/sam/presence/message", json={"channel": "chat", "device": "s24"}).json() == {"ok": True, "applied": True}
    assert api(_hub(ph.MODE_OFF)).post("/sam/presence/message", json={"channel": "chat"}).status_code == 409


def jitter(feed):
    """Те же опросы для детектора и для хаба: тихо, потом дрожание; возвращает последнее чтение."""
    reading = None
    for i in range(QUIET_POLLS + JITTER_POLLS):
        shake = JITTER_DB if i >= QUIET_POLLS and i % 2 else 0.0
        reading = feed([rs.Station("a", "phy0", -50.0 + shake, 0, "S24")])
    return reading


def hub_activity(hub):
    jitter(lambda stations: hub.feed_stations(stations, 10.0))
    return hub.snapshot()["signal"]["activity"]


def test_tuning_changes_detector_live_and_survives_restart(api, tmp_path):
    f = tmp_path / "tuning.json"
    hub = ph.PresenceHub(ph.MODE_RSSI, pr.EMPTY_LAYOUT, clock=lambda: 10.0, tuning_file=f)
    client = api(hub)
    assert client.get("/sam/presence/tuning").json()["tuning"]["noise_floor_db"] == 0.6
    assert client.post("/sam/presence/tuning", json={"noise_floor_db": 0.2}).json()["tuning"]["noise_floor_db"] == 0.2
    # детектор работающего хаба сразу видит новый порог: то же дрожание даёт больше активности, чем у хаба по умолчанию
    assert hub_activity(hub) > hub_activity(ph.PresenceHub(ph.MODE_RSSI, pr.EMPTY_LAYOUT, clock=lambda: 10.0))
    # порог сохранён и переживает перезапуск
    restarted = api(ph.PresenceHub(ph.MODE_RSSI, pr.EMPTY_LAYOUT, clock=lambda: 10.0, tuning=ph.load_tuning(f), tuning_file=f))
    assert restarted.get("/sam/presence/tuning").json()["tuning"]["noise_floor_db"] == 0.2
    assert client.post("/sam/presence/tuning", json={"noise_floor_db": 99}).json()["tuning"]["noise_floor_db"] == 3.0   # прижато к границе
    assert client.post("/sam/presence/tuning", json={"nope": 1}).status_code == 422
    assert client.delete("/sam/presence/tuning").json()["tuning"]["noise_floor_db"] == 0.6


def test_lower_noise_floor_counts_jitter_as_motion():
    quiet = jitter(rs.MotionDetector(tuning=rs.Tuning(noise_floor_db=2.0)).update)
    sensitive = jitter(rs.MotionDetector(tuning=rs.Tuning(noise_floor_db=0.0)).update)
    assert sensitive.activity > quiet.activity
    assert sensitive.lines["a"]["name"] == "S24" and "std" in sensitive.lines["a"]
