"""Пакеты песочницы (SAM-83, ADR-016): точная версия из PyPI или npm, OSV до установки, установка отдельным
запуском с сетью песочницы. Логика - без сети и Docker; живая установка - под меткой integration."""
import json
import os
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from types import SimpleNamespace

import httpx
import pytest

from gateway import sandbox, sandbox_net, sandbox_pkgs
from gateway.sandbox_pkgs import NPM, PIP, Package
from gateway.tools.coding_agents import CliResult

LIMITS = sandbox.Limits(image=sandbox.DEFAULT_IMAGE, network="none", memory="512m", cpus="1", pids="128")
JOB = "0123456789ab"
AT = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
SIX, LEFT_PAD = Package(PIP, "six", "1.16.0"), Package(NPM, "left-pad", "1.3.0")
DOCKER_CHECK_TIMEOUT_S = 10


def _live_ready():
    """Живой Docker с образами песочницы и стража; без них живой тест пропускается с причиной."""
    images = (sandbox.DEFAULT_IMAGE, sandbox_net.DEFAULT_GUARD_IMAGE)
    try:
        ready = bool(shutil.which("docker")) and all(subprocess.run(
            ["docker", "image", "inspect", i], capture_output=True,
            timeout=DOCKER_CHECK_TIMEOUT_S).returncode == 0 for i in images)
    except (OSError, subprocess.TimeoutExpired):
        ready = False
    if not ready:
        pytest.skip(f"Docker не запущен или нет образов {' и '.join(images)}")


# ─── разбор ───────────────────────────────────────────────────────────────────

def test_exact_versions_parse():
    packages, error = sandbox_pkgs.parse(["pip:six==1.16.0", "npm:left-pad@1.3.0", "npm:@types/node@22.7.4",
                                          "pip:Django==5.1rc1"])
    assert error is None
    assert [str(p) for p in packages] == ["pip:six==1.16.0", "npm:left-pad@1.3.0", "npm:@types/node@22.7.4",
                                          "pip:Django==5.1rc1"]


@pytest.mark.parametrize("item, reason", [
    ("pip:six", "нужна точная версия"),
    ("npm:left-pad", "нужна точная версия"),
    ("npm:@types/node", "нужна точная версия"),
    ("pip:six>=1.16", "диапазон версий нельзя"),
    ("pip:six~=1.16", "диапазон версий нельзя"),
    ("pip:six==1.*", "точная версия"),
    ("npm:left-pad@^1.3.0", "точная версия"),
    ("npm:left-pad@latest", "точная версия"),
    ("npm:left-pad@1.x", "точная версия"),
    ("pip:https://evil.example/six.whl", "URL, git и пути нельзя"),
    ("pip:git+https://github.com/a/b", "URL, git и пути нельзя"),
    ("npm:github:a/b", "URL, git и пути нельзя"),
    ("npm:a/b@1.0.0", "URL, git и пути нельзя"),
    ("pip:./local", "URL, git и пути нельзя"),
    ("pip:/abs/path", "URL, git и пути нельзя"),
    ("npm:file:../x", "URL, git и пути нельзя"),
    ("pip:-e .", "флаги установки"),
    ("pip:--index-url=x", "флаги установки"),
    ("pip:six[extra]==1.0", "без extras"),
    ("six==1.16.0", "префикс pip: или npm:"),
    ("gem:rails@7.0.0", "префикс pip: или npm:"),
], ids=lambda v: v if isinstance(v, str) and ":" in v else None)
def test_anything_but_an_exact_registry_version_is_refused(item, reason):
    packages, error = sandbox_pkgs.parse([item])
    assert packages == [] and reason in error and "\n" not in error


@pytest.mark.parametrize("items", ["pip:six==1.16.0", [1], [f"pip:p{i}==1.0" for i in range(21)]],
                         ids=["not_a_list", "not_strings", "too_many"])
def test_package_list_shape_is_checked(items):
    assert sandbox_pkgs.parse(items)[1]


# ─── OSV ──────────────────────────────────────────────────────────────────────

class Client:
    def __init__(self, status=200, body=None, raises=None):
        self.status, self.body, self.raises, self.sent = status, body, raises, []

    def post(self, url, json=None, timeout=None):
        self.sent.append((url, json, timeout))
        if self.raises:
            raise self.raises
        return httpx.Response(self.status, json=self.body, request=httpx.Request("POST", url))


