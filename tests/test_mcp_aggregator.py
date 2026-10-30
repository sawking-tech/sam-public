"""SAM-76: агрегатор MCP - ошибка инструмента и срок ожидания очереди к серверу.

Модуль агрегатора при импорте настраивает корневой логгер; тест возвращает обработчики pytest.
"""
import asyncio
import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

AGGREGATOR = Path(__file__).resolve().parent.parent / "mcp" / "mcp_aggregator.py"
SHORT_S = 0.05   # срок, который заведомо истекает: сессия висит без конца
LONG_S = 5       # срок, который в тесте не истекает


@pytest.fixture(scope="module")
def agg():
    handlers, filters, level = list(logging.root.handlers), list(logging.root.filters), logging.root.level
    spec = importlib.util.spec_from_file_location("sam_mcp_aggregator", AGGREGATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    logging.root.handlers[:] = handlers
    logging.root.filters[:] = filters
    logging.root.setLevel(level)
    return module


class _Session:
    """Сессия stdio-сервера: отвечает сразу или висит, пока тест не отпустит; entered - вызов вошёл в сессию."""

    def __init__(self, is_error=False, text="ok", hang=False):
        self._is_error, self._text = is_error, text
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        if not hang:
            self.release.set()

    async def call_tool(self, name, arguments):
        self.entered.set()
        await self.release.wait()
        return SimpleNamespace(content=[SimpleNamespace(text=self._text)], isError=self._is_error)


class _Broken:
    async def call_tool(self, name, arguments, timeout=None):
        raise ConnectionError("stdio server died")


def _server(agg, session):
    srv = agg.SubServer("test")
    srv.session = session
    return srv


async def _crash_in_aggregator(agg):
    a = agg.MCPAggregator.__new__(agg.MCPAggregator)
    a._tool_index = {"x": _Broken()}
    return await a.call("x", {})


@pytest.mark.parametrize("call, is_error, text", [
    (lambda agg: _server(agg, _Session(is_error=True, text="denied")).call_tool("x", {}), True, "denied"),
    (lambda agg: _server(agg, _Session(text="done")).call_tool("x", {}), False, "done"),
    (_crash_in_aggregator, True, "died"),
], ids=["tool_error_flagged", "ok_not_flagged", "server_crash_in_aggregator"])
async def test_tool_result_and_crash_become_flagged_results(agg, call, is_error, text):
    out = await call(agg)
    assert out.is_error is is_error and text in out.text


async def test_queue_wait_and_execution_timeouts_differ(agg, monkeypatch):
    """Стоявший в очереди за долгим вызов не начинался - так и сказано; срок исполнения считается от начала;
    упавший вызов отпускает сервер."""
    monkeypatch.setattr(agg, "QUEUE_TIMEOUT_S", SHORT_S)
    busy = _Session(hang=True)
    srv = _server(agg, busy)
    first = asyncio.create_task(srv.call_tool("slow", {}, timeout=LONG_S))
    await busy.entered.wait()                         # первый вызов вошёл в сессию и держит сервер
    queued = await srv.call_tool("next", {}, timeout=LONG_S)
    assert queued.is_error and "not started" in queued.text
    busy.release.set()
    assert not (await first).is_error

    monkeypatch.setattr(agg, "QUEUE_TIMEOUT_S", LONG_S)
    out = await _server(agg, _Session(hang=True)).call_tool("slow", {}, timeout=SHORT_S)
    assert out.is_error and "timed out" in out.text

    srv = _server(agg, _Broken())
    with pytest.raises(ConnectionError):
        await srv.call_tool("x", {})
    srv.session = _Session(text="ok")
    out = await srv.call_tool("y", {})
    assert out.text == "ok" and not out.is_error


def test_aggregator_refuses_call_without_key(agg, monkeypatch):
    """SAM-79: агрегатор на 0.0.0.0 - без ключа powershell на ПК не запустить."""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SAM_API_KEY", "s3cret")
    client = TestClient(agg.create_app("unused.json"))   # без with: серверы MCP не поднимаются
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "powershell", "arguments": {"script": "Get-Process"}}}
    assert client.post("/mcp", json=body).status_code == 401


@pytest.mark.parametrize("content_type, status", [
    ("text/plain", 415),                               # простой POST любой страницы в браузере (SAM-188)
    ("application/x-www-form-urlencoded", 415),
    ("application/json; charset=utf-8", 200),
], ids=["text_plain", "form", "json"])
def test_aggregator_takes_json_rpc_only_as_json(agg, monkeypatch, content_type, status):
    """С этой машины ключ не нужен: страница браузера звала run_command простым POST без preflight."""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("SAM_API_KEY", "")
    client = TestClient(agg.create_app("unused.json"))   # без with: серверы MCP не поднимаются
    body = '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}'
    r = client.post("/mcp", content=body, headers={"Content-Type": content_type})
    assert r.status_code == status
