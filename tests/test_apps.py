"""Живые приложения SAM (SAM-182): решения без Docker, прокси шлюза, изоляция и перезапуск на живом Docker."""
import contextlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from dataclasses import asdict, replace

import pytest

from gateway import apps, sandbox
from tests.fakes_core import app_client  # noqa: F401  (фикстура)

LIMITS = sandbox.Limits(image=sandbox.DEFAULT_IMAGE, network="none", memory="256m", cpus="1", pids="64")
APP = "0123456789ab"
SLUG = "chart"                      # slug манифеста по умолчанию: из имени chart
BASE = "http://127.0.0.1:18400"


class FakeDocker:
    """Записывает команды docker; inspect отвечает по набору запущенных контейнеров, сбой - по подстроке."""

    def __init__(self, running: set[str] = frozenset(), fail: str = "") -> None:
        self.calls: list[list[str]] = []
        self.running, self.fail = set(running), fail

    def __call__(self, argv: list[str]) -> apps.DockerResult:
        self.calls.append(argv)
        if self.fail and self.fail in " ".join(argv):
            return apps.DockerResult(125, "", "docker: Error response from daemon: boom")
        if argv[1] == "inspect":
            return apps.DockerResult(0, "true\n" if argv[-1] in self.running else "false\n", "")
        if argv[1:3] == ["network", "inspect"]:
            return apps.DockerResult(1, "", "no such network")
        return apps.DockerResult(0, "", "")


def manifest(**kw) -> apps.Manifest:
    return apps.Manifest(**{"id": APP, "name": "chart", "command": "python -m http.server 8000", "host_port": 18400,
                            "slug": SLUG, **kw})


# ─── решения ──────────────────────────────────────────────────────────────────

def test_app_container_has_no_way_out_no_port_and_no_manifest():
    argv = apps.app_argv(manifest(), apps.APPS_DIR / APP, LIMITS)
    joined = " ".join(argv)
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    # смонтированы код и данные (SAM-181), манифест - нет
    assert mounts == [f"{apps.APPS_DIR / APP / apps.SRC}:/work", f"{apps.APPS_DIR / APP / apps.DATA}:{apps.DATA_IN}"]
    assert f"--network {apps.app_network(APP)}" in joined and "--restart unless-stopped" in joined
    assert "-p" not in argv and not any(a.split("=")[0] in ("-e", "--env", "--env-file", "--privileged") for a in argv)
    for flag in ("--read-only", "--cap-drop ALL", "--user 1000:1000", "--pull=never", "--memory 256m"):
        assert flag in joined
    assert apps.network_argv(apps.app_network(APP), internal=True, subnet="10.231.0.0/28")[-4:] == [
        "--internal", "--subnet", "10.231.0.0/28", apps.app_network(APP)]


def test_each_app_gets_its_own_small_subnet():
    assert apps.app_subnet(18400) == "10.231.0.0/28" and apps.app_subnet(18401) == "10.231.0.16/28"
    last = apps.ports()[-1]
    assert apps.app_subnet(last) != apps.app_subnet(last - 1)


def test_edge_sits_in_shared_network_and_publishes_only_on_loopback():
    argv = apps.edge_argv(manifest(), LIMITS)
    assert argv[argv.index("--network") + 1] == apps.EDGE_NETWORK
    assert argv[argv.index("-p") + 1] == f"127.0.0.1:18400:{apps.EDGE_PORT_IN}"
    assert argv.count("-v") == 1 and argv[argv.index("-v") + 1].endswith(f"apps_edge.py:{apps.EDGE_IN}:ro")
    assert argv[-4:] == [apps.EDGE_IN, apps.APP_ALIAS, "8000", str(apps.EDGE_PORT_IN)]


