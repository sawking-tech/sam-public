"""Модель нового агента (SAM-233): явный выбор человека, затем оркестратор по списку приоритетов, затем роутер."""
import pytest

from gateway import agent, llm_client, model_picker
from gateway import llm_router

RANKING = [{"priority": 1, "source": "openrouter", "model": "cheap"},
           {"priority": 2, "source": "polza", "model": "second"},
           {"priority": 3, "source": "polza", "model": "third"}]


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(agent, "_mongo_ok", False)
    monkeypatch.setattr(agent._agents_tool, "level_source", lambda level: "")
    monkeypatch.setattr(llm_client, "cooling_sources", lambda: set())

    async def recommend(db, http_get, steps, cooling, deadline_s=None, **kw):
        return {"reason": "r", "ranking": RANKING}

    async def source_config(source, model=""):
        return {"source": source, "model": model}

    async def best():
        return {"source": "router", "model": "default"}
    monkeypatch.setattr(model_picker, "recommend", recommend)
    monkeypatch.setattr(llm_router, "source_config", source_config)
    monkeypatch.setattr(agent, "get_best_llm", lambda *a, **k: best())


async def test_first_available_model_of_the_ranking_is_taken(world):
    cfg = await agent._llm_for_level("smart", 20)
    assert (cfg["source"], cfg["model"]) == ("openrouter", "cheap")
    assert [(f["source"], f["model"]) for f in cfg["fallbacks"]] == [("polza", "second"), ("polza", "third")]   # запасные по приоритету


async def test_a_cooling_source_is_skipped_without_a_probe(world, monkeypatch):
    monkeypatch.setattr(llm_client, "cooling_sources", lambda: {"openrouter"})
    cfg = await agent._llm_for_level("smart", 20)
    assert (cfg["source"], cfg["model"]) == ("polza", "second")


async def test_a_model_without_config_falls_to_the_next_priority(world, monkeypatch):
    async def source_config(source, model=""):
        return None if model in ("cheap", "second") else {"source": source, "model": model}
    monkeypatch.setattr(llm_router, "source_config", source_config)
    assert (await agent._llm_for_level("smart", 20))["model"] == "third"


async def test_owner_choice_for_the_level_beats_the_picker(world, monkeypatch):
    monkeypatch.setattr(agent._agents_tool, "level_source", lambda level: "polza:owner-pick")
    assert (await agent._llm_for_level("smart", 20))["model"] == "owner-pick"


async def test_picker_failure_leaves_the_router_choice(world, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("каталог недоступен")
    monkeypatch.setattr(model_picker, "recommend", broken)
    assert (await agent._llm_for_level("smart", 20))["source"] == "router"


async def test_a_resumed_agent_gets_the_model_of_its_task(world, monkeypatch):
    """Воскрешение идёт через оркестратор с текстом задачи, а не на модели шлюза по умолчанию."""
    seen = {}
    monkeypatch.setattr(agent, "_find_task", lambda task_id: {"level": "smart", "pool_steps": 20})

    async def resume(deps, task_id, message, llm_config):
        seen["cfg"] = llm_config
    monkeypatch.setattr(agent._worker_runtime, "resume_worker_task", resume)
    monkeypatch.setattr(agent, "_worker_deps_for", lambda task_id: None)
    await agent._resume_worker_task("abcd1234", "продолжай", {"source": "polza", "model": "default-flash"})
    assert (seen["cfg"]["source"], seen["cfg"]["model"]) == ("openrouter", "cheap")
