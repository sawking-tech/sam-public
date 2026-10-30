"""Приложение из задачи песочницы (ADR-016 «Файлы», ADR-021): копия задачи шлюзом байтами под её замком, без ссылок,
спецфайлов и .git, с пакетами; files поверх; строка export в журнале задачи."""
import json
import os
import uuid

import pytest

from gateway import apps, sandbox
from gateway.tools import apps as tool
from tests.test_apps import LIMITS, FakeDocker, _get, docker_ready  # noqa: F401  (фикстура)
from tests.test_sandbox_import import _in_time

JOB = "0123456789ab"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8


@pytest.fixture
def job(tmp_path):
    """Задача песочницы с готовым приложением, пакетами и мусором, который не переносится."""
    jobs = tmp_path / "jobs"
    base = jobs / JOB
    (base / "static").mkdir(parents=True)
    (base / "app.py").write_bytes(b"print('app')\n")
    (base / "static" / "chart.png").write_bytes(PNG)
    (base / ".pkgs" / "six").mkdir(parents=True)
    (base / ".pkgs" / "six" / "__init__.py").write_bytes(b"x = 1\n")
    (base / "node_modules" / "left-pad").mkdir(parents=True)
    (base / "node_modules" / "left-pad" / "index.js").write_bytes(b"module.exports = 1\n")
    (base / ".git").mkdir()
    (base / ".git" / "config").write_bytes(b"[core]\n")
    (base / f"{sandbox.TMP_PREFIX}x.tmp").write_bytes(b"tmp")
    (tmp_path / "host-secret.txt").write_text("HOST", encoding="utf-8")
    return tmp_path / "apps", jobs


def _make(apps_root, jobs, events, docker=None, **kw):
    params = {"name": "график", "command": "python app.py", "from_job": JOB, **kw}
    return tool.app_make_impl(params, apps_root, events.append, docker or FakeDocker(), sandbox_root=jobs)


def _src(apps_root, events):
    return apps_root / events[0]["id"] / apps.SRC


def test_job_tree_moves_into_the_app_with_packages(job):
    apps_root, jobs = job
    events = []
    text = _make(apps_root, jobs, events)
    src = _src(apps_root, events)
    assert "создано" in text and events[0]["slug"] in text
    assert (src / "static" / "chart.png").read_bytes() == PNG                    # байт в байт
    assert (src / ".pkgs" / "six" / "__init__.py").exists() and (src / "node_modules" / "left-pad" / "index.js").exists()
    assert not (src / ".git").exists() and not (src / f"{sandbox.TMP_PREFIX}x.tmp").exists()
    line = json.loads((jobs / f"{JOB}.log").read_text(encoding="utf-8").splitlines()[-1])
    assert line["kind"] == "export" and line["app"] == events[0]["id"] and line["files"] == 4 and line["code"] == 0
    assert line["skipped_total"] == 1 and ".git" in line["skipped"][0]


def test_files_override_the_copy_and_from_path_narrows_it(job):
    apps_root, jobs = job
    events = []
    _make(apps_root, jobs, events, files={"chart.png": "replaced"}, from_path="static")
    src = _src(apps_root, events)
    assert (src / "chart.png").read_text(encoding="utf-8") == "replaced" and not (src / "app.py").exists()
    # пакеты лежат в корне задачи - при подпапке они всё равно в корне приложения
    assert (src / ".pkgs" / "six" / "__init__.py").exists() and (src / "node_modules" / "left-pad" / "index.js").exists()


@pytest.mark.parametrize("path", ["D:", "C:", "C:x", "D:/Boot", "../..", "missing"])
def test_from_path_with_a_drive_or_outside_is_refused(job, path):
    apps_root, jobs = job
    text = _make(apps_root, jobs, [], from_path=path)
    assert "from_path" in text and "\n" not in text


def test_executable_bit_moves_without_setuid(job):
    apps_root, jobs = job
    if os.name == "nt":
        pytest.skip("права POSIX на Windows не задаются; проверка идёт на Linux")
    tool_path = jobs / JOB / ".pkgs" / "bin" / "tool"
    tool_path.parent.mkdir(parents=True)
    tool_path.write_bytes(b"#!/bin/sh\n")
    os.chmod(tool_path, 0o4755)
    events = []
    _make(apps_root, jobs, events)
    src = _src(apps_root, events)
    assert (src / ".pkgs" / "bin" / "tool").stat().st_mode & 0o7777 == sandbox.EXEC_FILE_MODE
    assert (src / "app.py").stat().st_mode & 0o7777 == sandbox.JOB_FILE_MODE


