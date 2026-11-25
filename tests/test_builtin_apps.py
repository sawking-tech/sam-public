"""Встроенные приложения монитор и радар (шаг 1 переноса, SAM-199): манифесты, ручки радара из его OpenAPI, адреса,
панель. OpenAPI радара - настоящий (presence_api на выдуманном плане), HTTP к нему подменён."""
import asyncio
import json
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from gateway import apps, builtin_apps, restart
from radar import presence_api
from gateway.tools import apps as tool
from tests.fakes_core import app_client  # noqa: F401  (фикстура)
from tests.test_apps import FakeDocker
from tests.test_apps_api import stand
from tests.test_mcp_aggregator import agg  # noqa: F401  (фикстура)
from tests.test_presence_where import TELL_LAYOUT, hub_at

pytestmark = pytest.mark.usefixtures("radar_on")

RADAR_TOOLS = ["where", "history", "layout", "tell", "devices", "furniture"]
# Команды заданы явно: по умолчанию остановка и перезапуск радара - скрипты стенда на Windows, а на Linux
# их нет, и тест проверял бы powershell, а не радар. Что именно стоит в командах - дело теста restart.
ENV = {"SAM_RADAR": "1", "SAM_SERVER": "http://localhost:8123", "SAM_RADAR_URL": "http://127.0.0.1:8099",
       "SAM_APPS_STOP_CMD_RADAR": "radar-stop", "SAM_RESTART_CMD_RADAR": "radar-restart",
       "SAM_RESTART_CMD_MCP": "mcp-restart"}   # перезапуск агрегатора после снятия - тоже свой
RADAR_BASE = "http://127.0.0.1:8099"
RADAR_SPEC = TestClient(presence_api.create_app(hub_at(TELL_LAYOUT))).get("/openapi.json").json()


class RadarHttp:
    """HTTP шлюза к радару: OpenAPI настоящего сервиса радара, ответ ручки - заданный; запросы копятся."""
    def __init__(self, answer="[]", status=200):
        self.answer, self.status, self.calls = answer, status, []

    def __call__(self, method, url, query, body, timeout=None, headers=None):
        if url == f"{RADAR_BASE}/openapi.json":
            return 200, json.dumps(RADAR_SPEC)
        if not url.startswith(RADAR_BASE):
            return 404, "not found"
        self.calls.append((method, url, query, json.loads(body) if body else None, headers))
        return self.status, self.answer


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(tool, "_openapi_cache", {})


def shipped():
    return builtin_apps.defaults(ENV)


def builtin(slug):
    return {b.slug: b for b in shipped()}[slug]


class FakeRun:
    def __init__(self, code=0, last=""):
        self.calls, self.code, self.last = [], code, last

    async def __call__(self, command, log):
        self.calls.append(command)
        return self.code, self.last


def test_radar_tools_come_from_its_openapi_by_the_mark():
    radar = {b.slug: b for b in shipped()}[apps.RADAR_SLUG]
    http = RadarHttp()
    tools = tool.builtin_tools(radar, http)
    assert [t["name"] for t in tools] == RADAR_TOOLS and all(t["description"] for t in tools)
    assert radar.control == restart.RADAR and tool.builtin_tools(builtin("monitor"), http) == []   # у монитора ручек нет


def test_defaults_take_addresses_from_the_stand_settings():
    page, monitor, radar = shipped()
    assert (monitor.slug, monitor.url, monitor.health) == ("monitor", "http://localhost:8123/monitor",
                                                          "http://localhost:8123/health")
    # страница приложений - страница шлюза, его health check; не удаляется (SAM-204)
    assert (page.slug, page.url, page.health) == ("apps", "http://localhost:8123/apps", "http://localhost:8123/health")
    assert page.control == "" and page.removable is False
    assert (radar.slug, radar.url, radar.health) == ("radar", "http://127.0.0.1:8099/", "http://127.0.0.1:8099/health")
    assert monitor.control == ""
    assert {b.slug for b in shipped()} == apps.RESERVED_SLUGS


