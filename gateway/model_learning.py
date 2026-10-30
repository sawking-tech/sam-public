"""Обучение выбора модели по итогам запусков агентов (SAM-258): скользящее окно по «модели, виду работы, среде».

Чистая функция: запуски (документы `_task_runs`) приходят аргументом. По последним WINDOW_RUNS запускам пары считаются
доля успехов, медианные время и стоимость. Успех - итог, подтверждённый проверкой (контракт итога, SAM-237; проверка
результата, SAM-273), а не слова агента о себе: исполнитель говорит, что сделал по плану, и делает по-своему. Мало
запусков (меньше MIN_RUNS) - данных нет, модель считается способной: доказать провал может только факт.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Iterable

from .env import env_int

WINDOW_RUNS = env_int("SAM_ORCH_WINDOW", 30)          # последних запусков на пару модель-вид
MIN_RUNS = env_int("SAM_ORCH_MIN_RUNS", 5)            # меньше - данных нет, модель считается способной
STATUS_DONE = "done"


@dataclass(frozen=True)
class Learned:
    runs: int
    success_rate: float
    median_seconds: float | None
    median_cost_usd: float | None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def learn(runs: Iterable[dict], window: int = WINDOW_RUNS) -> dict[tuple, Learned]:
    """{(модель, вид, среда): итог}, и общий по виду {(модель, вид): итог} - по последним window запускам каждой группы.
    Модель без продавца: та же модель у двух агрегаторов одинаково умна, цена у каждого своя (model_choice). Запуск:
    source, model, kind, env, status, finished_at, duration_s, cost_usd."""
    by_pair: dict[tuple, list[dict]] = {}
    for run in runs:
        key, kind = run.get("model", ""), run.get("kind") or ""
        by_pair.setdefault((key, kind, run.get("env") or ""), []).append(run)
        by_pair.setdefault((key, kind), []).append(run)
    out: dict[tuple, Learned] = {}
    for pair, items in by_pair.items():
        recent = sorted(items, key=lambda r: r.get("finished_at") or 0, reverse=True)[:window]
        done = sum(1 for r in recent if r.get("status") == STATUS_DONE)
        out[pair] = Learned(
            runs=len(recent), success_rate=done / len(recent),
            median_seconds=_median([r["duration_s"] for r in recent if r.get("duration_s") is not None]),
            median_cost_usd=_median([r["cost_usd"] for r in recent if r.get("cost_usd") is not None]))
    return out


NEUTRAL_STEPS_FACTOR = 1.0   # данных нет: прикидка создателя берётся как есть, оценка не смещается
REDO = "redo"                # итог приняли, а человек попросил переделать: для статистики это провал


def steps_factor(runs: Iterable[dict], kind: str, min_runs: int = MIN_RUNS) -> float:
    """Во сколько раз задачи этого вида на деле дольше прикидки создателя: медиана (потрачено / прикидка) по последним
    WINDOW_RUNS запускам вида. Запусков с обоими числами меньше min_runs - NEUTRAL_STEPS_FACTOR."""
    ratios = [r["steps_spent"] / r["steps"] for r in runs
              if (r.get("kind") or "") == kind and r.get("steps") and r.get("steps_spent")][:WINDOW_RUNS]
    return statistics.median(ratios) if len(ratios) >= min_runs else NEUTRAL_STEPS_FACTOR


def failed_models(runs: Iterable[dict], task_id: str) -> frozenset[str]:
    """Модели (без продавца), которые эту задачу уже провалили (не done, в том числе переделка): повтор идёт к
    следующей, у другого агрегатора та же модель тоже не берётся."""
    return frozenset(r.get("model", "") for r in runs
                     if r.get("task_id") == task_id and r.get("status") != STATUS_DONE)


def success_p(key: str, kind: str, env: str, learned: dict[tuple, Learned]) -> tuple[float, Learned | None]:
    """Вероятность, что модель закроет задачу: доля успехов по факту на этом виде работы в этой среде; мало запусков -
    по виду работы в любой среде; и там мало - 1: без фактов модель считается способной. Второе - факты оценки."""
    for group in ((key, kind, env), (key, kind)):
        seen = learned.get(group)
        if seen is not None and seen.runs >= MIN_RUNS:
            return seen.success_rate, seen
    return 1.0, None
