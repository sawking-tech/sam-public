"""Tasks tool — get_tasks (listing) + cancel_task.

Чистые функции с DI. Остальные task-инструменты (create_task,
send_to_task, report_status) сильно связаны с runtime агента
(asyncio Task handles, ContextVar воркера, event_bus injection) и
остаются в agent.py до выноса runtime-слоя.
"""
from __future__ import annotations

import difflib
from datetime import datetime, timedelta
from typing import Any, Iterable


def get_tasks_impl(
    params: dict,
    db: Any,
    tasks_module: Any,
    *,
    stuck_after: timedelta = timedelta(hours=1),
    limit: int = 8,
    recent: timedelta = timedelta(hours=24),
) -> str:
    """Список задач с фильтром по status/device. Прячет «застрявшие»
    running/pending старше stuck_after — они считаются мусором. Без статуса - идущие и закончившиеся за `recent`:
    о сделанном в другой сессии спрашивают как о своём («как там трекер»), а «нет задач» звучало как «не было»."""
    if db is None:
        return "MongoDB недоступен"

    status = (params.get("status") or "").strip()
    device = (params.get("device") or "").strip()
    creator = (params.get("creator") or "").strip()   # только агенты этого создателя: свои дети, их модели

    tasks = tasks_module.list_tasks(
        db, status or None, device or None, limit=limit,
    )
    if creator:
        tasks = [t for t in tasks if t.get("creator") == creator]

    cutoff = datetime.utcnow() - stuck_after
    tasks = [
        t for t in tasks
        if t.get("created_at", datetime.min) > cutoff
        or t.get("status") not in ("running", "pending")
    ]

    finished = [] if status else tasks_module.list_finished_since(db, datetime.utcnow() - recent, limit=limit)
    if device:
        finished = [t for t in finished if t.get("device") == device]
    if creator:
        finished = [t for t in finished if t.get("creator") == creator]
    if not tasks and not finished:
        return (
            f"Нет ни идущих задач, ни закончившихся за {recent.total_seconds() / 3600:.0f} ч" if not status
            else f"Нет задач со статусом '{status}'"
        )
    lines = [tasks_module.format_task_summary(t) for t in tasks] or (["Идущих задач нет"] if not status else [])
    if finished:
        lines.append(f"Закончились за {recent.total_seconds() / 3600:.0f} ч:")
        lines += [_with_outcome(tasks_module.format_task_summary(t), t) for t in finished]
    return "\n".join(lines)


RESULT_CHARS = 200   # итог закончившейся задачи в списке: суть, полный - в её секции монитора


def _with_outcome(summary: str, task: dict) -> str:
    outcome = " ".join(str(task.get("result") or task.get("error") or "").split())
    return summary + (f"\n  итог: {outcome[:RESULT_CHARS]}" if outcome else "")


def cancel_task_impl(
    params: dict,
    db: Any,
    tasks_module: Any,
    task_handles: dict,
) -> str:
    """Отменить задачу по 8-char prefix или полному UUID. Если task_id="all"
    или "running" — отменяет все running."""
    if db is None:
        return "MongoDB недоступен"

    target = (params.get("task_id") or "").strip()
    if not target:
        return "Нужен task_id (8-char prefix или 'all' для отмены всех running)"

    # 'all' / 'running' — массовая отмена
    if target.lower() in ("all", "running", "все"):
        cancelled = 0
        for tid in list(task_handles.keys()):
            handle = task_handles.get(tid)
            if handle and not handle.done():
                handle.cancel()
                cancelled += 1
            tasks_module.cancel_task(db, tid)
        return f"Отменено {cancelled} активных задач."

    # Резолвим prefix → полный id
    full_id = target
    handle = task_handles.get(target)
    if handle is None:
        cands = [k for k in task_handles.keys() if k.startswith(target)]
        if len(cands) == 1:
            full_id = cands[0]
            handle = task_handles[full_id]
        elif len(cands) > 1:
            return f"Неоднозначный prefix '{target}' — {len(cands)} совпадений. Уточни."

    if handle is not None and not handle.done():
        handle.cancel()

    ok = tasks_module.cancel_task(db, full_id)
    if ok or handle is not None:
        return f"Задача {full_id[:8]} отменена."
    return f"Задача '{target}' не найдена."


# Похожие имена агентов: calorie_tracker_v2 и calorie_tracker_v3 - одна работа; промах send_to_agent по имени
# calorie_tracker_v3b - опечатка в существующем (15 окт, SAM-221). Доля совпадения difflib
SIMILAR_NAME = 0.8


def closest_names(name: str, names: Iterable[str], n: int = 3) -> list[str]:
    """Имена задач, похожие на названное, - подсказка при промахе."""
    return difflib.get_close_matches(name, [x for x in names if x], n=n, cutoff=SIMILAR_NAME)


def _app_slugs_in(text: str, slugs: Iterable[str]) -> set[str]:
    norm = (text or "").lower().replace("_", "-")     # агент пишет calorie_tracker, slug приложения - calorie-tracker
    return {s for s in slugs if s and s in norm}


def duplicate_task(name: str, description: str, running: Iterable[dict], app_slugs: Iterable[str],
                   own_id: str = "") -> dict | None:
    """Идущая задача о той же работе: похожее имя или то же приложение в описании. 15 окт по одному трекеру разом
    шли calorie_tracker_v2 и v3 и правили одно приложение; дополнение идёт в идущего агента, а не вторым."""
    slugs = list(app_slugs)
    mine = _app_slugs_in(description, slugs)
    for t in running:
        if own_id and str(t.get("id", "")).startswith(own_id):
            continue                                  # агент, который сам ставит задачу, себе не дубль
        other = t.get("name") or ""
        if name and other and difflib.SequenceMatcher(None, name, other).ratio() >= SIMILAR_NAME:
            return t
        if mine & _app_slugs_in(t.get("description") or "", slugs):
            return t
    return None


def duplicate_refusal(t: dict) -> str:
    short = str(t.get("id", ""))[:8]
    what = (t.get("description") or "")[:80]
    return (f"Уже идёт задача {short} ({t.get('name') or 'без имени'}): «{what}». Дополнение или уточнение - "
            f"send_to_agent(task_id={short}); неверная постановка - cancel_agent и заново; другая работа рядом с ней - "
            "spawn_agent с parallel=true")