# ─── пересборка из задачи: целиком и с откатом ─────────────────────────────────

def _update(apps_root, jobs, events, docker, **kw):
    return tool.app_update_impl({"app": events[0]["id"], "from_job": JOB, **kw}, apps_root, events.append, docker,
                                sandbox_root=jobs)


def test_update_from_a_job_keeps_files_the_job_lacks_and_the_previous_version(job):
    """15 окт задача с одними фото стёрла index.html приложения: задача ложится поверх текущего кода."""
    apps_root, jobs = job
    events = []
    _make(apps_root, jobs, events, files={"index.html": "<p>трекер</p>"})
    (jobs / JOB / "static" / "chart.png").unlink()
    (jobs / JOB / "photo.jpg").write_bytes(b"jpg")
    assert "обновлено" in _update(apps_root, jobs, events, FakeDocker())
    src = _src(apps_root, events)
    assert (src / "index.html").read_text(encoding="utf-8") == "<p>трекер</p>" and (src / "photo.jpg").exists()
    assert (src / "static" / "chart.png").exists()                                    # из задачи не удалить без clean
    prev = src.parent / apps.SRC_PREV
    assert (prev / "index.html").exists() and not (prev / "photo.jpg").exists()       # прежняя версия - рядом
    assert not (src.parent / apps.SRC_NEW).exists() and not (src.parent / apps.SRC_OLD).exists()


def test_clean_rebuild_drops_files_deleted_in_the_job(job):
    apps_root, jobs = job
    events = []
    _make(apps_root, jobs, events, files={"index.html": "<p>старое</p>"})
    (jobs / JOB / "static" / "chart.png").unlink()
    assert "обновлено" in _update(apps_root, jobs, events, FakeDocker(), clean=True)
    src = _src(apps_root, events)
    assert not (src / "static" / "chart.png").exists() and not (src / "index.html").exists() and (src / "app.py").exists()


@pytest.mark.parametrize("kw", [{"from_path": "missing"}, {"files": {"../x": "y"}}], ids=["export_fails", "files_fail"])
def test_failed_rebuild_leaves_the_running_app_alone(job, kw):
    apps_root, jobs = job
    events = []
    _make(apps_root, jobs, events)
    src = _src(apps_root, events)
    before = sorted(p.relative_to(src).as_posix() for p in src.rglob("*"))
    docker = FakeDocker()
    text = _update(apps_root, jobs, events, docker, **kw)
    assert "from_path" in text or "вне папки" in text
    assert docker.calls == []                                   # контейнеры не останавливались
    assert sorted(p.relative_to(src).as_posix() for p in src.rglob("*")) == before
    assert not (src.parent / apps.SRC_NEW).exists()


def test_rebuild_that_does_not_start_rolls_back(job):
    apps_root, jobs = job
    events = []
    _make(apps_root, jobs, events)
    (jobs / JOB / "app.py").write_bytes(b"print('v2')\n")

    class FailOnce(FakeDocker):
        failed = False

        def __call__(self, argv):
            if not self.failed and argv[1:3] == ["run", "-d"]:
                self.failed = True
                return apps.DockerResult(125, "", "docker: Error response from daemon: boom")
            return super().__call__(argv)
    text = _update(apps_root, jobs, events, FailOnce())
    assert "вернула прежнюю версию" in text and "\n" not in text
    assert (_src(apps_root, events) / "app.py").read_bytes() == b"print('app')\n"
    assert apps.load(apps_root, events[0]["id"]).wanted == apps.RUNNING


def test_update_copies_the_job_again(job):
    apps_root, jobs = job
    events = []
    _make(apps_root, jobs, events)
    (jobs / JOB / "app.py").write_bytes(b"print('v2')\n")
    text = tool.app_update_impl({"app": events[0]["id"], "from_job": JOB}, apps_root, events.append, FakeDocker(),
                                sandbox_root=jobs)
    assert "обновлено" in text and (_src(apps_root, events) / "app.py").read_bytes() == b"print('v2')\n"


@pytest.mark.parametrize("from_job, expected", [("ffffffffffff", "нет"), ("../jobs", "нет"), ("ABC", "нет")],
                         ids=["unknown", "climb", "malformed"])
def test_unknown_or_broken_job_is_refused(job, from_job, expected):
    apps_root, jobs = job
    events = []
    text = _make(apps_root, jobs, events, from_job=from_job)
    assert "Задачи песочницы" in text and expected in text and "\n" not in text
    assert events == [] and (not apps_root.exists() or list(apps_root.iterdir()) == [])   # папки приложения не осталось


