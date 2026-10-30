"""Сессии монитора: вид по признакам (gateway/session_kinds.py), удаление со всеми следами и уборка умерших агентов
и старых проверок (gateway/session_purge.py), ручки GET и DELETE /sam/sessions. История - на mongomock."""
from datetime import datetime, timedelta

import pytest

from gateway import session_kinds as sk
from gateway import session_purge as sp
from tests.fakes_core import app_client  # noqa: F401 - фикстура

NOW = datetime(2026, 10, 15, 6, 0)
FRESH = timedelta(hours=24)
GRACE = timedelta(hours=1)
CHECK_TTL = timedelta(hours=72)
PEOPLE = sk.person_sessions(["homepc"])
PROTECTED = sk.protected_sessions("homepc")


def _rec(session_id, hours_ago, **extra):
    return {"session_id": session_id, "role": "user", "content": "x", "timestamp": NOW - timedelta(hours=hours_ago),
            **extra}


def test_kind_comes_from_the_data_not_the_name_order():
    assert sk.classify("voice", PEOPLE) == sk.KIND_PERSON and sk.classify("cli", PEOPLE) == sk.KIND_PERSON
    assert sk.classify("homepc", PEOPLE) == sk.KIND_PERSON                       # устройство каталога
    assert sk.classify("task:b343f470", PEOPLE) == sk.KIND_AGENT
    assert sk.classify("fast-1", PEOPLE) == sk.KIND_CHECK                           # свой X-Device-ID прогона
    assert sk.classify(sk.CHECK_PREFIX + "voice", PEOPLE) == sk.KIND_CHECK


def test_selector_rows_are_fresh_first_and_old_checks_are_stale():
    rows = [{"id": "fast-1", "last": NOW - timedelta(days=6), "count": 6},
            {"id": "voice", "last": NOW - timedelta(days=3), "count": 87},
            {"id": "apps-recheck-1", "last": NOW - timedelta(hours=2), "count": 9},
            {"id": "task:b343f470", "last": NOW - timedelta(days=2), "count": 27}]
    got = sk.describe(rows, PEOPLE, PROTECTED, "homepc", NOW, FRESH)
    assert [r["id"] for r in got] == ["apps-recheck-1", "task:b343f470", "voice", "fast-1", "homepc"]
    stale = {r["id"]: r["stale"] for r in got}
    assert stale == {"apps-recheck-1": False, "task:b343f470": True, "voice": False, "fast-1": True,
                     "homepc": False}                                  # канал человека не устаревает
    assert {r["id"] for r in got if r["protected"]} == {"voice", "homepc"}
    assert [r["id"] for r in got if r["default"]] == ["homepc"]


def test_purge_removes_every_trace_of_the_session(mongo_db):
    sid = "task:b343f470"
    for name in sp.TRACE_COLLECTIONS:
        mongo_db[name].insert_many([{"session_id": sid}, {"session_id": "voice"}])
    mongo_db.worker_sessions.insert_one({"_id": "b343f470-1", "short_id": "b343f470"})
    gone = sp.purge(mongo_db, sid)
    assert gone == {**{name: 1 for name in sp.TRACE_COLLECTIONS}, sp.WORKER_SESSIONS: 1}
    assert not sp.exists(mongo_db, sid) and sp.exists(mongo_db, "voice")   # чужая сессия цела
    assert sp.purge(mongo_db, sid) == {}


def test_sweep_takes_dead_agents_and_old_marked_checks_only(mongo_db):
    mongo_db.sessions.insert_many([
        _rec("task:dead0001", 30),                                 # задачи и контекста нет - оживить нельзя
        _rec("task:done0002", 30),                                 # задача ещё в sam.tasks (сутки после done)
        _rec("task:idle0003", 30),                                 # контекст для воскрешения ещё жив
        _rec("task:new00004", 0.1),                                # только что: задачу пишут до первого шага
        _rec(sk.CHECK_PREFIX + "old", 100), _rec(sk.CHECK_PREFIX + "fresh", 2),
        _rec("fast-1", 200), _rec("voice", 200)])                  # без метки и каналы людей уборка не трогает
    mongo_db.sessions_archive.insert_one(_rec("task:arch0005", 30))   # сжатая целиком - только в архиве
    mongo_db.tasks.insert_one({"id": "done0002-aaaa", "status": "done"})
    mongo_db.worker_sessions.insert_one({"_id": "idle0003-bbbb", "short_id": "idle0003"})

    assert sp.sweep(mongo_db, NOW, GRACE, CHECK_TTL) == ["task:arch0005", "task:dead0001", sk.CHECK_PREFIX + "old"]
    left = set(mongo_db.sessions.distinct("session_id"))
    assert left == {"task:done0002", "task:idle0003", "task:new00004", sk.CHECK_PREFIX + "fresh", "fast-1", "voice"}
    assert mongo_db.sessions_archive.count_documents({}) == 0


