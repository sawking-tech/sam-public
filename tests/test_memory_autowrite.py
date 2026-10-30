"""Автозапись памяти перед сжатием (SAM-63, ADR-015): факты удаляемого куска - в долгую память, с источником.
Настоящие сжатие, AutoWriter и RAGStore; подменены модель извлечения, Qdrant, эмбеддер и Mongo (mongomock)."""
import json
from datetime import datetime, timedelta

import pytest

from gateway import compression
from gateway import memory_extract as mx
from gateway.repos import ArchiveRepo, SessionMetaRepo, SessionRepo
from tests.fakes_core import make_rag_store

T0 = datetime(2026, 10, 9, 3, 0)
MURKA = {"facts": [{"text": "Мурка - кошка Антона.", "summary": "Мурка - кошка Антона",
                   "questions": ["Кто такая Мурка?"]}]}
# Сжатие при 24 сообщениях сессии уносит первые 4 (окно сырых сообщений - остальные)
SESSION_SIZE = 24
REMOVED = 4
SAME_TEXT_SCORE = 0.999


class _Llm:
    """Модель извлечения: отдаёт заданный ответ и помнит промпты."""

    def __init__(self, answer):
        self.answer = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        self.prompts = []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answer


async def _never(prompt):
    raise AssertionError("модель не нужна")


def _chunk():
    return [{"role": "user", "content": "Мурка опять залезла на шкаф", "timestamp": T0},
            {"role": "assistant", "content": "Кошки любят высоту", "timestamp": T0 + timedelta(minutes=1)},
            {"role": "user", "content": "Мурка - моя кошка, ей три года", "timestamp": T0 + timedelta(minutes=2)}]


def _msg(i, session):
    return {"_id": str(i), "session_id": session, "role": "user", "content": f"сообщение {i}",
            "timestamp": T0 + timedelta(minutes=i), "msg_id": f"m-{i}"}


async def _summary(messages, llm_config, prior):
    return "резюме"


async def _compress(mongo_db, session, on_forget, events):
    compression._compress_hashes.clear()
    for i in range(SESSION_SIZE):
        mongo_db.sessions.insert_one(_msg(i, session))
    ArchiveRepo(mongo_db).ensure_indexes()
    return await compression.compress_session_history(
        session, {}, on_forget=on_forget, session_repo=SessionRepo(mongo_db), archive_repo=ArchiveRepo(mongo_db),
        meta_repo=SessionMetaRepo(mongo_db), summarizer=_summary, is_trivial_summary=lambda t: False,
        event_emit=lambda k, d: events.append((k, d)))


async def test_compression_writes_facts_of_the_forgotten_chunk(mongo_db):
    """Сжатие отдаёт извлечению ровно удаляемый кусок; факт пишется в память с меткой и источником."""
    store = make_rag_store(mongo_db)
    llm, events = _Llm(MURKA), []
    await _compress(mongo_db, "s1", mx.AutoWriter(llm, store), events)

    prompt = llm.prompts[0]
    assert all(f"сообщение {i}" in prompt for i in range(REMOVED))
    assert f"сообщение {REMOVED}" not in prompt
    assert ("memory_autowrite", {"session": "s1", "facts": ["Мурка - кошка Антона."]}) in events
    fact = mongo_db["_memories"].find_one({"text": "Мурка - кошка Антона."})
    assert fact["tags"] == [mx.AUTO_TAG]
    assert fact["author"] == "auto:s1 2026-10-09 03:00-03:03"
    assert [r.text for r in store.search("Кто такая Мурка?", lexical_branch=False)] == ["Мурка - кошка Антона."]


TOO_MANY = {"facts": [{"text": f"факт номер {i}"} for i in range(mx.MAX_FACTS + 40)]}


@pytest.mark.parametrize("answer,chunk,written", [
    ('<think>{"facts": [{"text": "ложь"}]}</think>Вот: {"facts": [{"text": "Ирина любит чай"}]}', _chunk(),
     ["Ирина любит чай"]),                                               # JSON после think и прозы
    ("нет фактов", _chunk(), []),
    ("{не json}", _chunk(), []),
    ('{"facts": "строка"}', _chunk(), []),
    ("[1, 2]", _chunk(), []),
    ('{"facts": [{"summary": "без text"}, 5, {"text": "  "}]}', _chunk(), []),
    (TOO_MANY, _chunk(), [f"факт номер {i}" for i in range(mx.MAX_FACTS)]),   # больше MAX_FACTS - обрезано
    (None, [], []),                                                     # пустой кусок - без вызова модели
], ids=["json-inside-think", "prose", "broken-json", "facts-not-list", "not-object", "bad-items",
        "too-many", "empty-chunk"])
async def test_autowrite_skips_repeats_garbage_and_empty_chunks(mongo_db, answer, chunk, written):
    store = make_rag_store(mongo_db)
    # Дубль - только тот же текст (близость 1): векторы FakeEmbedder разных фактов близки, порог стенда их склеил бы
    writer = mx.AutoWriter(_never if answer is None else _Llm(answer), store, dup_score=SAME_TEXT_SCORE)
    assert await writer("voice", chunk) == written
    assert await writer("voice", chunk) == []                           # тот же кусок повторно - ничего нового
    assert mongo_db["_memories"].count_documents({}) == len(written)


async def test_failed_extraction_does_not_stop_compression(mongo_db):
    events = []

    async def _boom(session, messages):
        raise ConnectionError("provider down\nstack")
    result = await _compress(mongo_db, "s2", _boom, events)
    assert "Сжато" in result
    assert ("memory_autowrite_error", {"session": "s2", "error": "provider down"}) in events


@pytest.fixture
def extract_module_from_env(monkeypatch):
    """Модуль memory_extract, заново прочитавший окружение; после теста - снова по окружению процесса."""
    import importlib

    def load(**env):
        monkeypatch.delenv("SAM_MEMORY_AUTOWRITE", raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return importlib.reload(mx)
    yield load
    monkeypatch.undo()
    importlib.reload(mx)


@pytest.mark.parametrize("env, writes", [({}, False), ({"SAM_MEMORY_AUTOWRITE": "1"}, True)],
                         ids=["unset-off", "on"])
def test_autowrite_is_off_unless_the_variable_is_set(mongo_db, monkeypatch, extract_module_from_env, env, writes):
    """memory-in-pauses 8: без SAM_MEMORY_AUTOWRITE сжатие шлюза не получает автозаписи (on_forget=None) даже при
    живом хранилище памяти; с переменной - получает AutoWriter."""
    from gateway import agent
    module = extract_module_from_env(**env)
    monkeypatch.setattr(agent, "rag_store", make_rag_store(mongo_db))

    writer = agent._memory_autowriter()

    assert isinstance(writer, module.AutoWriter) if writes else writer is None
