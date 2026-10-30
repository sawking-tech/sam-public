"""Область записи для guard (SAM-236, ADR-026): решение по пути, без модели-судьи.

Судья по строке вызова слабой моделью режет безобидное (копирование в рабочую копию, запись в свою ветку) и называет это
«правкой ядра». Для файловых вызовов `mcp_call` путь известен точно, поэтому решает код: запись внутри рабочих мест
агентов (gateway/tools/workspace.py) - обычный вызов; секретный путь (.env, data/secrets, ключи) - всегда под guard;
всё остальное, в том числе команды оболочки, по-прежнему решает судья. Чистые функции: пути приходят аргументами.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

MCP_CALL = "mcp_call"
WRITE_TOOLS = frozenset({"write_file", "edit_file", "create_directory", "move_file"})
PATH_KEYS = ("path", "paths", "source", "destination", "file_path")
SECRET_PARTS = ("data/secrets",)
SECRET_SUFFIXES = (".key", ".pem")
ENV_NAME = ".env"
ENV_PUBLIC = (".env.example", ".env.sample")   # образцы без значений секретов


def _inner_args(args: dict) -> dict:
    """Аргументы инструмента MCP: словарь, либо строка JSON (так их шлёт слабая модель, SAM-239)."""
    inner = args.get("args")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            return {}
    return inner if isinstance(inner, dict) else {}


def _paths(inner: dict) -> list[str]:
    found: list[str] = []
    for key in PATH_KEYS:
        value = inner.get(key)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, list):
            found.extend(v for v in value if isinstance(v, str))
    return found


def is_secret(path: str) -> bool:
    normal = path.replace("\\", "/").lower()
    name = normal.rsplit("/", 1)[-1]
    if name in ENV_PUBLIC:
        return False
    return (name == ENV_NAME or name.startswith(ENV_NAME + ".") or name.endswith(SECRET_SUFFIXES)
            or any(part in normal for part in SECRET_PARTS))


def inside_any(path: str, roots: list[Path]) -> bool:
    """Абсолютный путь после разбора лежит в одном из корней; относительный не определён - не внутри."""
    target = Path(path)
    if not target.is_absolute():
        return False
    resolved = target.resolve()
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


def verdict(tool_name: str, args: dict, roots: list[Path]) -> bool | None:
    """True - обычный вызов без судьи; False - привилегированный (секретный путь); None - решает судья."""
    if tool_name != MCP_CALL:
        return None
    inner = _inner_args(args)
    paths = _paths(inner)
    if any(is_secret(p) for p in paths):
        return False
    if str(args.get("tool") or "") in WRITE_TOOLS and paths and all(inside_any(p, roots) for p in paths):
        return True
    return None


REASON_SECRET = "секретный путь ({path}) закрыт для агента: .env, data/secrets и ключи читает только человек"
REASON_OUTSIDE = "путь {path} вне рабочего места агента: открой место инструментом workspace и пиши в его папке"


def refusal_reason(tool_name: str, args: dict, roots: list[Path]) -> str:
    """Причина отказа файловому вызову по пути; пусто - вызов не файловый или причина не в пути (решал судья)."""
    if tool_name != MCP_CALL:
        return ""
    paths = _paths(_inner_args(args))
    secret = next((p for p in paths if is_secret(p)), None)
    if secret:
        return REASON_SECRET.format(path=secret)
    outside = next((p for p in paths if not inside_any(p, roots)), None)
    if str(args.get("tool") or "") in WRITE_TOOLS and outside:
        return REASON_OUTSIDE.format(path=outside)
    return ""


REFUSED_MEMORY_MAX = 256   # отказанных вызовов в памяти: хватает на ход агента, дальше старые вытесняются
REPEAT_REFUSED = "Этот вызов уже отказан, тот же вызов даст тот же отказ: измени подход или запроси право через request_permission."


class RefusalMemory:
    """Отказанные вызовы по сессии: второй такой же подряд не доходит до guard и не ведёт модель в цикл-стража."""

    def __init__(self, limit: int = REFUSED_MEMORY_MAX):
        self._limit = limit
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()

    @staticmethod
    def _key(session_id: str, tool_name: str, args: dict) -> tuple[str, str, str]:
        return session_id, tool_name, json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)

    def repeated(self, session_id: str, tool_name: str, args: dict) -> bool:
        return self._key(session_id, tool_name, args) in self._seen

    def remember(self, session_id: str, tool_name: str, args: dict) -> None:
        self._seen[self._key(session_id, tool_name, args)] = None
        while len(self._seen) > self._limit:
            self._seen.popitem(last=False)