@pytest.fixture
def stand(app_client, monkeypatch):
    from gateway import main
    client, db = app_client
    monkeypatch.setattr(main, "_mongo_ok", True)
    emitted = []
    monkeypatch.setattr(main.event_bus, "emit", lambda t, d: emitted.append((t, d)))
    return client, db, emitted


def owner_key():
    """Ключ ведущего: сессии не задач без ключа не удаляются (файл ключа тестов - tests/conftest.py)."""
    from gateway import privilege_key
    return privilege_key.headers()


def test_delete_removes_a_check_session_and_tells_the_monitors(stand):
    client, db, emitted = stand
    db.sessions.insert_many([_rec("fast-1", 1), _rec("voice", 1)])
    db["_dialogue_notes"].insert_one({"session_id": "fast-1"})
    r = client.delete("/sam/sessions/fast-1", headers=owner_key())
    assert r.status_code == 200 and r.json()["deleted"] == {"sessions": 1, "_dialogue_notes": 1}
    assert db.sessions.distinct("session_id") == ["voice"]
    assert emitted == [("session_deleted", {"session_id": "fast-1", "deleted": {"sessions": 1, "_dialogue_notes": 1}})]
    assert client.delete("/sam/sessions/fast-1", headers=owner_key()).status_code == 404


@pytest.mark.parametrize("sid, code", [("voice", 403), ("workpc", 403), ("default", 403), ("task:run00001", 409)],
                         ids=["voice", "this_machine", "no_device_header", "working_agent"])
def test_delete_refuses_people_channels_and_running_agents_in_one_line(stand, sid, code):
    client, db, emitted = stand
    db.sessions.insert_one(_rec(sid, 1))
    db.tasks.insert_one({"id": "run00001-cccc", "status": "running"})
    r = client.delete(f"/sam/sessions/{sid}")
    assert r.status_code == code and "\n" not in r.json()["detail"] and sid.split(":")[-1] in r.json()["detail"]
    assert db.sessions.count_documents({"session_id": sid}) == 1 and emitted == []


def test_a_task_session_needs_no_key_any_other_needs_the_owner_or_lead_key(stand, monkeypatch):
    from gateway import main
    client, db, _ = stand
    monkeypatch.setenv("SAM_COMMAND_SECRET", "482913")
    monkeypatch.setattr(main, "_session_delete_limiter", main.soul_api.SecretLimiter(log=lambda m: None))
    db.sessions.insert_many([_rec("fast-1", 1), _rec("fast-2", 1), _rec("task:old00001", 1)])
    denied = client.delete("/sam/sessions/fast-1")
    assert denied.status_code == 403 and "ключ" in denied.json()["detail"]
    assert client.delete("/sam/sessions/fast-1", headers={"X-SAM-Command-Secret": "111111"}).status_code == 403
    assert client.delete("/sam/sessions/fast-1", headers={"X-SAM-Command-Secret": "482913"}).status_code == 200
    assert client.delete("/sam/sessions/fast-2", headers=owner_key()).status_code == 200
    assert client.delete("/sam/sessions/task:old00001").status_code == 200
    assert db.sessions.count_documents({}) == 0


def test_wrong_owner_keys_close_session_deletes_for_a_while(stand, monkeypatch):
    from gateway import main
    client, db, _ = stand
    monkeypatch.setenv("SAM_COMMAND_SECRET", "482913")
    monkeypatch.setattr(main, "_session_delete_limiter", main.soul_api.SecretLimiter(max_failures=2, log=lambda m: None))
    db.sessions.insert_one(_rec("fast-1", 1))
    for _ in range(2):
        client.delete("/sam/sessions/fast-1", headers={"X-SAM-Command-Secret": "111111"})
    assert client.delete("/sam/sessions/fast-1", headers={"X-SAM-Command-Secret": "482913"}).status_code == 429
    assert client.delete("/sam/sessions/fast-1", headers=owner_key()).status_code == 200    # ключ ведущего паузой не закрыт