def test_osv_query_names_ecosystem_and_version():
    client = Client(body={"results": [{}, {}]})
    assert sandbox_pkgs.osv_check([SIX, LEFT_PAD], client) is None
    url, body, timeout = client.sent[0]
    assert url == sandbox_pkgs.DEFAULT_OSV_URL and timeout == sandbox_pkgs.DEFAULT_OSV_TIMEOUT_S
    assert body["queries"] == [{"package": {"name": "six", "ecosystem": "PyPI"}, "version": "1.16.0"},
                               {"package": {"name": "left-pad", "ecosystem": "npm"}, "version": "1.3.0"}]


@pytest.mark.parametrize("client, expected", [
    (Client(body={"results": [{"vulns": [{"id": "GHSA-1"}, {"id": "PYSEC-2"}]}, {}]}), "pip:six==1.16.0 - GHSA-1, PYSEC-2"),
    (Client(body={"results": [{}]}), "не на все пакеты"),
    (Client(status=503, body={}), "OSV ответил 503"),
    (Client(raises=httpx.ConnectError("down")), "OSV недоступен (ConnectError)"),
    (Client(raises=httpx.ReadTimeout("slow")), "OSV не ответил за 10 с"),
], ids=["vulns", "short_answer", "server_error", "unavailable", "timeout"])
def test_osv_refuses_on_vulns_or_no_answer(client, expected, monkeypatch):
    monkeypatch.delenv(sandbox_pkgs.OSV_TIMEOUT_ENV, raising=False)
    error = sandbox_pkgs.osv_check([SIX, LEFT_PAD], client)
    assert expected in error and "\n" not in error


def test_osv_is_not_asked_about_nothing():
    client = Client(raises=AssertionError("не должен звать"))
    assert sandbox_pkgs.osv_check([], client) is None


# ─── lock-файлы, команда, поставленное ────────────────────────────────────────

def test_lock_files_pin_versions_and_keep_earlier_ones():
    existing = {"requirements.lock": "Six==1.15.0\nnumpy==2.5.4\n",
                "package.json": json.dumps({"name": "mine", "dependencies": {"a": "1.0.0"}, "scripts": {"x": "y"}})}
    plan = sandbox_pkgs.plan([SIX, LEFT_PAD], existing.get)
    files = sandbox_pkgs.lock_files(plan)
    assert files["requirements.lock"] == "six==1.16.0\nnumpy==2.5.4\n"      # та же библиотека - новой версией
    assert json.loads(files["package.json"]) == {"name": "sam-sandbox-job", "private": True,
                                                 "dependencies": {"a": "1.0.0", "left-pad": "1.3.0"}}
    assert plan.dropped == ()
    fresh = sandbox_pkgs.lock_files(sandbox_pkgs.plan([LEFT_PAD], lambda name: None))
    assert list(fresh) == ["package.json"]


@pytest.mark.parametrize("lock, manifest, dropped", [
    ("--index-url http://evil/simple #==1\nsix==1.16.0\n", None, "--index-url"),
    ("--no-binary :all: #==1\n", None, "--no-binary"),
    ("pkg @ https://x.whl #==1\n", None, "pkg @ https"),
    ("-r other.txt\n", None, "-r other"),
    ("evil\n==1.0.0\n", None, "evil"),
    (None, {"dependencies": {"a": "git+https://evil/a.git"}}, "git+https"),
    (None, {"dependencies": {"b": "https://evil/b.tgz"}}, "https://evil"),
    (None, {"dependencies": {"c": "file:../c"}}, "file:"),
    (None, {"dependencies": {"d": "^1.0.0"}}, "^1.0.0"),
], ids=["index_url", "no_binary", "direct_url", "include", "split_line", "npm_git", "npm_url", "npm_file", "npm_range"])
def test_earlier_pins_that_leave_the_registry_are_dropped(lock, manifest, dropped):
    existing = {"requirements.lock": lock, "package.json": json.dumps(manifest) if manifest else None}
    plan = sandbox_pkgs.plan([SIX, LEFT_PAD], existing.get)
    assert any(dropped in d for d in plan.dropped)
    files = sandbox_pkgs.lock_files(plan)
    assert "evil" not in "".join(files.values()) and "--" not in files["requirements.lock"]
    assert all(sandbox_pkgs.parse_one(f"pip:{s}")[1] is None for s in files["requirements.lock"].split())


