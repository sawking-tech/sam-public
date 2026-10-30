"""SessionMetaRepo — служебная коллекция `_sessions_meta`.

Используется компрессией для кеша агрегированного summary.
"""

from __future__ import annotations

from datetime import datetime


class SessionMetaRepo:
    COLLECTION = "_sessions_meta"

    def __init__(self, db) -> None:
        self._db = db

    @property
    def col(self):
        return self._db[self.COLLECTION] if self._db is not None else None

    def get(self, session_id: str) -> dict | None:
        if self._db is None:
            return None
        try:
            return self.col.find_one({"session_id": session_id})
        except Exception:
            return None

    def upsert_summary_cache(
        self, session_id: str, summary: str, compressed_up_to,
    ) -> None:
        if self._db is None:
            return
        try:
            self.col.update_one(
                {"session_id": session_id},
                {"$set": {
                    "summary": summary,
                    "compressed_up_to": compressed_up_to,
                    "updated_at": datetime.utcnow(),
                }},
                upsert=True,
            )
        except Exception:
            pass

    def delete(self, session_id: str) -> None:
        if self._db is None:
            return
        try:
            self.col.delete_one({"session_id": session_id})
        except Exception:
            pass

    def clear_all(self) -> None:
        if self._db is None:
            return
        try:
            self.col.delete_many({})
        except Exception:
            pass
