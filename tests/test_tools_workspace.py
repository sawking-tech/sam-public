"""workspace (SAM-234): рабочее место задачи - git worktree от dev на настоящих временных репозиториях."""
import shutil
import subprocess
from pathlib import Path

import pytest

from gateway.tools import workspace as ws

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="нужен git")


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "dev")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def call(repo, tmp_path):
    places = tmp_path / "places"
    return lambda **params: ws.workspace_impl(params, repo=repo, root=places)


def test_open_makes_a_worktree_from_dev_and_leaves_the_main_tree_alone(repo, call, tmp_path):
    before = (_git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--short"), _git(repo, "branch", "--show-current"))
    out = call(action="open", name="Monitor Badge")
    place = tmp_path / "places" / "monitor-badge"
    assert place.is_dir() and "agent/monitor-badge" in out and "от dev" in out
    assert _git(place, "branch", "--show-current") == "agent/monitor-badge"
    assert (_git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--short"), _git(repo, "branch", "--show-current")) == before


def test_open_twice_is_idempotent_and_reopens_a_kept_branch(call, tmp_path):
    call(action="open", name="x1")
    assert "уже открыто" in call(action="open", name="x1")
    call(action="close", name="x1")                                  # ветка слита или пуста - удалилась или осталась
    assert "открыто" in call(action="open", name="x1")


def test_commit_goes_to_the_agent_branch_by_explicit_paths_and_never_to_dev(repo, call, tmp_path):
    call(action="open", name="job")
    place = tmp_path / "places" / "job"
    (place / "a.txt").write_text("two\n", encoding="utf-8")
    (place / "stray.txt").write_text("not committed\n", encoding="utf-8")
    out = call(action="commit", name="job", paths=["a.txt"],
               message="fix: a.txt says two\n\nCo-Authored-By: Someone <x@example.invalid>")
    assert out.startswith("Коммит") and "agent/job" in out
    assert _git(repo, "log", "dev", "--format=%s", "-1") == "init"                 # dev не сдвинулся
    assert _git(repo, "log", "agent/job", "--format=%s", "-1") == "fix: a.txt says two"
    assert "co-authored" not in _git(repo, "log", "agent/job", "--format=%B", "-1").lower()
    assert "stray.txt" in call(action="status", name="job")                        # явные пути: лишнее не в коммите


@pytest.mark.parametrize("paths", [["../outside.txt"], ["/etc/passwd"], ["-A"], [], "a.txt", ["sub/../../x"]],
                         ids=["parent", "absolute", "git-flag", "empty", "not-a-list", "sneaky"])
def test_commit_refuses_paths_outside_the_workspace(call, paths):
    call(action="open", name="job")
    assert "внутри рабочего места" in call(action="commit", name="job", paths=paths, message="m")


def test_commit_needs_a_message_and_something_to_commit(call):
    call(action="open", name="job")
    assert "сообщение" in call(action="commit", name="job", paths=["a.txt"], message="  Co-Authored-By: x\n")
    assert "Коммит не сделан" in call(action="commit", name="job", paths=["a.txt"], message="nothing changed")


def test_diff_stat_against_dev_and_close_keeps_an_unmerged_branch(repo, call, tmp_path):
    call(action="open", name="job")
    place = tmp_path / "places" / "job"
    assert "Расхождений с dev нет" in call(action="diff_stat", name="job")
    (place / "a.txt").write_text("two\n", encoding="utf-8")
    call(action="commit", name="job", paths=["a.txt"], message="change")
    assert "a.txt" in call(action="diff_stat", name="job")
    out = call(action="close", name="job")
    assert not place.exists() and "осталась" in out and _git(repo, "branch", "--list", "agent/job")   # -d не удаляет неслитое


def test_close_deletes_a_merged_branch(repo, call, tmp_path):
    call(action="open", name="job")
    out = call(action="close", name="job")                            # веток без новых коммитов слита в dev
    assert "удалена" in out and not _git(repo, "branch", "--list", "agent/job")


@pytest.mark.parametrize("params, message", [
    ({"action": "burn"}, "action - один из"),
    ({"action": "open", "name": "задача"}, "латиница"),
    ({"action": "open", "name": ""}, "латиница"),
    ({"action": "status", "name": "ghost"}, "нет: открой"),
], ids=["bad-action", "cyrillic-name", "empty-name", "missing-workspace"])
def test_one_line_errors_say_what_to_do(call, params, message):
    assert message in call(**params)


def test_names_are_normalized_to_a_safe_slug():
    assert ws.slugify("  Monitor/Badge: v2! ") == "monitor-badge-v2"
    assert len(ws.slugify("a" * 100)) == ws.SLUG_MAX


def test_git_is_called_without_a_shell_with_a_list_of_arguments(tmp_path):
    seen = []
    ws.workspace_impl({"action": "open", "name": "job"}, run=lambda args, cwd: (seen.append((args, cwd)) or (1, "x")),
                      repo=tmp_path, root=tmp_path / "w")
    assert all(isinstance(args, list) for args, _ in seen) and seen[-1][0][:2] == ["worktree", "add"]


def test_workspace_is_a_tool_of_the_main_model_and_of_workers_with_a_schema():
    from gateway import agent
    for names in (agent.MAIN_TOOL_NAMES, agent.WORKER_TOOL_NAMES):
        schema = {t["function"]["name"]: t["function"] for t in agent._build_tools_schema(names)}
        assert "workspace" in schema and schema["workspace"]["parameters"]["required"] == ["action", "name"]
    assert "workspace" in agent.TOOL_IMPL


async def test_workspace_runs_through_the_tool_map_on_a_real_repo(repo, tmp_path, monkeypatch):
    from gateway import agent
    monkeypatch.setenv(ws.ROOT_ENV, str(tmp_path / "places"))
    monkeypatch.setattr(ws, "REPO", repo)
    monkeypatch.setattr(ws, "run_git", lambda args, cwd: ws.subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8").returncode and (1, "x") or (0, ""))
    out = await agent.TOOL_IMPL["workspace"]({"action": "open", "name": ""})
    assert "латиница" in out                          # путь от модели до функции работает, ошибка одной строкой
