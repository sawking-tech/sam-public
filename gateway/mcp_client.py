"""MCP HTTP client — JSON-RPC 2.0 поверх Streamable HTTP transport.

Используется для вызова инструментов на удалённых ПК через mcp-aggregator.
Pure async, без MongoDB зависимостей. Получение MCP URL устройства —
отдельный helper get_device_mcp_url, который принимает SessionRepo-подобный
catalog accessor через DI (чтобы не тянуть _db).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .access import auth_headers


HTTP_ERROR_MIN = 400
ERROR_TEXT_LIMIT = 200  # тело ошибки в контексте модели; длиннее - шум

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def parse_sse(text: str) -> dict:
    """Извлечь JSON из SSE-ответа MCP-сервера. Fallback на plain JSON."""
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except Exception:
                pass
    try:
        return json.loads(text)
    except Exception:
        return {}


async def mcp_request(
    mcp_url: str,
    method: str,
    params: dict,
    req_id: int = 1,
    timeout: float = 60.0,
    transport: Any = None,
) -> dict:
    """JSON-RPC вызов MCP-сервера через Streamable HTTP.

    Делает initialize → method за один HTTP клиент (session_id передаётся
    в заголовке mcp-session-id между вызовами). transport - транспорт httpx; None - сеть.
    """
    import httpx
    base = mcp_url.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout, **({"transport": transport} if transport is not None else {})) as client:
        init = await client.post(f"{base}/mcp", headers={**MCP_HEADERS, **auth_headers()}, json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sam-gateway", "version": "1.0"},
            },
        })
        session_id = init.headers.get("mcp-session-id", "")
        headers = {**MCP_HEADERS, **auth_headers(), "mcp-session-id": session_id}
        resp = await client.post(f"{base}/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": req_id, "method": method, "params": params,
        })
    # HTTP-ошибку отдаём как JSON-RPC error: parse_sse дал бы {}, и вызов
    # выглядел бы для модели пустым успехом (SAM-72)
    if resp.status_code >= HTTP_ERROR_MIN:
        return {"error": {"code": resp.status_code, "message": resp.text[:ERROR_TEXT_LIMIT]}}
    return parse_sse(resp.text)


def resolve_device_mcp_url(
    device: str,
    catalog_get: Callable[[str, str], dict | None],
) -> tuple[str | None, str]:
    """Получить MCP URL устройства из каталога.

    Args:
        device: имя устройства (например "homepc")
        catalog_get: функция (type, name) → doc | None.
                     Обычно catalog.get(_db, "device", name)

    Returns:
        (url, error_message) — если url=None, error_message объясняет.
    """
    doc = catalog_get("device", device)
    if not doc:
        return None, f"Устройство '{device}' не найдено в каталоге"
    if not doc.get("online"):
        return None, f"'{device}' офлайн — используй wake_device чтобы включить"
    mcp_url = doc.get("mcp_url", "")
    if not mcp_url:
        return None, f"MCP URL для '{device}' не задан"
    return mcp_url, ""
