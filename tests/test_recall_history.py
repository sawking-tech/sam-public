"""Давнее из архива (SAM-228, gateway/recall_history.py): блок к фразе, запись-ссылка и её развёртка, быстрая без
поднятого. Архив подменён функциями поиска и чтения; репозиторий архива - на mongomock."""
from datetime import datetime, timedelta

from gateway import recall_history as rh
from gateway import runner
from gateway.repos import ArchiveRepo
from tests.test_runner import _make_deps

T0 = datetime(2026, 10, 10, 12, 0)
LLM = {"source": "lmstudio", "model": "qwen", "prompt_format": "qwen"}


def _arch(i, text, minute, role="user"):
    return {"_id": f"a{i}", "_orig_id": f"o{i}", "role": role, "content": text, "timestamp": T0 + timedelta(minutes=minute)}


ARCHIVE = [_arch(0, "test-do-not-use: полка в кладовке", 0), _arch(1, "Полку повешу в субботу.", 1, "assistant"),
           _arch(2, "test-do-not-use: а цвет полки белый", 40), _arch(3, "", 41), _arch(4, "[вызов]", 42, "tool_call")]


def _search(q, s, n):
    return ARCHIVE[:n]


def _fetch(ids):
    return [d for d in ARCHIVE if d["_id"] in set(ids)]


def test_archive_block_lists_talk_lines_only():
    block = rh.archive_block("voice", "полка", _search, limit=5)
    assert block.startswith(rh.ARCHIVE_HEADER)
    assert "человек: test-do-not-use: полка в кладовке" in block and "SAM: Полку повешу в субботу." in block
    assert "[вызов]" not in block
    assert rh.archive_block("voice", " ", _search) == "" and rh.archive_block("voice", "x", lambda *a: []) == ""


def test_recall_writes_one_reference_and_expand_restores_talk_by_time_with_gap_marks():
    written = []
    reply = rh.recall_impl({"query": "полка"}, "voice", _search, written.append)
    [ref] = written
    assert ref["role"] == rh.RECALL_ROLE and ref[rh.IDS_FIELD] == ["a0", "a1", "a2"] and "реплик: 3" in reply
    history = [{"_id": "h1", "role": "user", "content": "сейчас", "timestamp": T0 + timedelta(hours=5)},
               {**ref, "_id": "ref", "timestamp": T0 + timedelta(hours=5, minutes=1)}]
    out = rh.expand(history, _fetch)
    recalled = [d for d in out if d.get(rh.RECALLED)]
    assert [d["_id"] for d in recalled] == ["a0", "a1", "a2"] and out[:3] == recalled   # по времени - раньше окна
    assert recalled[0]["content"].startswith(rh.START_MARK)   # граница «отсюда давнее»
    assert recalled[2]["content"].startswith(rh.GAP_MARK) and not recalled[1]["content"].startswith(rh.GAP_MARK)
    assert len(out) == len(history) + 3   # история не копируется: развёртка только в контексте хода


def test_recall_without_query_or_hits():
    assert "Нужна тема" in rh.recall_impl({}, "voice", _search, print)
    assert "ничего нет" in rh.recall_impl({"query": "x"}, "voice", lambda *a: [], print)


def test_window_caps_expansion():
    ref = {"role": rh.RECALL_ROLE, rh.IDS_FIELD: ["a0", "a1", "a2"], "timestamp": T0 + timedelta(hours=1)}
    assert [d["_id"] for d in rh.expand([ref], _fetch, window=1) if d.get(rh.RECALLED)] == ["a2"]


def test_main_gets_archive_and_recalled_fast_gets_neither():
    deps = _make_deps()
    archive = rh.ARCHIVE_HEADER + "\n- test-do-not-use: архивная строка"
    history = [{**_arch(9, "test-do-not-use: поднятое", 0), rh.RECALLED: True},
               {"role": rh.RECALL_ROLE, "content": "Вернулись к прошлому разговору: полка", "timestamp": T0}]
    main = runner.build_messages(deps, "voice", "фраза", list(history), "", LLM, archive=archive)
    fast = runner.build_fast_messages(deps, "voice", "фраза", list(history), "", LLM)
    assert "архивная строка" in str(main) and "поднятое" in str(main) and "Вернулись к прошлому" in str(main)
    assert "архивная строка" not in str(fast) and "поднятое" not in str(fast) and "Вернулись" not in str(fast)


def test_archive_keeps_secret_mark_and_reads_by_ids(mongo_db):
    repo = ArchiveRepo(mongo_db)
    repo.archive_messages("voice", [{"_id": "m1", "role": "user", "content": "test-do-not-use: по секрету",
                                     "secret": True, "timestamp": T0},
                                    {"_id": "m2", "role": "user", "content": "привет", "timestamp": T0}], "compressed")
    docs = {d["_orig_id"]: d for d in mongo_db.sessions_archive.find()}
    assert docs["m1"]["secret"] is True and "secret" not in docs["m2"]
    assert [d["_orig_id"] for d in repo.get_by_ids([docs["m2"]["_id"]])] == ["m2"]


def test_twin_of_a_window_line_is_not_repeated():
    """dedup_recent кладёт в архив копию, оригинал остаётся в окне: развёртка его не повторяет."""
    ref = {"role": rh.RECALL_ROLE, rh.IDS_FIELD: ["a0", "a1"], "timestamp": T0 + timedelta(hours=1)}
    window = {"_id": "w", "role": "user", "content": "test-do-not-use: полка в кладовке", "timestamp": T0}
    assert [d["_id"] for d in rh.expand([window, ref], _fetch) if d.get(rh.RECALLED)] == ["a1"]


async def test_archive_block_in_time_or_none():
    """Блок архива считается в потоке с пределом: не успел - ход без него."""
    import time
    deps = _make_deps()
    deps.get_archive_context = lambda s, q: "БЛОК"
    assert await runner.archive_for(deps, "voice", "x", 1.0) == "БЛОК"

    def slow(s, q):
        time.sleep(0.3)
        return "ПОЗДНО"
    deps.get_archive_context = slow
    assert await runner.archive_for(deps, "voice", "x", 0.05) == ""


def test_forget_topic_cleans_the_archive_too(mongo_db):
    """Забытая тема уходит и из архива: он читается в ход."""
    from gateway.tools.forget_topic import forget_topic_impl
    mongo_db.sessions_archive.insert_many([{"session_id": "voice", "role": "user", "content": "test-do-not-use: котёнок"},
                                           {"session_id": "voice", "role": "user", "content": "про полку"}])
    reply = forget_topic_impl({"query": "котёнок"}, None, mongo_db, "voice")
    assert "1 из архива" in reply and [d["content"] for d in mongo_db.sessions_archive.find()] == ["про полку"]
