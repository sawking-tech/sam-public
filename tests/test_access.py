"""Доступ к шлюзу и агрегатору по ключу (SAM-79): настоящее приложение шлюза по HTTP и WebSocket, клиенты шлюза
шлют ключ; сеть клиентов - httpx.MockTransport и urlopen."""
import socket

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway import access, mcp_client
from tests.fakes_agents import FakeMcpHttp, GatewayRig

KEY = "s3cret"
OWN, FOREIGN, OWN_LAN = "testclient", "192.168.1.5", "192.0.2.10"   # testclient - эта машина (tests/conftest.py)
PROBE = ("/sam/inject", {"session_id": "task:", "message": "x"})     # ручка без хранилища: дошли - 200


@pytest.fixture
def gw(monkeypatch, tmp_path):
    monkeypatch.setattr(access, "_own_addresses", lambda: frozenset({OWN_LAN}))
    return GatewayRig(monkeypatch, tmp_path)


@pytest.mark.parametrize("configured, host, headers, status", [
    ("", OWN, {}, 200),                                   # без ключа - своя машина
    ("", OWN_LAN, {}, 200),                               # свой адрес в сети - тоже эта машина
    ("", FOREIGN, {}, 401),                               # без ключа - чужой адрес
    (KEY, OWN, {}, 401),                                  # ключ задан - и своей машине нужен ключ
    (KEY, FOREIGN, {"X-SAM-Key": "wrong"}, 401),
    (KEY, FOREIGN, {"X-SAM-Key": KEY}, 200),
    (KEY, FOREIGN, {"Authorization": f"Bearer {KEY}"}, 200),
], ids=["own_no_key", "own_lan_no_key", "foreign_no_key", "own_needs_key", "wrong_key", "header", "bearer"])
def test_gateway_is_closed_by_api_key(gw, monkeypatch, configured, host, headers, status):
    monkeypatch.setenv(access.KEY_ENV, configured)
    client = TestClient(gw.main.app, client=(host, 50000))
    r = client.post(PROBE[0], json=PROBE[1], headers=headers)
    assert r.status_code == status
    if status == 401:
        assert (access.NO_KEY_REMOTE if not configured else access.BAD_KEY) == r.json()["error"]


def test_health_and_the_shell_stay_open_without_a_key(monkeypatch):
    """/health шлюза зовёт стенд (модель, Qdrant, MCP), поэтому за слоем доступа здесь - пустое приложение. Каркас
    страниц открыт тоже: шрифт с другого порта браузер просит без cookie ключа; остальное по-прежнему за ключом."""
    monkeypatch.setenv(access.KEY_ENV, KEY)
    app = FastAPI()
    app.get("/health")(lambda: {"status": "ok"})
    app.get("/static/shell/sam.css")(lambda: "css")
    app.get("/sam/config")(lambda: {})
    app.add_middleware(access.AccessMiddleware)
    client = TestClient(app, client=(FOREIGN, 50000))
    assert client.get("/health").status_code == 200
    assert client.get("/static/shell/sam.css").status_code == 200
    assert client.get("/sam/config").status_code == 401


def test_monitor_key_becomes_a_cookie_for_the_page_and_its_socket(gw, monkeypatch):
    monkeypatch.setenv(access.KEY_ENV, KEY)
    with pytest.raises(WebSocketDisconnect) as closed:
        with gw.client.websocket_connect("/ws/monitor") as ws:
            ws.receive_text()
    assert closed.value.code == access.WS_POLICY_VIOLATION
    r = gw.client.get(f"/monitor?key={KEY}")
    assert r.status_code == 200 and f"{access.COOKIE}={KEY}" in r.headers["set-cookie"]
    assert gw.client.post(PROBE[0], json=PROBE[1]).status_code == 200     # cookie из ответа выше
    with gw.client.websocket_connect("/ws/monitor"):
        pass


@pytest.mark.parametrize("env_key, file_key, sent", [
    ("", None, None),
    (KEY, None, KEY),
    ("", "fromfile", "fromfile"),      # голосовой клиент и client_agent .env сами не читают
], ids=["none", "env", "repo_env_file"])
async def test_clients_send_the_api_key(monkeypatch, tmp_path, env_key, file_key, sent):
    env = tmp_path / ".env"
    if file_key:
        env.write_text(f"{access.KEY_ENV}={file_key}\n", encoding="utf-8")
    monkeypatch.setattr(access, "_REPO_ENV", str(env))
    monkeypatch.setenv(access.KEY_ENV, env_key)
    aggregator = FakeMcpHttp({"tools/list": {"tools": []}})
    await mcp_client.mcp_request("http://pc:3000", "tools/list", {}, transport=aggregator.transport())
    assert aggregator.keys == [sent, sent]                 # initialize и сам вызов
    assert access.auth_headers() == ({"X-SAM-Key": sent} if sent else {})


@pytest.mark.parametrize("call", [lambda cli: cli._get("/sam/llm/status"),
                                  lambda cli: cli._post("/sam/llm/switch", {"adapter": "auto"})], ids=["get", "post"])
