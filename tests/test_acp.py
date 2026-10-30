"""Шлюз - клиент ACP (ADR-022): поставщик хода для llm_client поверх агента подпроцессом и роутер.

Агент - настоящий подпроцесс на SDK ACP (tests/fixtures/acp_fake_agent.py), поведение задаётся меткой в тексте хода.
"""
from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from gateway import llm_router
from gateway.acp import agent as acp_agent_module
from gateway.acp import config as acp_config
from gateway.acp.agent import AcpAgent
from gateway.acp.client import SamClient
from gateway.acp.prompt import CURRENT_LABEL, IMAGE_MARK, TOOL, prompt_text
from gateway.acp.provider import AcpProvider
from gateway.llm_client import LLMProviderError

FAKE_AGENT = Path(__file__).parent / "fixtures" / "acp_fake_agent.py"
# Сколько ждать кода выхода после обрыва stdio: процесс Python на Windows выходит дольше секунды под нагрузкой.
# Ожидание кончается, как только процесс вышел, - запас не замедляет тест
EXIT_WAIT_S = 10.0


def _cfg(tmp_path: Path, **over) -> acp_config.AcpConfig:
    return acp_config.AcpConfig(**{"command": sys.executable, "args": (str(FAKE_AGENT),), "cwd": tmp_path,
                                   "mcp_url": "", **over})


def _user(text: str) -> list[dict]:
    return [{"role": "system", "content": "Ты SAM."}, {"role": "user", "content": text}]


async def _call(cfg, text, resolver=None, stream=False, on_piece=None):
    agent = AcpAgent(cfg, SamClient(resolver) if resolver else SamClient())
    provider = AcpProvider(agent, **({"on_piece": on_piece} if on_piece else {}))
    try:
        reply = await provider.call({}, messages=_user(text), stream=stream, tools=[{"x": 1}])
        return [c async for c in reply] if stream else reply.choices[0].message
    finally:
        await agent.close()


# ─── Поставщик хода ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("stream", [True, False], ids=["stream", "whole"])
async def test_acp_provider_streams_and_returns_whole_message(tmp_path, stream):
    pieces = []
    out = await _call(_cfg(tmp_path), "MODE:echo", stream=stream, on_piece=pieces.append)
    if stream:
        assert [c.choices[0].delta.content for c in out if c.choices and c.choices[0].delta.content] == [
            "Привет, ", "Петя"]
        assert any(not c.choices for c in out)               # ход инструмента - пустой чанк
        assert out[-1].choices[0].finish_reason == "stop"
    else:
        assert out.content == "Привет, Петя" and out.tool_calls is None
    assert [p.kind for p in pieces if p.kind == TOOL] == [TOOL]


async def _allow(title, args):
    return True


async def _deny(title, args):
    return False


async def _broken(title, args):
    raise RuntimeError("судья недоступен")


@pytest.mark.parametrize("mode, resolver, answer", [
    ("MODE:permission", _allow, "permission:allow_once"),
    ("MODE:permission", _deny, "permission:reject_once"),
    ("MODE:permission", _broken, "permission:reject_once"),       # судья упал - отказ
    ("MODE:always", _allow, "permission:cancelled"),              # «навсегда» агенту не выдаётся
], ids=["allowed", "denied", "judge_failed", "only_allow_always"])
async def test_acp_permission_goes_to_resolver(tmp_path, mode, resolver, answer):
    asked = []

    async def recording(title, args):
        asked.append((title, args))
        return await resolver(title, args)
    assert (await _call(_cfg(tmp_path), mode, recording)).content == answer
    assert asked == [("delete_file", {"path": "x.txt"})]


async def test_acp_dead_agent_is_provider_error_and_next_turn_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(acp_agent_module, "EXIT_WAIT_S", EXIT_WAIT_S)
    agent = AcpAgent(_cfg(tmp_path), SamClient())
    provider = AcpProvider(agent)
    try:
        with pytest.raises(LLMProviderError) as err:
            await provider.call({}, messages=_user("MODE:die"))
        assert err.value.source == acp_config.SOURCE and not agent.alive
        resp = await provider.call({}, messages=_user("MODE:echo"))
    finally:
        await agent.close()
    assert resp.choices[0].message.content == "Привет, Петя"


async def test_acp_interrupted_turn_leaves_agent_ready_for_next(tmp_path):
    agent = AcpAgent(_cfg(tmp_path), SamClient())
    try:
        turn = agent.turn("MODE:echo")
        first = await turn.__anext__()
        await turn.aclose()                                  # ход прерван снаружи (перебивание, срок)
        resp = await AcpProvider(agent).call({}, messages=_user("MODE:echo"))
    finally:
        await agent.close()
    assert first.text == "Привет, " and resp.choices[0].message.content == "Привет, Петя"


