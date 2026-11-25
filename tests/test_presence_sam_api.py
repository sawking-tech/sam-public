"""Ручки радара для SAM (radar/presence_sam_api.py): шесть маршрутов с меткой x-sam-tool, ответы словами, отказы одной
строкой, MCP-переходник mcp/radar_server.py поверх тех же маршрутов. Планы выдуманные, сеть переходника идёт в TestClient."""
import importlib.util
import io
import json
import urllib.error
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway import app_tools
from radar import presence_answers as pa
from radar import presence_api
from radar import presence_hub as ph
from radar import presence_sam_api as api
from tests.test_presence_where import LAYOUT, TELL_LAYOUT, hub_at

NAMES = [api.WHERE, api.HISTORY, api.LAYOUT, api.TELL, api.DEVICES, api.FURNITURE]
NOW = datetime(2026, 10, 12, 1, 30)
AT = datetime(2026, 10, 12, 1, 10).timestamp()
ROWS = [{"t": AT - 40, "id": "person:second|person", "places": [["кухня", 0.7], ["коридор", 0.2]], "sure": 0.7},
        {"t": AT - 5, "id": "person:second|person", "places": [["диван в зале", 0.6]], "sure": 0.6},
        {"t": AT, "id": "bc39880c|phy1-ap0", "places": [["ванная", 0.9]], "sure": 0.9}]


class FakeHistory:
    def __init__(self, rows):
        self.rows, self.asked = rows, []

    def read_range(self, since, until, subject=None, min_sure=0.0):
        self.asked.append((since, until, subject))
        return self.rows


def client_for(layout=TELL_LAYOUT, rows=(), **kw) -> TestClient:
    hub = hub_at(layout, channels=("wifi", "told"), flow_history=FakeHistory(list(rows)), **kw)
    return TestClient(presence_api.create_app(hub, clock=NOW.timestamp))


def test_six_marked_routes_become_sam_tools_with_short_names_and_args():
    spec = client_for().get("/openapi.json").json()
    tools = {t["name"]: t for t in app_tools.from_openapi(spec)}
    assert list(tools) == NAMES                                       # отмечены ровно шесть, остальные маршруты - нет
    assert {n: list(t["params"]) for n, t in tools.items()} == {
        "where": ["person"], "history": ["person", "at"], "layout": [], "tell": ["person", "place", "pose"],
        "devices": [], "furniture": ["room"]}
    assert tools["tell"]["method"] == "POST" and tools["where"]["path"] == "sam/tools/where"
    assert tools["history"]["params"]["at"].startswith("момент") and all(t["description"] for t in tools.values())


def test_answers_are_words_from_the_hub():
    client = client_for(LAYOUT)
    where = json.loads(client.get("/sam/tools/where", params={"person": "second"}).text)
    assert where == []                                                # людей не видно - пустой список, не ошибка
    assert ",".join(o["title"] for o in json.loads(client.get("/sam/tools/furniture", params={"room": "зал"}).text)) == "Диван"
    assert len(json.loads(client.get("/sam/tools/furniture").text)) == 2
    layout = json.loads(client_for().get("/sam/tools/layout").text)
    assert layout["people"] == {"first": "Первая", "second": "Вторая"} and layout["told"] is True
    assert layout == client_for().get("/sam/presence/places").json()    # страница и SAM видят одну разметку


def test_history_picks_the_row_nearest_the_moment():
    client = client_for(rows=ROWS)
    got = json.loads(client.get("/sam/tools/history", params={"person": "Вторая", "at": "01:10"}).text)
    assert got["place"] == "диван в зале" and got["p"] == 0.6 and "person:second" in got["source"]
    empty = json.loads(client_for().get("/sam/tools/history", params={"person": "second", "at": "01:10"}).text)
    assert empty["result"] == pa.NO_DATA
    unknown = client.get("/sam/tools/history", params={"person": "кот", "at": "01:10"})
    assert unknown.status_code == 422 and "нет в разметке" in unknown.text
    bad = client.get("/sam/tools/history", params={"person": "second", "at": "в обед"})
    assert bad.status_code == 422 and "не разобрала" in bad.text


def test_history_reads_the_window_around_the_moment_for_that_person():
    history = FakeHistory([])
    hub = hub_at(TELL_LAYOUT, flow_history=history)
    TestClient(presence_api.create_app(hub, clock=NOW.timestamp)).get("/sam/tools/history", params={"person": "Вторая", "at": "01:10"})
    assert history.asked == [(AT - pa.HISTORY_WINDOW_S, AT + pa.HISTORY_WINDOW_S, "person:second")]


@pytest.mark.parametrize("text, expected", [
    ("01:10", datetime(2026, 10, 12, 1, 10)),
    ("вчера 23:40", datetime(2026, 10, 11, 23, 40)),
    ("02:00", datetime(2026, 10, 11, 2, 0)),                         # позже текущего - вчера
    ("2026-10-10 12:00", datetime(2026, 10, 10, 12, 0)),
    ("в обед", None),
])
def test_moment_is_parsed_from_words(text, expected):
    assert pa.parse_at(text, NOW) == expected


