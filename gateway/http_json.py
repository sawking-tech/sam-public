"""GET с JSON-ответом: общий для оркестратора моделей (каталоги поставщиков) и баланса (SAM-233)."""
from __future__ import annotations

import json

import httpx

TIMEOUT_S = 10   # ответ API баланса и каталога поставщика; дольше - строка ошибки вместо ответа


async def get_json(url: str, headers: dict) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def mcp_tool(url: str, headers: dict, tool: str, args: dict) -> dict:
    """Вызов одного инструмента MCP по HTTP (JSON-RPC, ответ JSON или SSE): initialize, tools/call; result как есть."""
    base = {**headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def _body(resp) -> dict:
        text = resp.text
        if "data:" in text:
            text = next((line[5:].strip() for line in text.splitlines() if line.startswith("data:")), text)
        return json.loads(text) if text.strip() else {}

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        init = await client.post(url, headers=base, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "sam", "version": "1"}}})
        init.raise_for_status()
        session = init.headers.get("mcp-session-id")
        sess = {"mcp-session-id": session} if session else {}
        await client.post(url, headers={**base, **sess}, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        call = await client.post(url, headers={**base, **sess}, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}})
        call.raise_for_status()
        data = _body(call)
    if "error" in data:
        raise RuntimeError(str(data["error"])[:200])
    return data.get("result", {})
