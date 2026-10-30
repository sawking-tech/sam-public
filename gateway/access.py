"""Доступ к gateway и MCP-агрегатору по общему ключу (SAM-79).

Оба слушают 0.0.0.0 (start_sam.ps1), и без ключа любое устройство в сети звало gateway и запускало
powershell на ПК через агрегатор. Теперь:
- SAM_API_KEY задан - запрос без ключа или с чужим получает 401 (WebSocket закрывается кодом 1008);
- SAM_API_KEY не задан - пускаются только запросы с этой машины (SAM_LOCAL_HOSTS), со строкой в логе на старте.

Ключ клиент шлёт заголовком X-SAM-Key или Authorization: Bearer; браузер монитора - один раз
параметром ?key= на /monitor, дальше cookie (fetch и WebSocket той же страницы шлют его сами).
/health открыт: по нему проверяют, жив ли сервис.

POST - только с Content-Type: application/json, иначе 415 (SAM-188). Любая страница в браузере на этой машине
может отправить простой POST (text/plain, форма, без тела) без preflight CORS, а с этой машины ключ не нужен:
так страница звала run_command агрегатора. Заголовок application/json браузер ставит только после preflight,
который сервер не разрешает. PUT, PATCH и DELETE простыми не бывают и проходят preflight так же.

Без ключа Host запроса - только этой машины (SAM_ALLOWED_HOSTS добавляет имена): иначе страница чужого имени,
перепривязанного на 127.0.0.1 (DNS rebinding), становилась своей и шла в шлюз и агрегатор без ключа. С ключом
такая страница его не получит. WebSocket браузер открывает с любой страницы без CORS, поэтому рукопожатие с
заголовком Origin пускается, только если Origin - та же страница (тот же Host) или из SAM_ALLOWED_ORIGINS;
клиенты на python Origin не шлют.

AccessMiddleware - чистый ASGI: закрывает и HTTP, и WebSocket одним местом; решение (decide) отделено
от протокола и проверяется без сети.
"""
from __future__ import annotations

import functools
import hmac
import json
import os
import socket
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

from . import privilege_key as _privilege

KEY_ENV = "SAM_API_KEY"
_REPO_ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
HEADER = "x-sam-key"
COOKIE = "sam_key"
QUERY = "key"
LOCAL_HOSTS_ENV = "SAM_LOCAL_HOSTS"
DEFAULT_LOCAL_HOSTS = "127.0.0.1,::1,localhost"
OPEN_PATHS = frozenset({"/health"})
# Каркас страниц - публичная статика (CSS, JS, шрифты, без данных): его берут страницы на других портах и заглушка
# приложения в песочнице, а шрифт браузер просит без cookie - за ключом он не грузился бы вовсе
OPEN_PREFIXES = ("/static/shell/",)
HOST_HEADER, ORIGIN_HEADER, CONTENT_TYPE_HEADER, AUTH_HEADER = "host", "origin", "content-type", "authorization"
ALLOWED_HOSTS_ENV = "SAM_ALLOWED_HOSTS"
# Имена, по которым к шлюзу и агрегатору ходят без ключа на этой машине: Docker Desktop - host.docker.internal;
# адреса и имя машины добавляются сами. Короткие имена сети (gateway из compose) соседний хост может отдать через
# LLMNR и перепривязать - только через SAM_ALLOWED_HOSTS
DEFAULT_ALLOWED_HOSTS = "localhost,host.docker.internal"
ALLOWED_ORIGINS_ENV = "SAM_ALLOWED_ORIGINS"
FOREIGN_HOST = "Host {host} не этой машины: добавьте имя в SAM_ALLOWED_HOSTS или задайте SAM_API_KEY"
FOREIGN_ORIGIN = "WebSocket с чужой страницы ({origin}): разрешены своя страница и SAM_ALLOWED_ORIGINS"
WS_POLICY_VIOLATION = 1008
NO_KEY_REMOTE = f"{KEY_ENV} не задан: доступ только с этой машины; задайте ключ в .env"
BAD_KEY = f"нужен ключ {KEY_ENV}: заголовок X-SAM-Key или Authorization: Bearer"
JSON_TYPE = "application/json"
NEEDS_JSON_METHODS = frozenset({"POST"})   # простой запрос браузера без preflight - GET, HEAD и POST
NOT_JSON = f"POST - только с Content-Type: {JSON_TYPE}"
UNSUPPORTED_MEDIA_TYPE = 415
UNAUTHORIZED = 401
FORBIDDEN = 403


def configured_key() -> str:
    return os.getenv(KEY_ENV, "").strip()


def local_hosts() -> set[str]:
    """Адреса этой машины: из SAM_LOCAL_HOSTS и собственные адреса в сети. Gateway зовёт агрегатор
    своего же ПК по адресу из каталога устройств (192.0.2.10), а не по 127.0.0.1."""
    hosts = {h.strip() for h in os.getenv(LOCAL_HOSTS_ENV, DEFAULT_LOCAL_HOSTS).split(",") if h.strip()}
    return hosts | _own_addresses()


