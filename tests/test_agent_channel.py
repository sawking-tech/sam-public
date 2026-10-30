"""Канал агента разработки к создателю (SAM-164): ручки шлюза ask, inbox и status поверх настоящего ask_creator_impl,
задач в mongomock и свежей шины; ожидание ответа в сервере канала с внедрёнными часами."""
import importlib.util
from pathlib import Path

import pytest

from gateway.tools.agents import QUESTION_SOURCE
from tests.fakes_agents import GatewayRig

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def gw(monkeypatch, tmp_path):
    return GatewayRig(monkeypatch, tmp_path)


def _server():
    spec = importlib.util.spec_from_file_location("agent_channel_server", ROOT / "mcp" / "agent_channel_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_coding_agent_asks_creator_and_reads_the_answer(gw, monkeypatch):
    monkeypatch.setattr(gw.agent, "AGENT_INBOX_MAX", 2)
    short = gw.running_task()
    session = f"task:{short}"

    r = gw.client.post(f"/sam/agents/{short}/ask", json={"question": "какой порт?"})
    assert r.status_code == 200 and "[INJECT]" in r.json()["result"]
    asked = gw.inbox()[0]
    assert asked["source"] == QUESTION_SOURCE and asked["session"] == session and "какой порт?" in asked["text"]
    assert gw.bus.of("task_message")[0]["content"] == "Вопрос создателю: какой порт?"

    # ответ основной модели - в сессию идущей задачи; канал забирает его один раз, не больше лимита за раз
    for answer in ("порт 8000", "и 3000", "и 10210"):
        assert gw.client.post("/sam/inject", json={"session_id": session, "message": answer}).json()["kind"] == \
            "running_inject"
    assert gw.client.post(f"/sam/agents/{short}/inbox", json={}).json() == {"messages": ["порт 8000", "и 3000"]}
    assert gw.client.post(f"/sam/agents/{short}/inbox", json={}).json() == {"messages": ["и 10210"]}
    assert gw.client.post(f"/sam/agents/{short}/inbox", json={}).json() == {"messages": []}

    assert gw.client.post(f"/sam/agents/{short}/status", json={"message": "читаю код"}).status_code == 200
    assert gw.bus.of("task_message")[-1] == {"event": "task_message", "content": "читаю код", "session": "voice",
                                             "task_id": short, "kind": "status"}


@pytest.mark.parametrize("method, path, body", [
    ("post", "/sam/agents/zz99/ask", {"question": "какой порт?"}),
    ("post", "/sam/agents/zz99/inbox", {}),
    ("post", "/sam/agents/zz99/status", {"message": "x"}),
])
def test_channel_of_a_task_that_is_not_running_is_refused(gw, method, path, body):
    gw.bus.inject("task:zz99", "чужая вводная")
    r = getattr(gw.client, method)(path, **({"json": body} if body is not None else {}))
    assert r.status_code == 404
    assert gw.bus.pop_inject("task:zz99") == "чужая вводная"   # очередь чужой задачи не тронута
    assert gw.inbox() == []


def test_wait_answer_returns_the_first_reply_or_nothing_by_the_deadline():
    srv = _server()
    t = [0.0]
    replies = [[], [], ["ответ создателя"]]
    got = srv.wait_answer(lambda: replies.pop(0), 120, 2, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert got == ["ответ создателя"] and t[0] == 4
    t[0] = 0.0
    assert srv.wait_answer(lambda: [], 5, 2, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s)) == []
