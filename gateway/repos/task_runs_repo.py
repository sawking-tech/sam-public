"""TaskRunsRepo - коллекция `_task_runs` (SAM-233): один документ на завершённый запуск агента, без TTL.

Запуск: task_id, source, model, tier, tier_why, status (done | failed | cancelled), started_at, finished_at, duration_s,
steps, input_tokens, output_tokens, cost_usd. Из них оркестратор учится (gateway/model_learning.py) и строит тирлист.
Документов мало (по одному на задачу), поэтому срока хранения нет: статистика не должна уходить вместе с TTL записей вызовов.
"""
from __future__ import annotations

COLLECTION = "_task_runs"
RECENT_LIMIT = 5000   # с запасом на окно по всем парам «модель, уровень»


class TaskRunsRepo:
    def __init__(self, db) -> None:
        self._db = db

    @property
    def col(self):
        return self._db[COLLECTION] if self._db is not None else None

    def ensure_indexes(self) -> None:
        if self._db is not None:
            self.col.create_index([("source", 1), ("model", 1), ("tier", 1), ("finished_at", -1)])

    def record(self, doc: dict) -> None:
        if self._db is not None:
            self.col.insert_one(dict(doc))

    def recent(self, limit: int = RECENT_LIMIT) -> list[dict]:
        """Последние запуски, новые первыми."""
        if self._db is None:
            return []
        return list(self.col.find({}, {"_id": 0}).sort("finished_at", -1).limit(limit))
