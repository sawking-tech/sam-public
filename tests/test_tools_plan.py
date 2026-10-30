"""plan (SAM-235): действия плана одним вызовом, раннер подменён."""
import subprocess

from gateway.tools import plan


def _tool(tmp_path):
    script = tmp_path / "plan.mjs"
    script.write_text("", encoding="utf-8")
    return {plan.TOOL_ENV: str(script), plan.PLAN_FILE_ENV: str(tmp_path / "plan.kdl")}


class Spy:
    def __init__(self, code=0, out="ok"):
        self.code, self.out, self.calls = code, out, []

    def __call__(self, argv, env):
        self.calls.append((argv, env))
        return self.code, self.out


def test_where_passes_count_and_plan_file(tmp_path):
    env, spy = _tool(tmp_path), Spy(out="СЕЙЧАС")
    assert plan.plan_impl({"action": "where", "count": 3}, spy, environ=env) == "СЕЙЧАС"
    argv, passed = spy.calls[0]
    assert argv[0] == "node" and argv[2:] == ["where", "3"]
    assert passed["PLAN_FILE"] == env[plan.PLAN_FILE_ENV]


def test_writes_are_closed_without_an_own_plan_file(tmp_path):
    spy = Spy()
    env = {plan.TOOL_ENV: _tool(tmp_path)[plan.TOOL_ENV]}      # SAM_PLAN_FILE не задан
    for action in ("take", "done"):
        assert plan.plan_impl({"action": action, "slug": "s"}, spy, environ=env) == plan.PLAN_NOT_SET
    assert plan.plan_impl({"action": "add", "slug": "n", "title": "T", "anchor": "a"}, spy, environ=env) == plan.PLAN_NOT_SET
    assert spy.calls == []
    plan.plan_impl({"action": "where"}, spy, environ=env)      # чтение открыто
    assert len(spy.calls) == 1


def test_a_task_node_opens_writes_and_limits_every_action_to_it(tmp_path):
    spy = Spy()
    env = {plan.TOOL_ENV: _tool(tmp_path)[plan.TOOL_ENV], plan.WITHIN_ENV: "task-node"}      # файла нет, узел задан
    for params in ({"action": "where"}, {"action": "take", "slug": "b1"}, {"action": "done", "slug": "b1"},
                   {"action": "add", "slug": "n", "title": "T", "anchor": "b1"}):
        assert plan.plan_impl(params, spy, environ=env) == "ok"
    assert all(argv[-2:] == ["--within", "task-node"] for argv, _ in spy.calls) and len(spy.calls) == 4


def test_take_and_done_need_slug(tmp_path):
    spy = Spy()
    for action in ("take", "done"):
        assert "slug" in plan.plan_impl({"action": action}, spy, environ=_tool(tmp_path))
    assert spy.calls == []
    plan.plan_impl({"action": "take", "slug": "s1"}, spy, environ=_tool(tmp_path))
    assert spy.calls[0][0][2:] == ["take", "s1"]


def test_add_builds_position_and_requires_anchor(tmp_path):
    env, spy = _tool(tmp_path), Spy()
    plan.plan_impl({"action": "add", "slug": "n", "title": "T", "anchor": "a", "position": "after"}, spy, environ=env)
    assert spy.calls[0][0][2:] == ["add", "n", "T", "--after", "a"]
    assert "anchor" in plan.plan_impl({"action": "add", "slug": "n", "title": "T"}, spy, environ=env)


def test_unknown_action_lists_valid_ones(tmp_path):
    out = plan.plan_impl({"action": "drop"}, Spy(), environ=_tool(tmp_path))
    assert "where" in out and "drop" in out


def test_check_exit_one_is_reported_with_output(tmp_path):
    out = plan.plan_impl({"action": "check"}, Spy(code=1, out="Нарушений: 1"), environ=_tool(tmp_path))
    assert out.startswith("План вернул код 1") and "Нарушений: 1" in out


def test_missing_node_and_script(tmp_path):
    def no_node(argv, env):
        raise FileNotFoundError
    assert "Node.js" in plan.plan_impl({"action": "where"}, no_node, environ=_tool(tmp_path))
    missing = {plan.TOOL_ENV: str(tmp_path / "none.mjs")}
    assert plan.TOOL_ENV in plan.plan_impl({"action": "where"}, Spy(), environ=missing)


def test_timeout_is_one_line(tmp_path):
    def slow(argv, env):
        raise subprocess.TimeoutExpired(argv, 1)
    assert "не ответил" in plan.plan_impl({"action": "where"}, slow, environ=_tool(tmp_path))
