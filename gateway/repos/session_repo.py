"""SessionRepo — операции над коллекцией sessions (история чата).

Каждая сессия хранит сообщения с полями:
  _id, session_id, role, content, timestamp, msg_id, tool_name, tool_args
И summary-документы:
  role="summary", is_summary=True, compression_level=1.. (уровень каскада, SAM-224),
  content (резюме), time_from, time_to, time_range
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Iterable


# Запись, которую модель не должна видеть: её бредовый ответ, снятый поправкой ведущего,
# или фраза не к SAM. Значение - причина; запись остаётся в базе и в мониторе (скрыта), но не в контексте и сводке
IGNORE = "ignore"
_VISIBLE = {IGNORE: {"$exists": False}}
# Канал реплики SAM: TEXT_CHANNEL - текстом в монитор, без озвучки (схема псевдографикой, список:
# монитор - основной терминал основной модели). Нет поля - голос
CHANNEL = "channel"
TEXT_CHANNEL = "text"
# Команда монитора (SAM-99): строка «/имя» ушла основной модели командой, не фразой; значение - имя команды
COMMAND = "command"
# Канал сводки (SAM-227): разговор (поля нет) или мысли - неозвученные блоки, вызовы и служебные шаги; у каждого
# канала своя цепочка сводок и свой каскад
SUMMARY_CHANNEL = "summary_channel"
THOUGHTS_CHANNEL = "thoughts"
# Индексы порций истории монитора (ensure_indexes)
MONITOR_PAGE_INDEX = "monitor_ts_id"
MONITOR_SESSION_INDEX = "monitor_session_ts_id"


class SessionRepo:
    """Доступ к коллекции `sessions` MongoDB.

    Все CRUD-операции инкапсулированы здесь. Domain-код (agent, compression)
    зависит от этого репо, не от _db напрямую.
    """

    COLLECTION = "sessions"

    def __init__(self, db, scrub: Callable[[Any], Any] | None = None) -> None:
        self._db = db
        # Чем чистить документ до записи (SAM-88: секрет не ложится в историю); None - как есть
        self._scrub = scrub or (lambda doc: doc)

    @property
    def col(self):
        return self._db[self.COLLECTION] if self._db is not None else None

    # ── Read ───────────────────────────────────────────────────────────

    def get_recent(self, session_id: str, limit: int = 20, summaries: int | None = None,
                   thought_summaries: int = 0) -> list[dict]:
        """Последние `limit` записей сессии в хронологическом порядке. summaries - сводки отдельно от лимита (SAM-224):
        последние `summaries` видимых сводок разговора и `thought_summaries` сводок мыслей (SAM-227) и `limit`
        последних сообщений, чтобы поток реплик не вытеснял сводки, а сводки одного канала - другого."""
        if self._db is None:
            return []
        try:
            if summaries is None:
                docs = list(self.col.find({"session_id": session_id, **_VISIBLE}, sort=[("timestamp", -1)], limit=limit))
            else:
                base = {"session_id": session_id, **_VISIBLE}
                docs = list(self.col.find({**base, "is_summary": {"$ne": True}}, sort=[("timestamp", -1)], limit=limit))
                talk = {**base, "is_summary": True, SUMMARY_CHANNEL: {"$ne": THOUGHTS_CHANNEL}}
                docs += list(self.col.find(talk, sort=[("timestamp", -1)], limit=summaries))
                if thought_summaries:
                    thoughts = {**base, "is_summary": True, SUMMARY_CHANNEL: THOUGHTS_CHANNEL}
                    docs += list(self.col.find(thoughts, sort=[("timestamp", -1)], limit=thought_summaries))
                docs.sort(key=lambda d: d.get("timestamp") or datetime.min, reverse=True)
            return list(reversed(docs))
        except Exception:
            return []

    def active_summaries(self, session_id: str, channel: str | None = None) -> list[dict]:
        """Видимые сводки сессии по времени - сводки контекста модели; слитые скрыты полем ignore (SAM-224).
        channel - только этот канал: THOUGHTS_CHANNEL или любой другой - разговор (SAM-227); None - все."""
        if self._db is None:
            return []
        query = {"session_id": session_id, "is_summary": True, **_VISIBLE}
        if channel is not None:
            query[SUMMARY_CHANNEL] = THOUGHTS_CHANNEL if channel == THOUGHTS_CHANNEL else {"$ne": THOUGHTS_CHANNEL}
        return list(self.col.find(query, sort=[("timestamp", 1)]))

    def recent_for_monitor(self, limit: int, hidden_limit: int, before=None, session: str | None = None,
                           before_id=None) -> list[dict]:
        """Снимок монитора: последние `limit` видимых записей всех сессий и скрытые (ignore) за то же время, не больше
        `hidden_limit`, новые первыми. Скрытые не вытесняют разговор: фона дома больше, чем реплик (SAM-98).
        before - только записи раньше этого времени: следующая порция истории, когда монитор листают вверх.
        before_id - _id самой ранней показанной записи: записи того же времени (вызов и итог инструмента в одну
        миллисекунду) с меньшим _id тоже берутся, порядок - время, затем _id, и граница порции их не теряет.
        session - только эта сессия (лента одной сессии или агента)."""
        if self._db is None:
            return []
        scope: list[dict] = [{"session_id": session}] if session else []
        if before is not None:
            scope.append({"$or": [{"timestamp": {"$lt": before}}, {"timestamp": before, "_id": {"$lt": before_id}}]}
                         if before_id is not None else {"timestamp": {"$lt": before}})
        order = [("timestamp", -1), ("_id", -1)]
        visible = list(self.col.find({"$and": [_VISIBLE, *scope]}).sort(order).limit(limit))
        hidden_scope = [{IGNORE: {"$exists": True}}, *scope]
        if len(visible) == limit and visible[-1].get("timestamp"):
            # скрытые - не раньше последней видимой по (время, _id): иначе скрытая той же миллисекунды с меньшим _id
            # стала бы курсором порции, и видимые между ними выпали бы из следующей
            edge_ts, edge_id = visible[-1]["timestamp"], visible[-1]["_id"]
            hidden_scope.append({"$or": [{"timestamp": {"$gt": edge_ts}},
                                         {"timestamp": edge_ts, "_id": {"$gte": edge_id}}]})
        hidden = list(self.col.find({"$and": hidden_scope}).sort(order).limit(hidden_limit))
        return visible + hidden

    def ensure_indexes(self) -> None:
        """Идемпотентно: порции монитора идут по (время, _id), лента одной сессии - по сессии и тем же полям."""
        if self._db is None:
            return
        try:
            self.col.create_index([("timestamp", -1), ("_id", -1)], name=MONITOR_PAGE_INDEX)
            self.col.create_index([("session_id", 1), ("timestamp", -1), ("_id", -1)], name=MONITOR_SESSION_INDEX)
        except Exception as e:
            print(f"[SessionRepo] index init: {e}")

    def get_all_for_compression(self, session_id: str) -> list[dict]:
        """Все сообщения сессии для компрессии — облегчённая проекция; поля вызовов инструментов нужны сжатию:
        прочитанный скилл уходит в сводку меткой, а не пересказом тела."""
        if self._db is None:
            return []
        try:
            return list(self.col.find(
                {"session_id": session_id, **_VISIBLE},
                {"_id": 1, "role": 1, "content": 1, "timestamp": 1, "msg_id": 1, "tool_name": 1, "tool_args": 1,
                 "tool_calls": 1, "linger": 1},
                sort=[("timestamp", 1)],
            ))
        except Exception:
            return []

    def get_all_full(self, session_id: str) -> list[dict]:
        """Все сообщения сессии целиком — для replay history в monitor."""
        if self._db is None:
            return []
        try:
            return list(self.col.find(
                {"session_id": session_id}, sort=[("timestamp", 1)],
            ))
        except Exception:
            return []

    def distinct_session_ids(self) -> list[str]:
        if self._db is None:
            return []
        try:
            return sorted(self.col.distinct("session_id"))
        except Exception:
            return []

    def sessions_summary(self) -> list[dict]:
        """Каждая сессия: id, время последней записи и число записей (селектор монитора делит их по видам)."""
        if self._db is None:
            return []
        try:
            rows = self.col.aggregate([{"$group": {"_id": "$session_id", "last": {"$max": "$timestamp"},
                                                   "count": {"$sum": 1}}}])
            return [{"id": r["_id"], "last": r.get("last"), "count": r["count"]} for r in rows
                    if isinstance(r.get("_id"), str)]
        except Exception:
            return []

    # ── Write ──────────────────────────────────────────────────────────

    def insert(self, doc: dict) -> None:
        """Вставить одно сообщение. Идемпотентно безопасный — игнорирует ошибки."""
        if self._db is None:
            return
        try:
            self.col.insert_one(self._scrub(doc))
        except Exception:
            pass

    def insert_summary(self, doc: dict):
        """Вставить сводку и вернуть её _id: слитая пара ссылается на неё (SAM-224)."""
        return self.col.insert_one(self._scrub(doc)).inserted_id

    def hide_ids(self, ids: Iterable, reason: str) -> int:
        """Скрыть записи из контекста модели: остаются в истории и в мониторе с причиной (поле ignore)."""
        ids = list(ids)
        if self._db is None or not ids:
            return 0
        return self.col.update_many({"_id": {"$in": ids}}, {"$set": {IGNORE: reason}}).modified_count

    def extend_summary(self, summary_id, time_to: str, time_range: str) -> None:
        """Сводка (её _id) поглотила отрезок пары-повтора: отрезок растягивается до конца пары (SAM-224)."""
        if self._db is None:
            return
        self.col.update_one({"_id": summary_id}, {"$set": {"time_to": time_to, "time_range": time_range}})

    def ignore_last(self, session_id: str, reason: str, roles: tuple[str, ...] = ("assistant",),
                    count: int = 1) -> list[str]:
        """Пометить последние `count` видимых записей с ролью из `roles` как IGNORE; вернуть их msg_id."""
        if self._db is None:
            return []
        try:
            docs = list(self.col.find({"session_id": session_id, "role": {"$in": list(roles)}, **_VISIBLE},
                                      {"_id": 1, "msg_id": 1}, sort=[("timestamp", -1)], limit=count))
            if docs:
                self.col.update_many({"_id": {"$in": [d["_id"] for d in docs]}}, {"$set": {IGNORE: reason}})
            return [d.get("msg_id", "") for d in docs]
        except Exception:
            return []

    def ignore_reply(self, session_id: str, text: str, reason: str, lookback: int = 20) -> list[str]:
        """Пометить IGNORE самый свежий видимый ответ модели с текстом ровно text; подстрокой не ищет: короткое
        «Да.» скрыло бы чужой ответ. Последние lookback. msg_id или []."""
        needle = (text or "").strip()
        if self._db is None or not needle:
            return []
        try:
            docs = self.col.find({"session_id": session_id, "role": "assistant", **_VISIBLE},
                                 {"_id": 1, "msg_id": 1, "content": 1}, sort=[("timestamp", -1)], limit=lookback)
            doc = next((d for d in docs if str(d.get("content") or "").strip() == needle), None)
            if doc is None:
                return []
            self.col.update_one({"_id": doc["_id"]}, {"$set": {IGNORE: reason}})
            return [doc.get("msg_id", "")]
        except Exception:
            return []

    def upsert_summary(
        self, msg_id, summary_text: str,
        time_from: str, time_to: str, time_range: str,
        compression_level: int = 1,
        channel: str | None = None,
    ) -> None:
        """Превратить сообщение с данным _id в summary-документ; channel - канал мыслей (SAM-227), None - разговор."""
        if self._db is None:
            return
        try:
            self.col.update_one(
                {"_id": msg_id},
                {"$set": {
                    "role": "summary",
                    "content": self._scrub(summary_text),
                    "time_from": time_from,
                    "time_to": time_to,
                    "time_range": time_range,
                    "is_summary": True,
                    "compression_level": compression_level,
                    **({SUMMARY_CHANNEL: channel} if channel else {}),
                }},
            )
        except Exception:
            pass

    def delete_by_ids(self, ids: Iterable) -> int:
        """Удалить по списку _id, вернуть количество удалённых."""
        if self._db is None:
            return 0
        id_list = list(ids)
        if not id_list:
            return 0
        try:
            result = self.col.delete_many({"_id": {"$in": id_list}})
            return result.deleted_count or 0
        except Exception:
            return 0

    def clear_all(self) -> None:
        """Очистить всю коллекцию (используется при старте gateway)."""
        if self._db is None:
            return
        try:
            self.col.delete_many({})
        except Exception:
            pass

    def clear_summaries(self, session_id: str) -> int:
        """Удалить все compressed-summary документы сессии (role=summary).
        Используется когда summary получились галлюцинированными.
        Оригиналы остаются в архиве — компрессия пересчитается заново
        при следующем триггере."""
        if self._db is None:
            return 0
        try:
            r = self.col.delete_many({"session_id": session_id, "role": "summary"})
            return r.deleted_count or 0
        except Exception:
            return 0

    def delete_last_exchange(self, session_id: str) -> int:
        """Удалить последний user-msg + последующий assistant-reply сессии.
        Возвращает сколько документов удалено."""
        if self._db is None:
            return 0
        try:
            # Берём последние 4 сообщения, ищем последний user + всё после него
            docs = list(self.col.find(
                {"session_id": session_id, **_VISIBLE},   # скрытая фраза-фон (SAM-98) - не последний обмен
            ).sort("timestamp", -1).limit(6))
            if not docs:
                return 0
            # docs идут от новых к старым. Идём от начала пока не упрёмся в user
            to_delete = []
            for d in docs:
                to_delete.append(d["_id"])
                if d.get("role") == "user":
                    break
            else:
                return 0
            result = self.col.delete_many({"_id": {"$in": to_delete}})
            return result.deleted_count or 0
        except Exception:
            return 0
