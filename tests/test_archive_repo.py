"""Архив сжатых сообщений ArchiveRepo (ADR-008) на mongomock."""

from datetime import datetime, timedelta

from gateway.repos import ArchiveRepo
from tests.fakes_core import app_client  # noqa: F401  (фикстура)

T0 = datetime(2026, 10, 10, 1, 0)
SEARCH_CEILING = 200   # потолок выдачи поиска в ArchiveRepo.search


def _orig(_id, role="user", content="x", minute=0):
    return {"_id": _id, "role": role, "content": content, "timestamp": T0 + timedelta(minutes=minute),
            "msg_id": f"m-{_id}"}


def test_archive_keeps_originals_once_and_searches_by_session(mongo_db):
    repo = ArchiveRepo(mongo_db)
    repo.ensure_indexes()
    repo.archive_messages("s1", [_orig("1", "user", "привет"), _orig("2", "assistant", "ответ")], reason="compressed")
    repo.archive_messages("s1", [_orig("1", "user", "привет")], reason="compressed")   # повтор не дублирует
    repo.archive_messages("s1", [], reason="x")
    archived = list(mongo_db.sessions_archive.find({}))
    assert sorted(d["_orig_id"] for d in archived) == ["1", "2"]
    assert all(d["session_id"] == "s1" and d["archive_reason"] == "compressed" and d["archived_at"] for d in archived)

    for i in range(SEARCH_CEILING + 5):
        repo.archive_messages("s2", [_orig(f"m{i}", minute=i)], "compressed")
    assert {d["_orig_id"] for d in repo.search(session_id="s1")} == {"1", "2"}
    assert len(repo.search(session_id="s2", limit=5)) == 5
    assert len(repo.search(session_id="s2", limit=9999)) == SEARCH_CEILING


def test_same_document_is_kept_once_per_reason_and_old_index_is_replaced(mongo_db):
    """Сводка живёт на _id сообщения, оригинал которого уже в архиве: слитая сводка ложится рядом со своей причиной."""
    mongo_db.sessions_archive.create_index("_orig_id", unique=True, sparse=True, name="orig_id_unique")   # прежняя база
    repo = ArchiveRepo(mongo_db)
    repo.ensure_indexes()
    assert "orig_id_unique" not in mongo_db.sessions_archive.index_information()

    assert repo.archive_messages("s1", [_orig("1", content="оригинал")], reason="compressed") == 1
    assert repo.archive_messages("s1", [_orig("1", content="сводка"), _orig("2")], reason="recursive_merge") == 2
    assert repo.archive_messages("s1", [_orig("1", content="сводка"), _orig("3")], reason="recursive_merge") == 1
    by_reason = {(d["_orig_id"], d["archive_reason"]): d["content"] for d in mongo_db.sessions_archive.find({})}
    assert by_reason == {("1", "compressed"): "оригинал", ("1", "recursive_merge"): "сводка",
                         ("2", "recursive_merge"): "x", ("3", "recursive_merge"): "x"}


def test_archive_search_endpoint_shows_what_was_removed_and_why(app_client, monkeypatch):  # noqa: F811
    """compression 21: GET /sam/archive/search отдаёт удалённое из истории сессии с причиной удаления, новые
    первыми. Поиск по q - текстовый индекс Mongo, mongomock его не держит: проверяется на стенде."""
    from gateway import main
    client, db = app_client
    monkeypatch.setattr(main, "_mongo_ok", True)
    repo = ArchiveRepo(db)
    repo.archive_messages("s1", [_orig("1", content="кот Мурка спит", minute=0)], reason="compressed")
    repo.archive_messages("s1", [_orig("2", content="повтор про кота", minute=1)], reason="dedup_recent")
    repo.archive_messages("s2", [_orig("3", content="чужая сессия", minute=2)], reason="compressed")

    body = client.get("/sam/archive/search", params={"session": "s1"}).json()

    assert body["count"] == 2
    assert [(i["content"], i["archive_reason"], i["session_id"]) for i in body["items"]] == [
        ("повтор про кота", "dedup_recent", "s1"), ("кот Мурка спит", "compressed", "s1")]
