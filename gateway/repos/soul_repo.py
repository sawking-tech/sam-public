"""SoulRepo - служебная коллекция `_soul`: душа SAM, её характер по разделам (gateway/prompts/soul.py).

Документ на раздел души: `_id` - имя раздела `## ИМЯ`, текст, порядок показа, номер версии, время и автор правки,
история прошлых версий (последние history_limit, новые в конце). Правка пишется только поверх той версии, которую
прочитала: две одновременные правки не затирают одна другую молча.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone

LIST_FIELDS = {"text": 0, "history": 0}   # список записей без текста и истории


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


class SoulRepo:
    COLLECTION = "_soul"

    def __init__(self, db, history_limit: int, connect: Callable[[], object | None] | None = None) -> None:
        """connect - ленивое подключение для шлюза, стартовавшего без Mongo: вызывается, пока db нет."""
        self._db, self._history_limit, self._connect = db, history_limit, connect

    def _database(self):
        if self._db is None and self._connect is not None:
            self._db = self._connect()
        return self._db

    @property
    def available(self) -> bool:
        return self._database() is not None

    @property
    def col(self):
        return self._database()[self.COLLECTION]

    def load(self) -> dict[str, str]:
        """Имя -> текст всех записей; исключения драйвера идут вызывающему (хранилище недоступно)."""
        return {doc["_id"]: doc.get("text", "") for doc in self.col.find({}, {"text": 1})}

    def names(self) -> set[str]:
        return {doc["_id"] for doc in self.col.find({}, {"_id": 1})}

    def entries(self) -> list[dict]:
        """Записи без текста и истории в порядке показа."""
        return list(self.col.find({}, LIST_FIELDS).sort([("order", 1), ("_id", 1)]))

    def get(self, name: str) -> dict | None:
        return self.col.find_one({"_id": name}, {"history": 0})

    def history(self, name: str) -> list[dict]:
        """Версии записи от новой к старой, текущая первой; нет записи - пусто."""
        doc = self.col.find_one({"_id": name})
        if not doc:
            return []
        current = {k: doc.get(k) for k in ("version", "text", "updated_at", "author")}
        return [current, *reversed(doc.get("history") or [])]

    def insert_missing(self, entries: Mapping[str, str], order: Mapping[str, int], author: str,
                       extra: Mapping[str, Mapping] | None = None, now: datetime | None = None) -> list[str]:
        """Засеять записи, которых нет; существующие не трогаются. extra - поля записи по имени (хэш заготовки).
        Вернуть имена засеянных."""
        inserted = []
        for name, text in entries.items():
            res = self.col.update_one({"_id": name}, {"$setOnInsert": {
                "text": text, "order": order.get(name, 0), "version": 1, "updated_at": _now(now), "author": author,
                "history": [], **((extra or {}).get(name) or {})}}, upsert=True)
            if res.upserted_id is not None:
                inserted.append(name)
        return inserted

    def version(self, name: str) -> int:
        doc = self.col.find_one({"_id": name}, {"version": 1})
        return int(doc.get("version") or 0) if doc else 0

    def seed_state(self) -> dict[str, dict]:
        """Имя -> текст, версия, автор и хэш заготовки: засев решает, что обновить из заготовки."""
        fields = {"text": 1, "version": 1, "author": 1, "seed_hash": 1}
        return {doc["_id"]: doc for doc in self.col.find({}, fields)}

    def delete(self, names: list[str]) -> list[str]:
        """Удалить записи с историей; вернуть имена удалённых."""
        return [name for name in names if self.col.delete_one({"_id": name}).deleted_count]

    def set_fields(self, name: str, fields: Mapping) -> None:
        self.col.update_one({"_id": name}, {"$set": dict(fields)})

    def put(self, name: str, text: str, author: str, expected: int, now: datetime | None = None,
            extra: Mapping | None = None) -> int:
        """Новая версия поверх версии expected, которую видел автор; прежняя уходит в историю. Вернуть номер новой
        версии; 0 - записи нет или в базе уже другая версия (правка между чтением автора и записью)."""
        doc = self.col.find_one({"_id": name}, {"history": 0})
        if not doc:
            return 0
        version = int(doc.get("version") or 0)
        if version != expected:
            return 0
        previous = {"version": version, "text": doc.get("text", ""), "updated_at": doc.get("updated_at"),
                    "author": doc.get("author", "")}
        res = self.col.update_one(
            {"_id": name, "version": version},
            {"$set": {"text": text, "version": version + 1, "updated_at": _now(now), "author": author,
                      **(extra or {})},
             "$push": {"history": {"$each": [previous], "$slice": -self._history_limit}}})
        return version + 1 if res.modified_count else 0
