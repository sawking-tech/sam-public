"""Приложения в API стенда (SAM-204): страница «Приложения» на шлюзе, список и действия без Docker, защита от простого
POST и чужой страницы. Роутер собирается с доступом шлюза (AccessMiddleware), как в gateway/main.py."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import access, apps, apps_api, builtin_apps
from tests.fakes_core import app_client  # noqa: F401  (фикстура)
from tests.test_apps import FakeDocker, manifest

A, B = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
API = apps_api.API


def stand(root, docker=None, builtin_run=None, http=None) -> TestClient:
    """Шлюз в миниатюре: доступ шлюза и роутер приложений над корнем root."""
    app = FastAPI()
    app.add_middleware(access.AccessMiddleware, any_body_prefixes=("/apps/",))
    app.include_router(apps_api.make_router(lambda: root, docker or FakeDocker(), builtin_run, http))
    return TestClient(app, base_url="http://127.0.0.1")


def panel(tmp_path, running=(), docker=None):
    for app_id in (A, B):
        (tmp_path / app_id / apps.SRC).mkdir(parents=True)
        apps.save(tmp_path, manifest(id=app_id, name=f"app-{app_id[0]}", slug=f"app-{app_id[0]}",
                                     request="сделай график"))
    up = {n for app_id in running for n in (apps.app_container(app_id), apps.edge_container(app_id))}
    docker = docker or FakeDocker(running=up)
    return stand(tmp_path, docker), docker


def post(client, app_id, action, body=None):
    return client.post(f"{API}/{app_id}/{action}", json=body or {})


def test_list_shows_cards_with_state_and_gateway_page(tmp_path):
    client, _ = panel(tmp_path, running=[A])
    (tmp_path / "cccccccccccc").mkdir()
    (tmp_path / "cccccccccccc" / apps.MANIFEST).write_text("{broken", encoding="utf-8")
    data = client.get(API).json()
    cards = {c["id"]: c for c in data["apps"]}
    assert "running" not in cards[A]                     # приложения запущены всегда: живость не опрашивается
    assert cards[A]["path"] == f"/apps/{cards[A]['slug']}/" and cards[A]["request"] == "сделай график"
    assert len(data["errors"]) == 1 and "gateway_port" not in data      # страница - на шлюзе, порт не нужен
    page = client.get(apps_api.PAGE_PATH)
    assert page.status_code == 200 and "приложения" in page.text and page.headers["x-frame-options"] == "DENY"
    assert page.headers["cache-control"] == "no-store"


def test_stop_start_and_delete_with_confirmation(tmp_path):
    client, docker = panel(tmp_path)
    assert post(client, A, "stop").json()["ok"] and apps.load(tmp_path, A).wanted == apps.STOPPED
    assert post(client, A, "start").json()["text"].endswith("продолжает работу")
    assert apps.load(tmp_path, A).wanted == apps.RUNNING
    assert post(client, A, "delete").status_code == 400                 # без подтверждения
    assert post(client, A, "delete", {"confirm": B}).status_code == 400  # подтверждение чужого id
    assert post(client, A, "delete", {"confirm": A}).json()["ok"]
    assert not (tmp_path / A).exists() and (tmp_path / B).exists()
    slug = apps.load(tmp_path, B).slug                                  # SAM-205: по slug, как в карточке
    assert post(client, slug, "stop").json()["ok"] and apps.load(tmp_path, B).wanted == apps.STOPPED
    assert post(client, slug, "delete", {"confirm": slug}).json()["ok"] and not (tmp_path / B).exists()
    assert ["docker", "network", "rm", apps.app_network(A)] in docker.calls


def test_simple_post_from_another_page_is_refused(tmp_path):
    client, docker = panel(tmp_path)
    for kind in ("text/plain", "application/x-www-form-urlencoded"):
        res = client.post(f"{API}/{A}/stop", content=b"{}", headers={"Content-Type": kind})
        assert res.status_code == 415
    assert apps.load(tmp_path, A).wanted == apps.RUNNING and docker.calls == []


def test_router_refuses_a_simple_post_even_without_the_gateway_access():
    """Тип тела роутер проверяет и сам: подключённый без AccessMiddleware, он не открывает docker простому POST."""
    path = f"{API}/{A}/stop"
    assert apps_api.refusal(path, None, "127.0.0.1:8000", "text/plain")[0] == access.UNSUPPORTED_MEDIA_TYPE
    assert apps_api.refusal(path, None, "127.0.0.1:8000", "application/json; charset=utf-8") is None


def test_unknown_app_or_action_is_one_line(tmp_path):
    client, _ = panel(tmp_path)
    assert post(client, "ffffffffffff", "stop").status_code == 404
    assert post(client, A, "format").status_code == 404
    assert post(client, "..", "stop").status_code == 404
    assert client.get(f"{API}/ffffffffffff/settings").status_code == 404


def test_dns_rebinding_host_and_foreign_origin_are_refused(tmp_path):
    client, docker = panel(tmp_path)
    assert client.get(API, headers={"Host": "evil.example:8000"}).status_code == access.FORBIDDEN
    assert client.post(f"{API}/{A}/stop", json={}, headers={"Host": "evil.example:8000"}).status_code == access.FORBIDDEN
    assert client.post(f"{API}/{A}/stop", json={}, headers={"Origin": "http://evil.example"}).status_code == 400
    assert apps.load(tmp_path, A).wanted == apps.RUNNING and docker.calls == []
    assert post(client, A, "stop").status_code == 200                # без Origin - не браузер
    own = client.post(f"{API}/{A}/start", json={}, headers={"Origin": "http://127.0.0.1"})
    assert own.status_code == 200                                    # своя страница шлюза


def test_broken_delete_body_is_one_line(tmp_path):
    client, _ = panel(tmp_path)
    res = client.post(f"{API}/{A}/delete", content=b"", headers={"Content-Type": "application/json"})
    assert res.status_code == 400 and "confirm" in res.json()["detail"]
    res = client.post(f"{API}/{A}/delete", content=b"[1]", headers={"Content-Type": "application/json"})
    assert res.status_code == 400 and "JSON-объект" in res.json()["detail"]


def test_delete_keeps_the_folder_when_docker_did_not_remove_the_app(tmp_path):
    client, _ = panel(tmp_path, docker=FakeDocker(fail="rm -f"))
    out = post(client, A, "delete", {"confirm": A}).json()
    assert out["ok"] is False and "папка оставлена" in out["text"] and (tmp_path / A).exists()


def test_delete_treats_missing_containers_as_removed(tmp_path):
    class Gone(FakeDocker):
        def __call__(self, argv):
            self.calls.append(argv)
            return apps.DockerResult(1, "", "Error response from daemon: No such container: x")
    (tmp_path / A).mkdir()
    apps.save(tmp_path, manifest(id=A))
    assert apps.delete(tmp_path, manifest(id=A), Gone()) is None and not (tmp_path / A).exists()


def test_page_is_a_gateway_page_with_the_list_inside(tmp_path):
    """Страница на origin шлюза: каркас и API - своими путями, список - в самой странице, «<» не закрывает script."""
    client, _ = panel(tmp_path)
    apps.save(tmp_path, manifest(id=A, name="</script><b>", slug="app-a"))
    page = client.get(apps_api.PAGE_PATH).text
    assert "__GATEWAY__" not in page and apps_api.APPS_MARK not in page
    assert '<script src="/static/shell/shell.js" defer data-page="apps"></script>' in page
    assert '<script src="/shell/config.js"></script>' in page
    assert '"/sam/apps"' in page and "/api/apps" not in page
    assert "</script><b>" not in page and "\\u003c/script>\\u003cb>" in page


def test_gateway_serves_the_apps_page_and_api_as_a_builtin_app(app_client, tmp_path):  # noqa: F811
    """Шлюз сам отдаёт страницу приложений и их API; её плитка - встроенное приложение с health check шлюза."""
    client, _ = app_client
    builtin_apps.ensure(tmp_path)
    assert client.get(apps_api.PAGE_PATH).status_code == 200
    tiles = {c["slug"]: c for c in client.get(API).json()["apps"]}
    assert tiles["apps"]["builtin"] and tiles["apps"]["removable"] is False
    assert tiles["apps"]["url"] == apps_api.PAGE_PATH and tiles["apps"]["health"].endswith("/health")
    assert client.post(f"{API}/apps/delete", json={"confirm": "apps"}).status_code == 400
    assert builtin_apps.remove(tmp_path, "apps").startswith("apps не удаляется")
    old = client.get("/apps-panel", follow_redirects=False)              # прежний адрес панели :8011
    assert old.status_code in (302, 307) and old.headers["location"] == apps_api.PAGE_PATH
    frame = client.get("/apps/apps", follow_redirects=False)            # /apps/<slug> встроенного - его страница
    assert frame.headers["location"].endswith(apps_api.PAGE_PATH)


def test_app_icon_is_a_checked_svg_on_its_tile(tmp_path):
    """Лого плитки (поле icon): SVG строкой, проверено при записи и отдано карточкой списка."""
    logo = '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>'
    assert apps.icon_error(logo) is None and apps.icon_error("") is None
    assert "SVG" in apps.icon_error("<img src=x onerror=alert(1)>")
    assert "строка" in apps.icon_error(42)
    assert str(apps.ICON_MAX_CHARS) in apps.icon_error("<svg>" + "x" * apps.ICON_MAX_CHARS)
    client, _ = panel(tmp_path)
    apps.save(tmp_path, manifest(id=A, slug="app-a", icon=logo))
    cards = {c["id"]: c for c in client.get(API).json()["apps"]}
    assert cards[A]["icon"] == logo and cards[B]["icon"] == ""


def test_delete_and_install_only_from_this_machine():
    """Ревью SAM-204: удалить и поставить - только с этой машины, как у прежней панели на 127.0.0.1; общий ключ API
    есть и у телефона, а удаление стирает приложение с данными. Остальные действия - с любого допущенного клиента."""
    from gateway import apps_api
    local = {"127.0.0.1"}
    assert apps_api.local_only("delete", "192.0.2.11", local)[0] == apps_api.FORBIDDEN
    assert apps_api.local_only("install", "192.0.2.11", local)[0] == apps_api.FORBIDDEN
    assert apps_api.local_only("delete", "127.0.0.1", local) is None
    assert apps_api.local_only("stop", "192.0.2.11", local) is None


def test_revive_skips_an_app_stopped_while_the_gateway_was_starting(tmp_path, monkeypatch):
    """Ревью SAM-204: revive перечитывает манифест под замком приложения - остановленное за это время не поднимается."""
    from gateway import apps
    started = []
    m = apps.Manifest(id="abcdefabcdef", name="t", command="x", host_port=18401, port=8000, wanted=apps.RUNNING)
    from dataclasses import replace
    monkeypatch.setattr(apps, "scan", lambda root: ([m], []))
    monkeypatch.setattr(apps, "find", lambda root, ref: replace(m, wanted=apps.STOPPED))   # остановили за это время
    monkeypatch.setattr(apps, "is_running", lambda mm, run: False)
    monkeypatch.setattr(apps, "start", lambda root, mm, limits, run: started.append(mm.id))
    assert apps.revive(tmp_path, run=lambda argv: None) == [] and started == []
    assert apps.app_lock("abcdefabcdef") is apps.app_lock("abcdefabcdef")
