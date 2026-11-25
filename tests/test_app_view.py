"""Живое приложение в каркасе SAM: страница /apps/<slug>, фрейм с CSP sandbox, переопределение каркаса."""
import pytest

from gateway import app_view, apps, builtin_apps
from tests.fakes_core import app_client  # noqa: F401  (фикстура)
from tests.test_builtin_apps import ENV

APP = "aaaaaaaaaaaa"


def shipped_builtins(root, monkeypatch) -> None:
    """Манифесты встроенных с адресами ENV: старт шлюза пишет их из окружения до выхода из старта, а ENV
    задаётся позже - здешняя запись перекрывает его адреса и остаётся последней (окружение стенда в тестах - 8099)."""
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    builtin_apps.ensure(root, builtin_apps.defaults(ENV))


def manifest(**kw) -> apps.Manifest:
    return apps.Manifest(**{"id": APP, "name": "График", "command": "c", "host_port": 18400, "slug": "grafik",
                            "request": "сделай график", **kw})


def test_page_frames_the_app_and_takes_the_frame_from_the_gateway():
    page = app_view.render(manifest())
    assert '<iframe id="app" src="/apps/grafik/" sandbox="allow-scripts" title="График">' in page
    assert '<script src="/static/shell/shell.js" defer data-page="grafik" data-fill="1" data-maxw="4000" ></script>' \
        in page
    assert '"request": "сделай график"' in page and "__" not in page.replace("__proto__", "")


@pytest.mark.parametrize("shell, attrs", [
    (True, ""), (False, 'data-shell="off"'), ({"off": True}, 'data-shell="off"'),
    ({"bar": False, "panelw": 360}, 'data-bar="off" data-panelw="360"'), ({"panelw": "360px"}, ""),
])
def test_manifest_shell_becomes_only_the_shell_attributes(shell, attrs):
    assert app_view.shell_attrs(shell) == attrs
    assert f'data-maxw="4000" {attrs}></script>' in app_view.render(manifest(shell=shell))


def test_names_from_the_app_cannot_break_out_of_the_page():
    page = app_view.render(manifest(name="x</script><img src=x onerror=alert(1)>__INFO__",
                                    request="</script><script>alert(2)</script>"))
    assert "<img" not in page and "<script>alert" not in page
    assert page.count("<script") == page.count("</script>")
    assert "x&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;__INFO__" in page        # подставлено один раз


@pytest.mark.usefixtures("radar_on")
def test_gateway_serves_the_view_by_slug_and_moves_id_and_builtins(tmp_path, app_client, monkeypatch):  # noqa: F811
    client, _ = app_client                                   # APPS_DIR фикстуры - tmp_path
    (tmp_path / APP).mkdir()
    apps.save(tmp_path, manifest(shell=False))
    page = client.get("/apps/grafik")
    assert page.status_code == 200 and 'src="/apps/grafik/"' in page.text and 'data-shell="off"' in page.text
    moved = client.get(f"/apps/{APP}", follow_redirects=False)
    assert moved.status_code == 308 and moved.headers["location"] == "/apps/grafik"
    shipped_builtins(tmp_path, monkeypatch)
    radar = client.get("/apps/radar", follow_redirects=False)
    assert radar.status_code == 307 and radar.headers["location"] == "/apps/radar/"       # радар - по адресу шлюза
    monitor = client.get("/apps/monitor", follow_redirects=False)
    assert monitor.status_code == 307 and monitor.headers["location"].endswith("/monitor")
    assert client.get("/apps/nothing").status_code == 404


@pytest.mark.usefixtures("radar_on")
def test_gateway_forwards_the_radar_to_its_process_without_the_app_sandbox(tmp_path, app_client, monkeypatch):  # noqa: F811
    import httpx

    from gateway import builtin_proxy
    client, _ = app_client
    shipped_builtins(tmp_path, monkeypatch)
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            return httpx.Response(405)                       # как у радара: маршруты FastAPI без HEAD
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True}, headers={"set-cookie": "x=1"})

    monkeypatch.setattr(builtin_proxy, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    res = client.get("/apps/radar/sam/presence/real?x=1")
    assert res.status_code == 200 and res.json() == {"ok": True}
    assert seen == ["http://127.0.0.1:8099/sam/presence/real?x=1"]
    seen.clear()
    assert "content-security-policy" not in res.headers and "set-cookie" not in res.headers   # свой origin, cookie радара не проходят
    assert client.head("/apps/radar/").status_code == 200
    moved = client.get("/apps/radar?view=1", follow_redirects=False)
    assert moved.status_code == 307 and moved.headers["location"] == "/apps/radar/?view=1"     # параметры радара не теряются
    big = client.post("/apps/radar/sam/presence/screenshot", content=b"x", headers={"content-length": str(10 ** 9)})
    assert big.status_code == 413


@pytest.mark.usefixtures("radar_on")
def test_gateway_presents_its_key_and_host_to_the_radar_and_keeps_the_page_key(tmp_path, app_client, monkeypatch):  # noqa: F811
    import httpx

    from gateway import access, builtin_proxy
    client, _ = app_client
    shipped_builtins(tmp_path, monkeypatch)
    monkeypatch.setenv(access.KEY_ENV, "k3y")
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(builtin_proxy, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    res = client.get("/apps/radar/?key=k3y&view=1", headers={"host": "localhost:8000"})
    assert res.status_code == 200 and f"{access.COOKIE}=k3y" in res.headers.get("set-cookie", "")   # файлы страницы - по cookie
    got = seen[0]
    assert str(got.url) == "http://127.0.0.1:8099/?view=1"                     # ключ из адреса дальше шлюза не идёт
    assert got.headers[access.HEADER] == "k3y" and got.headers["host"] == "localhost:8000"


def test_gateway_websocket_is_only_for_a_builtin_with_its_process(tmp_path, app_client):  # noqa: F811
    from starlette.websockets import WebSocketDisconnect
    client, _ = app_client
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/apps/grafik/ws"):
            pass
    assert closed.value.code == 4404
