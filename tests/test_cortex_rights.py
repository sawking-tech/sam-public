"""Права сессии Cortex от создателя (SAM-133): выдача инструментов, модель по метке, каналы к создателю, выдача по
ходу и дети агента. Настоящий CortexAgent; подменены модель, MCP сессии и клиент ACP."""
import asyncio

import pytest

from cortex.acp import channels
from cortex.acp.agent import GRANT_METHOD
from tests.fakes_agents import (CORTEX_LLM, DuringStep, FakeAcpClient, FakeLLMClient, FakeMcp, _resp, _tc, cortex_agent,
                                cortex_turn)

TOOLS = {"radar_where": "в спальне", "run_command": "ok", "browser": "страница"}
WEAK = {**CORTEX_LLM, "model": "qwen-weak"}


def _step(*calls):
    return _resp(tool_calls=[_tc(f"c{i}", name, args) for i, (name, args) in enumerate(calls)])


@pytest.mark.parametrize("meta, visible, model, picked", [
    ({"creator": "main", "tools": ["radar_where"]}, {"radar_where", *channels.BASE_NAMES}, "test-model", ["router"]),
    ({"tools": "radar_where", "level": "genius"}, set(TOOLS), "test-model", ["router"]),   # мусор - поле не задано
    ({"level": "Weak"}, set(TOOLS), "qwen-weak", ["lmstudio"]),                            # метка - своя модель
    ({"level": "smart"}, set(TOOLS), "test-model", ["zai", "router"]),                     # источник недоступен
], ids=["grant_narrows", "garbage_meta", "level_model", "level_source_down"])
async def test_cortex_session_rights_limit_tools_and_pick_model(meta, visible, model, picked):
    llm = FakeLLMClient([_resp("Готово.")])
    agent, chosen = cortex_agent(llm, FakeMcp(TOOLS), sources={"lmstudio": WEAK},
                                 level_map={"weak": "lmstudio", "smart": "zai"})
    await cortex_turn(agent, **meta)
    assert set(llm.tools_seen[0]) == visible and llm.models == [model] and chosen == picked


@pytest.mark.parametrize("creator, reached", [("main", True), ("", False)], ids=["with_creator", "without_creator"])
async def test_cortex_channels_only_with_creator(creator, reached):
    client = FakeAcpClient()
    llm = FakeLLMClient([_step(("ask_creator", {"question": "какой порт?"})), _resp("Жду ответа.")])
    agent, _ = cortex_agent(llm, FakeMcp({"radar_where": "x"}), client)
    _, sid = await cortex_turn(agent, "сделай", **({"creator": creator, "tools": []} if creator else {}))
    assert bool(set(channels.BASE_NAMES) & set(llm.tools_seen[0])) is reached
    if reached:
        assert "radar_where" not in llm.tools_seen[0]                    # выдача пустая, каналы есть всегда
        assert client.events == [(channels.CHANNEL_METHOD, {"session_id": sid, "kind": "ask", "text": "какой порт?",
                                                            "question": "какой порт?"})]
    else:
        assert client.events == [] and "Инструмента ask_creator нет" in llm.calls[1]["messages"][-1]["content"]


async def test_cortex_grant_mid_turn_is_visible_from_the_next_step():
    client = FakeAcpClient(ext=lambda method, params: {"result": "Доставлено"})
    grant = DuringStep(GRANT_METHOD, [{"tool": "run_command"}, {"tool": "agent:zz99", "note": "disk: свободное место"},
                                      {"tool": "ghost"}],
                       _step(("send_to_agent", {"to": "xx00", "message": "чужому"}),
                             ("send_to_agent", {"to": "ab12", "message": "своему"})))
    llm = FakeLLMClient([grant, _step(("run_command", {"command": "dir"}),
                                      ("send_to_agent", {"to": "zz99", "message": "выданному"})), _resp("ок")])
    agent, _ = cortex_agent(llm, FakeMcp(TOOLS), client)
    session = (await agent.new_session(cwd=".", mcp_servers=[], creator="main", tools=["radar_where"], peers=["ab12"],
                                       peer_notes={"ab12": "radar: где Ирина"})).session_id
    await cortex_turn(agent, "x", session=grant.into(agent, session).session)
    assert [r["granted"] for r in grant.results] == [True, True, False]               # неизвестное имя - отказ
    assert "run_command" not in llm.tools_seen[0] and "run_command" in llm.tools_seen[1]
    send = next(t for t in llm.calls[1]["tools"] if t["function"]["name"] == "send_to_agent")["function"]
    assert "ab12 - radar: где Ирина" in send["description"] and "zz99 - disk: свободное место" in send["description"]
    assert [p["to"] for _, p in client.events] == ["ab12", "zz99"]                     # чужому - не дошло до клиента


@pytest.mark.parametrize("depth, offered", [(None, False), (1, True)], ids=["no_right", "right_to_children"])
async def test_cortex_spawn_is_offered_only_with_the_right_to_children(depth, offered):
    llm = FakeLLMClient([_resp("Готово.")])
    agent, _ = cortex_agent(llm, FakeMcp(TOOLS))
    await cortex_turn(agent, "x", creator="main", tools=["radar_where"], **({"depth": depth, "children": 1} if depth else {}))
    schemas = str(llm.calls[0]["tools"])
    assert ("spawn_agent" in llm.tools_seen[0]) is offered
    assert ("spawn_agent" in schemas or "grant_child" in schemas) is offered   # у последнего уровня - ни упоминания


