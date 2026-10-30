"""Счёт токенов (SAM-48): usage каждого вызова LLM с сессией и ролью.

Роль и сессию ставит вызывающий код через usage_scope: agent_loop (main / worker),
guard, суммаризатор. llm_client после вызова зовёт record_call, тот собирает
документ и отдаёт его приёмнику. Приёмник подключает корень композиции
(gateway/agent.py: TokenUsageRepo.record); без него запись не идёт, и счёт
никогда не роняет сам вызов LLM.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

ROLE_MAIN = "main"            # ход главного агента (runner, agent_loop оркестратора)
ROLE_WORKER = "worker"        # agent_loop воркера, сессия task:XXXX
ROLE_GUARD = "guard"          # судья вызовов инструментов guard.py: обычный и привилегированный
ROLE_JUDGE = "judge"          # судья входящей реплики и ответа guard.py: на пути реального времени
ROLE_SUMMARIZE = "summarize"  # сжатие истории
ROLE_WORKFLOW = "workflow"    # шаг ask workflow: разовый вопрос основной модели (workflow_ask.py)
ROLE_UNKNOWN = "unknown"      # вызов вне usage_scope: видно, какой путь ещё не размечен
SESSION_UNKNOWN = ""
WORKER_SESSION_PREFIX = "task:"
# Поле usage со стоимостью вызова, если поставщик её присылает (OpenRouter - usage.cost, проверено 27 окт). Валюту
# запись не хранит: её объявляет книга цен для источника (gateway/pricing.py), код имён поставщиков не знает
USAGE_COST_FIELD = "cost"

_scope: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "llm_usage_scope", default=None,
)
_sink: Callable[[dict], None] | None = None
# Трасса хода (SAM-81): начало хода и этапы в мс от него. Словарь общий для дочерних задач хода:
# ContextVar копируется в задачу, а сам словарь - тот же объект
_trace: contextvars.ContextVar[dict | None] = contextvars.ContextVar("llm_turn_trace", default=None)
MS_PER_SECOND = 1000


def set_sink(sink: Callable[[dict], None] | None) -> None:
    """Куда писать документы; None - отключить запись."""
    global _sink
    _sink = sink


@contextlib.contextmanager
def usage_scope(session_id: str, role: str) -> Iterator[None]:
    """Все вызовы LLM внутри блока записываются на эту сессию и роль."""
    token = _scope.set((session_id, role))
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope() -> tuple[str, str]:
    return _scope.get() or (SESSION_UNKNOWN, ROLE_UNKNOWN)


def role_scope(role: str):
    """Сменить роль, оставив сессию внешнего блока (guard внутри хода)."""
    return usage_scope(current_scope()[0], role)


@contextlib.contextmanager
def trace_turn() -> Iterator[None]:
    """Ход с трассой: этапы внутри блока меряются от его начала (Claude Code пишет так же ttft_ms
    в span llm_request, docs: code.claude.com/docs/en/monitoring-usage)."""
    token = _trace.set({"t0": time.monotonic(), "stages": {}})
    try:
        yield
    finally:
        _trace.reset(token)


def mark_stage(name: str) -> None:
    """Этап хода: первый раз за ход, мс от начала. Вне trace_turn ничего не делает."""
    trace = _trace.get()
    if trace is not None and name not in trace["stages"]:
        trace["stages"][name] = int((time.monotonic() - trace["t0"]) * MS_PER_SECOND)


def current_stages() -> dict[str, int]:
    trace = _trace.get()
    return dict(trace["stages"]) if trace is not None else {}


def role_for_session(session_id: str) -> str:
    return ROLE_WORKER if session_id.startswith(WORKER_SESSION_PREFIX) else ROLE_MAIN


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def extract_usage(response: Any) -> dict | None:
    """usage ответа в виде {input, output, total}, плюс cached - если поставщик прислал
    prompt_tokens_details.cached_tokens (z.ai, OpenAI); None, если usage нет."""
    usage = _field(response, "usage")
    if usage is None:
        return None
    inp = _field(usage, "prompt_tokens")
    out = _field(usage, "completion_tokens")
    total = _field(usage, "total_tokens")
    if inp is None and out is None and total is None:
        return None
    inp, out = int(inp or 0), int(out or 0)
    usage_out = {"input": inp, "output": out, "total": int(total) if total is not None else inp + out}
    cached = _field(_field(usage, "prompt_tokens_details"), "cached_tokens")
    if cached is not None:
        usage_out["cached"] = int(cached)
    cost = _field(usage, USAGE_COST_FIELD)
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        usage_out["cost"] = float(cost)
    return usage_out


def build_record(llm_config: dict, response: Any, latency_ms: int,
                 now: datetime | None = None) -> dict:
    """Документ одного вызова. Без usage токены - None: вызов посчитан, объём неизвестен."""
    session_id, role = current_scope()
    usage = extract_usage(response)
    stages = current_stages()
    record = {
        "timestamp": now or datetime.now(timezone.utc),
        "session_id": session_id,
        "role": role,
        "source": llm_config.get("source", ""),
        "model": llm_config.get("model", ""),
        "input_tokens": usage["input"] if usage else None,
        "output_tokens": usage["output"] if usage else None,
        "total_tokens": usage["total"] if usage else None,
        # Часть входа из кэша префикса поставщика; входит в input_tokens, у z.ai дешевле в 4 раза
        #. None - поставщик не сообщил
        "cached_input_tokens": usage.get("cached") if usage else None,
        "latency_ms": latency_ms,
    }
    if usage and "cost" in usage:
        record["cost"] = usage["cost"]   # стоимость от поставщика точнее таблицы цен
    if stages:
        record["stages"] = stages
    return record


def record_call(llm_config: dict, response: Any, latency_ms: int) -> None:
    """Записать вызов; сбой приёмника печатается одной строкой и не идёт дальше."""
    if _sink is None:
        return
    try:
        _sink(build_record(llm_config, response, latency_ms))
    except Exception as e:
        print(f"[TokenUsage] запись не удалась: {e}")
