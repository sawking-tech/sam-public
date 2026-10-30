"""Настройка Cortex из окружения (ADR-022). Процесс Cortex читает .env репозитория сам: шлюз передаёт подпроцессу
только переменные из SAM_ACP_ENV."""
from __future__ import annotations

import os
from dataclasses import dataclass

from gateway.env import parse_int, read

MAX_TURNS_ENV = "SAM_CORTEX_MAX_TURNS"
TASK_ENV = "SAM_CORTEX_LLM_TASK"
PERMISSION_TOOLS_ENV = "SAM_CORTEX_PERMISSION_TOOLS"
SEARCH_MIN_TOOLS_ENV = "SAM_CORTEX_TOOL_SEARCH_MIN"
CORE_TOOLS_ENV = "SAM_CORTEX_CORE_TOOLS"
SEARCH_LIMIT_ENV = "SAM_CORTEX_TOOL_SEARCH_LIMIT"
LATE_INJECTS_ENV = "SAM_CORTEX_LATE_INJECTS"

# Шагов цикла на ход: как у главного хода шлюза (run_agent_loop по умолчанию)
DEFAULT_MAX_TURNS = 20
# Задача роутера моделей: default - та же цепочка поставщиков, что у шлюза; acp пропускается всегда (это мы сами)
DEFAULT_TASK = "default"
SELF_SOURCE = "acp"
# Инструменты MCP стенда, которые меняют машину: перед вызовом - разрешение клиента, то есть судьи SAM.
# Чтение, радар и время идут без судьи: лишний вызов модели на каждый вопрос «где кто».
# Инструмент с пометкой destructiveHint в MCP спрашивает разрешение и без этого списка.
DEFAULT_PERMISSION_TOOLS = (
    "run_command", "open_terminal", "powershell", "bg_start",
    "write_file", "edit_file", "move_file", "create_directory",
    "browser_run_code", "browser_evaluate", "browser_file_upload",
    "git_commit", "git_add", "git_reset", "git_checkout", "git_create_branch",
)


# Поиск инструментов включается, когда их в сессии больше порога: описание инструмента стенда в среднем 530 знаков
# (70 инструментов - 36 950 знаков, замер 12 окт), 20 инструментов - около 10 тыс. знаков на каждый вызов модели
DEFAULT_SEARCH_MIN_TOOLS = 20
# Видны всегда: load_skill - скилл говорит модели, какой инструмент нужен, и называет его по имени
DEFAULT_CORE_TOOLS = ("load_skill",)
DEFAULT_SEARCH_LIMIT = 5   # находок на запрос, как у Hermes
# Продолжений хода по реплике, пришедшей во время финального ответа: одна-две на ход в жизни; дальше - петля агентов,
# будящих друг друга внутри одного хода
DEFAULT_LATE_INJECTS = 3


@dataclass(frozen=True)
class CortexConfig:
    max_turns: int = DEFAULT_MAX_TURNS
    llm_task: str = DEFAULT_TASK
    permission_tools: frozenset[str] = frozenset(DEFAULT_PERMISSION_TOOLS)
    search_min_tools: int = DEFAULT_SEARCH_MIN_TOOLS
    core_tools: tuple[str, ...] = DEFAULT_CORE_TOOLS
    search_limit: int = DEFAULT_SEARCH_LIMIT
    late_injects: int = DEFAULT_LATE_INJECTS


def from_env(environ: dict | None = None) -> CortexConfig:
    env = os.environ if environ is None else environ
    tools = _names(env.get(PERMISSION_TOOLS_ENV), DEFAULT_PERMISSION_TOOLS)
    return CortexConfig(
        max_turns=read(MAX_TURNS_ENV, DEFAULT_MAX_TURNS, parse_int, environ=env),
        llm_task=(env.get(TASK_ENV) or DEFAULT_TASK).strip(),
        permission_tools=frozenset(tools),
        search_min_tools=read(SEARCH_MIN_TOOLS_ENV, DEFAULT_SEARCH_MIN_TOOLS, parse_int, environ=env),
        core_tools=_names(env.get(CORE_TOOLS_ENV), DEFAULT_CORE_TOOLS),
        search_limit=read(SEARCH_LIMIT_ENV, DEFAULT_SEARCH_LIMIT, parse_int, environ=env),
        late_injects=read(LATE_INJECTS_ENV, DEFAULT_LATE_INJECTS, parse_int, environ=env),
    )


def _names(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    """Список имён через запятую; не задан - default, пустая строка - пустой список."""
    if raw is None:
        return default
    return tuple(n.strip() for n in raw.split(",") if n.strip())