def test_npm_manifest_keeps_only_pins():
    existing = {"package.json": json.dumps({"name": "x", "overrides": {"left-pad": "git+https://evil"},
                                            "scripts": {"preinstall": "curl evil"}, "dependencies": {}})}
    files = sandbox_pkgs.lock_files(sandbox_pkgs.plan([LEFT_PAD], existing.get))
    assert set(json.loads(files["package.json"])) == {"name", "private", "dependencies"}


def test_install_command_uses_wheels_official_registries_and_no_scripts():
    cmd = sandbox_pkgs.install_command(sandbox_pkgs.plan([SIX, LEFT_PAD], lambda name: None))
    for flag in ("python -I -m pip install --isolated --only-binary=:all: --no-cache-dir", "--target /work/.pkgs",
                 "--index-url https://pypi.org/simple six==1.16.0",
                 "python -I -m pip list --isolated --disable-pip-version-check --path /work/.pkgs --format json",
                 "npm install --ignore-scripts --no-audit --no-fund --registry https://registry.npmjs.org",
                 f"cd {sandbox_pkgs.NPM_BUILD_DIR}", sandbox_pkgs.PIP_LIST_MARK, sandbox_pkgs.NPM_LIST_MARK):
        assert flag in cmd
    # ничего из папки задачи: ни -r requirements.lock, ни npm в /work с его .npmrc и package-lock.json
    assert "-r requirements" not in cmd and "cd /work" not in cmd and "/work/.npmrc" not in cmd
    assert "npm" not in sandbox_pkgs.install_command(sandbox_pkgs.plan([SIX], lambda name: None))


def test_installed_packages_come_from_the_install_output():
    out = ("Successfully installed six\n" + sandbox_pkgs.PIP_LIST_MARK + "\n"
           + json.dumps([{"name": "six", "version": "1.16.0"}, {"name": "idna", "version": "3.10"}]) + "\n"
           + "added 2 packages\n" + sandbox_pkgs.NPM_LIST_MARK + "\n"
           + json.dumps({"dependencies": {"left-pad": {"version": "1.3.0",
                                                       "dependencies": {"tiny": {"version": "0.1.0"}}}}}))
    found, error = sandbox_pkgs.installed(out, {PIP, NPM})
    assert error is None
    assert {str(p) for p in found} == {"pip:six==1.16.0", "pip:idna==3.10", "npm:left-pad@1.3.0", "npm:tiny@0.1.0"}
    assert [str(p) for p in sandbox_pkgs.unchecked(found, [SIX, LEFT_PAD])] == ["npm:tiny@0.1.0", "pip:idna==3.10"]


@pytest.mark.parametrize("out, managers", [
    ("no marks", {PIP}),
    (sandbox_pkgs.PIP_LIST_MARK + "\n[{broken", {PIP}),
    (sandbox_pkgs.PIP_LIST_MARK + '\n[{"name": "six", "version": "1.16.0"}]', {PIP, NPM}),
    (sandbox_pkgs.NPM_LIST_MARK + "\n", {NPM}),
], ids=["no_mark", "broken_json", "npm_mark_missing", "npm_ls_empty"])
def test_missing_install_list_is_a_refusal(out, managers):
    found, error = sandbox_pkgs.installed(out, managers)
    assert found == [] and "не вернула список" in error and "\n" not in error


@pytest.mark.parametrize("item", ["pip:evil\n==1.0.0", "pip:six==1.16.0\n", "pip:six==１.16.0", "npm:left-pad@1.3.0\n",
                                  "npm:left-pad@1.٣.0"],
                         ids=["newline_in_name", "trailing_newline", "fullwidth_digit", "npm_newline", "arabic_digit"])
def test_newlines_and_unicode_digits_do_not_parse(item):
    assert sandbox_pkgs.parse_one(item)[1]


@pytest.mark.parametrize("name", ["requirements.lock", "package.json", ".npmrc", "package-lock.json",
                                  "npm-shrinkwrap.json", "pip.conf", ".pkgs/sitecustomize.py", "node_modules/a/index.js",
                                  "sub/.NPMRC", "./.npmrc"])
def test_reserved_names(name):
    assert sandbox_pkgs.reserved(str(PurePosixPath(name)))


