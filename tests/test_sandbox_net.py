"""Сеть песочницы по запросу (SAM-83): интернет есть, дом и хост закрыты файрволом; без запроса сети нет."""
import shutil
import subprocess
import uuid

import pytest

from gateway import sandbox, sandbox_net
from gateway.apps import DockerResult
from gateway.tools.coding_agents import CliResult

LIMITS = sandbox.Limits(image=sandbox.DEFAULT_IMAGE, network="none", memory="256m", cpus="1", pids="64")
JOB = "0123456789ab"


class Docker:
    """inspect - ответы `network inspect` по порядку (None - сети нет), последний повторяется."""

    def __init__(self, guard_out="6\n", guard_code=0, inspect=(None,), create_err=""):
        self.calls, self.guard_out, self.guard_code = [], guard_out, guard_code
        self.inspect, self.create_err = list(inspect), create_err

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[1:3] == ["network", "inspect"]:
            out = self.inspect.pop(0) if len(self.inspect) > 1 else self.inspect[0]
            return DockerResult(1, "", "No such network") if out is None else DockerResult(0, f"{out}\n", "")
        if argv[1:3] == ["network", "create"] and self.create_err:
            return DockerResult(1, "", self.create_err)
        if "--privileged" in argv:
            return DockerResult(self.guard_code, self.guard_out, "" if self.guard_code == 0 else "boom")
        return DockerResult(0, "", "")


def test_guard_script_drops_every_closed_range_once():
    script = sandbox_net.guard_script("10.232.0.0/24")
    for d in sandbox_net.CLOSED:
        assert f"-C DOCKER-USER -s 10.232.0.0/24 -d {d} -j DROP" in script     # проверка, чтобы не дублировать
        assert f"-I DOCKER-USER -s 10.232.0.0/24 -d {d} -j DROP" in script
    assert "192.168.0.0/16" in sandbox_net.CLOSED and "10.0.0.0/8" in sandbox_net.CLOSED


def test_ensure_creates_network_and_applies_rules():
    docker = Docker()
    assert sandbox_net.ensure(docker) is None
    # только IPv4: правила CLOSED - IPv4
    assert ["docker", "network", "create", "--ipv6=false", "--subnet", "10.232.0.0/24", "sam-sandbox-net"] in docker.calls
    guard = next(c for c in docker.calls if "--privileged" in c)
    assert "--net=host" in guard and sandbox_net.DEFAULT_GUARD_IMAGE in guard
    assert sandbox_net.DEFAULT_GUARD_IMAGE in sandbox_net.GUARD_BUILD


@pytest.mark.parametrize("found", ["false 10.232.0.0/24 ", "false fd00::/64 10.232.0.0/24 "],
                         ids=["ipv4", "extra_record_first"])
def test_existing_network_in_its_subnet_is_reused(found):
    docker = Docker(inspect=(found,))
    assert sandbox_net.ensure(docker) is None
    assert not any(c[1:3] == ["network", "create"] for c in docker.calls)
    assert sandbox_net.NET_FORMAT in docker.calls[0]


@pytest.mark.parametrize("found, expected", [
    ("false 172.30.0.0/16 ", "172.30.0.0/16"),
    ("true 10.232.0.0/24 fd00::/64 ", "IPv6"),
    ("true fd00::/64 10.232.0.0/24 ", "IPv6"),
    ("", "не описал"),
], ids=["other_subnet", "dual_stack", "dual_stack_v6_first", "empty_inspect"])
def test_unfit_existing_network_gets_no_internet(found, expected):
    docker = Docker(inspect=(found,))
    error = sandbox_net.ensure(docker)
    assert expected in error and "docker network rm sam-sandbox-net" in error and "\n" not in error
    assert not any("--privileged" in c for c in docker.calls)     # правила не ставятся, сеть не дана


def test_race_on_create_rechecks_the_network():
    # соседний запуск создал сеть между проверкой и create: create падает, повторная сверка проходит
    docker = Docker(inspect=(None, "false 10.232.0.0/24 "), create_err="network sam-sandbox-net already exists")
    assert sandbox_net.ensure(docker) is None
    assert [c[1:3] for c in docker.calls[:3]] == [["network", "inspect"], ["network", "create"], ["network", "inspect"]]
    lost = Docker(inspect=(None,), create_err="boom")
    assert "не создана: boom" in sandbox_net.ensure(lost)