async def test_acp_agent_reports_a_process_that_exited_between_turns(tmp_path):
    """SAM-195: health check Cortex - не поднятый агент не поломка, вышедший между ходами - поломка до следующего хода."""
    agent = AcpAgent(_cfg(tmp_path), SamClient())
    try:
        assert not agent.exited
        await AcpProvider(agent).call({}, messages=_user("MODE:echo"))
        assert not agent.exited
        agent._process.kill()
        await agent._process.wait()
        assert agent.exited
        await AcpProvider(agent).call({}, messages=_user("MODE:echo"))
        assert not agent.exited
    finally:
        await agent.close()


async def test_acp_agent_counts_running_turns_for_restart(tmp_path):
    """SAM-195: перезапуск Cortex ждёт идущих ходов - счёт растёт на ход и падает и после прерванного."""
    agent = AcpAgent(_cfg(tmp_path), SamClient())
    try:
        turn = agent.turn("MODE:echo")
        await turn.__anext__()
        assert agent.turns == 1
        await turn.aclose()
        assert agent.turns == 0
        await AcpProvider(agent).call({}, messages=_user("MODE:echo"))
        assert agent.turns == 0
    finally:
        await agent.close()


# ─── Окружение и дом агента ───────────────────────────────────────────────────

async def test_acp_agent_gets_only_listed_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ACP_FAKE_PASSED", "да")
    monkeypatch.setenv("ACP_FAKE_HIDDEN", "секрет")
    cfg = _cfg(tmp_path, env_names=("ACP_FAKE_PASSED", "ACP_FAKE_UNSET"))
    assert (await _call(cfg, "MODE:env")).content == "да|None"


async def test_acp_agent_home_is_prepared_from_template_with_an_absolute_path(tmp_path, monkeypatch):
    template = tmp_path / "template"
    template.mkdir()
    (template / "config.yaml").write_text("model: glm", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / "state.db").write_text("состояние", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = _cfg(tmp_path, cwd=tmp_path / "work", home_template=template, home=Path("home"), home_var="ACP_FAKE_HOME")
    assert acp_config.agent_env(cfg, {})["ACP_FAKE_HOME"] == str(home.resolve())   # агент работает в своей папке
    assert (await _call(cfg, "MODE:home")).content == "model: glm"
    assert (home / "state.db").read_text(encoding="utf-8") == "состояние"           # состояние агента не тронуто


@pytest.mark.parametrize("listed, answer", [
    (" ACP_FAKE_PASSED , ,ACP_FAKE_UNSET", "да|None"),
    ("", "None|None"),
], ids=["listed_names", "nothing_listed"])
async def test_acp_env_names_come_from_sam_acp_env_and_nothing_else_leaks(tmp_path, monkeypatch, listed, answer):
    """agents 4: SAM_ACP_ENV разбирается в список имён; агент получает только их - ни ключей шлюза, ни прочего."""
    monkeypatch.setenv("ACP_FAKE_PASSED", "да")
    monkeypatch.setenv("ACP_FAKE_HIDDEN", "секрет")
    monkeypatch.setenv("SAM_API_KEY", "test-do-not-use")
    environ = {**os.environ, acp_config.COMMAND_ENV: sys.executable,
               acp_config.ARGS_ENV: str(FAKE_AGENT), acp_config.CWD_ENV: str(tmp_path), acp_config.ENV_NAMES_ENV: listed}
    cfg = acp_config.from_env(environ)
    assert acp_config.agent_env(cfg, environ) == ({"ACP_FAKE_PASSED": "да"} if listed else {})
    assert (await _call(replace(cfg, mcp_url=""), "MODE:env")).content == answer


def test_hermes_template_keeps_the_agent_a_bare_reasoning_engine(tmp_path):
    """agents 48: конфиг Hermes, который шлюз кладёт в дом агента, - пустой набор ACP (не отсутствующий: в Hermes
    24 июля пустой или пропущенный список значит «все инструменты»), отключённые наборы, без своей памяти."""
    yaml = pytest.importorskip("yaml")
    template = Path(acp_config.ROOT) / "gateway" / "acp" / "hermes"
    cfg = acp_config.AcpConfig(command="hermes-acp.exe", home_template=template, home=tmp_path / "home",
                               home_var="HERMES_HOME")
    acp_config.prepare_home(cfg)
    conf = yaml.safe_load((tmp_path / "home" / "config.yaml").read_text(encoding="utf-8"))
    assert conf["platform_toolsets"]["acp"] == []
    assert {"terminal", "file", "web", "browser", "code_execution", "delegation", "memory", "skills",
            "cronjob"} <= set(conf["agent"]["disabled_toolsets"])
    assert conf["memory"]["memory_enabled"] is False and conf["memory"]["user_profile_enabled"] is False


@pytest.mark.parametrize("over", [
    {"home_template": Path("нет"), "home_var": "ACP_FAKE_HOME"},
    {"command": "no-such-agent.exe"},
], ids=["missing_home_template", "missing_command"])
async def test_acp_agent_that_cannot_start_is_provider_error(tmp_path, over):
    over = {k: (tmp_path / v if isinstance(v, Path) else v) for k, v in over.items()}
    with pytest.raises(LLMProviderError):
        await _call(_cfg(tmp_path, home=tmp_path / "home", **over), "привет")


@pytest.mark.parametrize("raw, timeout", [("15", 15.0), ("минута", acp_config.DEFAULT_START_TIMEOUT_S)])
def test_acp_numbers_from_env_fall_back_to_defaults(raw, timeout):
    cfg = acp_config.from_env({acp_config.COMMAND_ENV: "hermes-acp.exe", acp_config.START_TIMEOUT_ENV: raw})
    assert cfg.start_timeout_s == timeout
    assert acp_config.from_env({}) is None


# ─── Роутер: ход у агента только по флагу ─────────────────────────────────────

ROUTER = {"source": "gemini", "model": "gemini-2.0-flash"}


@pytest.mark.parametrize("backend, command, task, skip, acp", [
    ("acp", "hermes-acp.exe", "turn", set(), True),
    ("acp", "hermes-acp.exe", "fast", set(), True),
    (None, "hermes-acp.exe", "turn", set(), False),                # без флага
    ("acp", None, "turn", set(), False),                           # флаг есть, агента нет
    ("acp", "hermes-acp.exe", "turn", {"acp"}, False),             # агент в паузе после сбоя - замена по роутеру
    ("acp", "hermes-acp.exe", "default", set(), False),            # служебные задачи - мимо агента
], ids=["turn", "fast", "no_flag", "no_command", "paused", "service_task"])
async def test_turn_goes_to_acp_only_by_backend_flag(monkeypatch, backend, command, task, skip, acp):
    monkeypatch.setenv("LLM_SOURCE", ROUTER["source"])          # роутер без опроса поставщиков
    monkeypatch.setenv("GEMINI_API_KEY", "test-do-not-use")
    monkeypatch.setenv("LLM_FAST_SOURCE", "off")
    for name, value in ((acp_config.BACKEND_ENV, backend), (acp_config.COMMAND_ENV, command)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    cfg = await llm_router.get_best_llm(task=task, skip_sources=skip)
    assert cfg["source"] == (acp_config.SOURCE if acp else ROUTER["source"])


def test_prompt_text_puts_system_first_history_then_current_message():
    text = prompt_text([
        {"role": "system", "content": "Ты SAM."},
        {"role": "user", "content": "Где Маша?"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "radar_where", "arguments": "{}"}}]},
        {"role": "tool", "content": "Маша на кухне"},
        {"role": "assistant", "content": "На кухне."},
        {"role": "user", "content": [{"type": "text", "text": "А Петя?"}, {"type": "image_url", "image_url": {}}]},
    ])
    assert text.startswith("Ты SAM.")
    assert "called radar_where({})" in text
    assert "Tool result: Маша на кухне" in text
    assert text.endswith(f"{CURRENT_LABEL}: А Петя?\n{IMAGE_MARK}")
    assert text.index("User: Где Маша?") < text.index(CURRENT_LABEL)