@pytest.mark.parametrize("answer", [{"results": ["x"]}, {"results": [{"vulns": "GHSA"}]}, {"results": [{"vulns": [1]}]},
                                    ["results"]], ids=["result_str", "vulns_str", "vuln_int", "list_answer"])
def test_osv_answer_out_of_format_is_a_refusal(answer):
    client = Client(body=answer)
    error = sandbox_pkgs.osv_check([SIX], client)
    assert ("не по формату" in error or "не на все" in error) and "\n" not in error


# ─── в sandbox.run ────────────────────────────────────────────────────────────

class Docker:
    """Подменяет docker: установка печатает список поставленного, остальные запуски - ok."""

    def __init__(self, install=None, cleanup=None):
        self.runs = []
        self.install = install or CliResult(0, sandbox_pkgs.PIP_LIST_MARK + '\n[{"name": "six", "version": "1.16.0"}]', "")
        self.cleanup = cleanup or CliResult(0, "", "")

    def __call__(self, argv, cwd, timeout, env=None):
        command = argv[-1]
        self.runs.append((argv[argv.index("--network") + 1], command, timeout))
        if command == sandbox_pkgs.CLEANUP:
            return self.cleanup
        return self.install if "install" in command else CliResult(0, "ok", "")


def _run(params, tmp_path, docker, osv=lambda pkgs: None):
    return sandbox.run(params, root=tmp_path, limits=LIMITS, runner=docker, new_id=lambda: JOB, now=lambda: AT,
                       net_ensure=lambda run: None, osv=osv)


def _journal(tmp_path):
    return [json.loads(s) for s in (tmp_path / f"{JOB}.log").read_text(encoding="utf-8").splitlines()]


def test_without_packages_nothing_is_installed(tmp_path):
    docker = Docker()
    _run({"command": "python main.py"}, tmp_path, docker, osv=lambda pkgs: pytest.fail("OSV без пакетов"))
    assert [(net, cmd) for net, cmd, _ in docker.runs] == [("none", "python main.py")]


def test_install_runs_with_network_and_the_command_without(tmp_path):
    docker = Docker()
    out = _run({"command": "python -c 'import six'", "packages": ["pip:six==1.16.0"]}, tmp_path, docker)
    (install_net, install_cmd, install_timeout), (cmd_net, cmd, _) = docker.runs
    assert install_net == sandbox_net.net_name() and "python -I -m pip install --isolated" in install_cmd
    assert install_timeout == sandbox.DEFAULT_PKGS_TIMEOUT_S
    assert cmd_net == "none" and cmd == "python -c 'import six'"
    assert out.code == 0 and out.text.startswith("Пакеты поставлены: pip:six==1.16.0")
    assert (tmp_path / JOB / "requirements.lock").read_text(encoding="utf-8") == "six==1.16.0\n"
    kinds = [line["kind"] for line in _journal(tmp_path)]
    assert kinds == ["network", "packages", "run"]
    assert _journal(tmp_path)[1]["requested"] == ["pip:six==1.16.0"] and _journal(tmp_path)[1]["code"] == 0


@pytest.mark.parametrize("params, osv, expected", [
    ({"command": "ls", "packages": ["pip:six"]}, lambda p: None, "нужна точная версия"),
    ({"command": "ls", "packages": ["pip:six==1.16.0"]}, lambda p: "У пакетов известные уязвимости (OSV): x",
     "уязвимости"),
    ({"command": "ls", "packages": ["pip:six==1.16.0"]}, lambda p: "OSV недоступен (ConnectError): ...", "OSV недоступен"),
], ids=["bad_spec", "vulns", "osv_down"])
def test_refused_packages_run_nothing_and_are_journaled(tmp_path, params, osv, expected):
    docker = Docker()
    out = _run({**params, "files": {"a.py": "1"}}, tmp_path, docker, osv=osv)
    assert expected in out.text and "\n" not in out.text and docker.runs == []
    assert not (tmp_path / JOB / "a.py").exists()                       # отказ до записи файлов
    assert _journal(tmp_path)[0]["kind"] == "packages" and expected in _journal(tmp_path)[0]["error"]


