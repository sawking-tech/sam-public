"""Область записи guard (SAM-236): путь решает код, судья зовётся только когда путь не решает."""
import json

import pytest

from gateway import guard, guard_scope
from gateway.text_utils import strip_thinking
from tests.fakes_agents import _resp

CFG = {"model": "x"}


def _call(tool, **inner):
    return {"device": "homepc", "tool": tool, "args": inner}


@pytest.fixture
def roots(tmp_path):
    root = tmp_path / "worktrees"
    root.mkdir()
    return [root]


@pytest.mark.parametrize("make, expected", [
    (lambda r: _call("write_file", path=str(r[0] / "job" / "a.py"), content="x"), True),
    (lambda r: _call("create_directory", path=str(r[0] / "job" / "new")), True),
    (lambda r: _call("move_file", source=str(r[0] / "job" / "a"), destination=str(r[0] / "job" / "b")), True),
    (lambda r: {"device": "pc", "tool": "write_file", "args": json.dumps({"path": str(r[0] / "job" / "a")})}, True),
    (lambda r: _call("write_file", path=str(r[0] / ".." / "outside.py")), None),
    (lambda r: _call("write_file", path="relative/a.py"), None),
    (lambda r: _call("move_file", source=str(r[0] / "a"), destination=str(r[0].parent / "b")), None),
    (lambda r: _call("run_command", command="Copy-Item a b"), None),
    (lambda r: _call("read_file", path=str(r[0] / "job" / "a.py")), None),
    (lambda r: _call("write_file", path=str(r[0] / "job" / ".env"), content="x"), False),
    (lambda r: _call("read_text_file", path="C:\\sam\\.env"), False),
    (lambda r: _call("read_file", path="C:/sam/data/secrets/privilege.key"), False),
    (lambda r: _call("read_file", path="C:/sam/.env.example"), None),
    (lambda r: _call("read_multiple_files", paths=["C:/ok.txt", "C:/sam/.env"]), False),
], ids=["write-in", "mkdir-in", "move-in", "args-as-json-string", "dotdot", "relative", "move-out", "shell",
        "read-in", "env-in-place", "env-read", "key-read", "env-example", "multi-with-env"])
def test_verdict_by_path(roots, make, expected):
    assert guard_scope.verdict("mcp_call", make(roots), roots) is expected


def test_other_tools_are_left_to_the_judge(roots):
    assert guard_scope.verdict("remember", {"fact": "x"}, roots) is None


class _Judge:
    """Судья-ловушка: считает вызовы и отвечает заданным словом."""

    def __init__(self, word):
        self.word, self.calls = word, 0

    async def __call__(self, cfg, **kw):
        self.calls += 1
        return _resp(content=self.word)


def _scope(roots):
    return lambda tool, args: guard_scope.verdict(tool, args, roots)


async def test_privileged_judge_is_not_asked_for_a_write_inside_a_workspace(roots):
    judge = _Judge("PRIVILEGED")
    args = _call("write_file", path=str(roots[0] / "job" / "a.py"), content="x")
    ok = await guard.guard_privileged("mcp_call", args, False, CFG, llm_call_fn=judge,
                                      strip_thinking_fn=strip_thinking, scope=_scope(roots))
    assert ok is True and judge.calls == 0


async def test_secret_path_is_refused_without_a_key_even_if_the_judge_would_pass_it(roots):
    judge = _Judge("ORDINARY")
    args = _call("read_text_file", path="C:/sam/.env")
    ok = await guard.guard_privileged("mcp_call", args, False, CFG, llm_call_fn=judge,
                                      strip_thinking_fn=strip_thinking, scope=_scope(roots))
    assert ok is False and judge.calls == 0
    with_key = await guard.guard_privileged("mcp_call", args, True, CFG, llm_call_fn=judge,
                                            strip_thinking_fn=strip_thinking, scope=_scope(roots))
    assert with_key is True


async def test_tool_judge_is_skipped_for_workspace_writes_and_blocks_secrets(roots):
    judge = _Judge("UNSAFE")
    inside = _call("write_file", path=str(roots[0] / "job" / "a.py"))
    assert await guard.guard_tool_call("mcp_call", inside, CFG, llm_call_fn=judge,
                                       strip_thinking_fn=strip_thinking, scope=_scope(roots)) is True
    secret = _call("read_file", path="C:/sam/data/secrets/privilege.key")
    assert await guard.guard_tool_call("mcp_call", secret, CFG, llm_call_fn=judge,
                                       strip_thinking_fn=strip_thinking, scope=_scope(roots)) is False
    assert judge.calls == 0


