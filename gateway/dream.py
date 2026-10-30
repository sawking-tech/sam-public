"""Ночная консолидация памяти (SAM-22, ADR-015): запуск по расписанию в паузе, один на все процессы.

Раз в CHECK_INTERVAL_S проверяется, пора ли: ночное окно часов (DREAM_FROM_H-DREAM_TO_H, может переходить
через полночь), с прошлого запуска прошло DREAM_INTERVAL_H, ворота простоя открыты (SAM-62). Тогда - запись
блокировки в Mongo (второй gateway не запустит то же), шаги по очереди, событие `dream` с отчётом шагов.

Шаги v1 берут готовое из `gateway/rag.py`:
- report - `maintenance_check`: сколько фактов и векторов, расхождения; ничего не меняет;
- prune_index - `prune_dead_facets`: удалить из Qdrant векторы, по которым ни разу не нашли за 90 дней;
  Mongo не трогается, `reindex_all` восстановит. Только при SAM_DREAM_APPLY=1.
Факты в Mongo консолидация v1 не меняет и не удаляет: слияние повторов и забывание - следующий шаг SAM-22
после живой проверки.

Решение «пора ли» (due) и порядок шагов отделены от Mongo, Qdrant и часов.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from .heartbeat import in_hours
from .env import env_bool, env_float, env_int

ENABLED = env_bool("SAM_DREAM", False)
APPLY = env_bool("SAM_DREAM_APPLY", False)
DREAM_FROM_H = env_int("SAM_DREAM_FROM_H", 3)     # ночью: домашние спят, GPU свободен
DREAM_TO_H = env_int("SAM_DREAM_TO_H", 6)
DREAM_INTERVAL_H = env_float("SAM_DREAM_INTERVAL_H", 20.0)   # раз в ночь
CHECK_INTERVAL_S = 600.0
LOCK_TTL = timedelta(hours=1)    # упавший запуск не держит блокировку дольше часа
COLLECTION = "_dream"
LOCK_ID = "lock"


def due(now: datetime, last_run: datetime | None, from_h: int = DREAM_FROM_H, to_h: int = DREAM_TO_H,
        interval_h: float = DREAM_INTERVAL_H) -> bool:
    if not in_hours(now.hour, from_h, to_h):
        return False
    return last_run is None or now - last_run >= timedelta(hours=interval_h)


class MongoLock:
    """Блокировка и время прошлого запуска - один документ в Mongo."""

    def __init__(self, db) -> None:
        self._col = db[COLLECTION]

    def last_run(self) -> datetime | None:
        doc = self._col.find_one({"_id": LOCK_ID}) or {}
        return doc.get("last_run")

    def acquire(self, now: datetime) -> bool:
        from pymongo.errors import DuplicateKeyError
        try:
            self._col.update_one({"_id": LOCK_ID, "$or": [{"until": {"$lt": now}}, {"until": None}]},
                                 {"$set": {"until": now + LOCK_TTL}}, upsert=True)
            return True
        except DuplicateKeyError:      # документ есть и блокировка не истекла: запуск идёт
            return False

    def release(self, now: datetime) -> None:
        self._col.update_one({"_id": LOCK_ID}, {"$set": {"until": None, "last_run": now}})


@dataclass(frozen=True)
class Step:
    name: str
    run: Callable[[], dict]


def default_steps(store, apply: bool = APPLY) -> list[Step]:
    steps = [Step("report", store.maintenance_check)]
    if apply:
        steps.append(Step("prune_index", store.prune_dead_facets))
    return steps


class Dream:
    def __init__(self, steps: list[Step], lock, gate, emit: Callable[[str, dict], Any],
                 now: Callable[[], datetime] = datetime.now) -> None:
        self._steps, self._lock, self._gate, self._emit, self._now = steps, lock, gate, emit, now

    async def run_once(self) -> dict | None:
        """Один запуск, если блокировка взята; отчёт по шагам. Сбой шага не останавливает следующие."""
        if not self._lock.acquire(self._now()):
            return None
        report: dict = {}
        try:
            for step in self._steps:
                try:
                    # Mongo и Qdrant синхронны: в потоке, иначе стоят все сессии (SAM-74)
                    report[step.name] = await asyncio.to_thread(step.run)
                except Exception as e:
                    first = next((ln for ln in str(e).splitlines() if ln.strip()), type(e).__name__)
                    report[step.name] = {"error": first}
        finally:
            self._lock.release(self._now())
        self._emit("dream", {"steps": report})
        return report

    async def tick(self) -> dict | None:
        if not self._gate.is_idle() or not due(self._now(), self._lock.last_run()):
            return None
        return await self.run_once()

    async def run(self, interval_s: float = CHECK_INTERVAL_S) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.tick()
            except Exception as e:   # Mongo пропала - следующая проверка через interval_s
                print(f"[Dream] запуск не состоялся: {e}")