def test_failed_install_stops_the_command(tmp_path):
    docker = Docker(install=CliResult(1, "", "ERROR: No matching distribution found for six==9.9.9"))
    out = _run({"command": "ls", "packages": ["pip:six==9.9.9"]}, tmp_path, docker)
    assert "Пакеты не поставились (код 1: ERROR: No matching distribution" in out.text
    assert len(docker.runs) == 1


def test_vulnerable_dependency_removes_what_was_installed(tmp_path):
    docker = Docker(install=CliResult(0, sandbox_pkgs.PIP_LIST_MARK + json.dumps(
        [{"name": "six", "version": "1.16.0"}, {"name": "jinja2", "version": "2.4"}]), ""))
    asked = []

    def osv(pkgs):
        asked.append([str(p) for p in pkgs])
        return "У пакетов известные уязвимости (OSV): pip:jinja2==2.4 - GHSA-x" if len(asked) == 2 else None

    out = _run({"command": "ls", "packages": ["pip:six==1.16.0"]}, tmp_path, docker, osv=osv)
    assert asked == [["pip:six==1.16.0"], ["pip:jinja2==2.4"]]          # зависимость проверена отдельно
    assert "jinja2" in out.text and "убраны" in out.text
    assert docker.runs[-1][:2] == ("none", sandbox_pkgs.CLEANUP)        # без сети, и команда не шла
    assert _journal(tmp_path)[-2]["dependencies"] == ["pip:jinja2==2.4"]


EARLIER_LOCK = "numpy==2.5.4\n"


@pytest.mark.parametrize("docker, osv, expected", [
    (Docker(install=CliResult(1, "", "ERROR: boom")), lambda p: None, "не поставились"),
    (Docker(install=CliResult(0, sandbox_pkgs.PIP_LIST_MARK + '[{"name": "jinja2", "version": "2.4"}]', "")),
     lambda p: "У пакетов известные уязвимости (OSV): x" if p and p[0].name == "jinja2" else None, "уязвимости"),
], ids=["install_failed", "vulnerable_dependency"])
def test_lock_stays_as_it_was_when_install_does_not_succeed(tmp_path, docker, osv, expected):
    (tmp_path / JOB).mkdir()
    (tmp_path / JOB / "requirements.lock").write_text(EARLIER_LOCK, encoding="utf-8")
    out = _run({"job": JOB, "packages": ["pip:six==1.16.0"]}, tmp_path, docker, osv=osv)
    assert expected in out.text
    assert (tmp_path / JOB / "requirements.lock").read_text(encoding="utf-8") == EARLIER_LOCK


def test_missing_install_list_cleans_up_and_refuses(tmp_path):
    docker = Docker(install=CliResult(0, "installed, but no list", ""))
    out = _run({"command": "ls", "packages": ["pip:six==1.16.0"]}, tmp_path, docker,
               osv=lambda p: None)
    assert "не вернула список пакетов pip" in out.text and "убраны" in out.text
    assert docker.runs[-1][1] == sandbox_pkgs.CLEANUP and len(docker.runs) == 2     # команда не шла


def test_failed_cleanup_is_said(tmp_path):
    docker = Docker(install=CliResult(0, "no list", ""), cleanup=CliResult(1, "", "rm: busy"))
    out = _run({"packages": ["pip:six==1.16.0"]}, tmp_path, docker)
    assert "убрать поставленное не удалось" in out.text and ".pkgs" in out.text and "\n" not in out.text


def test_lock_write_failure_is_journaled_not_raised(tmp_path, monkeypatch):
    def broken(base, name, text):
        raise PermissionError("denied")
    monkeypatch.setattr(sandbox, "replace_job_file", broken)
    out = _run({"command": "ls", "packages": ["pip:six==1.16.0"]}, tmp_path, Docker())
    assert out.code == 0 and "Пакеты поставлены" in out.text
    assert "lock-файлы не записаны" in [s for s in _journal(tmp_path) if s["kind"] == "packages"][0]["lock_error"]


# ─── папку задачи пишет код задачи: шлюз не идёт по ссылкам и не открывает спецфайлы ───

HOST_TEXT = "HOST-SECRET==1.0\n"


def _symlink_or_skip(target, link):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"симлинк не создать ({e.__class__.__name__}): на Windows без прав разработчика; "
                    "тот же случай проверяет живой тест")


