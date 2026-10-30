"""Persistent worker sessions (SAM-38).

Раньше воркер умирал после `STOP_WORKER` и весь его in-memory контекст
(parsed URLs, прогресс по задаче) терялся. Повторное обращение через
`send_to_task` к завершённой таске возвращало "не найдена".

Теперь: после успешного завершения воркера сохраняем `messages[]` в
коллекцию `worker_sessions`. При `send_to_task(task_id, msg)` для
idle-воркера — поднимаем сессию из MongoDB и запускаем новый
`_agent_loop` с восстановленным контекстом + новым inject-сообщением.

Поля документа:
  _id          = task_id (полный uuid)
  short_id     = первые 8 символов (для прямого поиска)
  device       = целевое устройство
  description  = исходное описание таски
  messages     = JSON-serializable messages для _agent_loop
  parent_session = сессия инициатора (для _auto_respond)
  created_at, last_activity, status  ("idle" | "running" | "dead")
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("sam.worker_session")

_COLLECTION = "worker_sessions"
_TTL_HOURS = 24  # auto-cleanup idle сессий старше суток


def ensure_indexes(db) -> None:
    """Идемпотентно создать индексы и TTL."""
    if db is None:
        return
    try:
        col = db[_COLLECTION]
        col.create_index("short_id", name="short_id_lookup")
        col.create_index([("parent_session", 1), ("last_activity", -1)],
                         name="parent_recent")
        # TTL: документы старше _TTL_HOURS автоматически удаляются
        col.create_index("last_activity",
                         expireAfterSeconds=_TTL_HOURS * 3600,
                         name="ttl_last_activity")
    except Exception as e:
        log.warning("ensure_indexes failed: %s", e)


def _serialize_messages(messages: list) -> list:
    """Привести messages в JSON-safe вид (на случай если там OpenAI объекты)."""
    out = []
    for m in messages:
        if isinstance(m, dict):
            out.append(m)
            continue
        try:
            out.append(json.loads(json.dumps(m, default=lambda o: getattr(o, "__dict__", str(o)))))
        except Exception:
            out.append({"role": "user", "content": str(m)})
    return out


def save_session(db, task_id: str, *, device: str, description: str,
                 messages: list, parent_session: str,
                 status: str = "idle") -> bool:
    if db is None or not task_id:
        return False
    try:
        col = db[_COLLECTION]
        now = datetime.utcnow()
        col.update_one(
            {"_id": task_id},
            {"$set": {
                "_id": task_id,
                "short_id": task_id[:8],
                "device": device,
                "description": description,
                "messages": _serialize_messages(messages),
                "parent_session": parent_session,
                "last_activity": now,
                "status": status,
            },
             "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        return True
    except Exception as e:
        log.warning("save_session failed for %s: %s", task_id[:8], e)
        return False


def load_session(db, task_id: str) -> Optional[dict]:
    if db is None or not task_id:
        return None
    try:
        return db[_COLLECTION].find_one({"_id": task_id})
    except Exception:
        return None


def find_by_prefix(db, prefix: str) -> Optional[dict]:
    """Найти session по 8-символьному short_id (как get_tasks возвращает)."""
    if db is None or not prefix:
        return None
    try:
        col = db[_COLLECTION]
        # точное совпадение short_id
        doc = col.find_one({"short_id": prefix[:8]})
        if doc:
            return doc
        # fallback: префикс полного _id
        return col.find_one({"_id": {"$regex": f"^{prefix}"}})
    except Exception:
        return None


def mark_running(db, task_id: str) -> None:
    if db is None:
        return
    try:
        db[_COLLECTION].update_one(
            {"_id": task_id},
            {"$set": {"status": "running", "last_activity": datetime.utcnow()}},
        )
    except Exception:
        pass


def mark_idle(db, task_id: str, messages: list | None = None) -> None:
    if db is None:
        return
    update: dict = {"status": "idle", "last_activity": datetime.utcnow()}
    if messages is not None:
        update["messages"] = _serialize_messages(messages)
    try:
        db[_COLLECTION].update_one({"_id": task_id}, {"$set": update})
    except Exception as e:
        log.warning("mark_idle failed: %s", e)


def mark_dead(db, task_id: str) -> None:
    if db is None:
        return
    try:
        db[_COLLECTION].update_one(
            {"_id": task_id},
            {"$set": {"status": "dead", "last_activity": datetime.utcnow()}},
        )
    except Exception:
        pass


def list_idle(db, parent_session: str | None = None, max_age_minutes: int = 60,
              limit: int = 10) -> list[dict]:
    """Idle-воркеры пригодные для send_to_task. Сортировка от свежих."""
    if db is None:
        return []
    cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
    query: dict = {"status": "idle", "last_activity": {"$gte": cutoff}}
    if parent_session:
        query["parent_session"] = parent_session
    try:
        return list(db[_COLLECTION].find(query, sort=[("last_activity", -1)], limit=limit))
    except Exception:
        return []
