"""Долгая память RAGStore (SAM-40, SAM-45, SAM-102): Mongo - источник правды, Qdrant - индекс фасетов, поиск
гибридный - вектор и слова. Настоящий RAGStore и MemoryRepo; подменены Qdrant, эмбеддер и Mongo (mongomock)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from gateway import rag as rag_module
from gateway.rag import DimensionMismatchError, EmbedderUnavailableError, RAGStore
from gateway.repos import MemoryRepo
from tests.fakes_core import FakeEmbedder, FakeQdrant, make_rag_store

# Порог проверки «загрузка эмбеддера не держит поиск»: фабрика ждёт отпускания не дольше, чтобы тест не завис
# навсегда, если поиск всё-таки ждёт загрузку (тогда загрузка закончится и проверка упадёт)
EMBEDDER_RELEASE_WAIT_S = 10


@pytest.fixture
def fake_qdrant():
    return FakeQdrant()


@pytest.fixture
def rag_store(mongo_db, fake_qdrant):
    return make_rag_store(mongo_db, qdrant=fake_qdrant)


class BlindEmbedder:
    """Вектор, не различающий имя: короткий запрос ортогонален любому факту, косинус 0 - ниже любого порога.
    Так вела себя живая память:«Барсик» не находил «Кот Барсик» выше 0,5."""
    DIM = 4
    SHORT = 20   # короче - запрос, длиннее - факт

    def get_sentence_embedding_dimension(self):
        return self.DIM

    def encode(self, text, normalize_embeddings=False):
        return [1.0, 0.0, 0.0, 0.0] if len(text) > self.SHORT else [0.0, 1.0, 0.0, 0.0]


HOME_FACTS = [
    ("Правило Антона: SAM всегда отвечает восторженно и тепло", ["правило", "тон"]),
    ("Кот Барсик: белый пушистый шотландский котик окраса шиншилла", ["питомцы", "кот"]),
    ("День рождения Ирины - 3 марта, подарок выбрать заранее", ["ирина", "дата"]),
]


@pytest.fixture
def blind_store(mongo_db, fake_qdrant):
    store = make_rag_store(mongo_db, qdrant=fake_qdrant, embedder=BlindEmbedder())
    for text, tags in HOME_FACTS:
        store.remember(text, tags=tags)
    return store


# ─── запись и поиск ───────────────────────────────────────────────────────────

def test_remembered_fact_is_found_by_meaning_once(rag_store, fake_qdrant, mongo_db):
    """Факт с summary и вопросами - один документ Mongo и фасет на каждый ракурс; поиск отдаёт его один раз,
    текст - из Mongo."""
    assert rag_store.remember("") is None and rag_store.remember("   ") is None
    mid = rag_store.remember("у Мурки день рождения 1 января", tags=["мурка"], summary="Мурка - день рождения",
                             questions=["когда у Мурки день рождения?", "когда день рождения кота?", "что в мае?"])
    points = fake_qdrant.points()
    assert mongo_db["_memories"].count_documents({}) == 1
    assert len(points) == 4
    assert {p["payload"]["mongo_id"] for p in points} == {mid}
    assert sorted(p["payload"]["facet_type"] for p in points) == ["question"] * 3 + ["summary"]

    mongo_db["_memories"].update_one({"_id": ObjectId(mid)}, {"$set": {"text": "обновлённый из Mongo"}})
    results = rag_store.search("когда день рождения", top_k=10, score_threshold=0.0)
    assert [r.mongo_id for r in results] == [mid]
    assert results[0].text == "обновлённый из Mongo"
    assert rag_store.search("") == []

    fact = rag_store.get_fact(mid)
    assert (fact["mongo_id"], fact["tags"]) == (mid, ["мурка"])


def test_search_skips_archived_and_foreign_tags(rag_store, mongo_db):
    cats = rag_store.remember("кошки любят молоко", tags=["кошки"])
    rag_store.remember("собаки любят бегать", tags=["собаки"])
    for lexical_branch in (False, True):
        found = rag_store.search("любят", tag_filter="кошки", score_threshold=0.0, lexical_branch=lexical_branch)
        assert [r.mongo_id for r in found] == [cats]
    MemoryRepo(mongo_db).archive(cats)
    for lexical_branch in (False, True):
        found = rag_store.search("кошки любят", score_threshold=0.0, lexical_branch=lexical_branch)
        assert cats not in [r.mongo_id for r in found]


@pytest.mark.parametrize("query,threshold,expected", [
    ("Барсик", None, "Кот Барсик"),
    ("Ирине", None, "День рождения Ирины"),
    ("питомцы", None, "Кот Барсик"),
    ("Ирина", 0.99, "День рождения Ирины"),
], ids=["name", "case-form", "tag-word", "threshold-0.99"])
def test_word_branch_finds_what_vector_misses(blind_store, query, threshold, expected):
    assert blind_store.search(query, score_threshold=threshold, lexical_branch=False) == []
    results = blind_store.search(query, score_threshold=threshold)
    assert [r.text[:len(expected)] for r in results] == [expected]
    assert results[0].score == 0.0 and results[0].lexical > 0


def _scripted_store(mongo_db, fake_qdrant, vectors):
    class Scripted:
        def get_sentence_embedding_dimension(self):
            return 3

        def encode(self, text, normalize_embeddings=False):
            return vectors.get(text, [0.0, 0.0, 1.0])
    return make_rag_store(mongo_db, qdrant=fake_qdrant, embedder=Scripted())


RULE = "Правило: SAM всегда отвечает тепло и заботится о Антону и Ирине, о настроении всего дома"
BIRTHDAY = "День рождения Ирины - 3 марта"
CATS, DOGS, MICKEY = "кошки любят молоко", "собаки бегают во дворе", "Барсик любит гулять"


@pytest.mark.parametrize("facts,vectors,query,threshold,order", [
    # Нет общих слов с фактами: гибрид отдаёт ровно векторный порядок
    ([CATS, DOGS, MICKEY], {CATS: [0.2, 1, 0], DOGS: [1, 0.1, 0], MICKEY: [0.6, 0.6, 0],
                            "домашние животные": [1, 0, 0]}, "домашние животные", 0.0, [DOGS, MICKEY, CATS]),
    # Факт есть в обеих ветках - первый
    ([CATS, DOGS, MICKEY], {CATS: [1, 0, 0], DOGS: [0.9, 0.1, 0], MICKEY: [0.5, 0.5, 0],
                            "Барсик домашний": [1, 0, 0]}, "Барсик домашний", 0.0, [MICKEY, CATS, DOGS]),
    # Живая память: запрос-имя - сначала найденное по словам, вектор дополняет
    ([RULE, BIRTHDAY], {RULE: [1.0, 0.1, 0.0], BIRTHDAY: [0.0, 1.0, 0.0], "Ирина": [1.0, 0.0, 0.0]},
     "Ирина", None, [BIRTHDAY, RULE]),
    # Фраза - обычный RRF, «у» - служебное слово (lexical.STOP_WORDS)
    ([RULE, BIRTHDAY], {RULE: [1.0, 0.1, 0.0], BIRTHDAY: [0.0, 1.0, 0.0], "праздник у Ирины": [1.0, 0.0, 0.0]},
     "праздник у Ирины", None, [RULE, BIRTHDAY]),
], ids=["no-common-words-vector-order", "both-branches-first", "name-query-words-first", "phrase-rrf"])
def test_hybrid_ranking_order(mongo_db, fake_qdrant, facts, vectors, query, threshold, order):
    store = _scripted_store(mongo_db, fake_qdrant, vectors)
    for text in facts:
        store.remember(text)
    assert [r.text for r in store.search(query, top_k=3, score_threshold=threshold)] == order


def test_search_answers_by_words_while_embedder_loads(mongo_db, fake_qdrant):
    """Прогрев шлюза грузит эмбеддер в своём потоке; поиск в это время идёт словами, а не ждёт загрузку:
    после поиска загрузка всё ещё идёт."""
    entered, release = threading.Event(), threading.Event()

    def slow_factory():
        entered.set()
        release.wait(EMBEDDER_RELEASE_WAIT_S)
        return BlindEmbedder()
    store = make_rag_store(mongo_db, qdrant=fake_qdrant)
    store._embedder_factory = slow_factory
    MemoryRepo(mongo_db).insert(HOME_FACTS[1][0], tags=HOME_FACTS[1][1])
    warm = threading.Thread(target=store._get_embedder)
    warm.start()
    try:
        assert entered.wait(EMBEDDER_RELEASE_WAIT_S)
        results = store.search("Барсик")
        assert store.embedder_loading()
        assert results[0].text.startswith("Кот Барсик")
    finally:
        release.set()
        warm.join()
    assert not store.embedder_loading()


def test_embedder_loads_once_and_absence_is_explicit(mongo_db):
    loads = []
    started = threading.Barrier(4)

    def factory():
        loads.append(1)
        return BlindEmbedder()

    def call():
        started.wait()
        store._get_embedder()
    store = RAGStore(memory_repo=MemoryRepo(mongo_db), embedder_factory=factory)
    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert loads == [1]

    absent = RAGStore(memory_repo=MemoryRepo(mongo_db), embedder_factory=lambda: None)
    with pytest.raises(EmbedderUnavailableError):
        absent.embed("hi")


# ─── правка и забывание ───────────────────────────────────────────────────────

def test_update_and_forget_keep_vectors_in_sync(rag_store, fake_qdrant, mongo_db):
    mid = rag_store.remember("старый текст", summary="старо", questions=["старый вопрос?"])
    before = {p["id"] for p in fake_qdrant.points()}

    assert rag_store.update(mid, new_text="новый текст")                       # без переиндексации
    assert rag_store.get_fact(mid)["text"] == "новый текст"
    assert {p["id"] for p in fake_qdrant.points()} == before

    assert rag_store.update(mid, new_text="новый", new_summary="ново",
                            new_questions=["новый вопрос?", "ещё один?"], reindex=True)
    after = {p["id"] for p in fake_qdrant.points()}
    assert len(after) == 3 and not after & before

    rag_store.forget_by_id(mid)
    assert mongo_db["_memories"].count_documents({}) == 0 and fake_qdrant.points() == []
    assert rag_store.get_fact(mid) is None

    rag_store.remember("факт про кошек", tags=["кошки"])
    dogs = rag_store.remember("факт про собак", tags=["собаки"])
    assert rag_store.forget_by_query("кошки").startswith("Забыл")
    assert [str(d["_id"]) for d in mongo_db["_memories"].find()] == [dogs]
    assert {p["payload"]["mongo_id"] for p in fake_qdrant.points()} == {dogs}


def test_collection_with_other_dimension_is_refused(rag_store, fake_qdrant, mongo_db):
    rag_store.ensure_collection()
    assert fake_qdrant.collections["context"]["dim"] == FakeEmbedder().dim

    foreign = make_rag_store(mongo_db, qdrant=FakeQdrant(existing_collection_dim=384))
    with pytest.raises(DimensionMismatchError):
        foreign.ensure_collection()


# ─── сбои и обслуживание ──────────────────────────────────────────────────────

class _FailingWrites(MemoryRepo):
    """Mongo отвечает на чтение, но запись падает."""

    def update_by_id(self, *a, **kw):
        raise RuntimeError("write down")


class _NoModel:
    def embed(self, text):
        raise EmbedderUnavailableError("no model")


@pytest.mark.parametrize("case", ["last-used-write", "module-embed"])
def test_memory_failure_is_one_log_line(case, mongo_db, monkeypatch, caplog):
    if case == "last-used-write":
        store = make_rag_store(mongo_db)
        store.remember("кошки любят молоко")
        store.memory_repo = _FailingWrites(mongo_db)
        assert [r.text for r in store.search("кошки", score_threshold=0.0)] == ["кошки любят молоко"]
        assert "last_used_at" in caplog.text
    else:
        monkeypatch.setattr(rag_module, "_default_store", _NoModel())
        assert rag_module.embed("x") == []
        assert "embed: no model" in caplog.text


def test_tag_threshold_gates_vector_branch(mongo_db, fake_qdrant, monkeypatch):
    """Строгий порог метки skill (SAM_RAG_TAG_MIN) отсекает факт скилла в векторной ветке; из пар порогов
    побеждает самый строгий, битая пара пропускается."""
    text = "Скилл радар: где мои люди"
    writer = make_rag_store(mongo_db, qdrant=fake_qdrant)
    writer.remember(text, tags=["skill", "x"])
    writer.remember("Петя любит радар на кухне", tags=["fact"])

    def skill_found(tag_min: str) -> bool:
        monkeypatch.setenv(rag_module.TAG_MIN_SCORE_ENV, tag_min)
        found = make_rag_store(mongo_db, qdrant=fake_qdrant).search(text, score_threshold=0.0, lexical_branch=False)
        assert found
        return any("skill" in r.tags for r in found)
    assert not skill_found("skill=1.01,x=0.1,битое")
    assert not skill_found("skill=0.1,x=1.01")
    assert skill_found("skill=0.0")


def test_prune_unused_deletes_only_facts_unused_longer_than_the_limit(rag_store, fake_qdrant, mongo_db):
    """memory-search 29: факт без использования дольше 90 дней удаляется из Mongo и Qdrant без возврата; давний, но
    недавно найденный, и свежий остаются."""
    now = datetime.utcnow()
    old, recent = now - timedelta(days=120), now - timedelta(days=10)
    facts = {
        "never_used": ("кот Мурка спит на окне", {"created_at": old}),
        "used_long_ago": ("рецепт борща со свёклой", {"created_at": old, "last_used_at": now - timedelta(days=100)}),
        "used_lately": ("Ирина пьёт чай без сахара", {"created_at": old, "last_used_at": recent}),
        "fresh": ("сборка проекта в райдере", {}),
    }
    ids = {}
    for name, (text, fields) in facts.items():
        ids[name] = rag_store.remember(text)
        if fields:
            mongo_db["_memories"].update_one({"_id": ObjectId(ids[name])}, {"$set": fields})

    assert rag_store.prune_unused(max_age_days=90) == {"deleted": 2, "checked": 2}
    left = {str(d["_id"]) for d in mongo_db["_memories"].find({})}
    assert left == {ids["used_lately"], ids["fresh"]}
    assert {p["payload"]["mongo_id"] for p in fake_qdrant.points()} == left


def test_maintenance_reports_and_prunes_dead_facets(rag_store, fake_qdrant):
    """То, что зовёт ночная консолидация (gateway/dream.py): отчёт без сирот, старые ненайденные фасеты удаляются."""
    rag_store.remember("факт 1")
    rag_store.remember("факт 2 другой")
    report = rag_store.maintenance_check()
    assert (report["mongo_facts"], report["qdrant_vectors"], report["orphan_vectors"]) == (2, 2, 0)

    old = (datetime.utcnow() - timedelta(days=120)).isoformat()
    stale = fake_qdrant.points()[0]
    stale["payload"]["created_at"] = old
    assert rag_store.prune_dead_facets(min_access_count=0, max_age_days=90)["deleted"] == 1
    assert stale["id"] not in {p["id"] for p in fake_qdrant.points()}


# ─── фаза 4: пересборка индекса и записи скиллов ──────────────────────────────

def test_reindex_all_rebuilds_the_index_from_mongo_for_a_new_embedder(mongo_db, fake_qdrant):
    """memory-search 30: смена эмбеддера - коллекция пересоздаётся под новую размерность, на каждый факт Mongo
    точка с его mongo_id и метками, точки без факта уходят, поиск снова находит факты."""
    old = make_rag_store(mongo_db, qdrant=fake_qdrant, embedder=FakeEmbedder(dim=4))
    kot = old.remember("Кот Барсик любит спать на окне", tags=["кот"], summary="Барсик спит на окне")
    partner = old.remember("День рождения Ирины 3 марта", tags=["ирина"])
    old.remember("лишний факт")
    mongo_db["_memories"].delete_one({"_id": ObjectId(old.search("лишний факт", score_threshold=0.0)[0].mongo_id)})

    new = make_rag_store(mongo_db, qdrant=fake_qdrant, embedder=FakeEmbedder(dim=8))
    report = new.reindex_all()

    assert report == {"total_docs": 2, "reindexed": 2}
    assert fake_qdrant.collections["context"]["dim"] == 8
    points = fake_qdrant.points()
    assert sorted((p["payload"]["mongo_id"], tuple(p["payload"]["tags"])) for p in points) == sorted(
        [(kot, ("кот",)), (partner, ("ирина",))])
    assert [r.mongo_id for r in new.search("Барсик спит на окне", top_k=1, score_threshold=0.0)] == [kot]


def test_reindex_all_keeps_question_facets(rag_store, fake_qdrant):
    """memory-search 2 и 30: после пересборки у факта снова точка на summary и на каждый вопрос."""
    rag_store.remember("у Мурки день рождения 1 января", summary="Мурка - день рождения",
                       questions=["когда у Мурки день рождения?", "что в мае?"])
    rag_store.reindex_all()
    assert len(fake_qdrant.points()) == 3


def test_added_questions_survive_update_and_reindex(rag_store, fake_qdrant):
    """Вопросы, добавленные ночной уборкой, и вопросы факта переживают правку текста и пересборку индекса."""
    mongo_id = rag_store.remember("у Мурки день рождения 1 января", summary="Мурка - день рождения",
                                  questions=["когда у Мурки день рождения?"])
    assert rag_store.expand_facets(mongo_id, ["что в мае?"]) == 1
    rag_store.expand_facets(mongo_id, ["что в мае?"])          # повтор вопроса не дублирует его в факте
    assert rag_store.update(mongo_id, new_text="у Мурки день рождения 2 января", reindex=True)
    rag_store.reindex_all()
    facets = sorted(p["payload"]["facet_text"] for p in fake_qdrant.points() if p["payload"]["facet_type"] == "question")
    assert facets == ["когда у Мурки день рождения?", "что в мае?"]


def test_reindexed_points_get_a_fresh_term_before_pruning(rag_store, fake_qdrant):
    """Пересобранная точка начинает счёт заново: access_count с нуля, срок до уборки - от пересборки, иначе первая же
    ночная уборка снесла бы точки всех фактов старше её порога."""
    from datetime import datetime, timedelta
    mongo_id = rag_store.remember("старый факт", summary="старый факт", questions=["что старое?"])
    rag_store.memory_repo.col.update_one({"_id": __import__("bson").ObjectId(mongo_id)},
                                         {"$set": {"created_at": datetime.utcnow() - timedelta(days=365)}})
    rag_store.reindex_all()
    fresh = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    assert all(p["payload"]["created_at"] >= fresh for p in fake_qdrant.points())


# ─── фаза 5: P3 ───────────────────────────────────────────────────────────────

LEXICAL_ENV_SCRIPT = """
import mongomock
from tests.fakes_core import FakeQdrant, make_rag_store
from tests.test_rag import HOME_FACTS, BlindEmbedder
store = make_rag_store(mongomock.MongoClient()["t"], qdrant=FakeQdrant(), embedder=BlindEmbedder())
for text, tags in HOME_FACTS:
    store.remember(text, tags=tags)