def test_cli_sends_the_key(monkeypatch, call):
    # gateway.cli тоже клиент шлюза: с заданным ключом без него каждая команда получала 401 (night-review)
    from gateway import cli
    monkeypatch.setenv(access.KEY_ENV, "k1")
    sent = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def _urlopen(req, timeout=None):
        sent.append(req.get_header("X-sam-key"))
        return _Resp()
    monkeypatch.setattr(cli.urllib.request, "urlopen", _urlopen)
    call(cli)
    assert sent == ["k1"]


@pytest.mark.parametrize("method, path, content_type, refused", [
    ("POST", "/mcp", "text/plain", True),
    ("POST", "/sam/cancel-all", "", True),                        # POST без тела тоже простой запрос
    ("POST", "/sam/inject", "multipart/form-data; boundary=x", True),
    ("POST", "/sam/inject", "Application/JSON; charset=utf-8", False),
    ("GET", "/monitor", "", False),
    ("DELETE", "/sam/presence/heat", "", False),                  # не простой метод: браузер шлёт preflight
    ("POST", "/apps/0123456789ab/api", "text/plain", False),     # прокси приложения: тело для его контейнера
], ids=["text_plain", "no_body", "multipart", "json", "get", "delete", "app_proxy"])
def test_post_needs_json_body_type(method, path, content_type, refused):
    assert (access.body_reason(method, path, content_type, ("/apps/",)) == access.NOT_JSON) is refused


def test_gateway_answers_415_to_a_simple_post(gw):
    r = TestClient(gw.main.app, client=(OWN, 50000)).post(PROBE[0], content=b"x", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415 and r.json()["error"] == access.NOT_JSON


@pytest.mark.parametrize("configured, host, refused", [
    ("", "localhost:8000", False),
    ("", "127.0.0.1:3000", False),
    ("", "[::1]:8000", False),
    ("", "host.docker.internal:8000", False),     # n8n в Docker
    ("", "evil.example:8000", True),              # имя перепривязано на 127.0.0.1 (DNS rebinding)
    ("", "", True),                               # HTTP/1.0 без Host
    ("", "localhost.:8000", True),
    ("", "[::1]evil:8000", True),
    ("", "gateway:8000", True),                   # короткое имя сети: соседний хост отдаст его через LLMNR
    (KEY, "evil.example:8000", False),            # с ключом такой странице не войти и так
], ids=["localhost", "loopback_ip", "ipv6", "docker", "rebinding", "no_host", "trailing_dot", "bracket_tail",
        "short_lan_name", "with_key"])
def test_host_without_key_is_this_machine(configured, host, refused):
    reason = access.host_reason(configured, host, access.allowed_hosts())
    assert (reason is not None) is refused


def test_machine_name_is_this_machine():
    assert access.host_reason("", f"{socket.gethostname()}:8000", access.allowed_hosts()) is None


@pytest.mark.parametrize("origin, host, allowed, refused", [
    ("http://localhost:8000", "localhost:8000", set(), False),
    ("http://localhost:5678", "localhost:8000", set(), True),     # другая страница той же машины
    ("http://localhost:5678", "localhost:8000", {"http://localhost:5678"}, False),   # SAM_ALLOWED_ORIGINS
], ids=["same_page", "other_port", "allowed_origin"])
def test_websocket_origin_rule(origin, host, allowed, refused):
    assert (access.origin_reason(origin, host, allowed) is not None) is refused


def test_websocket_refused_by_host_without_key(gw):
    with pytest.raises(WebSocketDisconnect) as closed:
        with gw.client.websocket_connect("/ws/monitor", headers={"Host": "evil.example:8000"}) as ws:
            ws.receive_text()
    assert closed.value.code == access.WS_POLICY_VIOLATION


def test_inbox_is_not_a_get(gw):
    assert gw.client.get("/sam/agents/ab12/inbox").status_code == 405


def test_gateway_refuses_foreign_host_without_key(gw):
    r = TestClient(gw.main.app, client=(OWN, 50000), base_url="http://evil.example:8000").post(PROBE[0], json=PROBE[1])
    assert r.status_code == 403 and "evil.example" in r.json()["error"]


@pytest.mark.parametrize("origin, refused", [
    (None, False),                                # python-клиент: Origin не шлёт
    ("http://testserver", False),                 # своя страница монитора
    ("http://localhost:5678", True),              # другая страница этой машины (n8n, dev-сервер)
    ("null", True),                               # страница приложения в песочнице
], ids=["no_origin", "own_page", "other_port", "sandboxed"])
def test_websocket_only_from_own_page(gw, origin, refused):
    headers = {} if origin is None else {"Origin": origin}
    if refused:
        with pytest.raises(WebSocketDisconnect) as closed:
            with gw.client.websocket_connect("/ws/monitor", headers=headers) as ws:
                ws.receive_text()
        assert closed.value.code == access.WS_POLICY_VIOLATION
    else:
        with gw.client.websocket_connect("/ws/monitor", headers=headers):
            pass


def test_missing_key_answers_before_body_type(gw, monkeypatch):
    """Чужой адрес без ключа узнаёт про ключ, а не про тип тела: 401 раньше 415."""
    monkeypatch.setenv(access.KEY_ENV, KEY)
    r = TestClient(gw.main.app, client=(FOREIGN, 50000)).post(PROBE[0], content=b"x", headers={"Content-Type": "text/plain"})
    assert r.status_code == 401
