"""Песочница (SAM-83, ADR-016): код SAM - в одноразовом контейнере без сети, секретов и файлов хоста.

Логика (команда docker, пути, итог) проверяется без Docker; изоляция - на живом Docker под меткой integration
(`pytest -m integration`), без Docker или образа живой тест пропускается с причиной.
"""
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway import sandbox
from gateway.tools.coding_agents import CliResult
from tests.fakes_core import app_client  # noqa: F401  (фикстура)

LIMITS = sandbox.Limits(image=sandbox.DEFAULT_IMAGE, network="none", memory="512m", cpus="1", pids="128")
JOB = "0123456789ab"


class _Runner:
    """Подменяет вызов docker: записывает argv и кладёт файлы, которые «создал» контейнер."""

    def __init__(self, result=None, make_files=None):
        self.calls = []
        self.result = result or CliResult(0, "", "")
        self.make_files = make_files or {}

    def __call__(self, argv, cwd, timeout, env=None):
        self.calls.append((argv, timeout))
        host = argv[argv.index("-v") + 1].rsplit(":/work", 1)[0]
        for name, text in self.make_files.items():
            with open(os.path.join(host, name), "w", encoding="utf-8") as f:
                f.write(text)
        return self.result


# ─── команда docker ───────────────────────────────────────────────────────────

def test_docker_command_isolates_the_job(tmp_path, monkeypatch):
    argv = sandbox.docker_argv(JOB, tmp_path, "python main.py", LIMITS)
    joined = " ".join(argv)
    for flag in ("--rm", "--network none", "--memory 512m", "--cpus 1", "--pids-limit 128", "--read-only",
                 "--cap-drop ALL", "--security-opt no-new-privileges", "--user 1000:1000", "--pull=never"):
        assert flag in joined
    assert argv[argv.index("-v") + 1] == f"{tmp_path}:/work"
    assert argv.count("-v") == 1                                   # только папка задачи
    assert not any(a.split("=")[0] in ("-e", "--env", "--env-file", "--privileged") for a in argv)   # и --env=X
    assert argv[-3:] == ["sh", "-c", "python main.py"] and argv[-4] == sandbox.DEFAULT_IMAGE
    monkeypatch.setenv("SAM_SANDBOX_NETWORK", "bridge")             # сеть - настройка
    assert sandbox.limits_from_env().network == "bridge"
    monkeypatch.delenv("SAM_SANDBOX_NETWORK")
    assert sandbox.limits_from_env().network == "none"


# ─── пути ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("params, refusal", [
    *[({"files": {name: "x"}, "command": "ls"}, "вне папки задачи")
      for name in ("../x.py", "/etc/passwd", "C:/Windows/x", "a/../../b", "", ".")],
    ({"files": {"ok.py": "print(1)", "../evil.py": "x"}, "command": "python ok.py"}, "вне папки задачи"),
    *[({"job": job, "command": "ls"}, "не из песочницы") for job in ("../..", "abc", "0123456789AB", "0123456789ab/..")],
], ids=["parent", "unix_root", "windows_root", "climb_out", "empty", "dot", "one_bad_of_two",
        "job_parent", "job_short", "job_upper", "job_climb"])
def test_sandbox_refuses_paths_outside_the_job(tmp_path, params, refusal):
    runner = _Runner()
    out = sandbox.run(params, root=tmp_path, limits=LIMITS, runner=runner, new_id=lambda: JOB)
    assert refusal in out.text and "\n" not in out.text
    assert runner.calls == []                                      # контейнер не запускался
    assert not (tmp_path / JOB / "ok.py").exists() and not (tmp_path / "evil.py").exists()   # ничего не записано
    assert sandbox.inside(tmp_path, "site/index.html") == (tmp_path / "site" / "index.html").resolve()


# ─── запуск и итог ────────────────────────────────────────────────────────────

def test_files_keep_their_line_endings(tmp_path):
    """Файл задачи пишется байт в байт: CRLF Windows ломал #!/bin/sh в контейнере Linux."""
    sandbox.write_files(tmp_path, {"run.sh": "#!/bin/sh\necho ok\n", "dos.txt": "a\r\nb"})
    assert (tmp_path / "run.sh").read_bytes() == b"#!/bin/sh\necho ok\n"
    assert (tmp_path / "dos.txt").read_bytes() == b"a\r\nb"