def test_create_writes_manifest_beside_the_code_and_gives_each_app_its_own_port(tmp_path):
    docker = FakeDocker()
    ids = iter(["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
    first, err = apps.create(tmp_path, "a", "python a.py", {"a.py": "print(1)"}, request="нарисуй график",
                             limits=LIMITS, run=docker, new_id=lambda: next(ids))
    second, _ = apps.create(tmp_path, "b", "python b.py", {}, limits=LIMITS, run=docker, new_id=lambda: next(ids))
    assert err is None and first.host_port != second.host_port
    saved = json.loads((tmp_path / first.id / apps.MANIFEST).read_text(encoding="utf-8"))
    assert saved["request"] == "нарисуй график" and saved["wanted"] == apps.RUNNING
    assert (tmp_path / first.id / apps.SRC / "a.py").is_file()
    created = [c[-1] for c in docker.calls if c[1:3] == ["network", "create"]]
    assert apps.app_network(first.id) in created and apps.EDGE_NETWORK in created


def test_failed_start_is_one_line_and_leaves_no_containers_or_network(tmp_path):
    docker = FakeDocker(fail=apps.edge_container(APP))
    (tmp_path / APP).mkdir()
    error = apps.start(tmp_path, manifest(), LIMITS, docker)
    assert "не запущено" in error and "\n" not in error
    assert docker.calls[-2][:3] == ["docker", "rm", "-f"]
    assert docker.calls[-1] == ["docker", "network", "rm", apps.app_network(APP)]


def test_revive_starts_only_wanted_and_down(tmp_path):
    for app_id, wanted in (("aaaaaaaaaaaa", apps.RUNNING), ("bbbbbbbbbbbb", apps.STOPPED),
                           ("cccccccccccc", apps.RUNNING)):
        (tmp_path / app_id).mkdir()
        apps.save(tmp_path, manifest(id=app_id, wanted=wanted))
    up = {apps.app_container("cccccccccccc"), apps.edge_container("cccccccccccc")}
    docker = FakeDocker(running=up)
    assert apps.revive(tmp_path, LIMITS, docker) == []
    started = {c[c.index("--name") + 1] for c in docker.calls if c[1:3] == ["run", "-d"]}
    assert started == {apps.app_container("aaaaaaaaaaaa"), apps.edge_container("aaaaaaaaaaaa")}


def test_tampered_or_broken_manifest_never_becomes_an_upstream(tmp_path):
    # приложение не перенаправит прокси шлюза: порт вне диапазона, чужой id, битый файл - не адрес
    bad = {"aaaaaaaaaaaa": json.dumps({**asdict(manifest()), "id": "aaaaaaaaaaaa", "host_port": 3000}),
           "bbbbbbbbbbbb": json.dumps({**asdict(manifest()), "id": "../.."}),
           "cccccccccccc": "{not json"}
    for app_id, text in bad.items():
        (tmp_path / app_id).mkdir()
        (tmp_path / app_id / apps.MANIFEST).write_text(text, encoding="utf-8")
        assert apps.upstream(tmp_path, app_id) is None
    (tmp_path / APP).mkdir()
    apps.save(tmp_path, manifest(wanted=apps.STOPPED))
    found, errors = apps.scan(tmp_path)
    assert [m.id for m in found] == [APP] and len(errors) == 3 and all("\n" not in e for e in errors)
    assert apps.revive(tmp_path, LIMITS, FakeDocker()) == errors           # битые не мешают остальным


@pytest.mark.parametrize("location, expected", [
    ("next.html", "next.html"),
    ("/dir/", f"/apps/{SLUG}/dir/"),
    (f"{BASE}/dir/?a=1", f"/apps/{SLUG}/dir/?a=1"),
    ("http://evil.example/x", None),
    ("//evil.example/x", None),
    ("\\\\evil.example/x", None),
    ("\\/evil.example", None),
    ("http://127.0.0.1:3000/", None),
    (None, None),
])
def test_redirect_stays_inside_the_app(location, expected):
    assert apps.local_location(location, SLUG, BASE) == expected


def test_stopped_app_has_no_upstream(tmp_path):
    (tmp_path / APP).mkdir()
    apps.save(tmp_path, manifest())
    assert apps.local_base(apps.upstream(tmp_path, APP)) == BASE
    docker = FakeDocker()
    (tmp_path / APP / apps.SRC).mkdir()
    (tmp_path / APP / apps.SRC / "app.py").write_text("print(1)", encoding="utf-8")
    apps.stop(tmp_path, manifest(), docker)
    assert apps.upstream(tmp_path, APP) is None and apps.upstream(tmp_path, "../x") is None
    # apps 29: контейнеры и своя сеть убраны, папка с кодом остаётся, запуск снова - с неё
    assert [sandbox.DOCKER, "network", "rm", apps.app_network(APP)] in docker.calls
    assert any(c[1:3] == ["rm", "-f"] and c[3] == apps.app_container(APP) for c in docker.calls)
    assert (tmp_path / APP / apps.SRC / "app.py").exists() and apps.load(tmp_path, APP).wanted == apps.STOPPED


# ─── прокси шлюза ─────────────────────────────────────────────────────────────

class _Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/dir":
            self.send_response(301)
            self.send_header("Location", "/dir/")
            self.end_headers()
            return
        body = b"x" * 5000 if self.path == "/big" else f"<p>page {self.path}</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_gateway_proxies_page_with_sandbox_csp_and_survives_bad_app(tmp_path, monkeypatch, app_client):  # noqa: F811
    from gateway import main
    server = http.server.HTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setenv(apps.PORT_BASE_ENV, str(port))                     # порт тестового сервера - в диапазоне
    (tmp_path / APP).mkdir()
    apps.save(tmp_path, manifest(host_port=port))
    monkeypatch.setattr(apps, "APPS_DIR", tmp_path)
    client, _ = app_client
    page = client.get(f"/apps/{SLUG}/index.html?x=1")
    assert page.status_code == 200 and "page /index.html?x=1" in page.text
    assert page.headers["content-security-policy"] == "sandbox allow-scripts"
    assert client.get("/apps/ffffffffffff/").status_code == 404
    moved = client.get(f"/apps/{SLUG}/dir", follow_redirects=False)
    assert moved.status_code == 301 and moved.headers["location"] == f"/apps/{SLUG}/dir/"
    monkeypatch.setattr(main, "APP_PROXY_MAX_BODY", 10)
    assert client.post(f"/apps/{SLUG}/x", content=b"y" * 11).status_code == 413
    monkeypatch.setattr(main, "APP_PROXY_MAX_BYTES", 1000)
    big = client.get(f"/apps/{SLUG}/big")
    assert big.status_code == 502 and "больше 1000" in big.json()["detail"]
    server.shutdown()
    server.server_close()
    dead = client.get(f"/apps/{SLUG}/")
    assert dead.status_code == 502 and "не отвечает" in dead.json()["detail"]
    # браузер открывает страницу - общая заглушка вместо JSON; API приложения - по-прежнему JSON
    browser = {"Accept": "text/html,application/xhtml+xml"}
    stub = client.get(f"/apps/{SLUG}/", headers=browser)
    assert stub.status_code == 503 and "не отвечает" in stub.text and "<html" in stub.text
    assert stub.headers["content-security-policy"] == "sandbox allow-scripts"
    apps.save(tmp_path, replace(apps.load(tmp_path, APP), wanted=apps.STOPPED))
    paused = client.get(f"/apps/{SLUG}/", headers=browser)
    assert paused.status_code == 503 and "приостановлено" in paused.text
    assert client.get(f"/apps/{SLUG}/").status_code == 404
    assert client.get("/apps/ffffffffffff/", headers=browser).status_code == 404     # чужого приложения нет
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("configured, host, headers, status", [
    ("", "testclient", {}, 200),                          # без SAM_API_KEY - эта машина
    ("", "192.168.1.5", {}, 401),                         # без ключа с другого устройства - закрыто
    ("s3cret", "192.168.1.5", {}, 401),
    ("s3cret", "192.168.1.5", {"X-SAM-Key": "s3cret"}, 200),
], ids=["own_no_key", "foreign_no_key", "foreign_without_key", "foreign_with_key"])
def test_app_page_is_open_exactly_like_the_whole_gateway(tmp_path, monkeypatch, app_client, configured, host,  # noqa: F811
                                                         headers, status):
    """apps 25: страница приложения за прокси шлюза под тем же слоем доступа, что весь шлюз: чужой адрес без ключа
    до контейнера не доходит."""
    from fastapi.testclient import TestClient

    from gateway import access, main
    server = http.server.HTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setenv(apps.PORT_BASE_ENV, str(port))
    (tmp_path / APP).mkdir()
    apps.save(tmp_path, manifest(host_port=port))
    monkeypatch.setattr(apps, "APPS_DIR", tmp_path)
    monkeypatch.setattr(access, "_own_addresses", lambda: frozenset())
    monkeypatch.setenv(access.KEY_ENV, configured)
    try:
        page = TestClient(main.app, client=(host, 50000)).get(f"/apps/{SLUG}/index.html", headers=headers)
    finally:
        server.shutdown()
        server.server_close()
    assert page.status_code == status
    assert ("page /index.html" in page.text) is (status == 200)


# ─── slug и адрес (SAM-205) ───────────────────────────────────────────────────

@pytest.mark.parametrize("name, slug", [
    ("График температуры", "grafik-temperatury"),
    ("Ёж и щука", "ezh-i-shchuka"),
    ("Chart 2.0!", "chart-2-0"),
    ("!!!", apps.SLUG_FALLBACK),
    ("а" * 100, "a" * apps.SLUG_MAX),
])
def test_slug_is_latin_digits_and_dash_from_the_name(name, slug):
    assert apps.slugify(name) == slug and apps.SLUG_RE.match(slug)


def test_taken_or_reserved_slug_gets_a_suffix():
    assert apps.unique_slug("grafik", set()) == "grafik"
    assert apps.unique_slug("grafik", {"grafik", "grafik-2"}) == "grafik-3"
    assert apps.unique_slug(apps.MONITOR_SLUG, set()) == f"{apps.MONITOR_SLUG}-2"
    assert apps.unique_slug(apps.RADAR_SLUG, set()) == f"{apps.RADAR_SLUG}-2"


def test_create_gives_a_unique_slug_and_never_monitor_or_radar(tmp_path):
    ids = iter(["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc", "dddddddddddd"])
    made = [apps.create(tmp_path, name, "c", {}, limits=LIMITS, run=FakeDocker(), new_id=lambda: next(ids))[0]
            for name in ("График", "график", "Monitor", "radar")]
    assert [m.slug for m in made] == ["grafik", "grafik-2", "monitor-2", "radar-2"]
    assert apps.page_path(made[1]) == "/apps/grafik-2/"
    assert json.loads((tmp_path / made[1].id / apps.MANIFEST).read_text(encoding="utf-8"))["slug"] == "grafik-2"


def test_old_manifest_without_slug_gets_one_on_read_and_keeps_it(tmp_path):
    # приложения до SAM-205: в манифесте нет slug; старшее по created получает slug без суффикса
    old = {"bbbbbbbbbbbb": "2026-10-13 10:00", "aaaaaaaaaaaa": "2026-10-14 10:00"}
    for app_id, created in old.items():
        (tmp_path / app_id).mkdir()
        record = {k: v for k, v in asdict(manifest(id=app_id, name="график", created=created)).items() if k != "slug"}
        (tmp_path / app_id / apps.MANIFEST).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    assert apps.load(tmp_path, "aaaaaaaaaaaa").slug == "grafik-2"
    saved = {i: json.loads((tmp_path / i / apps.MANIFEST).read_text(encoding="utf-8"))["slug"] for i in old}
    assert saved == {"bbbbbbbbbbbb": "grafik", "aaaaaaaaaaaa": "grafik-2"}
    assert {m.id: m.slug for m in apps.all_apps(tmp_path)} == saved          # второе чтение slug не меняет
    assert apps.find(tmp_path, "grafik").id == "bbbbbbbbbbbb" and apps.find(tmp_path, "aaaaaaaaaaaa").slug == "grafik-2"
    assert apps.find(tmp_path, "график") is None                              # по имени не ищется


@pytest.mark.parametrize("slug", [apps.MONITOR_SLUG, apps.RADAR_SLUG, "../x", "Grafik", "a--b"])
def test_manifest_with_reserved_or_bad_slug_is_not_read(tmp_path, slug):
    (tmp_path / APP).mkdir()
    apps.save(tmp_path, manifest(slug=slug))
    with pytest.raises(apps.ManifestError):
        apps.load(tmp_path, APP)
    assert apps.upstream(tmp_path, slug) is None


def test_old_id_address_moves_to_the_slug_with_path_and_query(tmp_path, monkeypatch, app_client):  # noqa: F811
    server = http.server.HTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setenv(apps.PORT_BASE_ENV, str(port))
    (tmp_path / APP).mkdir()
    apps.save(tmp_path, manifest(host_port=port))
    monkeypatch.setattr(apps, "APPS_DIR", tmp_path)
    client, _ = app_client
    try:
        moved = client.post(f"/apps/{APP}/save?a=1", content=b"x", follow_redirects=False)
        page = client.get(f"/apps/{APP}/index.html")
    finally:
        server.shutdown()
        server.server_close()
    assert moved.status_code == 308 and moved.headers["location"] == f"/apps/{SLUG}/save?a=1"
    assert page.status_code == 200 and str(page.url).endswith(f"/apps/{SLUG}/index.html")


def test_broken_manifest_write_keeps_the_old_manifest(tmp_path, monkeypatch):
    (tmp_path / APP).mkdir()
    apps.save(tmp_path, manifest())
    before = (tmp_path / APP / apps.MANIFEST).read_text(encoding="utf-8")

    def broken(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(apps.os, "replace", broken)
    with pytest.raises(OSError):
        apps.save(tmp_path, manifest(command="python other.py"))
    assert (tmp_path / APP / apps.MANIFEST).read_text(encoding="utf-8") == before
    assert [p.name for p in (tmp_path / APP).iterdir()] == [apps.MANIFEST]           # временного файла не осталось


def test_file_busy_in_a_neighbour_thread_is_retried_then_reported(monkeypatch):
    monkeypatch.setattr(apps, "SHARING_RETRY_S", 0)
    tries = []

    def busy_twice():
        tries.append(1)
        if len(tries) < 3:
            raise PermissionError("in use")
        return "text"
    assert apps._shared(busy_twice) == "text" and len(tries) == 3

    def always_busy():
        raise PermissionError("in use")
    with pytest.raises(PermissionError):
        apps._shared(always_busy)


def test_manifest_replace_waits_out_a_reader_in_a_neighbour_thread(tmp_path, monkeypatch):
    (tmp_path / APP).mkdir()
    real, busy = apps.os.replace, [PermissionError("read by scan")]

    def replace_once_busy(src, dst):
        if busy:
            raise busy.pop()
        real(src, dst)
    monkeypatch.setattr(apps, "SHARING_RETRY_S", 0)
    monkeypatch.setattr(apps.os, "replace", replace_once_busy)
    apps.save(tmp_path, manifest())
    assert apps.load(tmp_path, APP).command == manifest().command


def test_two_creates_at_once_get_their_own_port_and_slug(tmp_path, monkeypatch):
    # два воркера делают приложение одновременно: оба видят пустой список, если выбор не под замком
    slow_write = sandbox.write_files

    def write_files(src, files):
        time.sleep(0.3)
        return slow_write(src, files)
    monkeypatch.setattr(apps.sandbox, "write_files", write_files)
    ids, made = iter(["aaaaaaaaaaaa", "bbbbbbbbbbbb"]), []
    threads = [threading.Thread(target=lambda: made.append(apps.create(
        tmp_path, "график", "c", {"a": "b"}, limits=LIMITS, run=FakeDocker(), new_id=lambda: next(ids))[0]))
        for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(m.slug for m in made) == ["grafik", "grafik-2"] and made[0].host_port != made[1].host_port


# ─── живой Docker ─────────────────────────────────────────────────────────────

PAGE = "<h1>SAM app</h1>"
PROBE = """
import os, urllib.request
def t(name, url):
    try:
        urllib.request.urlopen(url, timeout=4)
        return name + "=REACHED"
    except urllib.error.HTTPError:
        return name + "=REACHED"
    except Exception:
        return name + "=blocked"
def manifest():
    try:
        open("/work/../app.json", "w").write("{}")
        return "manifest=WRITTEN"
    except OSError:
        return "manifest=protected"
open("probe.txt", "w").write(" ".join([t("internet", "http://1.1.1.1"),
    t("host", "http://host.docker.internal:8000/health"), t("mcp", "http://host.docker.internal:MCP_PORT/health"),
    t("mcp_by_ip", "http://HOST_IP:MCP_PORT/health"), t("neighbor", "http://NEIGHBOR:8000/"),
    "env=" + ("leak" if "SAM_TEST_SECRET" in os.environ else "clean"), manifest()]))
"""
LIVE_WAIT_S = 20
MCP_PORT = int(os.getenv("MCP_PORT", "3000"))   # агрегатор MCP этой машины (mcp/client_agent.py)


@contextlib.contextmanager
def host_listener(port: int):
    """Слушатель на порту MCP хоста на всех адресах: без него «blocked» в пробе ничего не доказывал бы. Порт уже
    занят (стенд запущен) - его и пробуем."""
    try:
        server = http.server.HTTPServer(("0.0.0.0", port), _Page)
    except OSError:
        yield
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def docker_ready():
    try:
        ready = bool(shutil.which("docker")) and subprocess.run(
            ["docker", "image", "inspect", LIMITS.image], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ready = False
    if not ready:
        pytest.skip(f"Docker не запущен или нет образа {LIMITS.image}")


def _get(port: int, path: str = "/") -> str:
    deadline = time.time() + LIVE_WAIT_S
    while True:
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3).read().decode()
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.5)


@pytest.mark.integration
def test_live_app_is_isolated_and_comes_back(docker_ready, tmp_path, monkeypatch):
    monkeypatch.setenv("SAM_TEST_SECRET", "s3cr3t")
    monkeypatch.setenv(apps.PORT_BASE_ENV, "18600")
    serve = "python -m http.server 8000"
    made = []
    try:
        b, err = apps.create(tmp_path, "b", serve, {"index.html": "<p>b</p>"}, limits=LIMITS)
        made.append(b)
        assert err is None, err
        neighbor = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}"
                                   "{{end}}", apps.app_container(b.id)], capture_output=True, text=True).stdout.strip()
        probe = (PROBE.replace("NEIGHBOR", neighbor).replace("MCP_PORT", str(MCP_PORT))
                 .replace("HOST_IP", socket.gethostbyname(socket.gethostname())))
        with host_listener(MCP_PORT):
            a, err = apps.create(tmp_path, "a", f"python probe.py; {serve}", {"index.html": PAGE, "probe.py": probe},
                                 limits=LIMITS)
            made.append(a)
            assert err is None, err
            assert PAGE in _get(a.host_port, "/index.html")
            assert _get(a.host_port, "/probe.txt") == ("internet=blocked host=blocked mcp=blocked mcp_by_ip=blocked "
                                                       "neighbor=blocked env=clean manifest=protected")
        apps._remove(a, apps.docker_run)                          # контейнеры пропали, как после сбоя Docker
        assert apps.revive(tmp_path, LIMITS) == []
        assert PAGE in _get(a.host_port, "/index.html")
    finally:
        for m in made:
            if m:
                apps.stop(tmp_path, m)


