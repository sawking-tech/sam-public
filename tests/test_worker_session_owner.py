"""Сессия воркера при создании и оживлении (ревью 16f8c30): воркер получает сессию своего хода, а не глобальную, и
оживляется по имени только в своей сессии - иначе его задачи песочницы стали бы собственностью чужой сессии."""
import asyncio
from datetime import datetime

import pytest

from gateway import agent
from gateway import token_usage
from gateway import worker_session as ws

TASK_ID = "a" * 32


@pytest.fixture
def spawn(monkeypatch, mongo_db):
    """spawn_agent без Mongo и LLM: запуск воркера и оживление записываются."""
    launched, resumed = [], []
    monkeypatch.setattr(agent, "_db", mongo_db)
    monkeypatch.setattr(agent, "_mongo_ok", True)
    monkeypatch.setattr(agent, "_current_llm", {"model": "fake"})
    monkeypatch.setattr(agent, "_resolve_device", lambda device: "pc")
    monkeypatch.setattr(agent, "_launch_worker", lambda spec: (launched.append(spec), "b" * 32)[1])

    async def resume(task_id, message, llm):
        resumed.append(task_id)
    monkeypatch.setattr(agent, "_resume_worker_task", resume)
    monkeypatch.setattr(agent, "_current_session_id", "workpc")     # глобальная - от другого, параллельного хода
    monkeypatch.delenv("SAM_AGENT_FRAMEWORK", raising=False)
    return launched, resumed, mongo_db


def _spawn_in(session: str, **kw) -> str:
    async def go():
        with token_usage.usage_scope(session, "main"):
            return await agent._impl_create_task("сделай график", **kw)
    return asyncio.run(go())


def test_worker_gets_the_session_of_its_turn(spawn):
    launched, _, _ = spawn
    _spawn_in("voice")
    assert launched[-1].parent_session == "voice"


def _idle_worker(db, session: str) -> None:
    db.tasks.insert_one({"id": TASK_ID, "name": "chart", "device": "pc", "status": "idle",
                         "created_at": datetime.utcnow()})
    ws.save_session(db, TASK_ID, device="pc", description="график", messages=[], parent_session=session)


def test_worker_of_another_session_is_not_revived_by_name(spawn):
    launched, resumed, db = spawn
    _idle_worker(db, "voice")
    text = _spawn_in("workpc", name="chart")
    assert resumed == [] and "создана" in text and launched[-1].parent_session == "workpc"


def test_own_worker_is_revived_by_name(spawn):
    launched, resumed, db = spawn
    _idle_worker(db, "voice")
    text = _spawn_in("voice", name="chart")
    assert "Реанимирую" in text and launched == []


@pytest.mark.parametrize("framework, env", [("cortex", ""), ("", "cortex")], ids=["param", "from_env"])
def test_spawn_agent_refuses_the_cortex_framework(spawn, monkeypatch, framework, env):
    """agents 25: spawn_agent выдачу инструментов не передаёт, поэтому каркас cortex - и параметром, и из
    SAM_AGENT_FRAMEWORK - отказ без запуска: иначе сессия Cortex получила бы все инструменты стенда."""
    from gateway.tools import agents as agents_tool
    launched, _, _ = spawn
    if env:
        monkeypatch.setenv("SAM_AGENT_FRAMEWORK", env)
    assert _spawn_in("voice", framework=framework) == agents_tool.CORTEX_NEEDS_GRANT
    assert launched == []
    assert "создана" in _spawn_in("voice", framework="sam") and len(launched) == 1
