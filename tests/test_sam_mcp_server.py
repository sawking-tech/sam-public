"""MCP-сервер SAM для основной модели вне шлюза (SAM-175): инструменты поверх REST шлюза. Граница - REST: заготовки
ответов по пути или настоящее приложение шлюза за urlopen (сквозная проверка ключа привилегий)."""
import importlib.util
import json
import urllib.request
from pathlib import Path

import pytest

from gateway import privilege_key as pk
from tests.fakes_agents import GatewayRig, UrlopenToApp

ROOT = Path(__file__).resolve().parents[1]
REPLIES = {"/sam/memory/search": {"facts": ["День рождения Ирины - 3 марта."]},
           "/sam/memory": {"result": "OK: запомнила"}, "/sam/agents": {"task_id": "ab12", "status": "ok"},
           "/sam/inject": {"status": "ok"}, "/sam/push/ack": {"acked": 2},
           "/sam/main-model": {"result": "OK: основная модель zai / glm"}, "/sam/agents/ab12/grant": {"granted": True},
           "/sam/agents/ab12/steps": {"granted": True, "result": "выдано 4 шагов, остаток задачи 4"},
           "/sam/sandbox/exec": {"result": "Код выхода 0 за 0.1 мс", "code": 0, "ms": 0.1},
           "/sam/stand/restart": {"result": "mcp: перезапущен"}}


def _load(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "mcp"))
    spec = importlib.util.spec_from_file_location("sam_server", ROOT / "mcp" / "sam_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv(pk.FILE_ENV, str(tmp_path / "privilege.key"))
    monkeypatch.setattr(pk, "_cached", [])
    mod = _load(monkeypatch)
    mod.calls = []

    def rest(method, path, body=None, query=None, headers=None):
        mod.calls.append({"method": method, "path": path, "body": body, "query": query, "headers": headers})
        return REPLIES[path]
    monkeypatch.setattr(mod, "request", rest)
    return mod


@pytest.mark.parametrize("call, method, path, body, query, out", [
    (lambda s: s.memory_search("день рождения Ирины"), "GET", "/sam/memory/search", None,
     {"q": "день рождения Ирины", "limit": 5}, "День рождения Ирины - 3 марта."),
    (lambda s: s.remember("Ирина любит чай"), "POST", "/sam/memory",
     {"fact": "Ирина любит чай", "summary": "", "tags": ""}, None, "OK: запомнила"),
    (lambda s: s.create_agent("проверь диск", level="weak"), "POST", "/sam/agents",
     {"description": "проверь диск", "level": "weak"}, None, "агент ab12: ok"),
    (lambda s: s.create_agent("узнай время", tools=["get_current_time"], framework="cortex", peers=["ab12"], depth=1),
     "POST", "/sam/agents", {"description": "узнай время", "tools": ["get_current_time"], "framework": "cortex",
                             "peers": ["ab12"], "depth": 1}, None, "агент ab12: ok"),
    (lambda s: s.answer_agent("ab12", "порт 8000"), "POST", "/sam/inject",
     {"session_id": "task:ab12", "message": "порт 8000"}, None, "ok"),
    (lambda s: s.grant_agent_tool("ab12", "run_command"), "POST", "/sam/agents/ab12/grant", {"tool": "run_command"},
     None, "выдано"),
    (lambda s: s.grant_agent_steps("task:ab12", 4, "по делу"), "POST", "/sam/agents/ab12/steps",
     {"steps": 4, "reason": "по делу"}, None, "выдано 4 шагов, остаток задачи 4"),
    (lambda s: s.push_ack(["a", "b"]), "POST", "/sam/push/ack", {"ids": ["a", "b"]}, None, "подтверждено: 2"),
    (lambda s: s.switch_main_model("zai"), "POST", "/sam/main-model", {"source": "zai", "model": "", "main": ""}, None,
     "OK: основная модель zai / glm"),
    (lambda s: s.sandbox_exec("x + 1"), "POST", "/sam/sandbox/exec", {"code": "x + 1", "lang": "", "timeout": 0}, None,
     "Код выхода 0 за 0.1 мс"),
    (lambda s: s.restart_component("mcp"), "POST", "/sam/stand/restart", {"name": "mcp", "force": False}, None,
     "mcp: перезапущен"),
], ids=["memory_search", "remember", "create_agent", "create_cortex_agent", "answer_agent", "grant", "grant_steps", "push_ack",
        "switch_main_model", "sandbox_exec", "restart_component"])
def test_sam_mcp_tools_call_gateway_with_key_only_where_needed(server, call, method, path, body, query, out):
    assert call(server) == out
    sent = server.calls[-1]
    assert (sent["method"], sent["path"], sent["body"], sent["query"]) == (method, path, body, query)
    assert sent["headers"] == (pk.headers() if pk.needs(method, path) else {})


def test_gateway_failure_is_one_line_for_the_model(server, monkeypatch):
    def down(*a, **kw):
        raise server.SamHttpError("шлюз недоступен; запущен ли SAM: start_sam.ps1")
    monkeypatch.setattr(server, "request", down)
    assert server.memory_search("x") == "шлюз недоступен; запущен ли SAM: start_sam.ps1"


def test_privileged_call_passes_the_gateway_with_the_key(monkeypatch, tmp_path):
    """Сквозь настоящий слой доступа: с ключом create_agent доходит до ручки - отказ ручки 400, а не 401."""
    gw = GatewayRig(monkeypatch, tmp_path)
    mod = _load(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", UrlopenToApp(gw.client))
    out = mod.create_agent("x", level="genius")
    assert out.startswith("шлюз ответил 400") and "genius" in out


def test_server_is_not_in_the_shared_aggregator():
    # ревью 12 окт: в агрегаторе его видели бы агенты Cortex и воркеры - смена модели и запись в память без ключа
    for path in (ROOT / "mcp" / "configs").glob("*.json"):
        cfg = json.loads(path.read_text(encoding="utf-8"))
        assert not any(s.get("args") == ["mcp/sam_server.py"] for s in cfg.get("servers", [])), path.name