print(len(store.search("Барсик")))
"""
SUBPROCESS_TIMEOUT_S = 60   # холодный импорт шлюза на занятой машине


@pytest.mark.parametrize("value, found", [("0", "0"), ("1", "1"), (None, "1")], ids=["off", "on", "default-on"])
def test_sam_rag_lexical_turns_the_word_branch_off(value, found):
    """stand 54: SAM_RAG_LEXICAL=0 выключает ветку слов - имя, которое вектор не различает, не находится;
    без переменной ветка включена. Процесс свой: переменная читается при импорте модуля."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = {k: v for k, v in os.environ.items() if k != "SAM_RAG_LEXICAL"}
    if value is not None:
        env["SAM_RAG_LEXICAL"] = value
    out = subprocess.run([sys.executable, "-c", LEXICAL_ENV_SCRIPT], cwd=Path(__file__).resolve().parents[1], env=env,
                         capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == found


@pytest.mark.parametrize("scan, found", [(2, False), (3, True)], ids=["beyond-scan", "within-scan"])
def test_word_branch_reads_no_more_than_the_scan_limit(blind_store, monkeypatch, scan, found):
    """memory-search 14: ветка слов читает не больше SAM_RAG_LEXICAL_SCAN фактов - факт за пределом по словам не
    находится."""
    monkeypatch.setattr(rag_module, "LEXICAL_SCAN_LIMIT", scan)
    results = blind_store.search("Ирине")
    assert bool(results and results[0].text.startswith("День рождения Ирины")) is found


@pytest.mark.xfail(strict=True, reason="дефект: MMR есть в RAGStore.search(mmr=True), но поиск шлюза его не включает")
def test_near_identical_facts_do_not_both_reach_the_turn(mongo_db, fake_qdrant, monkeypatch):
    """memory-search 18: MMR (λ 0,7) убирает почти одинаковые факты из выдачи хода. Два факта почти одним текстом
    (ниже порога дубля записи) и третий другой - в контекст хода идёт один из двух похожих и другой."""
    twin_a, twin_b, other = "Барсик любит гулять во дворе", "Барсик любит гулять по двору", "Кот Мурка спит на диване"
    store = _scripted_store(mongo_db, fake_qdrant, {twin_a: [1.0, 0.0, 0.0], twin_b: [0.99, 0.14, 0.0],
                                                    other: [0.6, 0.0, 0.8], "кто гуляет": [1.0, 0.05, 0.3]})
    for text in (twin_a, twin_b, other):
        mongo_db["_memories"].insert_one({"text": text})   # мимо дубля записи: так их пишут разные пути
    store.reindex_all()
    monkeypatch.setattr(rag_module, "_store", lambda: store)
    lines = rag_module.get_rag_context("кто гуляет", limit=2).splitlines()
    assert len(lines) == 2 and f"- {other}" in lines
