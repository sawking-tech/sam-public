"""Команда оболочки только на чтение (SAM-127): такую политика пропускает без LLM-судьи.

12 окт LLM-судья трижды назвал UNSAFE проверку места на диске (`wmic logicaldisk ... get`, `Get-PSDrive C`,
`Get-ChildItem -Directory | Sort-Object | Select-Object`), воркер не смог ответить, а главный агент выдумал цифры.
Чтение судье не нужно; всё остальное - по-прежнему к нему.

Команда - только на чтение, если:
- обёртка `powershell -Command "..."` / `pwsh -c '...'` разворачивается до внутренней команды;
- в ней нет цепочек и подстановок (`;`, `&`, перенаправлений, обратной кавычки, `$(`, `{`);
- каждое звено конвейера `|` начинается с команды из белого списка (без учёта регистра, префикс из слов);
- нет слов, которые меняют (`call`, `delete`, `create`, `set`) - у wmic и подобных глагол стоит в середине.
Белый список приходит параметром из конфига политики: команды Windows и Linux.
"""
from __future__ import annotations

import re
from typing import Iterable

_WRAPPER = re.compile(r"^(?:powershell|pwsh)(?:\.exe)?\s+(?:-\w+\s+)*?-(?:command|c)\s+([\"'])(.*)\1\s*$",
                      re.IGNORECASE | re.DOTALL)
_CHAINING = re.compile(r"[;&<>`{}]|\$\(|\r|\n")
_CHANGING_WORDS = re.compile(r"\b(?:call|delete|create|set)\b", re.IGNORECASE)


def unwrap(command: str) -> str:
    m = _WRAPPER.match(command.strip())
    return m.group(2).strip() if m else command.strip()


def is_read_only(command: str, allowed: Iterable[str]) -> bool:
    command = unwrap(command or "")
    if not command or _CHAINING.search(command) or _CHANGING_WORDS.search(command):
        return False
    prefixes = [p.lower().split() for p in allowed if p.strip()]
    for segment in command.split("|"):
        words = segment.lower().split()
        if not words or not any(words[:len(p)] == p for p in prefixes):
            return False
    return True