def test_ensure_writes_missing_and_drifted_manifests_and_keeps_equal_ones(tmp_path):
    assert builtin_apps.ensure(tmp_path, shipped()) == []
    path = tmp_path / "radar" / apps.MANIFEST
    assert "functions" not in json.loads(path.read_text(encoding="utf-8"))          # ручки живут в сервисе радара
    legacy = {**json.loads(path.read_text(encoding="utf-8")), "url": "http://x/", "functions": [], "mcp_server": "radar"}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert builtin_apps.find(tmp_path, "radar").url == "http://x/"                   # манифест прошлой версии читается
    monitor = tmp_path / "monitor" / apps.MANIFEST
    stamp = monitor.stat().st_mtime_ns
    builtin_apps.ensure(tmp_path, shipped())
    assert builtin_apps.find(tmp_path, "radar").url == "http://127.0.0.1:8099/"       # разошедшийся переписан
    assert monitor.stat().st_mtime_ns == stamp                                         # совпадающий не тронут


def test_builtins_are_not_container_apps_and_a_broken_one_is_one_line(tmp_path):
    builtin_apps.ensure(tmp_path, shipped())
    assert apps.scan(tmp_path) == ([], [])
    assert [b.slug for b in builtin_apps.scan(tmp_path)[0]] == ["apps", "monitor", "radar"]
    (tmp_path / "radar" / apps.MANIFEST).write_text("{broken", encoding="utf-8")
    (tmp_path / "monitor" / apps.MANIFEST).write_text(json.dumps({**asdict(builtin("monitor")), "kind": "container"}),
                                                       encoding="utf-8")
    found, errors = builtin_apps.scan(tmp_path)
    assert [b.slug for b in found] == ["apps"] and len(errors) == 2 and all("\n" not in e for e in errors)


@pytest.mark.parametrize("url, host, expected", [
    ("http://127.0.0.1:8010/", "192.168.1.5", "http://192.168.1.5:8010/"),
    ("http://localhost:8000/monitor", "sam.local", "http://sam.local:8000/monitor"),
    ("http://10.0.0.2:8010/", "192.168.1.5", "http://10.0.0.2:8010/"),
    ("http://127.0.0.1:8010/", None, "http://127.0.0.1:8010/"),
])
def test_loopback_address_follows_the_host_of_the_request(url, host, expected):
    assert builtin_apps.for_host(url, host) == expected


def test_start_and_stop_commands_of_the_builtins():
    monitor, radar = builtin("monitor"), builtin("radar")
    assert builtin_apps.command(monitor, "start", {}, True)[1].startswith("Монитор - часть шлюза")
    on = {"SAM_RADAR": "1"}                     # при выключенном радара нет и среди компонентов стенда
    start, _ = builtin_apps.command(radar, "start", on, True)          # по умолчанию - скрипт стенда
    stop, _ = builtin_apps.command(radar, "stop", on, True)
    assert str(restart.RADAR_SCRIPT) in start and not start.endswith("-Stop")
    assert str(restart.RADAR_SCRIPT) in stop and stop.endswith(builtin_apps.RADAR_STOP_SWITCH)
    assert builtin_apps.command(radar, "stop", {}, False) == ("", "Радар: остановка здесь не настроена: "
                                                                  "SAM_APPS_STOP_CMD_RADAR")
    on = {"SAM_RADAR": "1"}                        # выключенного радара среди компонентов нет (gateway/radar_switch.py)
    assert builtin_apps.command(radar, "stop", {**on, "SAM_APPS_STOP_CMD_RADAR": "systemctl stop radar"}, False)[0] == \
        "systemctl stop radar"
    assert builtin_apps.command(radar, "start", {**on, "SAM_RESTART_CMD_RADAR": "systemctl restart radar"}, False)[0] == \
        "systemctl restart radar"
    assert builtin_apps.command(radar, "start", {"SAM_RESTART_CMD_RADAR": "systemctl restart radar"}, False)[1] \
        .startswith("нет компонента")


@pytest.mark.asyncio
async def test_control_runs_the_command_and_says_failure_in_one_line():
    radar = builtin("radar")
    run = FakeRun()
    assert await builtin_apps.control(radar, "stop", run, {}, True) is None and run.calls[0].endswith("-Stop")
    assert "вернула 1: boom" in await builtin_apps.control(radar, "start", FakeRun(1, "boom"), ENV, True)
    assert "не кончилась" in await builtin_apps.control(radar, "start", FakeRun(None), ENV, True)
    nothing = FakeRun()
    for fixed in ("apps", "monitor"):
        assert "часть шлюза" in await builtin_apps.control(builtin(fixed), "stop", nothing, {}, True)
    assert not nothing.calls


