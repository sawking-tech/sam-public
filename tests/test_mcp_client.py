"""Тесты gateway.mcp_client (SAM-40): ответ SSE, адрес MCP устройства, HTTP-ошибка агрегатора."""
import httpx
import pytest

from gateway import mcp_client
from gateway.mcp_client import parse_sse, resolve_device_mcp_url


@pytest.mark.parametrize("text, expected", [
    ('data: {"jsonrpc":"2.0","id":1,"result":"ok"}\n\n', {"jsonrpc": "2.0", "id": 1, "result": "ok"}),
    ('{"jsonrpc":"2.0","id":1,"result":"plain"}', {"jsonrpc": "2.0", "id": 1, "result": "plain"}),
    ('id: 1\ndata: {"result":"first"}\n\nid: 2\ndata: {"result":"second"}\n\n', {"result": "first"}),
    ("not json at all", {}),
    ("", {}),
], ids=["data_prefix", "plain_json", "first_good_record", "garbage", "empty"])
def test_sse_answer_is_parsed(text, expected):
    assert parse_sse(text) == expected


@pytest.mark.parametrize("record, url, reason", [
    (None, None, "не найдено"),
    ({"online": False, "mcp_url": "http://x"}, None, "офлайн"),
    ({"online": True, "mcp_url": ""}, None, "MCP URL"),
    ({"online": True, "mcp_url": "http://10.0.0.1:3000"}, "http://10.0.0.1:3000", ""),
], ids=["not_in_catalog", "offline", "no_url", "ok"])
def test_device_mcp_url_resolved_or_reason(record, url, reason):
    asked = []

    def catalog(type_, name):
        asked.append((type_, name))
        return record
    got_url, err = resolve_device_mcp_url("workpc", catalog)
    assert got_url == url and reason in err and (err == "") == (reason == "")
    assert asked == [("device", "workpc")]


async def test_http_error_becomes_rpc_error(monkeypatch):
    """HTTP 502 от агрегатора возвращается как JSON-RPC error со статусом; сеть - MockTransport."""
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(502, text="bad gateway")), **kw))
    data = await mcp_client.mcp_request("http://agg", "tools/call", {})
    assert "502" in str(data["error"])
