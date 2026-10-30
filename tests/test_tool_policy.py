"""Слой политик вокруг tools (SAM-79): можно / нельзя / спросить по конфигу, решает код до вызова.
Путь политики передаётся явно: SAM_TOOL_POLICY стенда в тесты не попадает."""
import pytest

from gateway import tool_policy as tp
from tests.fakes_agents import policy

SHIPPED = "shipped"
BROKEN = "broken"   # битый файл - строка в лог и поставляемая политика
RUN_CHROME = (("mcp_call(run_command:*)", "ask"), ("mcp_call(run_command:start chrome*)", "allow"))
ENV_KEYS = (("set_env(*)", "allow"), ("set_env(*_API_KEY)", "deny"))
KEY_FILE = "data/secrets/privilege.key"


def _run(tool, **args):
    return {"tool": tool, "args": args}


@pytest.mark.parametrize("source, tool, args, expected", [
    # последнее совпавшее правило побеждает, без совпадений - allow
    (RUN_CHROME, "mcp_call", _run("run_command", command="del /s c:\\"), tp.ASK),
    (RUN_CHROME, "mcp_call", _run("run_command", command="start chrome"), tp.ALLOW),
    (RUN_CHROME, "mcp_call", _run("screenshot"), tp.ALLOW),
    # поздний deny перекрывает ранний allow
    (ENV_KEYS, "set_env", {"key": "GEMINI_API_KEY"}, tp.DENY),
    (ENV_KEYS, "set_env", {"key": "VOICE_SPEED"}, tp.ALLOW),
    # правило без уточнения покрывает весь инструмент
    ((("claude_delegate", "ask"),), "claude_delegate", {"task": "x"}, tp.ASK),
    # поставляемая политика: команды на ПК и окружение
    (SHIPPED, "mcp_call", _run("powershell", script="Stop-Process -Name notepad"), tp.ASK),
    (SHIPPED, "mcp_call", _run("powershell", script="Get-Process"), tp.ALLOW),       # только чтение (SAM-127)
    (SHIPPED, "mcp_call", _run("bg_start", command="notepad"), tp.ASK),
    (SHIPPED, "mcp_call", _run("open_terminal", command="del /q C:/x"), tp.ASK),     # команда в cmd.exe /K
    (SHIPPED, "mcp_call", _run("open_terminal"), tp.ALLOW),                          # пустой терминал можно
    (SHIPPED, "set_env", {"key": "GEMINI_API_KEY", "value": "x"}, tp.DENY),
    (SHIPPED, "set_env", {"key": "LMSTUDIO_URL", "value": "http://evil"}, tp.DENY),
    (SHIPPED, "set_env", {"key": "PLAYWRIGHT_MCP_EXTENSION_TOKEN", "value": "x"}, tp.ALLOW),
    (SHIPPED, "remember", {"text": "x"}, tp.ALLOW),
    # ключ привилегий не читается агентом ни одним путём
    (SHIPPED, "mcp_call", _run("run_command", command="type data\\secrets\\privilege.key"), tp.DENY),
    (SHIPPED, "run_command", {"command": f"cat {KEY_FILE}"}, tp.DENY),               # агент Cortex зовёт по имени
    (SHIPPED, "read_text_file", {"path": f"C:/repo/{KEY_FILE}"}, tp.DENY),
    (BROKEN, "set_env", {"key": "GEMINI_API_KEY"}, tp.DENY),
])
def test_policy_decides_by_rules(source, tool, args, expected, tmp_path, capsys):
    if source == SHIPPED:
        rules = tp.load_policy(tp.DEFAULT_POLICY_FILE)
    elif source == BROKEN:
        bad = tmp_path / "policy.json"
        bad.write_text("{not json", encoding="utf-8")
        rules = tp.load_policy(bad)
        assert "policy" in capsys.readouterr().out.lower()
    else:
        rules = policy(*source)
    assert rules.decide(tool, args) == expected


@pytest.mark.parametrize("rule, action, said", [
    ("mcp_call(oops", "ask", r"mcp_call\(oops"),
    ("set_env", "maybe", "maybe"),
])
def test_bad_rule_is_one_line_error(rule, action, said):
    with pytest.raises(tp.PolicyError, match=said) as err:
        tp.parse_rule(rule, action)
    assert "\n" not in str(err.value)
