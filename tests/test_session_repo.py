"""Mongo-репозитории истории (gateway/repos): окно истории для модели, пометка ignore, сводки сжатия, кэш сводки,
работа без Mongo. Настоящие репозитории на mongomock."""

from datetime import datetime, timedelta

import pytest

from gateway.event_bus import EventBus
from gateway.repos import ArchiveRepo, MemoryRepo, SessionMetaRepo, SessionRepo
from gateway.repos.dialogue_notes_repo import DialogueNotesRepo
from gateway.repos.session_repo import IGNORE

T0 = datetime(2026, 10, 10, 1, 0)
NOTES_MAX_AGE_S = 3600


def _msg(_id, session, role, content, minute=0, **extra):
    return {"_id": _id, "session_id": session, "role": role, "content": content,
            "timestamp": T0 + timedelta(minutes=minute), "msg_id": f"m{_id}", **extra}


def test_history_window_is_latest_messages_of_the_session(mongo_db):
    mongo_db.sessions.insert_many([_msg(str(i), "s1", "user", f"m{i}", minute=i) for i in (3, 0, 4, 1, 2)])
    mongo_db.sessions.insert_one(_msg("other", "s2", "user", "чужая", minute=9))
    repo = SessionRepo(mongo_db)
    assert [m["_id"] for m in repo.get_recent("s1", limit=10)] == ["0", "1", "2", "3", "4"]
    assert [m["_id"] for m in repo.get_recent("s1", limit=3)] == ["2", "3", "4"]
    assert [m["_id"] for m in repo.get_recent("s2")] == ["other"]


def test_ignored_record_stays_visible_but_out_of_context(mongo_db):
    """Пометка ignore (SAM-92, SAM-98): запись остаётся в базе и в снимке монитора, но не идёт модели и сжатию,
    не будит сжатие; отмена берёт последний обмен, а не фон."""
    repo = SessionRepo(mongo_db)
    mongo_db.sessions.insert_many([_msg("1", "voice", "user", "Скажи маме", 0),
                                   _msg("2", "voice", "assistant", "<voice>Ок</voice>", 1)])
    assert repo.ignore_last("voice", "поправка ведущего") == ["m2"]
    assert [d["msg_id"] for d in repo.get_recent("voice")] == ["m1"]
    assert [d["msg_id"] for d in repo.get_all_for_compression("voice")] == ["m1"]
    assert mongo_db.sessions.find_one({"_id": "2"})[IGNORE] == "поправка ведущего"
    assert repo.ignore_last("voice", "не по адресу", roles=("user",)) == ["m1"]
    assert repo.get_recent("voice") == []

    # Фон через шину: в базе с пометкой, вне контекста, сжатие будит только обычная реплика
    mongo_db.sessions.delete_many({})
    woke = []
    bus = EventBus()
    bus.set_db(mongo_db)
    bus.set_history_listener(woke.append)
    bus.emit("user_message", {"content": "Сэм, привет", "session": "voice"})
    bus.emit("user_message", {"content": "Спасибо", "session": "voice", IGNORE: "фон - обрывок вне беседы"})
    assert [d["content"] for d in repo.get_recent("voice")] == ["Сэм, привет"]
    assert mongo_db.sessions.find_one({"content": "Спасибо"})[IGNORE] == "фон - обрывок вне беседы"
    assert woke == ["voice"]

    # Отмена берёт последний обмен, фон после него остаётся (время записей задано, шина ставит своё)
    mongo_db.sessions.delete_many({})
    mongo_db.sessions.insert_many([_msg("1", "voice", "user", "Сэм, привет", 0),
                                   _msg("2", "voice", "assistant", "<voice>Привет</voice>", 1),
                                   _msg("3", "voice", "user", "Спасибо", 2, **{IGNORE: "фон"})])
    assert repo.delete_last_exchange("voice") == 2
    assert [d["content"] for d in mongo_db.sessions.find()] == ["Спасибо"]


def test_monitor_snapshot_is_not_crowded_out_by_background(mongo_db):
    docs = [{"session_id": "voice", "role": "user", "content": f"фраза {i}", "timestamp": T0 + timedelta(seconds=i)}
            for i in range(3)]
    docs += [{"session_id": "voice", "role": "user", "content": f"фон {i}", IGNORE: "фон",
              "timestamp": T0 + timedelta(seconds=i, milliseconds=500)} for i in range(-5, 3)]
    mongo_db.sessions.insert_many(docs)
    contents = {d["content"] for d in SessionRepo(mongo_db).recent_for_monitor(limit=2, hidden_limit=10)}
    assert {"фраза 1", "фраза 2"} <= contents                       # фон не вытеснил разговор
    assert contents - {"фраза 1", "фраза 2"} == {"фон 1", "фон 2"}  # фон - только за время снимка