async def test_agent_thoughts_never_reach_the_voice(tmp_path, monkeypatch):
    """speech-text 10: мысль агента ACP (agent_thought_chunk) идёт поставщиком отдельно от текста, llm_client не шлёт её
    в поток озвучки, а в итоге хода она - блок <think>, который не звучит. Агент - настоящий подпроцесс."""
    from gateway import llm_client
    from gateway.text_utils import transparent_speech
    agent = AcpAgent(_cfg(tmp_path), SamClient())
    monkeypatch.setitem(llm_client._source_calls, acp_config.SOURCE, AcpProvider(agent).call)
    streamed = []
    try:
        reply = await llm_client.make_llm_call({"source": acp_config.SOURCE, "model": "acp"},
                                               messages=_user("MODE:think"), on_delta=streamed.append)
    finally:
        await agent.close()

    assert "".join(streamed) == "Ирина в зале."
    assert "Ирина вчера была в зале" in reply.choices[0].message.content
    assert transparent_speech(reply.choices[0].message.content) == "Ирина в зале."


@pytest.mark.parametrize("env, answer", [
    ({acp_config.MCP_NAME_ENV: "sam-test", acp_config.MCP_URL_ENV: "http://127.0.0.1:3999/mcp"},
     "sam-test http://127.0.0.1:3999/mcp"),
    ({}, None),                                          # не задано - MCP стенда по умолчанию
], ids=["from_env", "default"])
async def test_agent_session_gets_the_sam_mcp_from_settings(tmp_path, env, answer):
    """agents 3: шлюз передаёт агенту MCP SAM в новой сессии - имя и адрес из SAM_ACP_MCP_NAME и SAM_ACP_MCP_URL."""
    environ = {acp_config.COMMAND_ENV: sys.executable, acp_config.ARGS_ENV: str(FAKE_AGENT),
               acp_config.CWD_ENV: str(tmp_path), **env}
    cfg = acp_config.from_env(environ)
    expected = answer or f"{acp_config.DEFAULT_MCP_NAME} {acp_config.DEFAULT_MCP_URL}"
    assert (await _call(cfg, "MODE:mcp")).content == expected
    assert (await _call(replace(cfg, mcp_url=""), "MODE:mcp")).content == "none"
