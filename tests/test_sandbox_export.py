"""Выгрузка из задачи песочницы в папку экспорта на ПК (SAM-83, ADR-016): только в SAM_SANDBOX_EXPORT_DIR, без
перезаписи, ссылок, .git, пакетов и секретов, с потолками; модели - путь `export:<имя>`."""
import json
import os
from datetime import datetime, timezone

import pytest

from gateway import sandbox, sandbox_export
from gateway.tools.coding_agents import CliResult

LIMITS = sandbox.Limits(image=sandbox.DEFAULT_IMAGE, network="none", memory="512m", cpus="1", pids="128")
JOB = "0123456789ab"
AT = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Папка экспорта и задача с отчётом, графиком, пакетами, историей git и секретом."""
    out = tmp_path / "export"
    out.mkdir()
    jobs = tmp_path / "jobs"
    base = jobs / JOB
    (base / "report" / ".git").mkdir(parents=True)
    (base / "report" / "index.html").write_bytes(b"<p>report</p>")
    (base / "report" / "chart.png").write_bytes(PNG)
    (base / "report" / ".env").write_bytes(b"KEY=secret")
    (base / "report" / ".git" / "config").write_bytes(b"x")
    (base / "report" / "node_modules").mkdir()
    (base / "report" / "node_modules" / "a.js").write_bytes(b"x")
    monkeypatch.setenv(sandbox_export.DIR_ENV, str(out))
    for env in (sandbox_export.MAX_FILES_ENV, sandbox_export.MAX_BYTES_ENV):
        monkeypatch.delenv(env, raising=False)
    return out, jobs


def _run(jobs, params, code=0):
    return sandbox.run(params, root=jobs, limits=LIMITS, runner=lambda *a, **k: CliResult(code, "ok", ""),
                       new_id=lambda: JOB, now=lambda: AT, osv=lambda p: None)


def _journal(jobs):
    return [json.loads(s) for s in (jobs / f"{JOB}.log").read_text(encoding="utf-8").splitlines()]


def test_folder_goes_out_without_git_packages_and_secrets(setup):
    out, jobs = setup
    res = _run(jobs, {"job": JOB, "command": "python make.py", "export": [{"from": "report", "to": "weekly"}]})
    assert (out / "weekly" / "chart.png").read_bytes() == PNG and (out / "weekly" / "index.html").exists()
    for gone in (".env", ".git", "node_modules"):
        assert not (out / "weekly" / gone).exists()
    assert "Выгружено на ПК: export:weekly - 2 файлов" in res.text and str(out) not in res.text
    line = [s for s in _journal(jobs) if s["kind"] == "export_pc"][0]
    assert line["to"] == ["export:weekly"] and line["files"] == 2 and line["skipped_total"] == 3 and line["code"] == 0


def test_single_file_with_default_name(setup):
    out, jobs = setup
    _run(jobs, {"job": JOB, "export": [{"from": "report/chart.png"}]})
    assert (out / "chart.png").read_bytes() == PNG


def test_export_is_off_without_a_folder(setup, monkeypatch):
    out, jobs = setup
    monkeypatch.setenv(sandbox_export.DIR_ENV, "")
    res = _run(jobs, {"job": JOB, "export": [{"from": "report"}]})
    assert "Выгрузка на ПК выключена" in res.text and sandbox_export.DIR_ENV in res.text


@pytest.mark.parametrize("which", ["drive", "home", "sandbox", "repo"])
def test_forbidden_export_folders(setup, monkeypatch, which):
    out, jobs = setup
    folder = {"drive": out.anchor, "home": str(sandbox_export.Path.home()), "sandbox": str(sandbox.SANDBOX_DIR),
              "repo": str(sandbox._REPO)}[which]
    monkeypatch.setenv(sandbox_export.DIR_ENV, folder)
    res = _run(jobs, {"job": JOB, "export": [{"from": "report"}]})
    assert "нельзя использовать" in res.text and "\n" not in res.text


@pytest.mark.parametrize("to, expected", [("../up", "без .."), ("D:/x", "без .. и диска"), ("/abs", "без ..")],
                         ids=["dot_dot", "drive", "absolute"])
def test_place_outside_the_export_folder_is_refused(setup, to, expected):
    out, jobs = setup
    res = _run(jobs, {"job": JOB, "export": [{"from": "report", "to": to}]})
    assert expected in res.text and not (out.parent / "up").exists()


def test_taken_name_is_refused_without_overwrite(setup):
    out, jobs = setup
    (out / "weekly").write_bytes(b"mine")
    res = _run(jobs, {"job": JOB, "export": [{"from": "report", "to": "weekly"}]})
    assert "уже есть - перезаписи нет" in res.text and (out / "weekly").read_bytes() == b"mine"


@pytest.mark.parametrize("source", ["report/.env", "report/.git", "report/node_modules/a.js"],
                         ids=["secret", "git", "packages"])
def test_secret_or_package_source_is_refused(setup, source):
    out, jobs = setup
    res = _run(jobs, {"job": JOB, "export": [{"from": source, "to": "x"}]})
    assert "не выгружается" in res.text and not (out / "x").exists()


def test_link_in_the_job_does_not_leave(setup):
    out, jobs = setup
    (jobs.parent / "host.txt").write_text("HOST", encoding="utf-8")
    try:
        os.symlink(jobs.parent / "host.txt", jobs / JOB / "report" / "leak.txt")
    except OSError as e:
        pytest.skip(f"симлинк не создать ({e.__class__.__name__}): на Windows без прав разработчика; на Linux идёт")
    _run(jobs, {"job": JOB, "export": [{"from": "report", "to": "w"}]})
    assert not (out / "w" / "leak.txt").exists()


@pytest.mark.parametrize("env, value, expected", [
    (sandbox_export.MAX_FILES_ENV, "1", "больше 1 файлов"),
    (sandbox_export.MAX_BYTES_ENV, "100", "больше 100 байт"),
], ids=["files", "bytes"])
def test_ceilings(setup, monkeypatch, env, value, expected):
    out, jobs = setup
    monkeypatch.setenv(env, value)
    res = _run(jobs, {"job": JOB, "export": [{"from": "report", "to": "w"}]})
    assert expected in res.text and not (out / "w").exists()


def test_failed_command_exports_nothing(setup):
    out, jobs = setup
    res = _run(jobs, {"job": JOB, "command": "python make.py", "export": [{"from": "report", "to": "w"}]}, code=1)
    assert "не делалась: команда завершилась с кодом 1" in res.text and not (out / "w").exists()


def test_export_shape_is_checked(setup):
    out, jobs = setup
    for value in ({"from": "x"}, ["x"], [{"from": "a", "to": "same"}, {"from": "b", "to": "same"}]):
        assert "export" in _run(jobs, {"job": JOB, "export": value}).text


@pytest.mark.integration
def test_live_container_output_lands_on_the_pc(setup):
    """Контейнер пишет файл, после команды он байт в байт в папке экспорта."""
    import shutil
    import subprocess
    import uuid
    try:
        ready = bool(shutil.which("docker")) and subprocess.run(
            ["docker", "image", "inspect", sandbox.DEFAULT_IMAGE], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ready = False
    if not ready:
        pytest.skip(f"Docker не запущен или нет образа {sandbox.DEFAULT_IMAGE}")
    out, jobs = setup
    res = sandbox.run({"command": "python -c \"open('out.bin','wb').write(bytes(range(256)))\"",
                       "export": [{"from": "out.bin", "to": "live/out.bin"}]},
                      root=jobs, limits=LIMITS, new_id=lambda: uuid.uuid4().hex[:12])
    assert res.code == 0 and (out / "live" / "out.bin").read_bytes() == bytes(range(256)), res.text


@pytest.mark.parametrize("items", [
    [{"from": "report/index.html", "to": "r/x"}, {"from": "report", "to": "r"}],
    [{"from": "report/index.html", "to": "A.html"}, {"from": "report/chart.png", "to": "a.html"}],
    [{"from": "report/index.html", "to": "report."}, {"from": "report/chart.png", "to": "report"}],
], ids=["nested", "case", "trailing_dot"])
def test_targets_that_overlap_are_refused(setup, items):
    out, jobs = setup
    res = _run(jobs, {"job": JOB, "export": items})
    case_only = items[0]["to"].lower().rstrip(". ") == items[1]["to"].lower().rstrip(". ") != items[0]["to"]
    if case_only and os.name != "nt":
        assert "Выгружено" in res.text            # на Linux это два разных имени
    else:
        assert "разведите to" in res.text and list(out.iterdir()) == []


def test_names_inside_a_folder_that_collapse_on_ntfs():
    # в задаче на Linux это разные файлы; на ПК с Windows - одно имя
    clashed = sandbox_export.clash(["w/Index.html", "w/index.html"]) is not None
    assert clashed == (os.name == "nt")
    assert (sandbox_export.clash(["w/report.", "w/report"]) is not None) == (os.name == "nt")


def test_name_taken_between_check_and_write_keeps_the_old_file(setup, monkeypatch):
    out, jobs = setup
    real = sandbox.read_regular

    def taken_meanwhile(base, name, limit):
        (out / "chart.png").write_bytes(b"mine")             # другая задача или человек заняли имя
        return real(base, name, limit)
    monkeypatch.setattr(sandbox, "read_regular", taken_meanwhile)
    res = _run(jobs, {"job": JOB, "export": [{"from": "report/chart.png"}]})
    assert "export:chart.png уже есть" in res.text and (out / "chart.png").read_bytes() == b"mine"
    assert [p.name for p in out.iterdir()] == ["chart.png"]   # временного файла не осталось


def test_junction_put_after_the_check_does_not_take_the_write(setup, monkeypatch):
    out, jobs = setup
    if os.name != "nt":
        pytest.skip("junction - только Windows; на Linux путь проходится дескрипторами с O_NOFOLLOW")
    import _winapi
    elsewhere = out.parent / "elsewhere"
    elsewhere.mkdir()
    real, calls = sandbox_export.place_error, []

    def then_junction(folder, to):
        found = real(folder, to)
        calls.append(to)
        if len(calls) == 2:          # вторая проверка - перед копией; junction встаёт сразу после неё
            _winapi.CreateJunction(str(elsewhere), str(out / "w"))
        return found
    monkeypatch.setattr(sandbox_export, "place_error", then_junction)
    res = _run(jobs, {"job": JOB, "export": [{"from": "report", "to": "w"}]})
    assert "не выгружен" in res.text and list(elsewhere.iterdir()) == []


def test_source_path_through_a_link_is_refused(setup):
    out, jobs = setup
    try:
        os.symlink(jobs.parent, jobs / JOB / "l", target_is_directory=True)
    except OSError as e:
        pytest.skip(f"симлинк не создать ({e.__class__.__name__}): на Windows без прав разработчика; на Linux идёт")
    res = _run(jobs, {"job": JOB, "export": [{"from": "l/export", "to": "x"}]})
    assert "через ссылку" in res.text and "jobs" not in res.text


def test_source_path_through_a_junction_is_refused(setup):
    out, jobs = setup
    if os.name != "nt":
        pytest.skip("junction - только Windows; симлинк проверяет тест выше")
    import _winapi
    _winapi.CreateJunction(str(jobs.parent), str(jobs / JOB / "j"))
    res = _run(jobs, {"job": JOB, "export": [{"from": "j/export", "to": "x"}]})
    assert "через ссылку" in res.text


@pytest.mark.parametrize("name", ["evil.lnk", "x.url", "a.scf", "Desktop.ini", "autorun.inf", "l.library-ms",
                                  "s.searchconnector-ms"])
def test_shell_parsed_files_do_not_leave(setup, name):
    out, jobs = setup
    (jobs / JOB / "report" / name).write_bytes(b"x")
    _run(jobs, {"job": JOB, "export": [{"from": "report", "to": "w"}]})
    assert (out / "w" / "index.html").exists() and not (out / "w" / name).exists()
    res = _run(jobs, {"job": JOB, "export": [{"from": f"report/{name}", "to": "y"}]})
    assert "не выгружается" in res.text


def test_device_names_are_refused_on_windows(setup):
    out, jobs = setup
    res = _run(jobs, {"job": JOB, "export": [{"from": "report/index.html", "to": "NUL.txt"}]})
    assert ("имя устройства" in res.text) == (os.name == "nt")


def test_risky_files_are_named_and_marked(setup):
    out, jobs = setup
    (jobs / JOB / "report" / "setup.exe").write_bytes(b"MZ")
    res = _run(jobs, {"job": JOB, "export": [{"from": "report", "to": "w"}]})
    assert "исполнимые или с макросами: export:w/setup.exe" in res.text
    if os.name == "nt":
        with open(f"{out / 'w' / 'setup.exe'}:Zone.Identifier", encoding="ascii") as f:
            assert "ZoneId=3" in f.read()


def test_autorun_and_path_folders_are_not_export_folders(setup, monkeypatch):
    out, jobs = setup
    on_path = out.parent / "bin"
    on_path.mkdir()
    monkeypatch.setenv("PATH", f"{on_path}{os.pathsep}{os.environ.get('PATH', '')}")
    for folder in (on_path, on_path / "sub"):
        folder.mkdir(exist_ok=True)
        monkeypatch.setenv(sandbox_export.DIR_ENV, str(folder))
        assert "автозапуска или PATH" in _run(jobs, {"job": JOB, "export": [{"from": "report"}]}).text
    startup = out.parent / "roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(out.parent / "roaming"))
    monkeypatch.setenv(sandbox_export.DIR_ENV, str(startup))
    assert "автозапуска или PATH" in _run(jobs, {"job": JOB, "export": [{"from": "report"}]}).text


def test_tool_offers_export():
    from gateway import agent
    assert "export" in {p["name"] for p in agent.SandboxRunTool.parameters}


def test_windows_apps_folder_is_not_an_export_folder(setup, monkeypatch):
    """sandbox 31: WindowsApps (там exe-заглушки, которые запускаются по имени) - не папка выгрузки, даже когда её
    нет в PATH процесса."""
    out, jobs = setup
    local = out.parent / "local"
    apps_dir = local / "Microsoft" / "WindowsApps"
    (apps_dir / "sub").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("PATH", os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                                               if "windowsapps" not in p.lower()))
    for folder in (apps_dir, apps_dir / "sub"):
        monkeypatch.setenv(sandbox_export.DIR_ENV, str(folder))
        assert "автозапуска или PATH" in _run(jobs, {"job": JOB, "export": [{"from": "report"}]}).text
    assert list(apps_dir.rglob("*.html")) == []