@pytest.mark.parametrize("name", ["requirements.lock", "package.json"])
def test_lock_symlink_to_a_host_file_is_refused(tmp_path, name):
    host = tmp_path / "host.txt"
    host.write_text(HOST_TEXT, encoding="utf-8")
    (tmp_path / JOB).mkdir()
    _symlink_or_skip(host, tmp_path / JOB / name)
    docker = Docker()
    out = _run({"job": JOB, "packages": ["pip:six==1.16.0", "npm:left-pad@1.3.0"]}, tmp_path, docker)
    assert f"{name} задачи - не обычный файл" in out.text and "\n" not in out.text and docker.runs == []
    assert host.read_text(encoding="utf-8") == HOST_TEXT                        # цель не тронута
    assert "HOST-SECRET" not in (tmp_path / f"{JOB}.log").read_text(encoding="utf-8")


def test_lock_fifo_is_refused_without_hanging(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO на этой ОС не создать; тот же случай проверяет живой тест")
    (tmp_path / JOB).mkdir()
    os.mkfifo(tmp_path / JOB / "requirements.lock")
    done = []
    worker = threading.Thread(target=lambda: done.append(
        _run({"job": JOB, "packages": ["pip:six==1.16.0"]}, tmp_path, Docker())), daemon=True)
    worker.start()
    worker.join(FIFO_WAIT_S)
    assert done and "не обычный файл" in done[0].text


FIFO_WAIT_S = 5     # отказ по lstat - доли секунды; зависание на open FIFO - навсегда


def test_lock_directory_is_refused(tmp_path):
    (tmp_path / JOB / "requirements.lock").mkdir(parents=True)
    out = _run({"job": JOB, "packages": ["pip:six==1.16.0"]}, tmp_path, Docker())
    assert "requirements.lock задачи - не обычный файл" in out.text


def test_windows_reparse_points_are_not_regular():
    # симлинк и FIFO из контейнера Linux на хосте Windows: lstat даёт обычный файл, отличает st_reparse_tag
    lx_symlink, lx_fifo = 0xA000001D, 0x80000024
    assert sandbox.is_regular(SimpleNamespace(st_mode=0o100666, st_reparse_tag=0))
    assert not sandbox.is_regular(SimpleNamespace(st_mode=0o100666, st_reparse_tag=lx_symlink))
    assert not sandbox.is_regular(SimpleNamespace(st_mode=0o100666, st_reparse_tag=lx_fifo))


def test_lock_write_replaces_a_symlink_not_its_target(tmp_path):
    host = tmp_path / "host.txt"
    host.write_text(HOST_TEXT, encoding="utf-8")
    _symlink_or_skip(host, tmp_path / "requirements.lock")
    sandbox.replace_job_file(tmp_path, "requirements.lock", "six==1.16.0\n")
    assert host.read_text(encoding="utf-8") == HOST_TEXT
    assert not (tmp_path / "requirements.lock").is_symlink()
    assert (tmp_path / "requirements.lock").read_text(encoding="utf-8") == "six==1.16.0\n"


def test_files_do_not_write_into_a_directory_in_place_of_a_file(tmp_path):
    (tmp_path / JOB / "main.py").mkdir(parents=True)
    out = _run({"files": {"main.py": "print(1)"}}, tmp_path, Docker())
    assert "не обычный файл" in out.text


def test_listing_and_serving_skip_links_out(tmp_path):
    base = tmp_path / JOB
    base.mkdir()
    (base / "page.html").write_text("ok", encoding="utf-8")
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "secret.html").write_text("s", encoding="utf-8")
    _symlink_or_skip(host_dir, base / "linked")
    os.symlink(host_dir / "secret.html", base / "secret.html")
    assert sandbox.list_files(base) == ["page.html"]
    assert sandbox.served_file(tmp_path, JOB, "secret.html") is None
    assert sandbox.served_file(tmp_path, JOB, "linked/secret.html") is None


