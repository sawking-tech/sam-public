"""ArchiveRepo — sessions_archive (SAM-36).

Все оригиналы сообщений перед компрессией сохраняются здесь.
TTL нет — архив живёт навсегда, может быть найден через text-search.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable


# Причины архива, которые ставит сжатие (gateway/compression.py): по ним видно, когда оно последний раз прошло
COMPRESSED_REASONS = ("compressed", "deep_compressed")

# Дубль - тот же документ с той же причиной. Сводка живёт на _id первого сообщения своего куска, оригинал которого
# уже в архиве как compressed: уникальность по одному _orig_id отбрасывала слитую сводку (recursive_merge)
ORIG_REASON_INDEX = "orig_id_reason_unique"
LEGACY_ORIG_INDEX = "orig_id_unique"


class ArchiveRepo:
    """Доступ к коллекции `sessions_archive`."""

    COLLECTION = "sessions_archive"

    def __init__(self, db) -> None:
        self._db = db

    @property
    def col(self):
        return self._db[self.COLLECTION] if self._db is not None else None

    def ensure_indexes(self) -> None:
        """Идемпотентно создать индексы:
        - unique по (_orig_id, archive_reason) - защита от дубликатов при reprocess; прежний индекс по одному
          _orig_id снимается
        - (session_id, timestamp) для browsing
        - text-index по content (русский) для full-text поиска
        """
        if self._db is None:
            return
        try:
            col = self.col
            col.create_index([("_orig_id", 1), ("archive_reason", 1)], unique=True, sparse=True,
                             name=ORIG_REASON_INDEX)
            if LEGACY_ORIG_INDEX in col.index_information():          # снимается после нового: уникальность не пропадает
                col.drop_index(LEGACY_ORIG_INDEX)
            col.create_index([("session_id", 1), ("timestamp", -1)], name="session_ts")
            col.create_index([("content", "text")], name="content_text",
                             default_language="russian")
        except Exception as e:
            print(f"[ArchiveRepo] index init: {e}")

    def archive_messages(
        self, session_id: str, messages: Iterable[dict], reason: str,
    ) -> int:
        """Сохранить сырые сообщения в архив. Дубли по (_orig_id, reason) игнорируются.

        Args:
            session_id: сессия архива
            messages: оригиналы из sessions (с _id, role, content, ...)
            reason: "compressed" | "deep_compressed" | "recursive_merge" | "dedup_recent"

        Returns:
            Сколько успешно вставлено (приблизительно — bulk insert не точный).
        """
        if self._db is None:
            return 0
        msgs = list(messages)
        if not msgs:
            return 0
        try:
            col = self.col
            now = datetime.utcnow()
            docs = []
            for m in msgs:
                docs.append({
                    "_orig_id": m.get("_id"),
                    "session_id": session_id,
                    "role": m.get("role"),
                    "content": m.get("content", ""),
                    "timestamp": m.get("timestamp"),
                    "msg_id": m.get("msg_id", ""),
                    "tool_name": m.get("tool_name"),
                    "tool_args": m.get("tool_args"),
                    "archived_at": now,
                    "archive_reason": reason,
                    **({"secret": True} if m.get("secret") else {}),   # пометка секрета (SAM-229) едет в архив
                    **({"who": m["who"]} if m.get("who") else {}),
                })
            try:
                result = col.insert_many(docs, ordered=False)
                return len(result.inserted_ids)
            except Exception as e:
                if "duplicate" not in str(e).lower():
                    print(f"[ArchiveRepo] partial fail: {e}")
                return (getattr(e, "details", None) or {}).get("nInserted", 0)   # ordered=False: остальные легли
        except Exception as e:
            print(f"[ArchiveRepo] error: {e}")
            return 0

    def sessions_summary(self) -> list[dict]:
        """Каждая сессия архива: id, время последней записи, число записей (сессия, сжатая целиком, есть только тут)."""
        if self._db is None:
            return []
        try:
            rows = self.col.aggregate([{"$group": {"_id": "$session_id", "last": {"$max": "$timestamp"},
                                                   "count": {"$sum": 1}}}])
            return [{"id": r["_id"], "last": r.get("last"), "count": r["count"]} for r in rows
                    if isinstance(r.get("_id"), str)]
        except Exception:
            return []

    def last_compressed_at(self, session_id: str) -> datetime | None:
        """Когда сжатие последний раз убрало сообщения сессии в архив (SAM-97): переживает рестарт шлюза."""
        if self._db is None:
            return None
        try:
            doc = self.col.find_one(
                {"session_id": session_id, "archive_reason": {"$in": list(COMPRESSED_REASONS)}},
                {"archived_at": 1}, sort=[("archived_at", -1)],
            )
        except Exception as e:
            print(f"[ArchiveRepo] last_compressed_at error: {e}")
            return None
        return doc.get("archived_at") if doc else None

    def get_by_ids(self, ids) -> list[dict]:
        """Реплики архива по их _id (развёртка записи-ссылки, SAM-228)."""
        ids = list(ids)
        if self._db is None or not ids:
            return []
        try:
            return list(self.col.find({"_id": {"$in": ids}}))
        except Exception as e:
            print(f"[ArchiveRepo] get_by_ids error: {e}")
            return []

    def search(
        self, query: str = "", session_id: str = "", limit: int = 20,
    ) -> list[dict]:
        """Полнотекстовый или session-фильтр поиск архива."""
        if self._db is None:
            return []
        col = self.col
        mongo_query: dict = {}
        if session_id:
            mongo_query["session_id"] = session_id
        if query:
            mongo_query["$text"] = {"$search": query}
        limit = max(1, min(limit, 200))
        try:
            cursor = col.find(mongo_query, limit=limit)
            if query:
                cursor = cursor.sort([("score", {"$meta": "textScore"})])
            else:
                cursor = cursor.sort([("timestamp", -1)])
            return list(cursor)
        except Exception as e:
            print(f"[ArchiveRepo] search error: {e}")
            return []
