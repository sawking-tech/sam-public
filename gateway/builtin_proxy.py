"""Встроенное приложение отдельным процессом (радар, `gateway/builtin_apps.py`) - по адресу шлюза /apps/<slug>/.

Шлюз пересылает запрос и WebSocket на настоящий адрес приложения, поэтому у радара один адрес со slug и тот же
origin, что у монитора: переход между страницами не меняет порт. Приложение поставляет SAM, поэтому ответ идёт без
CSP sandbox живых приложений: странице нужен свой origin, чтобы ходить к своему API относительными путями.

Запрос до сюда уже прошёл проверку доступа шлюза (gateway/access.py). Приложение слушает 127.0.0.1 со своей такой же
проверкой, поэтому шлюз предъявляет ему ключ SAM_API_KEY сам, а ключ из адреса (?key=) дальше не передаёт. Host
передаётся как есть: страница и редиректы приложения строят адреса от шлюза, а не от 127.0.0.1.
"""
import asyncio
from urllib.parse import urlencode, urlsplit

import httpx
import websockets
from fastapi import Request, WebSocket
from fastapi.responses import Response

from . import access
from .builtin_apps import Builtin
from .env import env_int

TIMEOUT_S = 30                   # весь ответ приложения; дольше - приложение зависло
# Тело запроса страницы (настройки, сообщение, снимок сцены): та же настройка, что у прокси живых приложений
MAX_BODY = env_int("SAM_APPS_PROXY_MAX_BODY", 2_000_000)
PASS_HEADERS = ("content-type", "accept", "accept-language", "cache-control", "if-none-match", "if-modified-since",
                "host")
BACK_HEADERS = ("content-type", "cache-control", "etag", "last-modified", "location")
WS_CLOSE_UNREACHABLE = 1011      # приложение не приняло WebSocket: страница переподключится сама
WS_CLOSE_NORMAL = 1000

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    """Один клиент на процесс: новый на каждый запрос - 0,4 с на создание (замер 15 окт), страница радара - три
    десятка файлов."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT_S)
    return _client


async def close() -> None:
    """Остановка шлюза: соединения клиента закрываются."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def base_of(b: Builtin) -> str:
    parts = urlsplit(b.url)
    return f"{parts.scheme}://{parts.netloc}"


def ws_base_of(b: Builtin) -> str:
    return "ws" + base_of(b)[len("http"):]


def upstream_query(pairs) -> str:
    """Параметры страницы без ключа доступа: ключ проверил шлюз, приложению и его логам он не нужен."""
    return urlencode([(k, v) for k, v in pairs if k != access.QUERY])


def auth_headers() -> dict[str, str]:
    key = access.configured_key()
    return {access.HEADER: key} if key else {}


class TooBig(Exception):
    pass


async def read_body(request: Request) -> bytes:
    """Тело не больше MAX_BODY: по content-length сразу, без него - по ходу, в память не больше предела."""
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        raise TooBig
    parts, size = [], 0
    async for part in request.stream():
        size += len(part)
        if size > MAX_BODY:
            raise TooBig
        parts.append(part)
    return b"".join(parts)


async def forward(b: Builtin, path: str, request: Request) -> Response:
    """Запрос страницы - приложению; недоступно - 502 одной строкой."""
    try:
        body = await read_body(request)
    except TooBig:
        return Response(f"тело запроса больше {MAX_BODY} байт", status_code=413)
    headers = {k: v for k, v in request.headers.items() if k.lower() in PASS_HEADERS} | auth_headers()
    query = upstream_query(request.query_params.multi_items())
    url = f"{base_of(b)}/{path}" + (f"?{query}" if query else "")
    try:
        method = "GET" if request.method == "HEAD" else request.method     # у FastAPI приложения HEAD нет, тело на HEAD шлюз не шлёт
        res = await client().request(method, url, content=body, headers=headers)
    except httpx.HTTPError as e:
        return Response(f"{b.name} не отвечает: {str(e) or type(e).__name__}", status_code=502)
    out = {k: v for k, v in res.headers.items() if k.lower() in BACK_HEADERS}
    return Response(res.content, status_code=res.status_code, headers=out)


async def relay(b: Builtin, path: str, page: WebSocket) -> None:
    """WebSocket страницы - приложению и обратно, пока жива любая сторона; код закрытия приложения - странице."""
    query = upstream_query(page.query_params.multi_items())
    try:
        upstream = await websockets.connect(f"{ws_base_of(b)}/{path}" + (f"?{query}" if query else ""),
                                            additional_headers=auth_headers(), open_timeout=TIMEOUT_S)
    except (OSError, websockets.WebSocketException, asyncio.TimeoutError):
        await page.close(code=WS_CLOSE_UNREACHABLE)
        return
    await page.accept()

    async def up():
        while True:
            msg = await page.receive()
            if msg["type"] == "websocket.disconnect":
                return
            await upstream.send(msg["text"] if msg.get("text") is not None else msg["bytes"])

    async def down():
        async for msg in upstream:
            await (page.send_text(msg) if isinstance(msg, str) else page.send_bytes(msg))

    tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)     # обрыв любой стороны - не ошибка обработчика
        await upstream.close()
        try:
            await page.close(code=upstream.close_code or WS_CLOSE_NORMAL)
        except (RuntimeError, OSError):          # страница уже закрыла сокет
            pass