def test_run_writes_files_and_names_the_preview(tmp_path):
    runner = _Runner(CliResult(0, "готово\n", ""), make_files={"index.html": "<h1>hi</h1>"})
    out = sandbox.run({"files": {"main.py": "print('готово')"}, "command": "python main.py"},
                      root=tmp_path, limits=LIMITS, runner=runner, new_id=lambda: JOB)
    assert (tmp_path / JOB / "main.py").read_text(encoding="utf-8") == "print('готово')"
    assert out.code == 0 and out.preview == f"/sandbox/{JOB}/index.html"
    assert "готово" in out.text and "index.html" in out.text and "main.py" in out.text
    again = sandbox.run({"job": JOB, "files": {"b.py": "2"}, "command": "ls"}, root=tmp_path, limits=LIMITS,
                        runner=_Runner())
    assert set(again.files) == {"main.py", "index.html", "b.py"}    # та же задача хранит свои файлы


def _missing(*a, **kw):
    raise FileNotFoundError("docker")


NO_IMAGE = "docker: Error response from daemon: No such image: sam-sandbox:2"
DOCKER_DOWN = "error during connect: Get http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.47/containers: open ..."


@pytest.mark.parametrize("params, runner, expected", [
    ({"command": "sleep 999", "timeout": 5}, _Runner(CliResult(None, "", "", timed_out=True)), "5 с"),
    ({"command": "ls"}, _Runner(CliResult(1, "", DOCKER_DOWN)), "Docker не отвечает: запустите Docker Desktop и повторите"),
    ({"command": "ls"}, _missing, "SAM_SANDBOX_DOCKER"),
    ({"command": "ls"}, _Runner(CliResult(125, "", NO_IMAGE)), "docker/Dockerfile.sandbox"),
    ({}, _Runner(), "Нужна command"),
], ids=["timeout_kills_container", "docker_down", "docker_missing", "image_missing", "nothing_to_do"])
def test_sandbox_failure_is_one_line(tmp_path, params, runner, expected):
    killed = []
    out = sandbox.run(params, root=tmp_path, limits=LIMITS, runner=runner, kill=killed.append, new_id=lambda: JOB)
    assert expected in out.text and "\n" not in out.text
    assert killed == ([f"sam-sandbox-{JOB}"] if "timeout" in params else [])


def test_command_stderr_is_not_a_docker_failure(tmp_path):
    # программа сама напечатала «no such image» и упала: это её вывод и её код, а не отсутствие образа
    out = sandbox.run({"command": "python x.py"}, root=tmp_path, limits=LIMITS,
                      runner=_Runner(CliResult(3, "", "no such image")), kill=lambda _: None, new_id=lambda: JOB)
    assert "код выхода 3" in out.text and "no such image" in out.text


# ─── журнал задачи ────────────────────────────────────────────────────────────

AT = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _journal(root):
    return [json.loads(s) for s in (root / f"{JOB}.log").read_text(encoding="utf-8").splitlines()]


def test_run_writes_a_journal_line_outside_the_mounted_folder(tmp_path):
    runner = _Runner(CliResult(2, "", "boom"))
    sandbox.run({"command": "python main.py"}, root=tmp_path, limits=LIMITS, runner=runner,
                new_id=lambda: JOB, now=lambda: AT)
    assert _journal(tmp_path) == [{"time": "2026-10-14T12:00:00+00:00", "kind": "run", "command": "python main.py",
                                   "network": "none", "code": 2}]
    mounted = runner.calls[0][0][runner.calls[0][0].index("-v") + 1].rsplit(":/work", 1)[0]
    journal = sandbox.journal_path(JOB, tmp_path).resolve()
    assert not journal.is_relative_to(Path(mounted).resolve())     # код в контейнере журнал не видит
    assert not (tmp_path / JOB / f"{JOB}.log").exists()


@pytest.mark.parametrize("params, net, error", [
    ({"command": "ls", "files": {"../x": "1"}}, None, "вне папки задачи"),
    ({"command": "ls", "network": "lan"}, None, "network - none или internet"),
    ({"command": "pip download x", "network": "internet"}, "Файрвол песочницы не встал", "Файрвол"),
], ids=["bad_file", "bad_network", "firewall_down"])
def test_refusal_is_journaled(tmp_path, params, net, error):
    runner = _Runner()
    sandbox.run(params, root=tmp_path, limits=LIMITS, runner=runner, new_id=lambda: JOB, now=lambda: AT,
                net_ensure=lambda run: net)
    lines = _journal(tmp_path)
    assert lines[-1]["kind"] == "run" and lines[-1]["code"] is None and error in lines[-1]["error"]
    assert runner.calls == []
    if net:
        assert lines[0] == {"time": "2026-10-14T12:00:00+00:00", "kind": "network", "mode": "internet",
                            "granted": False, "error": net}


