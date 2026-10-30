"""Cortex за ACP (ADR-022): ход через agent_loop с инструментами MCP сессии, поток клиенту, разрешения, отмена,
инъекции, поиск инструментов. Настоящий CortexAgent; подменены модель, MCP сессии и клиент ACP."""
import asyncio
import io
from types import SimpleNamespace

import acp
import pytest

from cortex.acp import config as cortex_config
from cortex.acp.agent import CANCELLED, END_TURN, INJECT_METHOD
from cortex.acp.mcp_tools import ERROR_PREFIX, McpTools
from cortex.acp.prompt import IMAGE_MARK
from cortex.acp.stdio import protocol_streams
from cortex.acp.tool_search import FOUND_NONE, SEARCH_NAME
from cortex.acp.updates import SEGMENT_BREAK
from tests.fakes_agents import (DuringStep, FakeAcpClient, FakeLLMClient, FakeMcp, _resp, _tc, cortex_agent,
                                cortex_turn)


def _call(name, args=None, call_id="c1"):
    return _resp(tool_calls=[_tc(call_id, name, args or {})])


# ─── Ответ потоком ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("blocks, replies, stream, tools, text, seen", [
    (["Где Ирина?"], [_resp("Ирина в зале.")], True, {}, "Ирина в зале.", "Где Ирина?"),
    (["Где Ирина?"], [_resp("Ответ без потока.")], False, {}, "Ответ без потока.", "Где Ирина?"),
    (["Где Ирина?"], [_resp("Сейчас посмотрю.", [_tc("c1", "radar_where", {})]), _resp("Ирина в зале.")], True,
     {"radar_where": "зал"}, "Сейчас посмотрю." + SEGMENT_BREAK + "Ирина в зале.", "Где Ирина?"),   # не слипаются
    (["a", SimpleNamespace(type="image"), "b"], [_resp("Вижу.")], True, {}, "Вижу.", f"a\n\n{IMAGE_MARK}\n\nb"),
], ids=["stream", "no_stream", "narration_and_answer", "image_marked"])
async def test_cortex_answer_streams_once(blocks, replies, stream, tools, text, seen):
    client, llm = FakeAcpClient(), FakeLLMClient(replies, stream=stream)
    agent, _ = cortex_agent(llm, FakeMcp(tools), client)
    session = await agent.new_session(cwd=".", mcp_servers=[])
    prompt = [acp.text_block(b) if isinstance(b, str) else b for b in blocks]
    assert (await agent.prompt(prompt, session.session_id)).stop_reason == END_TURN
    assert client.texts() == text
    assert llm.calls[0]["messages"][-1]["content"] == seen


async def test_cortex_tool_call_reaches_mcp_and_client_sees_progress():
    client, mcp = FakeAcpClient(), FakeMcp({"radar_where": "Ирина: зал, диван"})
    llm = FakeLLMClient([_call("radar_where", {"person": "Ирина"}), _resp("Ирина на диване в зале.")], stream=True)
    await cortex_turn(cortex_agent(llm, mcp, client)[0])
    assert mcp.calls == [("radar_where", {"person": "Ирина"})] and llm.tools_seen[0] == ["radar_where"]
    assert client.tool_updates() == [("tool_call", "in_progress"), ("tool_call_update", "completed")]
    assert client.texts() == "Ирина на диване в зале." and client.permissions == []   # чтение - без судьи


# ─── Рискованные инструменты ──────────────────────────────────────────────────

@pytest.mark.parametrize("tool, args, destructive, permission_tools, allow, asked, ran", [
    ("run_command", {"command": "del notes.txt"}, False, None, False, ["run_command"], False),   # судья отказал
    ("wipe", {}, True, frozenset(), True, ["wipe"], True),          # destructiveHint спрашивает и вне списка
    ("run_command", {"command": 'powershell -Command "Get-PSDrive C"'}, False, None, False, [], True),   # чтение
    ("run_command", {"command": "cat data/secrets/privilege.key"}, False, None, True, [], False),   # deny шлюза
], ids=["listed_denied", "destructive_outside_list", "read_only_shell", "gateway_policy_deny"])
async def test_cortex_risky_tool_follows_policy_and_judge(tool, args, destructive, permission_tools, allow, asked, ran):
    client = FakeAcpClient(allow=allow)
    mcp = FakeMcp({tool: {"result": "сделано", "destructive": destructive}})
    cfg = cortex_config.CortexConfig(**({"permission_tools": permission_tools} if permission_tools is not None else {}))
    await cortex_turn(cortex_agent(FakeLLMClient([_call(tool, args), _resp("Итог.")]), mcp, client, cfg=cfg)[0])
    assert client.permissions == asked and bool(mcp.calls) is ran
    assert client.tool_updates()[-1] == (("tool_call_update", "completed") if ran else ("tool_call_update", "failed"))