async def _created(method, params):
    await asyncio.sleep(0)   # создание ребёнка ждёт модель метки: оба параллельных вызова успевают начаться
    return {"result": f"Агент {params['description']} создан", "spawned": True}


async def test_cortex_spawns_one_child_not_wider_then_spawn_is_gone():
    client = FakeAcpClient(ext=_created)
    llm = FakeLLMClient([
        _step(("spawn_agent", {"description": "шире", "tools": "radar_where, browser"})),
        _step(("spawn_agent", {"description": "a", "tools": "radar_where"}),
              ("spawn_agent", {"description": "b", "tools": "radar_where"})),     # два в одном шаге - одно место
        _step(("grant_child", {"task_id": "ch11", "tool": "browser"}),
              ("grant_child", {"task_id": "ch11", "tool": "agent:pp00"})),
        _resp("ок")])
    agent, _ = cortex_agent(llm, FakeMcp(TOOLS), client)
    await cortex_turn(agent, "x", creator="main", tools=["radar_where", "run_command"], peers=["pp00"], depth=2,
                      children=1)
    spawned = [p for _, p in client.events if p["kind"] == "spawn"]
    assert len(spawned) == 1 and spawned[0]["tools"] == ["radar_where"] and spawned[0]["depth"] == 1
    fed = [m["content"] for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert any("шире твоих прав: browser" in c for c in fed)
    assert "spawn_agent" in llm.tools_seen[1] and "spawn_agent" not in llm.tools_seen[2]   # со следующего шага пропал
    assert "grant_child" in llm.tools_seen[2]
    granted = [p["tool"] for _, p in client.events if p["kind"] == "grant_child"]
    assert granted == ["agent:pp00"]                                                    # своего права нет - отказ
    assert any("browser нет в твоих правах" in m["content"] for m in llm.calls[3]["messages"] if m.get("role") == "tool")


@pytest.mark.parametrize("task, meta", [
    ("default", {}), ("turn", {}), ("default", {"level": "smart"}),
], ids=["default_task", "turn_task", "level_points_at_acp"])
async def test_cortex_never_picks_itself_as_its_model(monkeypatch, task, meta):
    """agents 11: всё окружение указывает на ACP-агента (основной ход, LLM_SOURCE, метка сложности), а модель
    Cortex - следующая в цепочке роутера, не сам Cortex. Настоящие роутер и выбор источника; LM Studio не отвечает."""
    from cortex.acp import config as cortex_config
    from cortex.acp.agent import CortexAgent
    from gateway import llm_router, tool_policy
    from tests.fakes_agents import FakeAcpClient
    for name, value in {"SAM_AGENT_BACKEND": "acp", "SAM_ACP_COMMAND": "hermes-acp", "LLM_SOURCE": "acp",
                        "GEMINI_API_KEY": "k"}.items():
        monkeypatch.setenv(name, value)

    async def lmstudio_down(*args, **kwargs):
        return None
    monkeypatch.setattr(llm_router, "_check_lmstudio", lmstudio_down)
    assert (await llm_router.get_best_llm(task=task))["source"] == "acp"     # без пропуска роутер отдал бы ACP
    llm = FakeLLMClient([_resp("Готово.")])
    agent = CortexAgent(cortex_config.CortexConfig(llm_task=task), llm_router.get_best_llm, connect_tools=FakeMcp(TOOLS).connect,
                        llm=llm, pick_source=llm_router.source_config, level_source=lambda level: "acp",
                        policy=tool_policy.load_policy(tool_policy.DEFAULT_POLICY_FILE))
    agent.on_connect(FakeAcpClient())
    await cortex_turn(agent, "x", **meta)

    assert llm.models == [llm_router.GEMINI_MODEL]


@pytest.mark.parametrize("value, expected", [
    ("openrouter", ("openrouter", "")),
    ("OpenRouter:z-ai/glm-5.3", ("openrouter", "z-ai/glm-5.3")),
    ("polza: openai/gpt-6.1-terra:batch", ("polza", "openai/gpt-6.1-terra:batch")),   # двоеточие в id модели сохраняется
    ("", ("", "")),
], ids=["source-only", "source-model", "colon-in-model-id", "empty"])
def test_level_value_splits_into_source_and_model(value, expected):
    from gateway.agent_levels import split_level
    assert split_level(value) == expected


async def test_cortex_passes_the_level_model_to_the_source_picker():
    """Метка smart=lmstudio:qwen-smart: источник и модель доходят до выбора конфига раздельно."""
    llm = FakeLLMClient([_resp("Готово.")])
    agent, chosen = cortex_agent(llm, FakeMcp(TOOLS), sources={"lmstudio": WEAK},
                                 level_map={"smart": "LMStudio:qwen-smart"})
    await cortex_turn(agent, level="smart")
    assert chosen == ["lmstudio:qwen-smart"] and llm.models == ["qwen-weak"]
