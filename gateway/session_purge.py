"""Удаление сессии со всеми следами и уборка сессий, которые больше никому не нужны.

Следы сессии: записи истории (sessions), архив сжатого (sessions_archive), заметки о разговоре (_dialogue_notes),
кэш сводки (_sessions_meta); у агента - ещё его сохранённый контекст (worker_sessions), из которого его оживляют.

Задача агента живёт в sam.tasks до expires_at (gateway/tasks.py: сутки после done, 2 ч после failed, TTL-индекс),
его контекст для воскрешения - в worker_sessions сутки с последней активности (gateway/worker_session.py). Обе
коллекции чистит TTL Mongo, а сессия `task:<id>` оставалась навсегда. Уборка снимает сессию агента, когда он не идёт
в памяти шлюза, нет ни задачи в любом статусе, ни контекста, и последняя запись старше grace (по умолчанию сутки,
как у успешного: упавший агент держит историю столько же, хотя его задача уходит через 2 ч). Сессии с явной меткой
проверки (session_kinds.CHECK_PREFIX) уходят по сроку без записей.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .repos.archive_repo import ArchiveRepo
from .repos.dialogue_notes_repo import DialogueNotesRepo
from .repos.meta_repo import SessionMetaRepo
from .repos.session_repo import SessionRepo
from .session_kinds import CHECK_PREFIX, TASK_PREFIX

TRACE_COLLECTIONS = (SessionRepo.COLLECTION, ArchiveRepo.COLLECTION, DialogueNotesRepo.COLLECTION,
                     SessionMetaRepo.COLLECTION)
WORKER_SESSIONS = "worker_sessions"   # gateway/worker_session.py _COLLECTION
TASKS = "tasks"


def task_key(session_id: str) -> str | None:
    """Короткий id задачи сессии агента, иначе None."""
    return session_id[len(TASK_PREFIX):] if session_id.startswith(TASK_PREFIX) else None


def purge(db, session_id: str) -> dict[str, int]:
    """Удалить следы сессии; сколько записей ушло из каждой коллекции (пустые не перечисляются)."""
    gone = {name: db[name].delete_many({"session_id": session_id}).deleted_count for name in TRACE_COLLECTIONS}
    key = task_key(session_id)
    if key:
        gone[WORKER_SESSIONS] = db[WORKER_SESSIONS].delete_many({"short_id": key}).deleted_count
    return {k: v for k, v in gone.items() if v}


def exists(db, session_id: str) -> bool:
    """Есть ли что удалять: записи сессии или контекст агента (бывает, что остался только он)."""
    if any(db[name].find_one({"session_id": session_id}, {"_id": 1}) for name in TRACE_COLLECTIONS):
        return True
    key = task_key(session_id)
    return bool(key and db[WORKER_SESSIONS].find_one({"short_id": key}, {"_id": 1}))


def agent_alive_in_db(db, key: str) -> bool:
    """Задача агента в любом статусе или его контекст для воскрешения ещё в Mongo."""
    return bool(db[TASKS].find_one({"id": {"$regex": "^" + re.escape(key)}}, {"_id": 1}) or
                db[WORKER_SESSIONS].find_one({"short_id": key}, {"_id": 1}))


def _last_activity(db) -> dict[str, datetime | None]:
    """Последняя запись каждой сессии по истории и архиву: сессия, сжатая целиком, живёт только в архиве."""
    out: dict[str, datetime | None] = {}
    for name in (SessionRepo.COLLECTION, ArchiveRepo.COLLECTION):
        for r in db[name].aggregate([{"$group": {"_id": "$session_id", "last": {"$max": "$timestamp"}}}]):
            sid, last = r["_id"], r.get("last")
            if isinstance(sid, str):
                prev = out.get(sid)
                out[sid] = max(d for d in (prev, last) if d) if (prev or last) else None
    return out


def dead_agent_sessions(db, now: datetime, grace: timedelta, live: frozenset[str] = frozenset()) -> list[str]:
    """Сессии агентов, которых нельзя оживить: не идут в памяти шлюза (`live` - короткие id), нет задачи и контекста,
    последняя запись старше `grace`."""
    out = []
    for sid, last in _last_activity(db).items():
        key = task_key(sid)
        if not key or key in live or (last and now - last < grace) or agent_alive_in_db(db, key):
            continue
        out.append(sid)
    return sorted(out)


def stale_check_sessions(db, now: datetime, ttl: timedelta) -> list[str]:
    """Сессии с явной меткой проверки, молчащие дольше `ttl`."""
    return sorted(sid for sid, last in _last_activity(db).items()
                  if sid.startswith(CHECK_PREFIX) and (last is None or now - last > ttl))


def sweep(db, now: datetime, grace: timedelta, check_ttl: timedelta, live: frozenset[str] = frozenset()) -> list[str]:
    """Уборка: умершие агенты и старые проверки; возвращает удалённые id."""
    ids = dead_agent_sessions(db, now, grace, live) + stale_check_sessions(db, now, check_ttl)
    for sid in ids:
        purge(db, sid)
    return ids