# ─── Конец хода ───────────────────────────────────────────────────────────────

async def test_cortex_turn_without_a_model_is_a_request_error():
    with pytest.raises(acp.RequestError):
        await cortex_turn(cortex_agent(FakeLLMClient([]), model=None)[0])


async def test_cortex_cancel_ends_the_turn_as_cancelled():
    started, never = asyncio.Event(), asyncio.Event()

    async def thinking(kwargs):
        started.set()
        await never.wait()
    agent, _ = cortex_agent(FakeLLMClient([thinking]))
    session = await agent.new_session(cwd=".", mcp_servers=[])
    turn = asyncio.ensure_future(agent.prompt([acp.text_block("?")], session.session_id))
    await started.wait()
    await agent.cancel(session.session_id)
    assert (await turn).stop_reason == CANCELLED


# ─── Инъекция в идущий ход ────────────────────────────────────────────────────

@pytest.mark.parametrize("during, steps, prompts", [
    ("tool_step", 2, ["Где Ирина?", "[INJECT] и Петя"]),             # следующий шаг того же хода видит реплику
    ("final", 2, ["Где Ирина?", "Твой ответ: Ирина в зале.\n[INJECT] и Петя"]),   # во время финала - ход продолжается
], ids=["next_step", "during_final"])
async def test_cortex_inject_reaches_the_running_turn(during, steps, prompts):
    first = _resp(tool_calls=[_tc("c1", "radar_where", {})]) if during == "tool_step" else _resp("Ирина в зале.")
    inject = DuringStep(INJECT_METHOD, [{"text": "и Петя"}], first)
    llm = FakeLLMClient([inject, _resp("Оба в зале.")])
    agent, _ = cortex_agent(llm, FakeMcp({"radar_where": "зал"}))
    session = (await agent.new_session(cwd=".", mcp_servers=[])).session_id
    await cortex_turn(agent, session=inject.into(agent, session).session)
    assert inject.results == [{"accepted": True}] and len(llm.calls) == steps
    for call, piece in zip(llm.calls, prompts):
        assert any(piece in str(m.get("content")) for m in call["messages"])


async def test_cortex_late_injects_are_capped_and_none_is_accepted_between_turns():
    looping = [DuringStep(INJECT_METHOD, [{"text": "разбуди"}], _resp(f"ход {i}")) for i in range(10)]   # петля агентов
    llm = FakeLLMClient(looping)
    agent, _ = cortex_agent(llm)
    session = (await agent.new_session(cwd=".", mcp_servers=[], creator="main", tools=[])).session_id
    for step in looping:
        step.into(agent, session)
    await cortex_turn(agent, "x", session=session)
    assert len(llm.calls) == 1 + cortex_config.DEFAULT_LATE_INJECTS
    assert await agent.ext_method(INJECT_METHOD, {"session_id": session, "text": "x"}) == {"accepted": False,
                                                                                          "reason": "хода нет"}


# ─── Поиск инструментов ───────────────────────────────────────────────────────

DESCRIPTIONS = {"radar_where": "Где сейчас человек по радару: комната и место",
                "read_file": "Прочитать файл с диска", "git_log": "История коммитов git",
                "load_skill": "Прочитать тело скилла по имени"}


def _many(load_skill="скилл"):
    tools = {f"tool_{i}": "x" for i in range(25)}
    tools.update({name: {"result": "Ирина: зал", "description": text} for name, text in DESCRIPTIONS.items()})
    tools["load_skill"] = {"result": load_skill, "description": DESCRIPTIONS["load_skill"]}
    return FakeMcp(tools)