def test_kind_and_protection_ignore_case_and_an_empty_setting(monkeypatch):
    assert sk.classify("HomePC", PEOPLE) == sk.KIND_PERSON and sk.classify("Voice", PEOPLE) == sk.KIND_PERSON
    monkeypatch.setenv("SAM_PROTECTED_SESSIONS", "")
    protected = sk.protected_sessions("homepc")
    assert all(sk.is_protected(s, protected) for s in ("voice", "VOICE", "default", "HomePC"))


def test_a_failed_agent_keeps_its_history_a_day_like_a_successful_one(mongo_db):
    """Задача упавшего агента уходит по TTL через 2 ч, история - через сутки после последней записи, как у
    успешного; задача в любом статусе и агент, идущий в памяти шлюза, держат сессию."""
    day = timedelta(hours=24)
    mongo_db.sessions.insert_many([_rec("task:fail0001", 3), _rec("task:fail0002", 30), _rec("task:canc0003", 30),
                                   _rec("task:mem00004", 30)])
    mongo_db.tasks.insert_one({"id": "canc0003-dddd", "status": "cancelled"})
    assert sp.dead_agent_sessions(mongo_db, NOW, day, frozenset({"mem00004"})) == ["task:fail0002"]


def test_a_session_left_only_as_agent_context_is_deleted_not_404(stand):
    client, db, emitted = stand
    db.worker_sessions.insert_one({"_id": "ctx00001-eeee", "short_id": "ctx00001"})
    r = client.delete("/sam/sessions/task:ctx00001")
    assert r.status_code == 200 and r.json()["deleted"] == {sp.WORKER_SESSIONS: 1}


def test_delete_waits_for_no_turn_compression_or_running_agent(stand, monkeypatch):
    """Удаление не идёт посреди хода, сжатия и пока агент идёт в памяти шлюза, хотя его задачи в Mongo уже нет."""
    from gateway import agent, compression, session_lock
    client, db, emitted = stand
    db.sessions.insert_many([_rec("fast-1", 1), _rec("fast-2", 1), _rec("task:mem00001", 1)])
    monkeypatch.setattr(session_lock, "_busy", {"fast-1"})
    monkeypatch.setattr(compression, "_compress_in_progress", {"fast-2"})
    monkeypatch.setattr(agent, "_task_context_map", {"task:mem00001": ("voice", "mem00001")})
    answers = {sid: client.delete(f"/sam/sessions/{sid}", headers=owner_key()) for sid in ("fast-1", "fast-2", "task:mem00001")}
    assert {sid: r.status_code for sid, r in answers.items()} == {"fast-1": 409, "fast-2": 409, "task:mem00001": 409}
    assert "идёт ход" in answers["fast-1"].json()["detail"] and "идёт сжатие" in answers["fast-2"].json()["detail"]
    assert "ещё работает" in answers["task:mem00001"].json()["detail"]
    assert db.sessions.count_documents({}) == 3 and emitted == []


def test_sessions_list_survives_a_mongo_error_and_shows_archive_only_sessions(stand, monkeypatch):
    from gateway import main
    client, db, _ = stand
    db.sessions_archive.insert_one(_rec("old-run", 5))                   # сжата целиком: записи только в архиве
    assert "old-run" in [r["id"] for r in client.get("/sam/sessions").json()]

    def broken():
        raise RuntimeError("mongo down")
    monkeypatch.setattr(main.session_repo, "sessions_summary", broken)
    r = client.get("/sam/sessions")
    assert r.status_code == 200 and [(x["id"], x["default"]) for x in r.json()] == [("workpc", True)]


def test_sweep_finds_mongo_that_came_up_after_the_gateway(monkeypatch):
    """Шлюз стартовал без Mongo: уборка берёт своё подключение, как только Mongo отвечает."""
    import pymongo

    from gateway import main

    class _Admin:
        def command(self, name):
            return {"ok": 1}

    class _Client:
        admin = _Admin()

        def __init__(self, *a, **k):
            pass

        def __getitem__(self, name):
            return f"db:{name}"
    monkeypatch.setattr(main, "_mongo_ok", False)
    monkeypatch.setattr(main, "_sweep_client", None)
    monkeypatch.setattr(pymongo, "MongoClient", _Client)
    assert main._sweep_db() == "db:sam"
