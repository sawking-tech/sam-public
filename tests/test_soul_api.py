"""REST души (gateway/soul_api.py): чтение, запись и откат с ключом привилегий или ключом команд."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import soul_api
from gateway.prompts import soul
from gateway.repos.soul_repo import SoulRepo
from gateway.soul_service import SoulService

KEY = "privilege-key"
SECRETS = {"command": "904317", "other": "111222"}


@pytest.fixture
def svc(mongo_db):
    return SoulService(SoulRepo(mongo_db, 5), invalidate=lambda: None, log=lambda line: None)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def log_lines():
    return []


@pytest.fixture
def client(svc, clock, log_lines):
    app = FastAPI()
    limiter = soul_api.SecretLimiter(clock=clock, log=log_lines.append)
    app.include_router(soul_api.make_router(lambda: svc, key=lambda: KEY, secrets=lambda: SECRETS, limiter=limiter))
    return TestClient(app)


@pytest.mark.parametrize("presented, secret, expected", [
    (KEY, "", soul_api.LEAD_AUTHOR),
    ("чужой", "", None),
    (None, "904317", soul_api.OWNER_AUTHOR),
    (None, "девятьсот четыре триста семнадцать", soul_api.OWNER_AUTHOR),   # как во фразе голосом
    (None, "111222", None),                                              # ключ другого назначения
    (None, "", None),
], ids=["privilege_key", "foreign_key", "command_secret", "secret_in_words", "other_secret", "nothing"])
def test_authorize(presented, secret, expected):
    assert soul_api.authorize(presented, KEY, secret, SECRETS) == expected


def test_list_read_and_history_are_open(client):
    names = [s["_id"] for s in client.get("/sam/soul").json()["sections"]]
    assert names == ["IDENTITY", "CHARACTER", "TONE"]   # душа - только характер
    assert client.get("/sam/soul/TONE").json()["text"] == soul.read_seed()["TONE"]
    assert client.get("/sam/soul/TONE/history").json()["versions"][0]["version"] == 1
    assert client.get("/sam/soul/NOPE").status_code == 404


@pytest.mark.parametrize("headers, body", [
    ({}, {"text": "x", "expected_version": 1}),
    ({"X-SAM-Privilege": "foreign"}, {"text": "x", "expected_version": 1}),
    ({}, {"text": "x", "expected_version": 1, "secret": "111222"}),
], ids=["no_key", "foreign_key", "other_secret"])
def test_write_without_a_key_is_refused(client, svc, headers, body):
    r = client.put("/sam/soul/TONE", json=body, headers=headers)
    assert r.status_code == 403 and "X-SAM-Privilege" in r.json()["detail"]
    assert svc.show("TONE")["version"] == 1
    assert client.post("/sam/soul/TONE/rollback", json={k: v for k, v in body.items() if k not in ("text", "expected_version")},
                       headers=headers).status_code == 403


def test_write_and_rollback_with_keys(client, svc):
    r = client.put("/sam/soul/TONE", json={"text": "Новый тон.", "expected_version": 1}, headers={"X-SAM-Privilege": KEY})
    assert r.status_code == 200 and r.json()["version"] == 2 and svc.show("TONE")["author"] == "lead"
    r = client.post("/sam/soul/TONE/rollback", json={"secret": "904317"})
    assert r.status_code == 200 and svc.show("TONE")["text"] == soul.read_seed()["TONE"]
    assert svc.show("TONE")["author"].startswith("owner")


def test_stale_version_is_409_reread(client, svc):
    svc.write("TONE", "Правка SAM.", "sam")
    r = client.put("/sam/soul/TONE", json={"text": "Моя.", "expected_version": 1, "secret": "904317"})
    assert r.status_code == 409 and r.json()["detail"] == "раздел TONE изменился (v2), перечитай"
    assert svc.show("TONE")["text"] == "Правка SAM."


def test_broken_write_is_409_with_the_reason(client, svc):
    r = client.put("/sam/soul/IDENTITY", json={"text": " ", "expected_version": 1, "secret": "904317"})
    assert r.status_code == 409 and "IDENTITY" in r.json()["detail"]
    assert svc.show("IDENTITY")["version"] == 1


def test_no_mongo_is_503():
    app = FastAPI()
    app.include_router(soul_api.make_router(lambda: SoulService(SoulRepo(None, 5)), key=lambda: KEY,
                                            secrets=lambda: SECRETS))
    assert TestClient(app).get("/sam/soul").status_code == 503


def test_instructions_and_personal_layer_are_not_served(client):
    for name in ("DOING", "_personal", "_header"):
        assert client.get(f"/sam/soul/{name}").status_code == 404
        r = client.put(f"/sam/soul/{name}", json={"text": "x", "expected_version": 1}, headers={"X-SAM-Privilege": KEY})
        assert r.status_code == 409 and "не раздел души" in r.json()["detail"]


def test_secret_brute_force_is_429_for_ten_minutes(client, svc, clock, log_lines):
    body = {"text": "x", "expected_version": 1}
    for attempt in range(soul_api.MAX_SECRET_FAILURES):
        clock.now = attempt * 60
        assert client.put("/sam/soul/TONE", json={**body, "secret": f"00000{attempt}"}).status_code == 403
    r = client.put("/sam/soul/TONE", json={**body, "secret": "904317"})   # верный ключ тоже ждёт
    assert r.status_code == 429 and "закрыта" in r.json()["detail"]
    assert client.put("/sam/soul/TONE", json=body, headers={"X-SAM-Privilege": KEY}).status_code == 200   # ведущий
    assert len(log_lines) == 1 and "закрыта на 10 мин" in log_lines[0]
    clock.now += soul_api.SECRET_BLOCK_S
    assert client.put("/sam/soul/TONE", json={"text": "y", "expected_version": 2, "secret": "904317"}).status_code == 200


def test_failures_outside_the_window_do_not_block(client, clock):
    body = {"text": "x", "expected_version": 1, "secret": "000000"}
    for attempt in range(soul_api.MAX_SECRET_FAILURES):
        clock.now = attempt * (soul_api.SECRET_WINDOW_S / 2)
        assert client.put("/sam/soul/TONE", json=body).status_code == 403


def test_gateway_serves_the_soul_routes():
    from gateway import main
    paths = {getattr(route, "path", "") for route in main.app.routes}
    assert {"/sam/soul", "/sam/soul/{name}", "/sam/soul/{name}/history", "/sam/soul/{name}/rollback"} <= paths


def test_real_gateway_lets_the_monitor_write_with_the_owner_secret_only(monkeypatch, tmp_path):
    """Через доступ шлюза (gateway/access.py): без ключа - 403 ручки, а не пропуск; ключ команд или привилегий - запись."""
    from gateway import agent
    from tests.fakes_agents import GatewayRig
    gw = GatewayRig(monkeypatch, tmp_path)
    svc = SoulService(SoulRepo(gw.db, 5), invalidate=lambda: None, log=lambda line: None)
    monkeypatch.setattr(agent, "soul_service", svc)
    monkeypatch.setenv("SAM_COMMAND_SECRET", SECRETS["command"])
    assert gw.client.put("/sam/soul/TONE", json={"text": "x", "expected_version": 1}).status_code == 403
    assert gw.client.put("/sam/soul/TONE", json={"text": "Тон.", "expected_version": 1, "secret": SECRETS["command"]}).status_code == 200
    assert gw.client.put("/sam/soul/TONE", json={"text": "Тон 2.", "expected_version": 2}, headers=gw.privileged).status_code == 200
    assert [v["author"] for v in gw.client.get("/sam/soul/TONE/history").json()["versions"][:2]] == ["lead", "owner"]


def test_every_router_route_reaches_the_gateway():
    """Все маршруты души видны в приложении, а не только первый.

    FastAPI 0.165 регистрирует из `include_router` один маршрут из четырёх - проверено на чистом окружении:
    `{'/sam/soul', '/sam/soul/{name}', '/sam/soul/{name}/history', '/sam/soul/{name}/rollback'}` пропадает из
    `app.routes` целиком, и монитор не может ни прочесть душу, ни откатить. Тест держит версию: пин в
    `gateway/requirements.txt` меняют вместе с ним."""
    from gateway import main
    assert "/sam/soul" in {getattr(r, "path", "") for r in main.app.routes}
    assert len(main.app.routes) > 70          # маршрутов десятки; один-два признак сломанного include_router
