"""Настройки, закрепление и опции каркаса живого приложения: манифест, панель, инструменты, страница."""
import http.server
import json
import threading

import pytest

from gateway import app_settings, apps, shell_config
from gateway.tools import apps as tool
from tests.fakes_core import app_client  # noqa: F401  (фикстура)
from tests.test_apps import FakeDocker, _Page
from tests.test_apps_api import stand

FIELDS = [{"name": "color", "type": "choice", "options": ["red", "blue"], "default": "red", "label": "Цвет"},
          {"name": "days", "type": "number", "default": 7, "label": "Дней"},
          {"name": "title", "type": "text", "default": "График"},
          {"name": "dark", "type": "bool", "default": False}]


def make(tmp_path, docker=None, events=None, **kw):
    params = {"name": "график", "files": {"index.html": "x"}, "command": "c", **kw}
    return tool.app_make_impl(params, tmp_path, (events if events is not None else []).append, docker or FakeDocker())


def test_declared_fields_are_kept_and_broken_ones_dropped():
    broken = [{"name": "Bad Name", "type": "text"}, {"name": "x", "type": "date"}, {"name": "c", "type": "choice"},
              "nope"]
    assert [f["name"] for f in app_settings.fields(FIELDS + broken)] == ["color", "days", "title", "dark"]
    assert app_settings.fields([{"name": "n", "type": "number", "default": "seven"}])[0]["default"] == 0


def test_values_take_saved_ones_that_fit_and_defaults_for_the_rest():
    saved = {"color": "blue", "days": "много", "extra": 1}
    assert app_settings.values(FIELDS, saved) == {"color": "blue", "days": 7, "title": "График", "dark": False}


@pytest.mark.parametrize("incoming, error", [
    ({"color": "green"}, "Цвет: одно из red, blue"),
    ({"days": True}, "Дней: нужно число"),
    ({"dark": "yes"}, "dark: да или нет"),
    ({"size": 1}, "таких настроек нет: size"),
    ("x", "настройки - объект {имя: значение}"),
])
def test_wrong_values_are_refused_in_one_line(incoming, error):
    assert app_settings.merge(FIELDS, {}, incoming) == (None, error)


@pytest.mark.parametrize("shell, options", [
    (True, {"off": False, "bar": True, "panelw": None}),
    (False, {"off": True, "bar": True, "panelw": None}),
    ({"off": True}, {"off": True, "bar": True, "panelw": None}),
    ({"bar": False, "panelw": 360}, {"off": False, "bar": False, "panelw": 360}),
    ({"panelw": 5000, "dom": "x"}, {"off": False, "bar": True, "panelw": None}),
])
def test_shell_options_of_an_app(shell, options):
    assert app_settings.shell_options(shell) == options


def test_app_make_declares_settings_and_shell_and_app_update_pins_without_restart(tmp_path):
    docker = FakeDocker()
    make(tmp_path, docker, settings=FIELDS, shell={"bar": False})
    m = apps.find(tmp_path, "grafik")
    assert [f["name"] for f in m.settings] == ["color", "days", "title", "dark"] and m.shell == {"bar": False}
    runs = len(docker.calls)
    assert tool.app_update_impl({"app": "grafik", "pinned": True}, tmp_path, [].append, docker) == \
        "Приложение график (grafik) закреплено"
    assert apps.find(tmp_path, "grafik").pinned and len(docker.calls) == runs
    assert "pinned - true или false" in tool.app_update_impl({"app": "grafik", "pinned": "да"}, tmp_path, [].append,
                                                             docker)
    assert "settings - список" in make(tmp_path, docker, settings=[{"name": "Bad", "type": "text"}])


