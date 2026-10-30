"""Запуск процессов в mcp/windows_control_server.py: stdin не наследуется, таймаут гасит дерево, вывод декодируется."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent / "mcp" / "windows_control_server.py"
GREETING = "Привет"


@pytest.fixture(scope="module")
def server():
    spec = importlib.util.spec_from_file_location("windows_control_server", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePopen:
    """Popen без процесса: запоминает аргументы, communicate отдаёт заданное или бросает таймаут."""

    def __init__(self, outputs, timeouts=0):
        self.outputs, self.timeouts, self.kwargs, self.pid = outputs, timeouts, None, 4242

    def __call__(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        return self

    def communicate(self, timeout=None):
        if self.timeouts:
            self.timeouts -= 1
            raise subprocess.TimeoutExpired("cmd", timeout)
        return self.outputs


@pytest.mark.parametrize("data, expected", [
    (GREETING.encode("utf-8"), GREETING),
    (GREETING.encode("cp866"), GREETING),
    (b"git version 2.48", "git version 2.48"),
], ids=["utf8_kept", "oem_console_fallback", "ascii"])
def test_output_decoded_utf8_then_console_codepage(server, data, expected):
    assert server.decode_output(data, "cp866") == expected


def test_empty_output_marked_and_long_cut(server):
    assert server.format_output("  ", "\n") == server.NO_OUTPUT
    assert server.format_output("x" * 50, "", limit=10) == "x" * 10


def test_child_does_not_inherit_server_stdin(server, monkeypatch):
    fake = FakePopen((b"ok", b""))
    monkeypatch.setattr(server.subprocess, "Popen", fake)
    assert server.run_process("git --version", shell=True) == "ok"
    assert fake.kwargs["stdin"] is subprocess.DEVNULL


def test_timeout_kills_tree_and_returns_partial_output(server, monkeypatch):
    fake = FakePopen((b"start\n", b""), timeouts=1)
    killed = []
    monkeypatch.setattr(server.subprocess, "Popen", fake)
    monkeypatch.setattr(server, "_kill_tree", killed.append)
    result = server.run_process("python long.py", shell=True, timeout=3)
    assert killed == [fake]
    assert result.startswith("Timeout (3s)") and result.endswith("start")


def test_command_timeout_below_aggregator_call_timeout(server):
    assert server.COMMAND_TIMEOUT_S < 30.0   # MCP_CALL_TIMEOUT_S по умолчанию в mcp/mcp_aggregator.py


def test_powershell_prelude_reads_files_as_utf8(server):
    prelude = server.powershell_prelude()
    assert "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8" in prelude
    assert "$PSDefaultParameterValues['Get-Content:Encoding'] = 'UTF8'" in prelude