DRIP_SECS = 3.0                 # приложение льёт ответ по байту дольше срока прокси, но каждый кусок вовремя
DRIP_STEP_S = 0.1


class _Drip(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        for _ in range(int(DRIP_SECS / DRIP_STEP_S)):
            try:
                self.wfile.write(b".")
                self.wfile.flush()
            except OSError:
                return
            time.sleep(DRIP_STEP_S)

    def log_message(self, *a):
        pass


def test_hanging_app_answers_502_within_the_proxy_deadline(tmp_path, monkeypatch, app_client):  # noqa: F811
    """apps 34: весь ответ приложения - не дольше APP_PROXY_TIMEOUT_S (30 с, в тесте 0,5 с): приложение, которое
    льёт ответ по байту без конца, не держит запрос шлюза - 502 одной строкой. Каждый кусок приходит вовремя, так что
    срок чтения httpx не помог бы."""
    from gateway import main
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setenv(apps.PORT_BASE_ENV, str(port))
    (tmp_path / APP).mkdir()
    apps.save(tmp_path, manifest(host_port=port))
    monkeypatch.setattr(apps, "APPS_DIR", tmp_path)
    assert main.APP_PROXY_TIMEOUT_S == 30
    monkeypatch.setattr(main, "APP_PROXY_TIMEOUT_S", 0.5)
    client, _ = app_client
    started = time.monotonic()
    page = client.get(f"/apps/{APP}/slow")
    took = time.monotonic() - started
    server.shutdown()
    server.server_close()

    assert page.status_code == 502 and "не отвечает" in page.json()["detail"]
    assert took < DRIP_SECS / 2


# ─── фаза 5: P3 ───────────────────────────────────────────────────────────────

def test_start_removes_old_containers_and_network_before_creating_them(tmp_path):
    """apps 27: apps.start перед стартом убирает старые контейнеры и сеть приложения - имя, занятое прошлым
    запуском, не роняет старт."""
    docker = FakeDocker()
    (tmp_path / APP / apps.SRC).mkdir(parents=True)
    assert apps.start(tmp_path, manifest(), LIMITS, docker) is None
    rm = docker.calls.index([sandbox.DOCKER, "rm", "-f", apps.app_container(APP), apps.edge_container(APP)])
    net_rm = docker.calls.index([sandbox.DOCKER, "network", "rm", apps.app_network(APP)])
    created = next(i for i, c in enumerate(docker.calls) if c[1:3] == ["network", "create"] and
                   c[-1] == apps.app_network(APP))
    first_run = next(i for i, c in enumerate(docker.calls) if c[1:3] == ["run", "-d"])
    assert rm < net_rm < created < first_run


def test_port_range_comes_from_settings_and_a_full_range_refuses_a_new_app(tmp_path, monkeypatch):
    """apps 5: диапазон портов - SAM_APPS_PORT_BASE и SAM_APPS_PORT_SPAN; кончился - отказ одной строкой, папки
    нового приложения не остаётся."""
    monkeypatch.setenv(apps.PORT_BASE_ENV, "18400")
    monkeypatch.setenv(apps.PORT_SPAN_ENV, "2")
    ids = iter(["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"])
    made = [apps.create(tmp_path, n, "python a.py", {}, limits=LIMITS, run=FakeDocker(), new_id=lambda: next(ids))
            for n in ("a", "b", "c")]
    assert [m.host_port for m, _ in made[:2]] == [18400, 18401]
    assert made[2][0] is None and "SAM_APPS_PORT_SPAN" in made[2][1] and "\n" not in made[2][1]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]


