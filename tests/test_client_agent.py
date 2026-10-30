"""Клиентский агент ПК (mcp/client_agent.py) поднимает свой агрегатор MCP дочерним процессом (stand 4).
Модуль настоящий; подменены запуск процесса, пауза старта и сеть."""
import subprocess
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "mcp"))
import client_agent  # noqa: E402
from tests.fakes_core import patch_module_sleep


class _Proc:
    def __init__(self, cmd, alive=True, **kw):
        self.cmd, self.alive, self.kw = cmd, alive, kw
        self.pid, self.terminated = 4242, False

    def poll(self):
        return None if self.alive else 1

    def communicate(self):
        return "Address already in use", None

    def terminate(self):
        self.terminated = True


@pytest.fixture
def popen(monkeypatch):
    started = []

    def fake(cmd, alive=True, **kw):
        started.append(_Proc(cmd, alive=fake.alive, **kw))
        return started[-1]
    fake.alive = True
    monkeypatch.setattr(client_agent.subprocess, "Popen", fake)
    patch_module_sleep(monkeypatch, client_agent, lambda s: None)
    return started, fake


def test_aggregator_is_a_child_process_on_the_mcp_port(popen, monkeypatch):
    started, fake = popen
    monkeypatch.setattr(client_agent, "MCP_PORT", 3999)
    proc = client_agent.start_mcp_proxy()
    assert proc is started[0]
    cmd = proc.cmd
    assert cmd[0] == sys.executable and cmd[1].endswith("mcp_aggregator.py") and cmd[2:] == ["--port", "3999"]
    assert proc.kw["stdout"] == subprocess.PIPE

    fake.alive = False                     # агрегатор упал сразу (порт занят) - агент живёт без него
    assert client_agent.start_mcp_proxy() is None


def test_main_starts_the_aggregator_and_stops_it_on_exit(popen, monkeypatch):
    started, _ = popen
    posted = []
    monkeypatch.setattr(client_agent, "collect_system_info", lambda name, server: {"ip": "1", "gpu": "g",
                                                                                   "capabilities": []})
    monkeypatch.setattr(client_agent, "post_json", lambda url, data: posted.append(url) or True)
    # первый же сон цикла heartbeat - сигнал остановки
    handlers = {}
    monkeypatch.setattr(client_agent.signal, "signal", lambda sig, handler: handlers.update(stop=handler))
    patch_module_sleep(monkeypatch, client_agent, lambda s: handlers and handlers["stop"](None, None))

    for argv, children in ((["client_agent.py"], 1), (["client_agent.py", "--no-mcp"], 0)):
        started.clear()
        monkeypatch.setattr(sys, "argv", argv)
        client_agent.main()
        assert len(started) == children
        assert all(p.terminated for p in started)
    assert posted[-1].endswith("/sam/client/offline")