def test_gateway_sends_the_monitor_to_its_page_and_forwards_the_radar(tmp_path, monkeypatch, app_client):  # noqa: F811
    client, _ = app_client
    monkeypatch.setattr(apps, "APPS_DIR", tmp_path / "empty")   # старт шлюза пишет манифесты в APPS_DIR фикстуры
    assert client.get("/apps/radar/", follow_redirects=False).status_code == 404         # манифеста нет
    monkeypatch.setattr(apps, "APPS_DIR", tmp_path / "shipped")
    builtin_apps.ensure(tmp_path / "shipped", shipped())
    radar = client.get("/apps/radar/", follow_redirects=False)       # радар шлюз пересылает; процесса на 8099 нет
    assert radar.status_code == 502 and "Радар не отвечает" in radar.text
    monitor = client.get("/apps/monitor/", follow_redirects=False)
    assert monitor.status_code == 307 and monitor.headers["location"] == "http://testserver:8123/monitor"


def test_app_list_shows_builtins_first_with_radar_tools(tmp_path):
    builtin_apps.ensure(tmp_path, shipped())
    tool.app_make_impl({"name": "график", "files": {"index.html": "x"}, "command": "c"}, tmp_path, [].append,
                       FakeDocker())
    lines = tool.app_list_impl({}, tmp_path, health=lambda url: "8099" in url, http=RadarHttp()).splitlines()
    assert lines[0] == "apps Приложения - встроенное, не отвечает, /apps"            # адрес страницы на шлюзе
    assert lines[1] == "monitor Монитор - встроенное, не отвечает, /monitor"
    assert lines[2].startswith("radar Радар - встроенное, работает, /apps/radar/; ручки app_call: where(person) - ")
    assert "; history(person, at) - " in lines[2] and "; tell(person, place, pose) - " in lines[2]
    assert lines[3].startswith("grafik ")
    down = tool.app_list_impl({}, tmp_path, health=lambda url: False, http=RadarHttp()).splitlines()
    assert down[2] == "radar Радар - встроенное, не отвечает, /apps/radar/"          # без ручек и без падения


def test_app_call_goes_to_the_radar_address_with_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SAM_API_KEY", "k-test")
    builtin_apps.ensure(tmp_path, shipped())
    http = RadarHttp('[{"person": "first", "room": "зал"}]')
    out = tool.app_call_impl({"app": "radar", "tool": "where", "args": {"person": "Первая"}}, tmp_path, http)
    assert out == '[{"person": "first", "room": "зал"}]'
    assert http.calls == [("GET", f"{RADAR_BASE}/sam/tools/where", {"person": "Первая"}, None, {"x-sam-key": "k-test"})]
    tool.app_call_impl({"app": "radar", "tool": "tell", "args": {"person": "Первая", "place": "кухня"}}, tmp_path, http)
    assert http.calls[-1][:4] == ("POST", f"{RADAR_BASE}/sam/tools/tell", {}, {"person": "Первая", "place": "кухня"})


def test_app_call_to_radar_refuses_in_one_line(tmp_path):
    builtin_apps.ensure(tmp_path, shipped())
    assert "нет ручки 'flow'" in tool.app_call_impl({"app": "radar", "tool": "flow"}, tmp_path, RadarHttp())
    refused = tool.app_call_impl({"app": "radar", "tool": "where", "args": {"x": 1}}, tmp_path, RadarHttp())
    assert refused == "у ручки where нет аргументов x; объявлены: person"
    off = RadarHttp("радар выключен: включи SAM_PRESENCE в .env", status=409)
    assert tool.app_call_impl({"app": "radar", "tool": "layout"}, tmp_path, off).startswith("ручка ответила 409: радар выключен")

    def down(*args, **kwargs):
        raise OSError("connection refused")
    assert "не ответила: OSError" in tool.app_call_impl({"app": "radar", "tool": "where"}, tmp_path, down)
    tool._openapi_cache.clear()                                        # обрыв не кэшируется, а описание кэшируется
    assert "restart_component radar" in tool.app_call_impl({"app": "radar", "tool": "where"}, tmp_path, down)
    assert "ручек для SAM нет" in tool.app_call_impl({"app": "monitor", "tool": "x"}, tmp_path, RadarHttp())