@pytest.mark.parametrize("docker, expected", [
    (Docker(guard_code=1), "не встал"),
    (Docker(guard_out="3\n"), "не полностью"),
    (Docker(guard_out="?\n"), "не подтвердил"),
])
def test_no_firewall_means_no_network(docker, expected):
    error = sandbox_net.ensure(docker)
    assert expected in error and "\n" not in error


def test_bad_subnet_setting_fails_loudly(monkeypatch):
    monkeypatch.setenv(sandbox_net.SUBNET_ENV, "not-a-net")
    with pytest.raises(ValueError):
        sandbox_net.subnet()


class Runner:
    def __init__(self):
        self.argv = None

    def __call__(self, argv, cwd, timeout):
        if argv[1] == "run" and "--privileged" not in argv and "--rm" in argv:
            self.argv = argv
        return CliResult(0, "ok", "")


def test_run_without_network_never_touches_the_firewall(tmp_path):
    runner, asked = Runner(), []
    sandbox.run({"command": "ls"}, root=tmp_path, limits=LIMITS, runner=runner, new_id=lambda: JOB,
                net_ensure=lambda run: asked.append(1))
    assert asked == [] and runner.argv[runner.argv.index("--network") + 1] == "none"


def test_run_with_internet_uses_guarded_network_or_refuses(tmp_path):
    runner = Runner()
    sandbox.run({"command": "pip download x", "network": "internet"}, root=tmp_path, limits=LIMITS, runner=runner,
                new_id=lambda: JOB, net_ensure=lambda run: None)
    assert runner.argv[runner.argv.index("--network") + 1] == sandbox_net.net_name()
    out = sandbox.run({"command": "ls", "network": "internet"}, root=tmp_path, limits=LIMITS, runner=Runner(),
                      new_id=lambda: JOB, net_ensure=lambda run: "Файрвол песочницы не встал, сеть не дана")
    assert out.text.startswith("Файрвол") and out.code is None
    bad = sandbox.run({"command": "ls", "network": "bridge"}, root=tmp_path, limits=LIMITS, runner=Runner(),
                      new_id=lambda: JOB)
    assert "network -" in bad.text


# ─── живой Docker ─────────────────────────────────────────────────────────────

PROBE = """
import urllib.request
def t(name, url):
    try:
        urllib.request.urlopen(url, timeout=6)
        return name + "=REACHED"
    except urllib.error.HTTPError:
        return name + "=REACHED"
    except Exception:
        return name + "=blocked"
print(" ".join([t("internet", "https://pypi.org/simple/"), t("router", "http://ROUTER/"),
                t("host", "http://host.docker.internal:8000/health")]))
"""


@pytest.fixture
def docker_ready():
    images = (sandbox.DEFAULT_IMAGE, sandbox_net.DEFAULT_GUARD_IMAGE)
    try:
        ready = bool(shutil.which("docker")) and all(subprocess.run(
            ["docker", "image", "inspect", i], capture_output=True, timeout=15).returncode == 0 for i in images)
    except (OSError, subprocess.TimeoutExpired):
        ready = False
    if not ready:
        pytest.skip(f"Docker не запущен или нет образов {sandbox.DEFAULT_IMAGE} и {sandbox_net.DEFAULT_GUARD_IMAGE}")


def _router() -> str:
    out = subprocess.run(["python", "-c", "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
                          "s.connect(('1.1.1.1',80));print(s.getsockname()[0])"], capture_output=True, text=True)
    return out.stdout.strip().rsplit(".", 1)[0] + ".1"          # шлюз домашней сети - .1 своей подсети


@pytest.mark.integration
def test_live_internet_open_home_and_host_closed(docker_ready, tmp_path):
    out = sandbox.run({"files": {"p.py": PROBE.replace("ROUTER", _router())}, "command": "python p.py",
                       "network": "internet", "timeout": 60}, root=tmp_path, limits=LIMITS,
                      new_id=lambda: uuid.uuid4().hex[:12])
    assert "internet=REACHED router=blocked host=blocked" in out.text, out.text