def test_granted_network_and_file_writes_in_journal(tmp_path):
    sandbox.run({"command": "ls", "network": "internet"}, root=tmp_path, limits=LIMITS, runner=_Runner(),
                new_id=lambda: JOB, now=lambda: AT, net_ensure=lambda run: None)
    sandbox.run({"job": JOB, "files": {"a.py": "1"}}, root=tmp_path, limits=LIMITS, runner=_Runner(), now=lambda: AT)
    kinds = [(s["kind"], s.get("granted"), s.get("network")) for s in _journal(tmp_path)]
    assert kinds == [("network", True, None), ("run", None, "internet")]   # запись файлов без команды - не запуск


@pytest.mark.parametrize("command, secret, kept", [
    ("git clone https://user:hunter2@github.com/a/b.git", "hunter2", "https://***:***@github.com/a/b.git"),
    ("git clone https://ghp_abc123@github.com/a/b", "ghp_abc123", "https://***@github.com/a/b"),
    ("curl -H 'Authorization: Bearer eyJ0eXAi' https://x", "eyJ0eXAi", "Authorization: Bearer ***' https://x"),
    ("curl -H authorization:abc https://x", "abc", "authorization:*** https://x"),
    ("curl 'https://x/api?token=t0k&q=1'", "t0k", "token=***&q=1"),
    ("python run.py --api_key=K3Y --password=p4ss secret=s3c", "K3Y", "--api_key=*** --password=*** secret=***"),
    ("curl -u user:pa55 https://x", "pa55", "-u ***:*** https://x"),
    ("curl -H 'X-Api-Key: k3y1' https://x", "k3y1", "'X-Api-Key: ***'"),
    ("curl -H 'X-Auth-Token: t0k1' https://x", "t0k1", "'X-Auth-Token: ***'"),
    ("curl -H 'Cookie: sid=c00k; theme=dark' https://x", "c00k", "'Cookie: ***'"),
    ("curl -H 'my-api-key: k3y2' https://x", "k3y2", "'my-api-key: ***'"),
    ("TOKEN='abc def' python a.py", "abc def", "TOKEN=*** python a.py"),
    ('PASSWORD="x y" python a.py', "x y", "PASSWORD=*** python a.py"),
    ("app --token T1k --verbose", "T1k", "--token *** --verbose"),
    ("app --password P4s", "P4s", "--password ***"),
    ("app --api-key K9y", "K9y", "--api-key ***"),
    ("mysql -pS3cret db", "S3cret", "mysql -p*** db"),
    ("echo ghp_" + "a" * 36, "ghp_", "echo ***"),
    ("echo gho_" + "b" * 36, "gho_", "echo ***"),
    ("echo ghs_" + "c" * 36, "ghs_", "echo ***"),
    ("echo ghu_" + "d" * 36, "ghu_", "echo ***"),
    ("echo github_pat_" + "e" * 40, "github_pat_", "echo ***"),
    ("echo sk-" + "f" * 32, "sk-", "echo ***"),
    ("echo xoxb-" + "1" * 12, "xoxb-", "echo ***"),
    ("echo AKIA" + "A" * 16, "AKIA", "echo ***"),
    ('curl -d \'{"api_key": "zzz"}\'', "zzz", '"api_key": "***"'),
    ('curl -d \'{"password": "p w"}\'', "p w", '"password": "***"'),
], ids=["url_user_pass", "url_token", "auth_bearer", "auth_plain", "query_token", "key_password_secret",
        "curl_user", "x_api_key", "x_token", "cookie", "api_key_header", "quoted_single", "quoted_double",
        "flag_token", "flag_password", "flag_api_key", "mysql_p", "ghp", "gho", "ghs", "ghu", "github_pat", "sk",
        "slack", "aws", "json_api_key", "json_password"])