def test_tell_moves_the_person_or_says_what_is_wrong():
    client = client_for()
    said = client.post("/sam/tools/tell", json={"person": "Вторая", "place": "Диван в зале", "pose": "lie"})
    assert said.status_code == 200 and said.text == "внесла: Вторая - Диван в зале, lie"
    near = client.post("/sam/tools/tell", json={"person": "second", "place": "диван у окна"})
    assert near.status_code == 422 and "похожие: диван, диван в зале" in near.text
    off = TestClient(presence_api.create_app(hub_at(TELL_LAYOUT, channels=("wifi",))))
    assert off.post("/sam/tools/tell", json={"person": "second", "place": "диван в зале"}).text == pa.TOLD_OFF_DETAIL


def test_disabled_radar_answers_one_line():
    hub = ph.PresenceHub(ph.MODE_OFF, {"name": "t", **LAYOUT}, clock=lambda: 10.0, tick_s=0.0)
    client = TestClient(presence_api.create_app(hub))
    for name in NAMES:
        got = client.post(f"/sam/tools/{name}", json={"person": "a", "place": "b"}) if name == api.TELL else \
            client.get(f"/sam/tools/{name}", params={"person": "a", "at": "01:10"})
        assert (got.status_code, got.text) == (api.OFF, pa.OFF_DETAIL)
    assert client.post(f"/sam/tools/{api.NOTE}", json={"channel": "chat"}).status_code == api.OFF


def test_note_takes_channel_evidence_but_not_words_and_is_hidden_from_the_model():
    """note - функция каналов SAM (SAM-198): свидетельство чата доходит до хаба, «со слов» - только через tell."""
    hub = hub_at(TELL_LAYOUT, channels=("wifi", "told", "chat"))
    seen = []
    hub.note_message = lambda *a, **kw: seen.append((a, kw)) or True
    client = TestClient(presence_api.create_app(hub, clock=NOW.timestamp))
    got = client.post(f"/sam/tools/{api.NOTE}", json={"channel": "chat", "device": "alpha", "who": "second"})
    assert (got.status_code, json.loads(got.text)) == (200, {"applied": True})
    assert seen == [(("chat", "alpha", "second", None), {"place": None})]
    told = client.post(f"/sam/tools/{api.NOTE}", json={"channel": pa.TOLD, "who": "second", "place": "диван в зале"})
    assert (told.status_code, told.text) == (api.REJECTED, api.NOTE_TOLD_DETAIL) and len(seen) == 1
    assert api.NOTE not in {t["name"] for t in app_tools.from_openapi(client.get("/openapi.json").json())}


# --- MCP-переходник: те же маршруты ---

@pytest.fixture
def server():
    path = Path(__file__).parent.parent / "mcp" / "radar_server.py"
    spec = importlib.util.spec_from_file_location("radar_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def route_to(server, monkeypatch, client: TestClient) -> list:
    """urlopen переходника - в TestClient радара; заголовки запросов копятся."""
    seen = []

    def urlopen(req, timeout=None):
        headers = {k.lower(): v for k, v in req.header_items()}
        seen.append(headers)
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        res = client.request(req.get_method(), "/" + path, content=req.data, headers=headers)
        if res.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, res.status_code, "x", {}, io.BytesIO(res.content))
        return io.BytesIO(res.content)
    monkeypatch.setattr(server.urllib.request, "urlopen", urlopen)
    return seen


def test_mcp_tools_relay_the_same_routes(server, monkeypatch):
    client = client_for(rows=ROWS)
    route_to(server, monkeypatch, client)
    assert server.radar_layout() == client.get("/sam/tools/layout").text
    assert server.radar_history("Вторая", "01:10") == client.get("/sam/tools/history", params={"person": "Вторая", "at": "01:10"}).text
    assert server.radar_tell("Вторая", "Диван в зале", "lie") == "внесла: Вторая - Диван в зале, lie"
    assert server.radar_tell("second", "диван у окна").startswith("ручка ответила 422: места «диван у окна» нет в разметке; похожие")
    assert json.loads(server.radar_furniture("зал"))[0]["title"] == "Диван" and json.loads(server.radar_where()) == []


def test_mcp_adapter_sends_the_key_and_says_when_radar_is_down(server, monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "k-test")
    seen = route_to(server, monkeypatch, client_for())
    assert json.loads(server.radar_devices()) == [] and seen[-1]["x-sam-key"] == "k-test"   # без ключа радар ответил бы 401

    def refuse(*args, **kwargs):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(server.urllib.request, "urlopen", refuse)
    assert "start_radar.ps1" in server.radar_where("second")
