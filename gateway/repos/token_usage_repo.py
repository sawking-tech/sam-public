"""TokenUsageRepo - коллекция `_token_usage` (SAM-48): один документ на вызов LLM."""

from __future__ import annotations

from ..env import env_int

# Срок хранения записей: агрегаты v1 смотрят на текущие сессии, долгая история не нужна
TTL_DAYS = env_int("SAM_TOKEN_USAGE_TTL_DAYS", 30)
SECONDS_PER_DAY = 86400


class TokenUsageRepo:
    COLLECTION = "_token_usage"

    def __init__(self, db) -> None:
        self._db = db

    @property
    def col(self):
        return self._db[self.COLLECTION] if self._db is not None else None

    def ensure_indexes(self) -> None:
        if self._db is None:
            return
        self.col.create_index("timestamp", expireAfterSeconds=TTL_DAYS * SECONDS_PER_DAY)
        self.col.create_index([("session_id", 1), ("role", 1)])

    def record(self, doc: dict) -> None:
        if self._db is None:
            return
        self.col.insert_one(dict(doc))

    def rows_since(self, since) -> list[dict]:
        """Записи вызовов с момента since (datetime UTC), для сводки расходов."""
        if self._db is None:
            return []
        return list(self.col.find({"timestamp": {"$gte": since}}))

    def totals_by_role(self, session_id: str) -> dict[str, dict]:
        """{роль: {calls, calls_without_usage, input_tokens, cached_input_tokens, output_tokens, total_tokens}}."""
        if self._db is None:
            return {}
        out: dict[str, dict] = {}
        for doc in self.col.find({"session_id": session_id}):
            row = out.setdefault(doc.get("role", ""), {
                "calls": 0, "calls_without_usage": 0,
                "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            })
            row["calls"] += 1
            if doc.get("total_tokens") is None:
                row["calls_without_usage"] += 1
                continue
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
                row[key] += doc.get(key) or 0
        return out
