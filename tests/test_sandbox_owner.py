"""Сессия задачи песочницы (пункт плана sandbox-job-owner): задача принадлежит сессии разговора, где её начали;
sandbox_run с job и from_job приложения из другой сессии - отказ одной строкой; показ в мониторе - как раньше."""
import contextvars
import json
from datetime import datetime, timezone

import pytest

from gateway import sandbox
from gateway.tools import apps as tool
from gateway.tools.coding_agents import CliResult
from tests.test_apps import FakeDocker

LIMITS = sandbox.Limits(image=sandbox.DEFAULT_IMAGE, network="none", memory="512m", cpus="1", pids="128")
JOB = "0123456789ab"
AT = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ALICE, BOB = sandbox.Owner("voice"), sandbox.Owner("workpc")


class Runner:
    def __init__(self):
        self.calls = 0

    def __call__(self, argv, cwd, timeout, env=None):
        self.calls += 1
        return CliResult(0, "ok", "")


def _run(root, params, owner, runner=None):
    return sandbox.run(params, root=root, limits=LIMITS, runner=runner or Runner(), new_id=lambda: JOB,
                       now=lambda: AT, osv=lambda p: None, owner=owner)


def _journal(root):
    return [json.loads(s) for s in (root / f"{JOB}.log").read_text(encoding="utf-8").splitlines()]


def test_own_session_keeps_working_in_its_job(tmp_path):
    first = _run(tmp_path, {"files": {"a.py": "1"}, "command": "ls"}, ALICE)
    again = _run(tmp_path, {"job": JOB, "command": "ls"}, ALICE)
    assert first.code == 0 and again.code == 0
    owner = json.loads(sandbox.owner_path(JOB, tmp_path).read_text(encoding="utf-8"))
    assert owner == {"session": "voice", "by": ""}
    assert [s["kind"] for s in _journal(tmp_path)].count("owner") == 1


def test_other_session_is_refused_in_sandbox_run(tmp_path):
    _run(tmp_path, {"files": {"a.py": "1"}}, ALICE)
    runner = Runner()
    out = _run(tmp_path, {"job": JOB, "files": {"b.py": "2"}, "command": "cat a.py"}, BOB, runner)
    assert out.text == f"Задача {JOB} другой сессии - начните новую задачу без job" and out.code is None
    assert runner.calls == 0 and not (tmp_path / JOB / "b.py").exists()
    assert "другой сессии" in _journal(tmp_path)[-1]["error"]


def test_job_without_owner_goes_to_the_first_session(tmp_path):
    (tmp_path / JOB).mkdir()                                      # задача до записи сессий
    (tmp_path / JOB / "old.py").write_text("1", encoding="utf-8")
    assert _run(tmp_path, {"job": JOB, "command": "ls"}, BOB).code == 0
    assert _journal(tmp_path)[0] == {"time": "2026-10-14T12:00:00+00:00", "kind": "owner", "session": "workpc",
                                     "by": ""}
    assert "другой сессии" in _run(tmp_path, {"job": JOB, "command": "ls"}, ALICE).text


def test_owner_file_lies_outside_the_mounted_folder(tmp_path):
    _run(tmp_path, {"files": {"a.py": "1"}}, ALICE)
    owner = sandbox.owner_path(JOB, tmp_path).resolve()
    assert not owner.is_relative_to((tmp_path / JOB).resolve())
    assert sandbox.list_files(tmp_path / JOB) == ["a.py"]


def test_internal_call_without_owner_is_not_checked(tmp_path):
    _run(tmp_path, {"files": {"a.py": "1"}}, ALICE)
    assert _run(tmp_path, {"job": JOB, "command": "ls"}, None).code == 0


def test_monitor_preview_shows_any_job(tmp_path):
    _run(tmp_path, {"files": {"index.html": "<p>ok</p>"}}, ALICE)
    assert sandbox.served_file(tmp_path, JOB, "index.html") == b"<p>ok</p>"


@pytest.mark.parametrize("who, ok", [(ALICE, True), (BOB, False)], ids=["own", "other"])
def test_from_job_of_another_session_is_refused(tmp_path, who, ok):
    jobs, apps_root = tmp_path / "jobs", tmp_path / "apps"
    _run(jobs, {"files": {"app.py": "print(1)"}}, ALICE)
    events = []
    text = tool.app_make_impl({"name": "x", "command": "python app.py", "from_job": JOB}, apps_root, events.append,
                              FakeDocker(), sandbox_root=jobs, owner=who)
    if ok:
        assert "создано" in text
    else:
        assert text == f"Задача {JOB} другой сессии - начните новую задачу без job" and events == []
        assert "другой сессии" in _journal(jobs)[-1]["error"]


def test_rebuild_from_another_sessions_job_is_refused(tmp_path):
    jobs, apps_root = tmp_path / "jobs", tmp_path / "apps"
    _run(jobs, {"files": {"app.py": "print(1)"}}, ALICE)
    events = []
    tool.app_make_impl({"name": "x", "command": "c", "files": {"a": "b"}}, apps_root, events.append, FakeDocker())
    text = tool.app_update_impl({"app": "x", "from_job": JOB}, apps_root, events.append, FakeDocker(),
                                sandbox_root=jobs, owner=BOB)
    assert "другой сессии" in text


def test_worker_owns_its_job_in_the_name_of_its_creator(monkeypatch):
    """Воркер работает от сессии создателя: основной агент той же сессии переносит его задачу в приложение."""
    from gateway import agent
    from gateway.worker import worker_session_var
    seen = []
    monkeypatch.setattr(agent._sandbox, "run", lambda params, owner=None: (seen.append(owner),
                        sandbox.RunOutcome(JOB, "ok"))[1])
    monkeypatch.setitem(agent._task_context_map, "task:w1", ("voice", "w1"))

    def as_worker():
        worker_session_var.set("task:w1")
        agent._impl_sandbox_run({"command": "ls"})
    contextvars.copy_context().run(as_worker)
    assert seen == [sandbox.Owner("voice", "w1")]
