"""Слой политик вокруг tools (SAM-79): можно, нельзя или спросить - по конфигу, решает код до вызова.

Правило - `tool(шаблон)` и действие allow, ask или deny. Шаблон сверяется glob-ом (`*`, `?`) с предметом
вызова: у mcp_call это `инструмент:команда`, у set_env - имя ключа, у остальных - аргументы JSON.
Правило без скобок покрывает весь tool.

Порядок - последнее подходящее правило побеждает, как в opencode (opencode.ai/docs/permissions): общее правило
пишется первым, исключения после него. Так белый список делает исключение из общего запрета (set_env) и из
общего «спросить» (команды на ПК). В Claude Code иначе: deny побеждает всегда, allow не вырезает исключение
из deny (code.claude.com/docs/en/permissions); SAM нужен белый список поверх запрета, поэтому порядок opencode.
tool без правил - allow: прежняя проверка (шаблоны опасных строк, LLM-guard у HIGH_RISK_TOOLS) остаётся.

ask в SAM - не вопрос человеку: вызов идёт к LLM-судье guard (gateway/guard.py) даже у tool не из
HIGH_RISK_TOOLS. Голосовое подтверждение - SAM-68.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass
from typing import Iterable
from pathlib import Path

from .readonly_command import is_read_only

ALLOW, ASK, DENY = "allow", "ask", "deny"
ACTIONS = (ALLOW, ASK, DENY)
DEFAULT_POLICY_FILE = Path(__file__).resolve().parent / "tool_policy.json"
POLICY_FILE = Path(os.getenv("SAM_TOOL_POLICY", str(DEFAULT_POLICY_FILE)))
_RULE = re.compile(r"^(?P<tool>[\w*?/.-]+)(?:\((?P<spec>.*)\))?$")
# Аргументы mcp_call, в которых лежит команда инструмента на ПК (mcp/windows_control_server.py)
_COMMAND_ARGS = ("command", "script", "program", "path", "url")
# Инструменты ПК, чья команда - строка оболочки: ask для команды только на чтение становится allow (SAM-127)
SHELL_TOOLS = ("run_command", "powershell")


class PolicyError(ValueError):
    """Правило или файл политик не разобран; текст - что не так."""


@dataclass(frozen=True)
class Rule:
    tool: str
    spec: str | None
    action: str

    def matches(self, tool: str, subject: str) -> bool:
        if not fnmatch.fnmatchcase(tool, self.tool):
            return False
        return self.spec is None or fnmatch.fnmatchcase(subject.lower(), self.spec.lower())


def parse_rule(text: str, action: str) -> Rule:
    if action not in ACTIONS:
        raise PolicyError(f"правило {text!r}: действие {action!r}, а нужно одно из {', '.join(ACTIONS)}")
    m = _RULE.match(text.strip())
    if not m:
        raise PolicyError(f"правило {text!r} не разобрано: нужно tool или tool(шаблон)")
    return Rule(m["tool"], m["spec"], action)


def subject(tool: str, args: dict) -> str:
    """Что сверяется с шаблоном правила."""
    if tool == "mcp_call":
        inner = args.get("args") if isinstance(args.get("args"), dict) else {}
        command = next((str(inner[k]) for k in _COMMAND_ARGS if inner.get(k)), "")
        return f"{args.get('tool', '')}:{command}"
    if tool in ("set_env", "get_env"):
        return str(args.get("key", ""))
    return json.dumps(args, ensure_ascii=False, sort_keys=True)


class ToolPolicy:
    def __init__(self, rules: list[Rule], read_only: Iterable[str] = ()) -> None:
        self.rules = list(rules)
        self.read_only = tuple(read_only)

    def decide(self, tool: str, args: dict) -> str:
        subj = subject(tool, args)
        hits = [r.action for r in self.rules if r.matches(tool, subj)]
        action = hits[-1] if hits else ALLOW
        if action == ASK and self._read_only_shell(tool, args):
            return ALLOW
        return action

    def _read_only_shell(self, tool: str, args: dict) -> bool:
        """mcp_call команды оболочки ПК только на чтение (readonly_command): судье не нужна."""
        if tool != "mcp_call" or args.get("tool") not in SHELL_TOOLS:
            return False
        inner = args.get("args") if isinstance(args.get("args"), dict) else {}
        return is_read_only(str(inner.get("command") or inner.get("script") or ""), self.read_only)


def parse_policy(data: dict) -> ToolPolicy:
    rules = data.get("rules")
    if not isinstance(rules, list):
        raise PolicyError("в файле политик нет списка rules")
    bad = next((r for r in rules if not isinstance(r, dict)), None)
    if bad is not None:
        raise PolicyError(f"правило {bad!r} - не объект: нужно {{\"rule\": ..., \"action\": ...}}")
    read_only = data.get("read_only_commands", [])
    if not isinstance(read_only, list):
        raise PolicyError("read_only_commands - не список строк")
    return ToolPolicy([parse_rule(str(r.get("rule", "")), str(r.get("action", ""))) for r in rules],
                      [str(c) for c in read_only])


def load_policy(path: Path = POLICY_FILE) -> ToolPolicy:
    """Файл политик; не читается или не разобран - строка в лог и политика из репозитория."""
    try:
        return parse_policy(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        if Path(path) == DEFAULT_POLICY_FILE:
            raise
        print(f"[Policy] {path} не прочитан ({e}); действует политика {DEFAULT_POLICY_FILE.name}")
        return load_policy(DEFAULT_POLICY_FILE)
