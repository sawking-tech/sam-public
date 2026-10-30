"""Очередь отложенных задач (SAM-260): тяжёлая задача при исчерпанном лимите встаёт сюда как есть и запускается сама.

Основная модель при лимите ничего не объясняет и не сокращает ответ: задача принята, шедулер запустит её, когда лимит
восстановится (окно скользит, трата возвращается через час, SAM-260). Чистая логика над коллекцией `_deferred_tasks`:
база приходит аргументом (mongomock в тестах), запуск и проверка лимита - функциями.
"""
from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable

COLLECTION = "_deferred_tasks"
PENDING, STARTED, FAILED = "pending", "started", "failed"
ACCEPTED = "Принято: задача в очереди, запустится сама, когда откроется лимит."

Gate = Callable[[str, int | None], tuple[bool, str]]          # (описание, шаги) -> (можно, причина)
Launch = Callable[[dict], Awaitable[dict]]                    # параметры create_agent -> ответ create_agent_impl


def enqueue(db, params: dict, now: datetime | None = None) -> str:
    """Поставить задачу в очередь как есть; возвращает id записи."""
    doc = {"params": params, "status": PENDING, "queued_at": now or datetime.utcnow()}
    return str(db[COLLECTION].insert_one(doc).inserted_id)


def pending_count(db) -> int:
    """Сколько тяжёлых задач ждёт лимита: они делят бюджет с новой (доля бюджета, model_choice)."""
    return db[COLLECTION].count_documents({"status": PENDING})


def pending(db) -> list[dict]:
    return list(db[COLLECTION].find({"status": PENDING}).sort("queued_at", 1))


async def run_ready(db, gate: Gate, launch: Launch, limit: int = 1) -> list[str]:
    """Запустить самые старые ожидающие задачи, пока лимит пускает (не больше limit за проход: расход нового агента
    лимит увидит только со следующим учётом). Возвращает id запущенных записей."""
    started: list[str] = []
    for doc in pending(db):
        if len(started) >= limit:
            break
        params = doc["params"]
        allowed, _ = gate(params.get("description", ""), params.get("steps"))
        if not allowed:
            break                           # старшая не проходит - младшие не обгоняют её
        try:
            result = await launch(params)
        except Exception as e:
            db[COLLECTION].update_one({"_id": doc["_id"]}, {"$set": {"status": FAILED, "error": f"{type(e).__name__}: {e}"[:200]}})
            print(f"[Deferred] ERROR задача {doc['_id']} не запущена ({type(e).__name__}): {e}")
            continue
        status = STARTED if result.get("status") == "ok" else PENDING   # занято/нет устройства: останется ждать
        db[COLLECTION].update_one({"_id": doc["_id"]}, {"$set": {"status": status, "launched_at": datetime.utcnow(),
                                                                "result": {k: result.get(k) for k in ("status", "task_id", "error")}}})
        if status == STARTED:
            started.append(str(doc["_id"]))
        else:
            break
    return started