def test_from_path_outside_or_missing_is_refused(job):
    apps_root, jobs = job
    for path in ("../..", "missing"):
        assert "from_path" in _make(apps_root, jobs, [], from_path=path)


def test_link_out_of_the_job_is_not_copied(job):
    apps_root, jobs = job
    try:
        os.symlink(jobs.parent / "host-secret.txt", jobs / JOB / "leak.txt")
    except OSError as e:
        pytest.skip(f"симлинк не создать ({e.__class__.__name__}): на Windows без прав разработчика; на Linux идёт")
    events = []
    _make(apps_root, jobs, events)
    assert not (_src(apps_root, events) / "leak.txt").exists()


def test_special_file_in_the_job_is_not_copied(job):
    """apps 8: FIFO, который код задачи оставил в папке, в приложение не переносится и не читается."""
    apps_root, jobs = job
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO на этой ОС не создать; на Linux идёт")
    os.mkfifo(jobs / JOB / "pipe")
    events = []
    _in_time(lambda: _make(apps_root, jobs, events))
    src = _src(apps_root, events)
    assert (src / "app.py").exists() and not (src / "pipe").exists()


def test_junction_out_of_the_job_is_not_followed(job):
    apps_root, jobs = job
    if os.name != "nt":
        pytest.skip("junction - только Windows; симлинк проверяет тест выше")
    import _winapi
    _winapi.CreateJunction(str(jobs.parent), str(jobs / JOB / "jump"))
    events = []
    _make(apps_root, jobs, events)
    assert not (_src(apps_root, events) / "jump").exists()


@pytest.mark.parametrize("env, value, expected", [
    (sandbox.EXPORT_MAX_FILES_ENV, "2", "больше 2 файлов"),
    (sandbox.EXPORT_MAX_BYTES_ENV, "100", "больше 100 байт"),
], ids=["files", "bytes"])
def test_ceilings_refuse_and_leave_no_app(job, monkeypatch, env, value, expected):
    apps_root, jobs = job
    monkeypatch.setenv(env, value)
    events = []
    assert expected in _make(apps_root, jobs, events) and events == []
    assert not apps_root.exists() or list(apps_root.iterdir()) == []      # apps 17: папки приложения не остаётся


def test_files_or_from_job_is_required(tmp_path):
    text = tool.app_make_impl({"name": "x", "command": "c"}, tmp_path, [].append, FakeDocker())
    assert "files" in text and "from_job" in text


def test_busy_job_is_not_exported(job, monkeypatch):
    apps_root, jobs = job
    monkeypatch.setenv(sandbox.JOB_WAIT_ENV, "0")
    held = sandbox._job_lock(jobs, JOB)
    held.lock.acquire()
    try:
        assert "занята" in _make(apps_root, jobs, [])
    finally:
        held.lock.release()


# ─── живое приложение из песочницы ─────────────────────────────────────────────

APP_PY = ("import http.server, six\n"
          "class H(http.server.BaseHTTPRequestHandler):\n"
          "    def do_GET(self):\n"
          "        body = ('six ' + six.__version__).encode()\n"
          "        self.send_response(200); self.end_headers(); self.wfile.write(body)\n"
          "http.server.HTTPServer(('0.0.0.0', 8000), H).serve_forever()\n")


@pytest.mark.integration
def test_live_app_from_a_sandbox_job_with_a_package(docker_ready, tmp_path, monkeypatch):  # noqa: F811
    """Песочница ставит six и пишет app.py; приложение из этой задачи стартует без сети и отвечает версией six."""
    monkeypatch.setenv(apps.PORT_BASE_ENV, "18650")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    out = sandbox.run({"packages": ["pip:six==1.16.0"], "files": {"app.py": APP_PY},
                       "command": "python -c \"import six; print(six.__version__)\""},
                      root=jobs, limits=LIMITS, new_id=lambda: uuid.uuid4().hex[:12])
    assert out.code == 0 and "1.16.0" in out.text, out.text
    m = None
    try:
        m, err = apps.create(tmp_path / "apps", "six-app", "python app.py", {}, limits=LIMITS, from_job=out.job_id,
                             sandbox_root=jobs)
        assert err is None, err
        assert _get(m.host_port, "/") == "six 1.16.0"
    finally:
        if m:
            apps.stop(tmp_path / "apps", m)
