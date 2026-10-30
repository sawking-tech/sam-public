"""Текст основной модели в монитор без озвучки (lead-monitor): POST /sam/session/{id}/note -> assistant_reply с
каналом text в монитор и историю; пустой текст - 400 без события. Шина настоящая, история - на mongomock."""
import json

import pytest

from gateway import main
from gateway.repos.session_repo import CHANNEL, TEXT_CHANNEL
from tests.fakes_core import app_client  # noqa: F401 - фикстура

SCHEME = "┌──┐\n│ГW│──> монитор\n└──┘  <tag>"


@pytest.fixture
def monitor(app_client):
    """Клиент, база и очередь подписчика монитора на общей шине."""
    client, db = app_client
    queue = main.event_bus.subscribe()
    yield client, db, queue
    main.event_bus.unsubscribe(queue)


def _shown(queue):
    out = []
    while not queue.empty():
        out.append(json.loads(queue.get_nowait()))
    return [e for e in out if e["type"] == "assistant_reply"]


def test_note_goes_to_monitor_and_history_as_text(monitor):
    client, db, queue = monitor
    r = client.post("/sam/session/voice/note", json={"text": SCHEME})
    assert r.json() == {"ok": True}
    [event] = _shown(queue)
    assert event["content"] == SCHEME and event["session"] == "voice" and event["model"] == "claude-code"
    assert event["source"] == "observer" and event[CHANNEL] == TEXT_CHANNEL
    doc = db.sessions.find_one({"session_id": "voice"})
    assert doc["content"] == SCHEME and doc[CHANNEL] == TEXT_CHANNEL   # после F5 монитор берёт тот же канал


def test_empty_note_is_refused(monitor):
    client, db, queue = monitor
    assert client.post("/sam/session/voice/note", json={"text": "  \n"}).status_code == 400
    assert _shown(queue) == [] and db.sessions.count_documents({}) == 0
