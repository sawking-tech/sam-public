"""Команда оболочки только на чтение идёт без LLM-судьи (SAM-127); всё остальное - к нему."""
import pytest

from gateway.readonly_command import is_read_only
from gateway.tool_policy import ALLOW, ASK, load_policy

ALLOWED = ["wmic logicaldisk", "Get-PSDrive", "Get-ChildItem", "Select-Object", "Sort-Object", "df", "ls"]


@pytest.mark.parametrize("command, read_only", [
    ("wmic logicaldisk where \"DeviceId='C:'\" get Size,FreeSpace,VolumeName", True),
    ("powershell -Command \"Get-PSDrive C | Select-Object Free, Used\"", True),
    (r"pwsh -NoProfile -c 'Get-ChildItem -Path C:\ -Directory | Sort-Object Length | Select-Object -First 15'", True),
    ("df -h", True),
    ("ls -la /home", True),
    ("Get-PSDrive C; Remove-Item x", False),
    ("Get-ChildItem | Remove-Item", False),
    ("ls > out.txt", False),
    ("ls && rm -rf /", False),
    ("echo $(rm -rf /)", False),
    ("wmic logicaldisk where name='C:' set VolumeName=x", False),
    ("wmic process call create calc", False),
    ("rm -rf /", False),
    ("", False),
], ids=["wmic_get", "powershell_wrapped", "pwsh_pipeline", "df", "ls", "chained_write", "piped_write", "redirect",
        "and_then", "subshell", "wmic_set", "wmic_call", "rm", "empty"])
def test_read_only_command_skips_the_judge(command, read_only):
    assert is_read_only(command, ALLOWED) is read_only


@pytest.mark.parametrize("command, decision", [
    ("wmic logicaldisk where \"DeviceId='C:'\" get Size,FreeSpace", ALLOW),
    ("Get-Content .env", ASK),   # чтение файлов - к судье: так читаются ключи
    ("del C:\\notes.txt", ASK),
], ids=["read_only_allowed", "file_read_asks", "delete_asks"])
def test_shipped_policy_lets_read_only_through_and_asks_for_the_rest(command, decision):
    args = {"device": "pc", "tool": "run_command", "args": {"command": command}}
    assert load_policy().decide("mcp_call", args) == decision