def test_runs_of_one_job_are_serialized(tmp_path):
    """Второй запуск той же задачи ждёт выхода первого: шлюз не пишет файлы рядом с живым контейнером."""
    busy, peak, guard = [0], [0], threading.Lock()

    def runner(argv, cwd, timeout, env=None):
        with guard:
            busy[0] += 1
            peak[0] = max(peak[0], busy[0])
        threading.Event().wait(RUN_HOLD_S)
        with guard:
            busy[0] -= 1
        return CliResult(0, "ok", "")

    def one(i):
        sandbox.run({"job": JOB, "files": {f"f{i}.py": "1"}, "command": "ls"}, root=tmp_path, limits=LIMITS,
                    runner=runner, now=lambda: AT)

    (tmp_path / JOB).mkdir()
    threads = [threading.Thread(target=one, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(SERIAL_WAIT_S)
    assert peak[0] == 1 and len([s for s in _journal(tmp_path) if s["kind"] == "run"]) == 3


RUN_HOLD_S = 0.2        # контейнер «работает» столько; три запуска подряд - 0,6 с
SERIAL_WAIT_S = 10


def test_busy_job_answers_in_one_line(tmp_path, monkeypatch):
    monkeypatch.setenv(sandbox.JOB_WAIT_ENV, "0")
    held = sandbox._job_lock(tmp_path, JOB)
    held.lock.acquire()
    try:
        out = _run({"job": JOB, "command": "ls"}, tmp_path, Docker())
    finally:
        held.lock.release()
    assert "занята другим запуском" in out.text and "\n" not in out.text


def _swap_after_check(monkeypatch, swap):
    """Подмена пути контейнером между проверкой шлюза и чтением или записью: inside() отвечает, затем путь меняется."""
    real = sandbox.inside

    def checked_then_swapped(base, name):
        found = real(base, name)
        swap()
        return found
    monkeypatch.setattr(sandbox, "inside", checked_then_swapped)


def test_served_file_swapped_to_a_link_after_the_check_is_not_served(tmp_path, monkeypatch):
    host = tmp_path / "host.txt"
    host.write_text(HOST_TEXT, encoding="utf-8")
    page = tmp_path / JOB / "page.html"
    page.parent.mkdir()
    page.write_text("ok", encoding="utf-8")
    _symlink_or_skip(host, tmp_path / "probe")

    def swap():
        page.unlink()
        os.symlink(host, page)
    _swap_after_check(monkeypatch, swap)
    assert sandbox.served_file(tmp_path, JOB, "page.html") is None


def test_write_into_a_folder_swapped_to_a_link_stays_inside(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    site = tmp_path / JOB / "site"
    site.mkdir(parents=True)
    _symlink_or_skip(outside, tmp_path / "probe")

    def swap():
        if site.is_dir() and not site.is_symlink():
            site.rmdir()
            os.symlink(outside, site)
    _swap_after_check(monkeypatch, swap)
    error = sandbox.write_files(tmp_path / JOB, {"site/index.html": "<p>x</p>"})
    assert error and "не записан" in error and list(outside.iterdir()) == []


def test_job_files_are_readable_by_the_container_user(tmp_path):
    if os.name == "nt":
        pytest.skip("права POSIX на Windows не задаются: контейнер видит папку через Docker Desktop")
    sandbox.replace_job_file(tmp_path, "requirements.lock", "six==1.16.0\n")
    sandbox.write_files(tmp_path, {"sub/main.py": "print(1)"})
    assert (tmp_path / "requirements.lock").stat().st_mode & 0o777 == sandbox.JOB_FILE_MODE
    assert (tmp_path / "sub" / "main.py").stat().st_mode & 0o777 == sandbox.JOB_FILE_MODE


def test_fifo_is_not_read_even_when_opened(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO на этой ОС не создать; тот же случай проверяет живой тест")
    os.mkfifo(tmp_path / "pipe")
    assert sandbox.read_regular(tmp_path, "pipe", sandbox.FILE_MAX_BYTES) is None     # fstat после open


def test_gateway_temp_files_are_not_job_files(tmp_path):
    (tmp_path / f"{sandbox.TMP_PREFIX}abc.tmp").write_text("x", encoding="utf-8")
    (tmp_path / "main.py").write_text("x", encoding="utf-8")
    assert sandbox.list_files(tmp_path) == ["main.py"]


def test_served_file_returns_content_capped(tmp_path, monkeypatch):
    (tmp_path / JOB).mkdir()
    (tmp_path / JOB / "big.html").write_bytes(b"x" * 11)
    (tmp_path / JOB / "ok.html").write_bytes(b"<p>ok</p>")
    monkeypatch.setenv(sandbox.SERVE_MAX_ENV, "10")
    assert sandbox.served_file(tmp_path, JOB, "ok.html") == b"<p>ok</p>"
    assert sandbox.served_file(tmp_path, JOB, "big.html") is None
    assert sandbox.served_file(tmp_path, JOB, "../x") is None


@pytest.mark.integration
def test_live_links_and_fifo_from_the_container(tmp_path):
    """Код задачи делает симлинк на файл хоста и FIFO на месте lock-файлов: следующий запуск с packages отказывает
    одной строкой, список файлов и показ их не трогают, шлюз не падает и не виснет (на Windows это точки повторного
    разбора, на Linux - настоящие симлинк и FIFO)."""
    _live_ready()
    first = sandbox.run({"command": "ln -s /etc/passwd requirements.lock && mkfifo package.json && "
                                    "ln -s /etc/hostname page.html"},
                        root=tmp_path, limits=LIMITS, new_id=lambda: uuid.uuid4().hex[:12])
    assert first.code == 0 and first.files == () and first.preview is None, first.text
    second = sandbox.run({"job": first.job_id, "packages": ["pip:six==1.16.0", "npm:left-pad@1.3.0"]},
                         root=tmp_path, limits=LIMITS)
    assert "не обычный файл" in second.text and "\n" not in second.text
    assert "root:" not in (tmp_path / f"{first.job_id}.log").read_text(encoding="utf-8")
    assert sandbox.served_file(tmp_path, first.job_id, "page.html") is None


@pytest.mark.parametrize("name", ["requirements.lock", "package.json", ".npmrc", "package-lock.json",
                                  "npm-shrinkwrap.json", "pip.conf", ".pkgs/sitecustomize.py", "node_modules/x/i.js",
                                  "./sub/../.npmrc"])
def test_files_cannot_steer_the_install(tmp_path, name):
    docker = Docker()
    out = _run({"packages": ["pip:six==1.16.0"], "files": {name: "--index-url http://evil/simple"}}, tmp_path, docker)
    assert "при packages не передаётся" in out.text and docker.runs == []
    assert not (tmp_path / JOB).exists() or sandbox.list_files(tmp_path / JOB) == []


def test_same_names_are_fine_without_packages(tmp_path):
    out = _run({"files": {"package.json": "{}"}}, tmp_path, Docker())
    assert "package.json" in out.files


@pytest.mark.parametrize("value", [5, {"pip": "six"}, [5]], ids=["int", "dict", "list_of_int"])
def test_packages_of_wrong_type_refuse_without_crash(tmp_path, value):
    out = _run({"command": "ls", "packages": value}, tmp_path, Docker())
    assert "packages - список строк" in out.text
    assert _journal(tmp_path)[0]["kind"] == "packages"


def test_packages_without_command_install_only(tmp_path):
    docker = Docker()
    out = _run({"packages": ["pip:six==1.16.0"]}, tmp_path, docker)
    assert len(docker.runs) == 1 and "Пакеты поставлены" in out.text and "requirements.lock" in out.text


def test_package_folders_are_not_job_files(tmp_path):
    (tmp_path / ".pkgs" / "six").mkdir(parents=True)
    (tmp_path / ".pkgs" / "six" / "page.html").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules" / "a").mkdir(parents=True)
    (tmp_path / "node_modules" / "a" / "i.png").write_text("x", encoding="utf-8")
    (tmp_path / "main.py").write_text("x", encoding="utf-8")
    assert sandbox.list_files(tmp_path) == ["main.py"]


def test_tool_offers_packages():
    from gateway import agent
    names = {p["name"] for p in agent.SandboxRunTool.parameters}
    assert "packages" in names and "pip:six==1.16.0" in agent.SandboxRunTool.description


# ─── живая установка ──────────────────────────────────────────────────────────

@pytest.mark.integration
def test_live_install_wheel_and_npm_package(tmp_path):
    _live_ready()
    command = "python -c \"import six; print('six', six.__version__)\" && node -e \"console.log(require('left-pad')('7', 3, '0'))\""
    out = sandbox.run({"packages": ["pip:six==1.16.0", "npm:left-pad@1.3.0"], "command": command, "timeout": 60},
                      root=tmp_path, limits=LIMITS, new_id=lambda: uuid.uuid4().hex[:12])
    assert out.code == 0 and "six 1.16.0" in out.text and "007" in out.text, out.text
    assert out.network == "none"                                        # команда шла без сети
