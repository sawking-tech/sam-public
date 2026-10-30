"""Промпты SAM, вынесенные из agent.py (SAM-40 рефакторинг).

Все системные промпты, шаблоны и константы guard'а в одном пакете.
Это первый шаг к чистой архитектуре — промпты это shared assets,
импортируются и в application (agent) и в infrastructure (compression).
"""

from .worker import WORKER_SYSTEM
from .guard import (
    DANGEROUS_PATTERNS,
    GUARD_MSG_PROMPT,
    GUARD_INLINE_RULES,
    GUARD_SECRET_RULES,
    GUARD_BLOCK_MARK,
    GUARD_TOOL_PROMPT,
    GUARD_PRIVILEGE_PROMPT,
)
from .summarize import (
    SUMMARIZE_BASE_PROMPT,
    SUMMARIZE_DIFF_PROMPT,
    SUMMARIZE_MERGE_PROMPT,
    SUMMARIZE_THOUGHTS_PROMPT,
    is_trivial_summary,
)

__all__ = [
    "SYSTEM_PROMPT",
    "WORKER_SYSTEM",
    "DANGEROUS_PATTERNS",
    "GUARD_MSG_PROMPT",
    "GUARD_INLINE_RULES",
    "GUARD_SECRET_RULES",
    "GUARD_BLOCK_MARK",
    "GUARD_TOOL_PROMPT",
    "GUARD_PRIVILEGE_PROMPT",
    "SUMMARIZE_BASE_PROMPT",
    "SUMMARIZE_DIFF_PROMPT",
    "SUMMARIZE_MERGE_PROMPT",
    "SUMMARIZE_THOUGHTS_PROMPT",
    "is_trivial_summary",
]


def __getattr__(name: str):
    """SYSTEM_PROMPT - сборка из текущего снимка души на момент обращения (gateway/prompts/system.py)."""
    if name == "SYSTEM_PROMPT":
        from .system import system_prompt
        return system_prompt()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