def test_journal_masks_secrets(command, secret, kept):
    masked = sandbox.mask_secrets(command)
    assert secret not in masked and kept in masked
    assert sandbox.mask_secrets("python main.py https://pypi.org/simple/") == "python main.py https://pypi.org/simple/"


def test_plural_names_stay_and_masking_errs_to_more():
    # --keys 3 и keys=3 - число, а не секрет; monkey=banana скрыто лишним: лучше перемаскировать, чем пропустить
    masked = sandbox.mask_secrets("python main.py --keys 3 keys=3 monkey=banana && mkdir -p out")
    assert masked == "python main.py --keys 3 keys=3 monkey=*** && mkdir -p out"


MASK_BUDGET_S = 1.0     # 50 000 символов на шаблонах с ограниченными квантификаторами - сотые доли секунды


@pytest.mark.parametrize("text", ["a" * 50000, "key" * 17000, "-" * 50000, "key " * 12000],
                         ids=["no_equals", "thousands_of_key", "dashes", "spaced_keys"])
def test_masking_is_linear(text):
    started = time.perf_counter()
    sandbox.mask_secrets(text)
    assert time.perf_counter() - started < MASK_BUDGET_S


def test_long_command_is_cut_before_masking(tmp_path):
    sandbox.run({"command": "echo " + "a" * 60000 + " token=t0k"}, root=tmp_path, limits=LIMITS, runner=_Runner(),
                new_id=lambda: JOB, now=lambda: AT)
    assert len(_journal(tmp_path)[0]["command"]) == sandbox.JOURNAL_COMMAND_MAX_CHARS


def test_journal_line_of_a_run_has_no_secret(tmp_path):
    sandbox.run({"command": "git clone https://u:pw@host/r.git"}, root=tmp_path, limits=LIMITS, runner=_Runner(),
                new_id=lambda: JOB, now=lambda: AT)
    assert _journal(tmp_path)[0]["command"] == "git clone https://***:***@host/r.git"


@pytest.mark.parametrize("params, expected", [
    ({"command": "ls", "network": "lan", "files": {"a.py": "1"}}, "network - none или internet"),
    ({"network": "internet", "files": {"a.py": "1"}}, "network=internet нужен вместе с command"),
], ids=["bad_mode", "internet_without_command"])
def test_network_is_checked_before_files_are_written(tmp_path, params, expected):
    out = sandbox.run(params, root=tmp_path, limits=LIMITS, runner=_Runner(), new_id=lambda: JOB, now=lambda: AT)
    assert expected in out.text and "\n" not in out.text
    assert not (tmp_path / JOB / "a.py").exists()
    assert expected in _journal(tmp_path)[-1]["error"]


def test_journal_write_failure_does_not_stop_the_run(tmp_path, caplog):
    (tmp_path / f"{JOB}.log").mkdir()                       # на месте файла журнала - папка: запись падает
    out = sandbox.run({"command": "ls"}, root=tmp_path, limits=LIMITS, runner=_Runner(CliResult(0, "ok", "")),
                      new_id=lambda: JOB)
    assert out.code == 0
    assert any("Журнал песочницы" in r.getMessage() for r in caplog.records)


def test_sandbox_event_names_the_network(monkeypatch):
    from gateway import agent
    seen = []
    monkeypatch.setattr(agent.event_bus, "emit", lambda kind, data: seen.append((kind, data)))
    monkeypatch.setattr(agent._sandbox, "run", lambda params, **kw: sandbox.RunOutcome(
        JOB, "ok", code=0, network="internet", network_granted=True))
    agent._impl_sandbox_run({"command": "ls", "network": "internet"})
    assert seen == [("sandbox_result", seen[0][1])] and seen[0][1]["network"] == {"mode": "internet", "granted": True}


@pytest.mark.parametrize("value,expected", [(None, 60), ("abc", 60), (-1, 60), (30, 30), (99999, 600)])
def test_model_timeout_is_bounded(monkeypatch, value, expected):
    monkeypatch.delenv("SAM_SANDBOX_TIMEOUT_S", raising=False)
    monkeypatch.delenv("SAM_SANDBOX_MAX_TIMEOUT_S", raising=False)
    assert sandbox._timeout(value) == expected


# ─── показ ────────────────────────────────────────────────────────────────────

