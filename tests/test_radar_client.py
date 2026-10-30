"""Клиент радара (SAM-105, SAM-198): свидетельство уходит функцией note, сбой не мешает, после сбоя пауза, чтение
функциями where и layout, пустой адрес - выключено. Шлюз модулей радара не импортирует. Сеть клиента идёт в TestClient."""
import ast
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from gateway import radar_client as rc
from gateway import scanning
from radar import presence_api
from radar import presence_sam_api as api
from tests.test_presence_where import TELL_LAYOUT, hub_at

GATEWAY = Path(__file__).resolve().parents[1] / "gateway"


def _client(post, now, url="http://radar:1/", get=None):
    return rc.RadarClient(url, post=post, get=get or (lambda u: None), clock=lambda: now[0], background=False)


def _sends_the_message_with_its_fields():
    sent = []
    _client(lambda url, payload: sent.append((url, payload)), [0.0]).note_message("voice", "s24", "resident-a", 0.4)
    assert sent == [("http://radar:1/sam/tools/note",
                     {"channel": "voice", "device": "s24", "who": "resident-a", "level": 0.4, "place": None})]


def _failure_does_not_raise_and_backs_off():
    calls, now = [], [0.0]

    def post(url, payload):
        calls.append(payload)
        raise OSError("radar down")
    client = _client(post, now)
    client.note_message("chat")
    client.note_message("chat")                      # в паузе: связь не пробуем
    assert len(calls) == 1
    now[0] = rc.RETRY_AFTER_S + 1
    client.note_message("chat")
    assert len(calls) == 2


def _empty_url_is_off():
    sent = []
    client = _client(lambda url, payload: sent.append(payload), [0.0], url="", get=lambda u: pytest.fail("чтение без адреса"))
    client.note_message("chat")
    assert not client.enabled and sent == [] and client.read(rc.WHERE) is None


def _url_from_env_with_default():
    assert rc.build({}).base_url == rc.DEFAULT_URL
    assert rc.build({rc.URL_ENV: "http://x:9"}).base_url == "http://x:9"
    assert not rc.build({rc.URL_ENV: ""}).enabled


def _read_failure_is_none():
    def down(url):
        raise OSError("radar down")
    assert _client(None, [0.0], get=down).read(rc.WHERE) is None
    asked = []
    assert _client(None, [0.0], get=lambda u: asked.append(u) or [1]).read(rc.WHERE, {"person": "a b"}) == [1]
    assert asked == ["http://radar:1/sam/tools/where?person=a+b"]


@pytest.mark.parametrize("case", [_sends_the_message_with_its_fields, _failure_does_not_raise_and_backs_off, _empty_url_is_off,
                                  _url_from_env_with_default, _read_failure_is_none],
                         ids=["send", "failure", "empty_url", "env", "read"])
def test_radar_client_sends_backs_off_after_failure_and_is_off_without_url(case):
    case()


def test_client_names_are_the_radar_functions():
    """Префикс и имена клиента - контракт с функциями радара: разойдутся - чат и сканирование молча промахнутся."""
    assert (rc.TOOLS_PREFIX, rc.NOTE, rc.WHERE, rc.LAYOUT) == (api.PREFIX, api.NOTE, api.WHERE, api.LAYOUT)


def _wired(hub) -> rc.RadarClient:
    """Клиент, чья сеть - TestClient сервиса радара над хабом."""
    web = TestClient(presence_api.create_app(hub))

    def path(url):
        parts = urlsplit(url)
        return parts.path + (f"?{parts.query}" if parts.query else "")

    def post(url, payload):
        web.post(path(url), json=payload).raise_for_status()

    def get(url):
        res = web.get(path(url))
        if res.status_code != 200:
            raise OSError(res.status_code)
        return json.loads(res.text)
    return rc.RadarClient("http://radar:1", post=post, get=get, background=False)


def test_chat_evidence_and_scanning_read_go_through_the_radar_functions():
    hub = hub_at(TELL_LAYOUT, channels=("wifi", "told", "chat"))
    seen = []
    hub.note_message = lambda *a, **kw: seen.append((a, kw)) or True
    client = _wired(hub)
    client.note_message("chat", "s24", "second")
    assert seen == [(("chat", "s24", "second", None), {"place": None})]
    people, names, place_words = scanning.read_radar(client)
    assert people == [] and names == {"first": "Первая", "second": "Вторая"}
    assert "диван в зале" in place_words and "зал" in place_words


def test_scanning_gets_none_when_the_radar_is_off():
    from radar import presence_hub as ph
    assert scanning.read_radar(_wired(ph.PresenceHub(ph.MODE_OFF, TELL_LAYOUT))) is None


def test_the_gateway_does_not_import_the_radar():
    """Радар - приложение экосистемы (SAM-198): шлюз ходит в него только функциями, модулей radar не импортирует."""
    bad = []
    for src in GATEWAY.rglob("*.py"):
        for node in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
            bad += [f"{src.name}: {n}" for n in names if n == "radar" or n.startswith("radar.")]
    assert bad == []