@functools.lru_cache(maxsize=1)
def _own_addresses() -> frozenset[str]:
    try:
        return frozenset(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        return frozenset()


def client_key() -> str:
    """Ключ для клиента: из окружения, иначе из .env в корне репозитория (голосовой клиент и
    client_agent .env сами не читают)."""
    key = configured_key()
    if key:
        return key
    try:
        from dotenv import dotenv_values
        return str(dotenv_values(_REPO_ENV).get(KEY_ENV) or "").strip()
    except Exception:
        return ""


def auth_headers() -> dict[str, str]:
    """Заголовок для клиентов SAM: пусто, если ключ не задан."""
    key = client_key()
    return {"X-SAM-Key": key} if key else {}


def decide(configured: str, presented: str | None, client_host: str | None, hosts: set[str]) -> str | None:
    """None - пустить; иначе причина отказа одной строкой."""
    if not configured:
        return None if client_host in hosts else NO_KEY_REMOTE
    if presented and hmac.compare_digest(presented.encode(), configured.encode()):
        return None
    return BAD_KEY


def body_reason(method: str, path: str, content_type: str, any_body_prefixes: tuple[str, ...] = ()) -> str | None:
    """None - тип тела годится; иначе причина отказа: POST без application/json мог прийти с чужой страницы."""
    if method.upper() not in NEEDS_JSON_METHODS or path.startswith(any_body_prefixes):
        return None
    return None if content_type.split(";")[0].strip().lower() == JSON_TYPE else NOT_JSON


def allowed_hosts() -> set[str]:
    """Имена этой машины для проверки Host: адреса, имя машины, DEFAULT_ALLOWED_HOSTS и SAM_ALLOWED_HOSTS."""
    extra = f"{DEFAULT_ALLOWED_HOSTS},{os.getenv(ALLOWED_HOSTS_ENV, '')}"
    names = {h.strip().lower() for h in extra.split(",") if h.strip()}
    return names | {h.lower() for h in local_hosts()} | {socket.gethostname().lower()}


def _hostname(host: str) -> str:
    """Имя из Host без порта: «localhost:8000» - localhost, «[::1]:8000» - ::1."""
    host = host.strip().lower()
    if host.startswith("["):
        name, _, rest = host[1:].partition("]")
        return name if rest == "" or (rest.startswith(":") and rest[1:].isdigit()) else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_reason(configured: str, host: str, allowed: set[str]) -> str | None:
    """С ключом - None; без ключа Host должен быть именем этой машины (DNS rebinding)."""
    if configured or _hostname(host) in allowed:
        return None
    return FOREIGN_HOST.format(host=host or "-")


def allowed_origins() -> set[str]:
    return {o.strip().lower() for o in os.getenv(ALLOWED_ORIGINS_ENV, "").split(",") if o.strip()}


def origin_reason(origin: str | None, host: str, allowed: set[str]) -> str | None:
    """WebSocket: без Origin (не браузер) или с Origin своей страницы - None; иначе причина отказа."""
    if origin is None:
        return None
    if urlparse(origin).netloc.lower() == host.strip().lower() or origin.strip().lower() in allowed:
        return None
    return FOREIGN_ORIGIN.format(origin=origin)


def _headers(scope: dict) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def presented_key(scope: dict) -> str | None:
    headers = _headers(scope)
    if headers.get(HEADER):
        return headers[HEADER]
    auth = headers.get(AUTH_HEADER, "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    if query.get(QUERY):
        return query[QUERY][0]
    if headers.get("cookie"):
        jar = SimpleCookie()
        jar.load(headers["cookie"])
        if COOKIE in jar:
            return jar[COOKIE].value
    return None


class AccessMiddleware:
    """any_body_prefixes - пути, куда POST идёт с любым телом: у шлюза прокси живых приложений (main.py)."""

    def __init__(self, app, any_body_prefixes: tuple[str, ...] = ()) -> None:
        self.app = app
        self.any_body_prefixes = any_body_prefixes

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] not in ("http", "websocket") or path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return await self.app(scope, receive, send)
        key = configured_key()
        reason, status = self._refusal(scope, key)
        if reason is None:
            # открыли по ?key= (монитор, радар /apps/radar/) - cookie, чтобы файлы и сокет страницы шли с ключом
            if scope["type"] == "http" and key and QUERY in parse_qs(scope.get("query_string", b"").decode("latin-1")):
                send = _with_cookie(send, key)
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION, "reason": reason[:120]})
            return
        body = json.dumps({"error": reason}, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json; charset=utf-8")]})
        await send({"type": "http.response.body", "body": body})

    def _refusal(self, scope: dict, key: str) -> tuple[str | None, int]:
        """Первая причина отказа и её статус HTTP; (None, 0) - пустить. Ключ и привилегии - раньше типа тела."""
        headers, http = _headers(scope), scope["type"] == "http"
        method, path = scope.get("method", ""), scope.get("path", "")
        client = (scope.get("client") or (None,))[0]
        checks = (
            (lambda: host_reason(key, headers.get(HOST_HEADER, ""), allowed_hosts()), FORBIDDEN),
            (lambda: None if http else origin_reason(headers.get(ORIGIN_HEADER), headers.get(HOST_HEADER, ""),
                                                    allowed_origins()), FORBIDDEN),
            (lambda: decide(key, presented_key(scope), client, local_hosts()), UNAUTHORIZED),
            (lambda: _privilege.decide(method, path, _privilege.presented(scope), _privilege.key())   # ручки агентов
             if http and _privilege.needs(method, path) else None, UNAUTHORIZED),
            (lambda: body_reason(method, path, headers.get(CONTENT_TYPE_HEADER, ""), self.any_body_prefixes)
             if http else None, UNSUPPORTED_MEDIA_TYPE),
        )
        for check, status in checks:
            if (reason := check()) is not None:
                return reason, status
        return None, 0


def _with_cookie(send, key: str):
    cookie = f"{COOKIE}={key}; Path=/; HttpOnly; SameSite=Strict".encode("latin-1")

    async def _send(message):
        if message["type"] == "http.response.start":
            message = {**message, "headers": list(message.get("headers", [])) + [(b"set-cookie", cookie)]}
        await send(message)
    return _send


def startup_warning() -> str | None:
    """Строка для лога на старте, если ключ не задан."""
    return None if configured_key() else f"[Access] {NO_KEY_REMOTE}"
