"""Сохранённые сессии воркеров (SAM-38) на mongomock: поиск по id, idle по возрасту и родителю, без Mongo."""
from datetime import datetime, timedelta

import pytest

from gateway import worker_session as ws

SESSIONS = [
    # (полный id, родительская сессия, часов с последней активности)
    ("deadbeef-cafe-1234", "parentA", 0),
    ("12345678-aabb-ccdd", "parentA", 3),
    ("abcdef00-1111-2222", "parentB", 1),
]


@pytest.fixture
def db(mongo_db):
    ws.ensure_indexes(mongo_db)
    for task_id, parent, hours in SESSIONS:
        assert ws.save_session(mongo_db, task_id, device="pc", description="x", messages=[], parent_session=parent)
        mongo_db["worker_sessions"].update_one(
            {"_id": task_id}, {"$set": {"last_activity": datetime.utcnow() - timedelta(hours=hours)}})
    return mongo_db


@pytest.mark.parametrize("lookup, found", [
    (lambda db: ws.find_by_prefix(db, "deadbeef"), ["deadbeef-cafe-1234"]),            # по короткому id
    (lambda db: ws.find_by_prefix(db, "12345678-aa"), ["12345678-aabb-ccdd"]),         # префикс длиннее 8
    (lambda db: ws.find_by_prefix(db, "ffffffff"), [None]),
    (lambda db: ws.list_idle(db, parent_session="parentA", max_age_minutes=60), ["deadbeef-cafe-1234"]),
    (lambda db: ws.list_idle(db, max_age_minutes=60 * 4), ["deadbeef-cafe-1234", "abcdef00-1111-2222",
                                                           "12345678-aabb-ccdd"]),       # от свежих
    (lambda db: ws.find_by_prefix(None, "deadbeef"), [None]),                           # без Mongo - мягкий отказ
    (lambda db: ws.load_session(None, "deadbeef-cafe-1234"), [None]),
    (lambda db: ws.list_idle(None), []),
], ids=["short_id", "long_prefix", "unknown", "idle_by_age_and_parent", "idle_all_recent_first", "no_mongo_find",
        "no_mongo_load", "no_mongo_idle"])
def test_worker_session_lookup(db, lookup, found):
    result = lookup(db)
    docs = result if isinstance(result, list) else [result]
    assert [d["_id"] if d else None for d in docs] == found


def test_worker_session_goes_running_idle_dead(db):
    tid = "deadbeef-cafe-1234"
    ws.mark_running(db, tid)
    assert ws.load_session(db, tid)["status"] == "running"
    ws.mark_idle(db, tid, messages=[{"role": "user", "content": "updated"}])
    doc = ws.load_session(db, tid)
    assert doc["status"] == "idle" and doc["messages"] == [{"role": "user", "content": "updated"}]
    ws.mark_dead(db, tid)
    assert ws.load_session(db, tid)["status"] == "dead"
    assert ws.save_session(None, tid, device="x", description="x", messages=[], parent_session="x") is False