def test_preview_is_served_only_inside_the_job(tmp_path, monkeypatch, app_client):  # noqa: F811
    (tmp_path / JOB).mkdir()
    (tmp_path / JOB / "index.html").write_text("<p>ok</p>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("key", encoding="utf-8")
    assert sandbox.served_file(tmp_path, JOB, "index.html") is not None
    assert sandbox.served_file(tmp_path, JOB, "../secret.txt") is None
    assert sandbox.served_file(tmp_path, "..", "secret.txt") is None
    assert sandbox.served_file(tmp_path, JOB, "missing.html") is None
    monkeypatch.setattr(sandbox, "SANDBOX_DIR", tmp_path)
    client, _ = app_client
    ok = client.get(f"/sandbox/{JOB}/index.html")
    assert ok.status_code == 200 and ok.headers["content-security-policy"] == "sandbox allow-scripts"
    assert client.get(f"/sandbox/{JOB}/..%2F..%2Fsecret.txt").status_code == 404


def test_tool_offers_network_on_request(monkeypatch):
    """Модель видит сеть по запросу (ADR-016): параметр network доходит до sandbox.run, по умолчанию сети нет."""
    from gateway import agent
    seen = []
    monkeypatch.setattr(agent._sandbox, "run", lambda params, **kw: (seen.append(params),
                        sandbox.RunOutcome("abc123abc123", "ok"))[1])
    names = {p["name"] for p in agent.SandboxRunTool.parameters}
    assert "network" in names and "network=internet" in agent.SandboxRunTool.description
    agent._impl_sandbox_run({"command": "pip download x", "network": "internet"})
    assert seen[-1]["network"] == "internet"


# ─── изоляция на живом Docker ─────────────────────────────────────────────────

DOCKER_CHECK_TIMEOUT_S = 10   # docker image inspect при запущенном движке отвечает за доли секунды
NET_PROBE = ("import urllib.request\n"
             "try:\n    urllib.request.urlopen('http://1.1.1.1', timeout=3); print('NET')\n"
             "except Exception as e:\n    print('NONET', type(e).__name__)\n")


CHART = ("import matplotlib.pyplot as plt\n"
         "plt.plot([1, 2, 3], [1, 4, 9])\n"
         "plt.savefig('chart.png')\n")


@pytest.fixture
def docker_ready():
    """Живой Docker с образом песочницы; без него живой тест пропускается с причиной."""
    try:
        ready = bool(shutil.which("docker")) and subprocess.run(
            ["docker", "image", "inspect", LIMITS.image], capture_output=True,
            timeout=DOCKER_CHECK_TIMEOUT_S).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ready = False
    if not ready:
        pytest.skip(f"Docker не запущен или нет образа {LIMITS.image}")


def _no_network(out):
    assert "NONET" in out.text and "NET\n" not in out.text


def _nothing_of_the_host(out):
    assert "s3cr3t" not in out.text and "HOST-FILE-MARK" not in out.text
    assert "No such file" in out.text and "1000" in out.text


def _timeout_leaves_no_container(out):
    assert "не уложилась в 3 с" in out.text
    running = subprocess.run(["docker", "ps", "-q", "--filter", f"name={out.job_id}"],
                             capture_output=True, text=True).stdout.strip()
    assert running == ""


def _chart_and_node(out):
    assert out.code == 0 and "chart.png" in out.files and "node v" in out.text


def _page_and_result(out):
    assert out.code == 0 and out.preview.endswith("/index.html")


@pytest.mark.integration
@pytest.mark.parametrize("command, files, timeout, check", [
    ("python net.py", {"net.py": NET_PROBE}, 30, _no_network),
    ("env; cat /work/../sibling.txt 2>&1; id -u", {}, 30, _nothing_of_the_host),
    ("sleep 60", {}, 3, _timeout_leaves_no_container),
    ("python make.py", {"make.py": "open('index.html','w').write('<h1>SAM</h1>')"}, 30, _page_and_result),
    ("python chart.py && node -e \"console.log('node', process.version)\"", {"chart.py": CHART}, 60, _chart_and_node),
], ids=["no_network", "no_gateway_env_or_host_files", "timeout_without_container", "page_and_result",
        "chart_and_node_on_read_only_root"])
def test_live_sandbox_isolation(docker_ready, tmp_path, monkeypatch, command, files, timeout, check):
    monkeypatch.setenv("SAM_TEST_SECRET", "s3cr3t")
    (tmp_path / "sibling.txt").write_text("HOST-FILE-MARK", encoding="utf-8")   # рядом с папкой задачи
    check(sandbox.run({"files": files, "command": command, "timeout": timeout}, root=tmp_path,
                      limits=LIMITS, new_id=lambda: uuid.uuid4().hex[:12]))


@pytest.mark.integration
def test_live_sandbox_has_git_and_npm(docker_ready, tmp_path):
    """Образ песочницы с git и npm: работают от uid 1000 на корне только для чтения, коммит и package.json - в /work."""
    command = ("git --version && npm --version && node --version && git init -q && "
               "git commit -q --allow-empty -m x && git log --format=%an && npm init -y > /dev/null")
    out = sandbox.run({"command": command, "timeout": 60}, root=tmp_path, limits=LIMITS,
                      new_id=lambda: uuid.uuid4().hex[:12])
    assert out.code == 0 and "git version" in out.text and "v22." in out.text, out.text
    assert "SAM" in out.text and "package.json" in out.files


@pytest.mark.integration
def test_live_sandbox_ignores_hooks_of_a_foreign_repo(docker_ready, tmp_path):
    """Хук pre-commit и fsmonitor из .git репозитория не выполняются; без защиты образа тот же хук срабатывает."""
    # хук пишется внутри контейнера: файл задачи с хоста Windows пришёл бы с CRLF, и #!/bin/sh не нашёлся бы
    command = ("git init -q && printf '#!/bin/sh\\ntouch HOOKED\\n' > .git/hooks/pre-commit && "
               "chmod +x .git/hooks/pre-commit && "
               "git config core.fsmonitor 'touch FSMON' && git commit -q --allow-empty -m x && git status -s >/dev/null"
               " && (test -e HOOKED && echo HOOK-RAN || echo HOOK-SKIPPED)"
               " && env -u GIT_CONFIG_PARAMETERS git -c core.fsmonitor=false commit -q --allow-empty -m y"
               " && (test -e HOOKED && echo CONTROL-RAN)")
    out = sandbox.run({"command": command, "timeout": 60}, root=tmp_path, limits=LIMITS,
                      new_id=lambda: uuid.uuid4().hex[:12])
    assert out.code == 0 and "HOOK-SKIPPED" in out.text and "FSMON" not in out.files, out.text
    assert "CONTROL-RAN" in out.text            # хук рабочий: защита, а не сломанный тест


def test_image_and_limits_come_from_sam_sandbox_settings(tmp_path, monkeypatch):
    """sandbox 6: образ и лимиты песочницы - из SAM_SANDBOX_IMAGE, _MEMORY, _CPUS, _PIDS; без них - безопасные
    значения; команда docker несёт именно их."""
    for name, value in {"SAM_SANDBOX_IMAGE": "sam-sandbox:test", "SAM_SANDBOX_MEMORY": "1g", "SAM_SANDBOX_CPUS": "2",
                        "SAM_SANDBOX_PIDS": "64"}.items():
        monkeypatch.setenv(name, value)
    argv = sandbox.docker_argv(JOB, tmp_path, "ls", sandbox.limits_from_env())
    joined = " ".join(argv)
    assert "--memory 1g" in joined and "--cpus 2" in joined and "--pids-limit 64" in joined
    assert argv[-4] == "sam-sandbox:test"
    for name in ("SAM_SANDBOX_IMAGE", "SAM_SANDBOX_MEMORY", "SAM_SANDBOX_CPUS", "SAM_SANDBOX_PIDS"):
        monkeypatch.delenv(name)
    assert sandbox.limits_from_env() == sandbox.Limits(image=sandbox.DEFAULT_IMAGE, network="none", memory="512m",
                                                       cpus="1", pids="128")


def test_model_sees_the_last_4000_chars_of_a_long_output(tmp_path):
    """sandbox 42: вывод запуска в контексте модели - последние 4000 знаков: итог и ошибка в конце длинного лога
    не теряются, начало лога отрезается."""
    head, end = "начало " * 1000, "x" * (sandbox.OUTPUT_MAX_CHARS - 6) + " итог!"
    out = sandbox.run({"command": "python long.py"}, root=tmp_path, limits=LIMITS,
                      runner=_Runner(CliResult(0, head + end, "")), new_id=lambda: JOB)
    shown = out.text.split("Вывод:\n", 1)[1].split("\nФайл", 1)[0]
    assert shown == end and "начало" not in out.text
