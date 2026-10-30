"""mcp_call (gateway/tools/mcp.py) через настоящий mcp_client и каталог устройств на mongomock; граница - HTTP
устройства (httpx.MockTransport)."""
import functools

import httpx
import pytest

from gateway import catalog, mcp_client
from gateway.tools import mcp as mcp_tool
from tests.fakes_agents import FakeMcpHttp

TOOLS = {"tools": [{"name": "run_command", "description": "Запустить cmd"},
                   {"name": "screenshot", "description": "Скрин экрана"}]}


def _text(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


@pytest.mark.parametrize("params, replies, said, exact", [
    ({}, {}, "Нужны device и tool. Пример: mcp_call(", False),
    ({"device": "ghost", "tool": "x"}, {}, "Устройство 'ghost' не найдено в каталоге", True),
    ({"device": "laptop", "tool": "x"}, {}, "'laptop' офлайн", False),
    ({"device": "pc", "tool": "tools/list"}, {"tools/list": TOOLS}, "  screenshot: Скрин экрана", False),
    ({"device": "pc", "tool": "tools/list"}, {"tools/list": {"tools": []}}, "Нет инструментов на 'pc'", True),
    ({"device": "pc", "tool": "run_command", "args": {"command": "echo hi"}},
     {"tools/call": _text("output ok")}, "output ok", True),
    ({"device": "pc", "tool": "nonexistent"},
     {"tools/call": {"error": "Unknown tool 'nonexistent'"}, "tools/list": TOOLS},
     "Инструмент 'nonexistent' не найден. Доступные: run_command, screenshot", True),
    ({"device": "pc", "tool": "screenshot"}, {"tools/call": _text("x" * 5000)}, "x" * 1200, True),  # обрезан
    ({"device": "pc", "tool": "x"}, {"tools/call": {"error": "permission denied"}}, "MCP error: permission denied", True),
    ({"device": "pc", "tool": "x"}, {"tools/call": httpx.Response(500, text="boom")}, "MCP error: {'code': 500", False),
    ({"device": "pc", "tool": "x"}, {"tools/call": "не JSON"}, "MCP error: пустой или нечитаемый ответ от 'pc'", True),
    ({"device": "pc", "tool": "run_command"}, {"tools/call": _text("exit 1: access denied", is_error=True)},
     "MCP error (run_command на pc): exit 1: access denied", True),
], ids=["no_device_or_tool", "unknown_device", "offline", "tools_list", "tools_list_empty", "call_ok",
        "unknown_tool_lists_others", "long_cut", "rpc_error", "http_error", "unreadable", "is_error"])
async def test_mcp_call_tells_the_model_result_or_error(mongo_db, params, replies, said, exact):
    catalog.upsert(mongo_db, "device", "pc", {"online": True, "mcp_url": "http://pc:3000"})
    catalog.upsert(mongo_db, "device", "laptop", {"online": False, "mcp_url": "http://laptop:3000"})
    server = FakeMcpHttp(replies)
    result = await mcp_tool.mcp_call_impl(
        params, lambda device: mcp_client.resolve_device_mcp_url(device, functools.partial(catalog.get, mongo_db)),
        functools.partial(mcp_client.mcp_request, transport=server.transport()))
    assert result == said if exact else said in result
    calls = [c for c in server.calls if c["method"] == "tools/call"]
    if calls:
        assert calls[0]["params"] == {"name": params["tool"], "arguments": params.get("args", {})}
        assert calls[0]["session"] == FakeMcpHttp.SESSION


async def _call_with(mongo_db, params, replies=None):
    catalog.upsert(mongo_db, "device", "pc", {"online": True, "mcp_url": "http://pc:3000"})
    server = FakeMcpHttp(replies or {"tools/call": _text("ok")})
    result = await mcp_tool.mcp_call_impl(
        params, lambda device: mcp_client.resolve_device_mcp_url(device, functools.partial(catalog.get, mongo_db)),
        functools.partial(mcp_client.mcp_request, transport=server.transport()))
    return result, [c for c in server.calls if c["method"] == "tools/call"]


async def test_args_given_as_a_json_string_reach_the_server_as_an_object(mongo_db):
    result, calls = await _call_with(mongo_db, {"device": "pc", "tool": "write_file", "args": '{"path": "a.txt"}'})
    assert result == "ok" and calls[0]["params"]["arguments"] == {"path": "a.txt"}


@pytest.mark.parametrize("bad", ["not json", "[1, 2]", 5])
async def test_args_that_are_not_an_object_answer_with_an_example_and_make_no_call(mongo_db, bad):
    result, calls = await _call_with(mongo_db, {"device": "pc", "tool": "write_file", "args": bad})
    assert result == mcp_tool.BAD_ARGS and calls == []


async def test_empty_device_and_tool_answer_with_a_call_example(mongo_db):
    result, calls = await _call_with(mongo_db, {"args": {}, "device": "", "tool": ""})
    assert result == mcp_tool.MISSING_PARAMS and 'tool="tools/list"' in result and calls == []