def test_context_takes_summaries_apart_from_message_limit(mongo_db):
    """SAM-224: сводки контекста идут отдельно от лимита сообщений, слитые (ignore) в контекст не идут."""
    mongo_db.sessions.insert_many([
        _msg("old", "s1", "summary", "слитая", 0, is_summary=True, ignore="слита в сводку x"),
        _msg("sum", "s1", "summary", "сводка", 1, is_summary=True),
        *[_msg(f"m{i}", "s1", "user", f"реплика {i}", 10 + i) for i in range(5)],
    ])
    repo = SessionRepo(mongo_db)
    assert [d["_id"] for d in repo.get_recent("s1", 2)] == ["m3", "m4"]
    assert [d["_id"] for d in repo.get_recent("s1", 2, summaries=4)] == ["sum", "m3", "m4"]
    assert [d["_id"] for d in repo.active_summaries("s1")] == ["sum"]


def test_hide_and_extend_summary(mongo_db):
    mongo_db.sessions.insert_many([_msg("a", "s1", "summary", "a", 1, is_summary=True, time_range="10:00 – 10:05")])
    repo = SessionRepo(mongo_db)
    new_id = repo.insert_summary({"session_id": "s1", "role": "summary", "is_summary": True, "content": "ab"})
    assert repo.hide_ids(["a"], "слита в сводку ab") == 1
    repo.extend_summary(new_id, "10:20:00", "10:00 – 10:20")
    doc = mongo_db.sessions.find_one({"_id": new_id})
    assert (doc["time_to"], doc["time_range"]) == ("10:20:00", "10:00 – 10:20")
    assert mongo_db.sessions.find_one({"_id": "a"})["ignore"] == "слита в сводку ab"


def test_summary_cache_is_one_doc_per_session(mongo_db):
    repo = SessionMetaRepo(mongo_db)
    assert repo.get("s1") is None
    repo.upsert_summary_cache("s1", "first", compressed_up_to=10)
    repo.upsert_summary_cache("s1", "second", compressed_up_to=20)
    repo.upsert_summary_cache("s2", "other", compressed_up_to=1)
    assert mongo_db["_sessions_meta"].count_documents({"session_id": "s1"}) == 1
    doc = repo.get("s1")
    assert (doc["summary"], doc["compressed_up_to"]) == ("second", 20) and "updated_at" in doc


def _session_without_db():
    repo = SessionRepo(None)
    assert repo.get_recent("s1") == [] and repo.get_all_for_compression("s1") == []
    assert repo.active_summaries("s1") == [] and repo.hide_ids(["a"], "x") == 0
    assert repo.delete_by_ids(["a"]) == 0
    repo.insert({"x": 1})
    repo.upsert_summary("1", "x", "a", "b", "c", 1)


def _memory_without_db():
    repo = MemoryRepo(None)
    assert repo.insert("x") is None and repo.get_by_id("any") is None
    assert repo.find_many() == [] and repo.find_one({"x": 1}) is None
    assert repo.delete_by_id("x") is False and repo.update_by_id("x", {}) is False


def _meta_without_db():
    repo = SessionMetaRepo(None)
    assert repo.get("s1") is None
    repo.upsert_summary_cache("s1", "x", 0)


def _archive_without_db():
    repo = ArchiveRepo(None)
    assert repo.search("query") == []
    assert repo.archive_messages("s1", [{"_id": "x", "content": "y"}], "compressed") == 0


def _notes_without_db():
    repo = DialogueNotesRepo(None, NOTES_MAX_AGE_S)
    assert repo.get("s1") is None and repo.put("s1", {"topic": "x"}) == 0


@pytest.mark.parametrize("check", [_session_without_db, _memory_without_db, _meta_without_db, _archive_without_db,
                                   _notes_without_db], ids=["session", "memory", "meta", "archive", "dialogue-notes"])
def test_repos_degrade_without_mongo(check):
    """Mongo недоступна: чтение пустое, запись не бросает."""
    check()


def test_monitor_indexes_are_created_once_and_again_without_error(mongo_db):
    """Порции монитора по (время, _id) и лента сессии - по индексам; повторный старт шлюза их не ломает."""
    from gateway import tasks
    from gateway.repos.session_repo import MONITOR_PAGE_INDEX, MONITOR_SESSION_INDEX
    repo = SessionRepo(mongo_db)
    repo.ensure_indexes()
    repo.ensure_indexes()
    info = mongo_db.sessions.index_information()
    assert info[MONITOR_PAGE_INDEX]["key"] == [("timestamp", -1), ("_id", -1)]
    assert info[MONITOR_SESSION_INDEX]["key"] == [("session_id", 1), ("timestamp", -1), ("_id", -1)]
    tasks.ensure_indexes(mongo_db)
    tasks.ensure_indexes(mongo_db)
    assert any(v["key"] == [("created_at", 1)] for v in mongo_db.tasks.index_information().values())
