"""DialogueNotesRepo - служебная коллекция `_dialogue_notes` (SAM-131): заметки основной модели о разговоре сессии.

Документ на сессию: тема, линия разговора, прогноз, ветки (кто что вероятно скажет и короткий ответ), версия и
время обновления. Перезаписывается целиком;
старше срока жизни - как будто нет: устаревшие заметки увели бы быструю модель в прошлую тему.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

FIELDS = ("topic", "line", "forecast", "digest", "rebuilding", "branches")


class DialogueNotesRepo:
    COLLECTION = "_dialogue_notes"

    def __init__(self, db, max_age_s: float) -> None:
        self._db, self._max_age = db, timedelta(seconds=max_age_s)

    @property
    def col(self):
        return self._db[self.COLLECTION] if self._db is not None else None

    def get(self, session_id: str, now: datetime | None = None) -> dict | None:
        """Свежие заметки сессии или None (нет, устарели, нет Mongo)."""
        if self._db is None:
            return None
        try:
            doc = self.col.find_one({"session_id": session_id})
        except Exception:
            return None
        if not doc:
            return None
        updated = doc.get("updated_at")
        if updated is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated is None or (now or datetime.now(timezone.utc)) - updated > self._max_age:
            return None
        return doc

    def put(self, session_id: str, notes: dict, now: datetime | None = None) -> int:
        """Перезаписать заметки; вернуть новую версию (0 - не записалось)."""
        if self._db is None:
            return 0
        try:
            old = self.col.find_one({"session_id": session_id}, {"version": 1}) or {}
            version = int(old.get("version") or 0) + 1
            self.col.update_one({"session_id": session_id},
                                {"$set": {**{k: notes.get(k, "") for k in FIELDS}, "version": version,
                                          "updated_at": now or datetime.now(timezone.utc)}}, upsert=True)
            return version
        except Exception:
            return 0