def test_app_make_and_app_update_draw_the_logo_without_restart(tmp_path):
    """Лого плитки (SAM-204): SVG от SAM в манифесте; смена лого не перезапускает приложение, чужое - отказ."""
    docker = FakeDocker()
    logo = '<svg viewBox="0 0 24 24"><path d="M3 20h18"/></svg>'
    make(tmp_path, docker, icon=logo)
    assert apps.find(tmp_path, "grafik").icon == logo
    runs = len(docker.calls)
    assert tool.app_update_impl({"app": "grafik", "icon": ""}, tmp_path, [].append, docker).endswith("обновлено")
    assert apps.find(tmp_path, "grafik").icon == "" and len(docker.calls) == runs
    refused = tool.app_update_impl({"app": "grafik", "icon": "<script>alert(1)</script>"}, tmp_path, [].append, docker)
    assert "разметка SVG" in refused and apps.find(tmp_path, "grafik").icon == ""
    assert "разметка SVG" in make(tmp_path, docker, icon="logo.png")


def test_pinned_apps_follow_the_builtins_in_the_nav_newest_first(tmp_path):
    for app_id, created, pinned in (("aaaaaaaaaaaa", "2026-10-13 10:00", True), ("bbbbbbbbbbbb", "2026-10-14 10:00", True),
                                    ("cccccccccccc", "2026-10-15 10:00", False)):
        (tmp_path / app_id).mkdir()
        apps.save(tmp_path, apps.Manifest(id=app_id, name=app_id[0], command="c", host_port=18400, created=created,
                                          slug=app_id[0], pinned=pinned))
    items = shell_config.nav(tmp_path, 8000)
    assert [i["key"] for i in items] == ["apps", "monitor", "b", "a"]
    assert items[2] == {"key": "b", "name": "b", "port": 8000, "path": "/apps/b", "pinned": True}


def panel(tmp_path):
    make(tmp_path, settings=FIELDS)
    docker = FakeDocker()
    return stand(tmp_path, docker), docker


def test_panel_pins_and_saves_settings_with_a_restart(tmp_path):
    client, docker = panel(tmp_path)
    assert client.post("/sam/apps/grafik/pin", json={}).json()["text"] == "график: закреплено"
    assert apps.find(tmp_path, "grafik").pinned
    client.post("/sam/apps/grafik/unpin", json={})
    assert not apps.find(tmp_path, "grafik").pinned
    got = client.get("/sam/apps/grafik/settings").json()
    assert got["values"] == {"color": "red", "days": 7, "title": "График", "dark": False}
    out = client.post("/sam/apps/grafik/settings", json={"values": {"color": "blue", "days": 30}}).json()
    assert out["ok"] and out["values"]["color"] == "blue"
    m = apps.find(tmp_path, "grafik")
    assert json.loads((tmp_path / m.id / app_settings.SETTINGS_FILE).read_text(encoding="utf-8"))["days"] == 30
    assert any(c[1:3] == ["run", "-d"] for c in docker.calls)                    # перезапуск с новыми
    bad = client.post("/sam/apps/grafik/settings", json={"values": {"color": "green"}})
    assert bad.status_code == 400 and "одно из" in bad.json()["detail"]
    assert app_settings.load(tmp_path, m)["color"] == "blue"


def test_page_reads_its_settings_from_the_gateway_not_the_container(tmp_path, monkeypatch, app_client):  # noqa: F811
    server = http.server.HTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setenv(apps.PORT_BASE_ENV, str(port))
    (tmp_path / "aaaaaaaaaaaa").mkdir()
    m = apps.Manifest(id="aaaaaaaaaaaa", name="график", command="c", host_port=port, slug="grafik", settings=FIELDS)
    apps.save(tmp_path, m)
    app_settings.save(tmp_path, m, {"days": 3})
    monkeypatch.setattr(apps, "APPS_DIR", tmp_path)
    client, _ = app_client
    try:
        got = client.get(f"/apps/grafik/{app_settings.PAGE_PATH}")
    finally:
        server.shutdown()
        server.server_close()
    assert got.status_code == 200 and got.json()["days"] == 3
    assert got.headers["content-security-policy"] == "sandbox allow-scripts"