@pytest.mark.parametrize("first, load_skill, found", [
    (_call(SEARCH_NAME, {"queries": ["радар человек"]}), "скилл", "Доступны со следующего шага:\n- radar_where"),
    (_call(SEARCH_NAME, {"queries": "radar_where"}), "скилл", "- radar_where"),                     # точное имя
    (_call("radar_where"), "скилл", "Ирина: зал"),                                                  # по имени без поиска
    (_call("load_skill", {"name": "radar"}), "Скилл radar: где кто - radar_where(person)", "radar_where(person)"),
], ids=["found_by_search", "exact_name", "called_by_name", "named_by_skill"])
async def test_cortex_many_tools_go_through_search(first, load_skill, found):
    mcp = _many(load_skill)
    llm = FakeLLMClient([first, _resp("Готово.")])
    await cortex_turn(cortex_agent(llm, mcp)[0])
    assert llm.tools_seen[0] == ["load_skill", SEARCH_NAME]                     # весь список - только в описании моста
    bridge = next(c for c in llm.calls[0]["tools"] if c["function"]["name"] == SEARCH_NAME)
    assert "radar_where" in bridge["function"]["description"]
    assert "radar_where" in llm.tools_seen[1]                                   # дальше виден полной схемой
    assert found in llm.calls[1]["messages"][-1]["content"]


async def test_cortex_search_without_finds_tells_the_model():
    llm = FakeLLMClient([_call(SEARCH_NAME, {"queries": "погода"}), _resp("Не нашла.")])
    await cortex_turn(cortex_agent(llm, _many())[0])
    assert llm.calls[1]["messages"][-1]["content"] == FOUND_NONE and llm.tools_seen[1] == llm.tools_seen[0]


# ─── Инструменты MCP: настоящий сервер FastMCP в процессе ─────────────────────

async def test_cortex_mcp_tools_from_a_real_server():
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session
    server = FastMCP("stand")

    @server.tool(description="где кто")
    def radar_where(person: str, when: str = "") -> str:
        return f"{person}: зал"

    @server.tool()
    def broken() -> str:
        raise ValueError("нет радара")

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        tools = McpTools()
        tools.add(session, (await session.list_tools()).tools)
        [schema] = tools.schemas(["radar_where"])
        assert schema["function"]["name"] == "radar_where" and schema["function"]["description"] == "где кто"
        assert tools.registry()["radar_where"] == [{"name": "person"}, {"name": "when"}]
        assert await tools.call("radar_where", {"person": "Ирина"}) == "Ирина: зал"
        assert (await tools.call("broken", {})).startswith(ERROR_PREFIX)
        assert await tools.call("ghost", {}) == f"{ERROR_PREFIX}нет инструмента ghost"
        assert tools.only(["radar_where"]).names == ["radar_where"]


# ─── Настройки и stdio ───────────────────────────────────────────────────────

@pytest.mark.parametrize("env, field, value", [
    ({cortex_config.MAX_TURNS_ENV: "7"}, "max_turns", 7),
    ({cortex_config.MAX_TURNS_ENV: "много"}, "max_turns", cortex_config.DEFAULT_MAX_TURNS),
    ({cortex_config.PERMISSION_TOOLS_ENV: "a, b"}, "permission_tools", frozenset({"a", "b"})),
    ({}, "permission_tools", frozenset(cortex_config.DEFAULT_PERMISSION_TOOLS)),
    ({cortex_config.SEARCH_MIN_TOOLS_ENV: "5"}, "search_min_tools", 5),
    ({cortex_config.CORE_TOOLS_ENV: ""}, "core_tools", ()),
    ({cortex_config.LATE_INJECTS_ENV: "1"}, "late_injects", 1),
])
def test_cortex_config_from_env(env, field, value):
    assert getattr(cortex_config.from_env(env), field) == value


async def test_protocol_streams_read_and_write_given_files():
    sink = io.BytesIO()
    reader, writer = await protocol_streams(io.BytesIO(b'{"a":1}\n'), sink, 1024)
    assert await reader.readline() == b'{"a":1}\n'
    writer.write(b"out\n")
    await writer.drain()
    assert sink.getvalue() == b"out\n"


async def test_cortex_reasoning_never_reaches_the_answer():
    """agents 14: рассуждение модели блоком <think> в ответ клиенту не попадает: ответ Cortex уходит голосом. Поток
    с <think> в самом тексте идёт как есть: парный блок не звучит (ADR-003, gateway/speech_meta.py), а рассуждение
    поставщика (reasoning_content) llm_client в поток не шлёт - тест speech-text в tests/test_llm_client.py."""
    client = FakeAcpClient()
    reply = _resp("<think>Ирина вчера была в зале, значит</think>Ирина в зале.")
    await cortex_turn(cortex_agent(FakeLLMClient([reply]), FakeMcp({}), client)[0])
    assert client.texts() == "Ирина в зале."
