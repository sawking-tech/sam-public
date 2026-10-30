"""SAM-88: привилегированный вызов выполняется только в ходе, где назван ключ; mcp_call судит основная модель."""
import asyncio

import pytest

from gateway import guard
from gateway.command_auth import secret_in_turn, secret_scope
from tests.fakes_core import FakeLLMClient, resp

MAIN = {"model": "glm-main", "source": "zai"}
NO_CALL = []   # судья не спрашивался


@pytest.mark.parametrize("tool, args, secret, verdicts, allowed, judged", [
    ("set_env", {"name": "X"}, False, [], False, 0),
    ("set_env", {"name": "X"}, True, [], True, 0),
    ("search_web", {"q": "погода"}, False, [], True, 0),
    ("mcp_call", {"tool": "run_command", "args": {"cmd": "del /q C:\\data"}}, False, [resp(content="PRIVILEGED")], False, 1),
    ("mcp_call", {"tool": "screenshot"}, False, [resp(content="ORDINARY")], True, 1),
    ("mcp_call", {"tool": "run_command"}, False, [RuntimeError("llm down")], False, 1),
], ids=["privileged_without_key_no_model", "privileged_with_key", "ordinary_needs_no_key",
        "mcp_call_judged_privileged", "mcp_call_judged_ordinary", "judge_failure_is_privileged"])
async def test_privileged_call_needs_the_key_or_the_judge(tool, args, secret, verdicts, allowed, judged):
    judge = FakeLLMClient(verdicts)
    ok = await guard.guard_privileged(tool, args, secret, MAIN, llm_call_fn=judge.make_llm_call,
                                      strip_thinking_fn=lambda t: t)
    assert ok is allowed
    assert len(judge.calls) == judged


@pytest.mark.parametrize("scope, purpose, inside", [
    (True, None, True),               # прежний признак одного ключа - ключ команд
    (["command", "other"], None, True),
    (["other"], None, False),
    (["other"], "other", True),
    (None, None, False),
], ids=["legacy_flag", "command_and_other", "other_does_not_open_privileged", "other_purpose", "none"])
def test_secret_scope_carries_purposes_and_resets(scope, purpose, inside):
    assert not secret_in_turn()
    with secret_scope(scope):
        assert (secret_in_turn(purpose) if purpose else secret_in_turn()) is inside
    assert not secret_in_turn()


@pytest.mark.parametrize("who, secret, allowed", [
    ("owner", None, False),                 # голос человека опознан, ключа нет
    ("owner", ["other"], False),            # ключ другого назначения
    ("owner", ["command"], True),
    (None, ["command"], True),             # ключ открывает и без опознания
], ids=["owner_voice_no_key", "owner_voice_other_key", "owner_voice_with_key", "unknown_voice_with_key"])
async def test_recognised_voice_does_not_open_privileged_calls(monkeypatch, who, secret, allowed):
    """voice-id 9: опознание голоса - не аутентификация. Фраза моста с `who` человека идёт через сокет шлюза в
    ход; привилегированный вызов в этом ходе проверяет настоящая проверка шлюза и без ключа отказывает."""
    from types import SimpleNamespace

    from gateway import agent, voice_socket
    from tests.fakes_voice import FakeVoiceSocket
    monkeypatch.setattr(voice_socket, "_listeners", set())
    monkeypatch.setattr(voice_socket, "_voice_sessions", set())

    async def best_llm(*a, **k):
        return MAIN
    monkeypatch.setattr(agent, "get_best_llm", best_llm)
    seen = []

    async def run_turn(text, session, **kw):
        seen.append((kw.get("who"), await agent._privilege_check("set_env", {"name": "SAM_X", "value": "1"})))
        return SimpleNamespace(voice="ок", tools=[])

    async def acquire_and_run(session, text, handler):
        return await handler(text)
    ws = FakeVoiceSocket()
    serving = asyncio.ensure_future(voice_socket.serve_voice_socket(ws, run_turn, acquire_and_run, lambda s: False))
    ws.send({"type": "turn", "session": "voice", "text": "поменяй настройку", **({"who": who} if who else {}),
             **({"secret": secret} if secret else {})})
    assert (await ws.recv())["type"] == "done"
    ws.close()
    with pytest.raises(ConnectionError):          # мост ушёл - сокет закрыт
        await asyncio.wait_for(serving, 5)
    assert seen == [(who, allowed)]


@pytest.mark.parametrize("workdir, needs_key", [
    ("SAM", True),                    # корень SAM - ядро
    ("SAM/gateway", True),
    ("SAM/voice/layer", True),
    ("SAM/data/secrets", True),       # ключ привилегий
    ("SAM/.claude/hooks", True),      # хуки ведущей сессии
    ("SAM/scripts", True),
    ("SAM/data/apps/calc/../../../gateway", True),
    ("", True),                       # по умолчанию - папка репозиториев, SAM внутри
    ("SAM/data/apps/calc", False),    # приложение - периферия
    ("router", False),                # другой репозиторий
], ids=["sam_root", "gateway", "voice_inside", "secrets", "hooks", "scripts", "dotdot_into_core", "default_root",
        "app_folder", "other_repo"])
async def test_coding_agent_needs_the_key_only_for_the_core(tmp_path, monkeypatch, workdir, needs_key):
    """SAM-194: агент кода без ключа правит периферию (приложение, другой репозиторий), а репозиторий SAM вне
    папок периферии - только с ключом."""
    from gateway import agent, core_paths
    from gateway.tools import coding_agents
    repos = tmp_path / "repos"
    for sub in ("SAM/gateway", "SAM/voice/layer", "SAM/data/secrets", "SAM/.claude/hooks", "SAM/scripts",
                "SAM/data/apps/calc", "router"):
        (repos / sub).mkdir(parents=True)
    monkeypatch.setenv(coding_agents.ROOTS_ENV, str(repos))
    monkeypatch.delenv(core_paths.PERIPHERY_DIRS_ENV, raising=False)
    monkeypatch.setattr(core_paths, "REPO", repos / "SAM")
    real_periphery = core_paths.periphery_dirs
    monkeypatch.setattr(core_paths, "periphery_dirs", lambda: real_periphery(repos / "SAM", {}))
    judge = FakeLLMClient([])

    async def best_llm():
        return MAIN

    monkeypatch.setattr(agent, "get_best_llm", best_llm)
    monkeypatch.setattr(agent.llm_client, "make_llm_call", judge.make_llm_call)
    ok = await agent._privilege_check("claude_delegate", {"task": "поправь", "workdir": workdir})
    assert ok is (not needs_key) and judge.calls == []


def test_periphery_comes_from_the_environment_and_unknown_folder_is_core(tmp_path):
    from gateway import core_paths
    repo = tmp_path / "SAM"
    assert core_paths.periphery_dirs(repo, {core_paths.PERIPHERY_DIRS_ENV: "data/apps, radar"}) == [
        (repo / "data/apps").resolve(), (repo / "radar").resolve()]
    assert core_paths.touches_core(None, repo, []) is True


async def test_without_a_core_check_the_coding_agent_always_needs_the_key():
    judge = FakeLLMClient([])
    ok = await guard.guard_privileged("claude_delegate", {"workdir": "router"}, False, MAIN,
                                      llm_call_fn=judge.make_llm_call, strip_thinking_fn=lambda t: t)
    assert ok is False
