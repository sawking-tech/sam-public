"""Прогон голосового хода по набору фраз (SAM-81): ожидания проверяет код, не модель-судья.

Устроено как оценка в OpenHands (docs.openhands.dev, evaluation harness): набор случаев, прогон
каждого, по случаю итог с причинами провала и метриками, общий итог. Случай - фраза человека и
ожидание: вызван spawn_agent или нет, ответ или молчание, нет обещания действия без вызова (SAM-07).

Набор собирается из истории Mongo sessions (cases_from_sessions) или из фикстуры; новых хранилищ нет.
Прогон по живой модели - scripts/eval_voice.py и tests/quality, по умолчанию не идёт.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from .runner import ACTION_VERBS

SPAWN = "spawn_agent"


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    phrase: str
    expect_spawn: bool | None = None      # None - не проверять
    expect_silent: bool | None = None     # молчание - законный ответ (SAM-77)
    history: tuple = ()                   # предыдущие реплики {role, content}, если важны


@dataclass
class EvalOutcome:
    case_id: str
    failures: list[str]
    voice: str = ""
    tools: list[str] = field(default_factory=list)
    latency_ms: int = 0

    @property
    def passed(self) -> bool:
        return not self.failures


def check(case: EvalCase, voice: str, tools: Iterable[str]) -> list[str]:
    """Причины провала случая; пусто - прошёл."""
    tools = list(tools)
    spawned = SPAWN in tools
    failures = []
    if case.expect_spawn is not None and spawned != case.expect_spawn:
        failures.append("ждали spawn_agent" if case.expect_spawn else "лишний spawn_agent")
    silent = not voice.strip()
    if case.expect_silent is not None and silent != case.expect_silent:
        failures.append("ждали молчание" if case.expect_silent else "ответа нет")
    if not spawned and any(v in voice.lower() for v in ACTION_VERBS):
        failures.append("обещание действия без spawn_agent")
    return failures


async def run_cases(cases: Iterable[EvalCase],
                    turn: Callable[[EvalCase], Awaitable[Any]]) -> list[EvalOutcome]:
    """turn(case) -> результат хода с полями voice и tools. Сбой хода - провал случая, прогон идёт дальше."""
    outcomes = []
    for case in cases:
        started = time.monotonic()
        try:
            result = await turn(case)
            voice, tools = str(result.voice or ""), list(result.tools or [])
            failures = check(case, voice, tools)
        except Exception as e:
            voice, tools, failures = "", [], [f"ход упал: {e}"]
        outcomes.append(EvalOutcome(case.case_id, failures, voice, tools,
                                    int((time.monotonic() - started) * 1000)))
    return outcomes


def summary(outcomes: list[EvalOutcome]) -> dict:
    latencies = [o.latency_ms for o in outcomes]
    return {
        "cases": len(outcomes),
        "passed": sum(o.passed for o in outcomes),
        "failed": [(o.case_id, o.failures) for o in outcomes if not o.passed],
        "latency_ms_median": int(statistics.median(latencies)) if latencies else 0,
        "latency_ms_max": max(latencies, default=0),
    }


def cases_from_sessions(docs: Iterable[dict]) -> list[EvalCase]:
    """Документы sessions одной сессии по времени -> случаи: фраза человека и вызвала ли она
    spawn_agent до следующей фразы. Служебные инъекции ([TASK_DONE ...], [INJECT ...]) пропускаются."""
    cases: list[EvalCase] = []
    current: dict | None = None
    spawned = False

    def _close():
        if current is not None:
            cases.append(EvalCase(f"{current.get('session_id', 's')}:{current.get('_id', len(cases))}",
                                  str(current.get("content", "")), expect_spawn=spawned))

    for doc in docs:
        role = doc.get("role")
        if role == "user":
            content = str(doc.get("content") or "").strip()
            if not content or content.startswith("["):
                continue
            _close()
            current, spawned = doc, False
        elif role == "tool_call" and doc.get("tool_name") in (SPAWN, "create_task"):
            spawned = True
    _close()
    return cases
