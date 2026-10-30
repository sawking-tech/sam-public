"""MemoryRepo — коллекция `_memories` (долговременная память для RAG).

Каждый факт хранится в MongoDB (исходный текст + тэги + timestamp) и в
Qdrant (embedding + payload с summary и mongo_id). Этот репо отвечает
только за MongoDB часть; Qdrant управляется отдельно в gateway/rag.py.
"""

from __future__ import annotations

from datetime import datetime


def _to_object_id(mongo_id):
    """Конвертировать str → ObjectId. ObjectId / другие типы оставляем."""
    if isinstance(mongo_id, str):
        try:
            from bson import ObjectId
            return ObjectId(mongo_id)
        except Exception:
            return mongo_id
    return mongo_id


class MemoryRepo:
    """Доступ к коллекции `_memories` MongoDB."""

    COLLECTION = "_memories"

    def __init__(self, db) -> None:
        self._db = db

    @property
    def col(self):
        return self._db[self.COLLECTION] if self._db is not None else None

    def insert(self, text: str, tags: list[str] | None = None,
               summary: str | None = None, author: str | None = None,
               questions: list[str] | None = None) -> str | None:
        """Сохранить факт. Возвращает mongo_id (str) или None при ошибке.

        questions - вопросы-фасеты: по ним пересобирается индекс при смене эмбеддера."""
        if self._db is None:
            return None
        try:
            doc = {
                "text": text,
                "tags": tags or [],
                "created_at": datetime.utcnow(),
            }
            if summary is not None:
                doc["summary"] = summary
            if author is not None:
                doc["author"] = author
            if questions:
                doc["questions"] = list(questions)
            result = self.col.insert_one(doc)
            return str(result.inserted_id)
        except Exception as e:
            print(f"[MemoryRepo] insert error: {e}")
            return None

    def get_by_id(self, mongo_id) -> dict | None:
        if self._db is None or not mongo_id:
            return None
        try:
            return self.col.find_one({"_id": _to_object_id(mongo_id)})
        except Exception:
            return None

    def find_many(self, query: dict | None = None, sort: list | None = None,
                  limit: int = 30) -> list[dict]:
        if self._db is None:
            return []
        try:
            cursor = self.col.find(query or {}, limit=limit)
            if sort:
                cursor = cursor.sort(sort)
            return list(cursor)
        except Exception:
            return []

    def find_one(self, query: dict) -> dict | None:
        if self._db is None:
            return None
        try:
            return self.col.find_one(query)
        except Exception:
            return None

    def delete_by_id(self, mongo_id, force: bool = False) -> bool:
        """Удалить факт. Если факт помечен protected=True — отказ
        (только при force=True можно стереть, для системных нужд)."""
        if self._db is None:
            return False
        try:
            flt = {"_id": _to_object_id(mongo_id)}
            if not force:
                flt["protected"] = {"$ne": True}
            result = self.col.delete_one(flt)
            return result.deleted_count > 0
        except Exception:
            return False

    def update_by_id(self, mongo_id, update: dict, force: bool = False) -> bool:
        """Обновить факт. Protected=True блокирует update без force."""
        if self._db is None:
            return False
        try:
            flt = {"_id": _to_object_id(mongo_id)}
            if not force:
                flt["protected"] = {"$ne": True}
            result = self.col.update_one(flt, {"$set": update})
            return result.modified_count > 0
        except Exception:
            return False

    # ── Эволюционный каркас (SAM-45 self-organizing knowledge base) ─────────
    # Поля Mongo doc которые могут появляться по мере жизни:
    #   archived: bool         — soft delete (для librarian-операций)
    #   related_ids: list[str] — cross-references на другие mongo_id
    #   merged_from: list[str] — audit когда merge сделан
    #   split_from: str        — audit когда split сделан
    #   access_count: int      — doc-level usage (vs facet-level в Qdrant)
    #   last_access_at: ts
    # Эти поля сейчас не обязательны — MemoryRepo работает без них.
    # Tools для structural ops появятся в SAM-45, но эти базовые
    # методы готовят почву чтобы агентам было что вызывать.

    def archive(self, mongo_id) -> bool:
        """Soft delete: помечает doc как archived, физически НЕ удаляет.
        Search должен фильтровать archived=False (см. RAGStore)."""
        return self.update_by_id(mongo_id, {
            "archived": True,
            "archived_at": datetime.utcnow(),
        })

    def unarchive(self, mongo_id) -> bool:
        """Восстановить из архива."""
        return self.update_by_id(mongo_id, {
            "archived": False,
            "archived_at": None,
        })

    def set_related(self, mongo_id, related_ids: list[str]) -> bool:
        """Привязать cross-references к другим mongo_id."""
        return self.update_by_id(mongo_id, {"related_ids": list(related_ids)})

    def add_questions(self, mongo_id, questions: list[str]) -> bool:
        """Дописать вопросы-фасеты без повторов; это индекс факта, поэтому и у защищённого (protected)."""
        if self._db is None or not questions:
            return False
        try:
            result = self.col.update_one(
                {"_id": _to_object_id(mongo_id)},
                {"$addToSet": {"questions": {"$each": list(questions)}}},
            )
            return result.modified_count > 0
        except Exception as e:
            print(f"[MemoryRepo] add_questions error: {e}")
            return False

    def add_related(self, mongo_id, other_id: str) -> bool:
        """Добавить одну cross-reference (идемпотентно)."""
        if self._db is None:
            return False
        try:
            result = self.col.update_one(
                {"_id": _to_object_id(mongo_id)},
                {"$addToSet": {"related_ids": other_id}},
            )
            return result.modified_count > 0
        except Exception:
            return False

    def find_active(self, query: dict | None = None, **kwargs) -> list[dict]:
        """find_many с автоматическим фильтром archived != True.
        Используется по умолчанию для активного поиска."""
        q = dict(query or {})
        q["$or"] = q.get("$or", []) + [
            {"archived": {"$exists": False}},
            {"archived": False},
        ]
        return self.find_many(q, **kwargs)