def test_app_tools_do_not_touch_builtins(tmp_path):
    builtin_apps.ensure(tmp_path, shipped())
    docker = FakeDocker()
    assert "restart_component radar" in tool.app_stop_impl({"app": "radar"}, tmp_path, docker)
    assert "часть шлюза" in tool.app_update_impl({"app": "monitor", "files": {"a": "b"}}, tmp_path, [].append, docker)
    assert docker.calls == [] and not (tmp_path / "monitor" / apps.SRC).exists()


def panel(tmp_path, monkeypatch, run=None):
    for name, value in ENV.items():          # поставляемые манифесты пишет шлюз при старте, по настройкам
        monkeypatch.setenv(name, value)
    builtin_apps.ensure(tmp_path)
    return stand(tmp_path, FakeDocker(), builtin_run=run or FakeRun(), http=RadarHttp())


def test_panel_lists_builtins_first_and_never_deletes_the_monitor(tmp_path, monkeypatch):
    run = FakeRun()
    client = panel(tmp_path, monkeypatch, run)
    data = client.get("/sam/apps").json()
    cards = data["apps"]
    assert [(c["slug"], c["builtin"], c["pinned"]) for c in cards] == [("apps", True, True), ("monitor", True, True),
                                                                       ("radar", True, True)]
    assert cards[2]["url"] == "/apps/radar/" and cards[2]["functions"][0]["name"] == "where"   # по slug, не по порту
    assert data["removed"] == []
    for body in ({}, {"confirm": "monitor"}):
        assert client.post("/sam/apps/monitor/delete", json=body).status_code == 400
    assert (tmp_path / "monitor" / apps.MANIFEST).exists()
    out = client.post("/sam/apps/radar/stop", json={}).json()
    assert out == {"ok": True, "text": "Радар: приостановлено"} and len(run.calls) == 1
    for fixed in ("monitor", "apps"):                                 # страницы шлюза отдельно не стоят
        assert client.post(f"/sam/apps/{fixed}/stop", json={}).json()["ok"] is False and len(run.calls) == 1
    assert client.post("/sam/apps/radar/pin", json={}).status_code == 400


def test_radar_is_removed_with_its_slug_and_comes_back(tmp_path, monkeypatch, agg):  # noqa: F811
    run = FakeRun()
    client = panel(tmp_path, monkeypatch, run)
    assert client.post("/sam/apps/radar/delete", json={"confirm": "monitor"}).status_code == 400
    out = client.post("/sam/apps/radar/delete", json={"confirm": "radar"}).json()
    assert out["ok"] and "данные остались" in out["text"]
    assert run.calls[0] == ENV["SAM_APPS_STOP_CMD_RADAR"]            # остановка - своя команда, не powershell
    assert run.calls[1] == ENV["SAM_RESTART_CMD_MCP"]                # агрегатор перечитывает метки
    assert not (tmp_path / "radar" / apps.MANIFEST).exists() and builtin_apps.removed(tmp_path) == ["radar"]
    builtin_apps.ensure(tmp_path, shipped())                                    # снятое не ставится снова само
    assert builtin_apps.find(tmp_path, "radar") is None
    assert client.get("/sam/apps").json()["removed"] == ["radar"]
    monkeypatch.setattr(agg, "APPS_DIR", str(tmp_path))
    started = []

    async def start_server(cfg):
        started.append(cfg["name"])
    config = tmp_path / "local.json"
    config.write_text(json.dumps({"servers": [{"name": "radar"}, {"name": "control"}]}), encoding="utf-8")
    pool = agg.MCPAggregator()
    monkeypatch.setattr(pool, "_start_server", start_server)
    asyncio.run(pool.start(str(config)))
    assert started == ["control"]                                    # функции radar_* у SAM не поднимаются
    assert client.post("/sam/apps/radar/install", json={}).json()["ok"]
    assert builtin_apps.find(tmp_path, "radar") is not None and builtin_apps.removed(tmp_path) == []
    assert not agg.removed_app("radar")


def test_monitor_stays_even_with_a_removed_mark(tmp_path):
    assert builtin_apps.remove(tmp_path, "monitor").startswith("monitor не удаляется")
    assert builtin_apps.remove(tmp_path, "apps").startswith("apps не удаляется")
    (tmp_path / "monitor").mkdir()
    (tmp_path / "monitor" / builtin_apps.REMOVED).write_text("", encoding="utf-8")        # руками
    builtin_apps.ensure(tmp_path, shipped())
    assert builtin_apps.find(tmp_path, "monitor") is not None and builtin_apps.removed(tmp_path) == []