def test_monitor_stands_in_the_frame_and_the_old_panel_link_still_works(app_client):  # noqa: F811
    """apps 51: монитор - в каркасе SAM, ссылка «Приложения» - в его полосе (/shell/config.js, tests/test_shell.py);
    старый адрес /apps-panel шлюза ведёт на страницу приложений самого шлюза (SAM-204)."""
    from pathlib import Path

    from gateway import apps_api
    client, _ = app_client
    monitor = (Path(apps.__file__).parent / "static" / "monitor.html").read_text(encoding="utf-8")
    assert '<script src="/static/shell/shell.js" defer data-page="monitor" data-fill="1"' in monitor
    assert 'id="main"' not in monitor                    # #main - рамка каркаса; у монитора своё #mon-main
    r = client.get("/apps-panel", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == apps_api.PAGE_PATH


class _NetDocker(FakeDocker):
    """Фейк с чужой сетью приложения в той же подсети."""

    def __init__(self, other: str, subnet: str) -> None:
        super().__init__()
        self.other, self.subnet = other, subnet

    def __call__(self, argv: list[str]) -> apps.DockerResult:
        if argv[1:3] == ["network", "ls"]:
            self.calls.append(argv)
            return apps.DockerResult(0, apps.app_network(self.other) + "\n", "")
        if argv[1:3] == ["network", "inspect"] and argv[3] == apps.app_network(self.other):
            self.calls.append(argv)
            return apps.DockerResult(0, self.subnet + "\n", "")
        return super().__call__(argv)


def test_start_removes_an_orphan_holding_its_subnet_but_not_a_neighbor_with_a_manifest(tmp_path):
    m = manifest()
    (tmp_path / APP).mkdir()
    apps.save(tmp_path, m)
    orphan = "ffffffffffff"
    docker = _NetDocker(orphan, apps.app_subnet(m.host_port))
    assert apps.start(tmp_path, m, LIMITS, docker) is None
    assert [sandbox.DOCKER, "rm", "-f", apps.app_container(orphan), apps.edge_container(orphan)] in docker.calls
    assert [sandbox.DOCKER, "network", "rm", apps.app_network(orphan)] in docker.calls     # сирота без манифеста убрана
    (tmp_path / orphan).mkdir()
    apps.save(tmp_path, manifest(id=orphan, slug="other", host_port=m.host_port))
    docker = _NetDocker(orphan, apps.app_subnet(m.host_port))
    error = apps.start(tmp_path, m, LIMITS, docker)
    assert error and orphan in error and "подсеть" in error                              # сосед с манифестом не тронут
    assert [sandbox.DOCKER, "network", "rm", apps.app_network(orphan)] not in docker.calls



def test_app_data_folder_is_made_on_start_and_goes_with_delete(tmp_path):
    """SAM-181: папка данных создаётся при запуске (до монтирования, иначе docker создал бы её от root), лежит рядом
    с кодом, а не в нём - обмен кода её не трогает; удаление приложения уносит её вместе с папкой."""
    docker = FakeDocker()
    (tmp_path / APP / apps.SRC).mkdir(parents=True)
    assert apps.start(tmp_path, manifest(), LIMITS, docker) is None
    data = tmp_path / APP / apps.DATA
    assert data.is_dir() and data.parent == (tmp_path / APP / apps.SRC).parent
    (data / "test-do-not-use.json").write_text("{}", encoding="utf-8")
    assert apps.delete(tmp_path, manifest(), docker) is None and not (tmp_path / APP).exists()