async def test_without_a_scope_the_judge_decides_as_before(roots):
    judge = _Judge("UNSAFE")
    args = _call("write_file", path=str(roots[0] / "job" / "a.py"))
    assert await guard.guard_tool_call("mcp_call", args, CFG, llm_call_fn=judge,
                                       strip_thinking_fn=strip_thinking) is False
    assert judge.calls == 1


def test_refusal_reason_names_secret_and_outside_path(roots):
    secret = guard_scope.refusal_reason("mcp_call", _call("read_file", path="C:/sam/.env"), roots)
    assert "секретный" in secret and ".env" in secret
    outside = guard_scope.refusal_reason("mcp_call", _call("write_file", path="C:/sam/gateway/a.py"), roots)
    assert "workspace" in outside and "gateway/a.py" in outside
    assert guard_scope.refusal_reason("mcp_call", _call("run_command", command="dir"), roots) == ""
    assert guard_scope.refusal_reason("spawn_agent", {}, roots) == ""


def test_agent_block_text_carries_the_reason_and_a_person_gets_none(roots):
    from types import SimpleNamespace
    from gateway import agent_loop
    bus = SimpleNamespace(emit=lambda *a, **k: None)
    deps = SimpleNamespace(event_bus=bus, agent_secret_message="[GUARD] отказ агенту", need_secret_message="[GUARD] ключ",
                           refusal_reason=lambda name, args: guard_scope.refusal_reason(name, args, roots),
                           refused=guard_scope.RefusalMemory())
    args = _call("read_file", path="C:/sam/.env")
    agent = agent_loop._secret_block(deps, "mcp_call", args, "task:abc", "c1")["content"]
    assert agent.startswith("[GUARD] отказ агенту") and "Причина: секретный путь" in agent
    person = agent_loop._secret_block(deps, "mcp_call", args, "homepc", "c2")["content"]
    assert person == "[GUARD] ключ"


def test_second_identical_refusal_in_a_session_says_it_is_already_refused():
    from types import SimpleNamespace
    from gateway import agent_loop
    deps = SimpleNamespace(event_bus=SimpleNamespace(emit=lambda *a, **k: None), agent_secret_message="[GUARD] отказ",
                           need_secret_message="[GUARD] ключ", refusal_reason=lambda n, a: "",
                           refused=guard_scope.RefusalMemory())
    args = _call("run_command", command="Copy-Item a b")
    first = agent_loop._secret_block(deps, "mcp_call", args, "task:abc", "c1")["content"]
    again = agent_loop._secret_block(deps, "mcp_call", args, "task:abc", "c2")["content"]
    other = agent_loop._secret_block(deps, "mcp_call", _call("run_command", command="dir"), "task:abc", "c3")["content"]
    elsewhere = agent_loop._secret_block(deps, "mcp_call", args, "task:xyz", "c4")["content"]
    assert first == other == elsewhere == "[GUARD] отказ"
    assert again == guard_scope.REPEAT_REFUSED


def test_refusal_memory_is_bounded():
    memory = guard_scope.RefusalMemory(limit=2)
    for n in range(3):
        memory.remember("s", "t", {"n": n})
    assert not memory.repeated("s", "t", {"n": 0}) and memory.repeated("s", "t", {"n": 2})


async def test_judges_see_the_workspace_folder_in_their_prompt():
    seen = []

    async def judge(cfg, **kw):
        seen.append(kw["messages"][0]["content"])
        return _resp("ORDINARY")

    args = _call("run_command", command="Copy-Item a b")
    await guard.guard_privileged("mcp_call", args, False, CFG, llm_call_fn=judge,
                                 strip_thinking_fn=strip_thinking, workspaces="C:/sam/data/worktrees")
    await guard.guard_tool_call("mcp_call", args, CFG, llm_call_fn=judge,
                                strip_thinking_fn=strip_thinking, workspaces="C:/sam/data/worktrees")
    assert len(seen) == 2 and all("C:/sam/data/worktrees" in text for text in seen)
