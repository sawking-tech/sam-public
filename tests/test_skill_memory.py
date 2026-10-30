"""Скиллы в долгой памяти (gateway/skill_memory.py): сверка папки скиллов с памятью штатными методами RAGStore.
Настоящие sync, RAGStore и MemoryRepo; подменены Qdrant, эмбеддер и Mongo (mongomock)."""
from __future__ import annotations

import pytest

from gateway import skill_memory
from gateway.repos import MemoryRepo
from gateway.skills import Skill
from tests.fakes_core import FakeQdrant, make_rag_store

RADAR = Skill("radar", "Где мои люди сейчас и где были. Вызывать, когда спрашивают «где кто» или «где был».", "тело радара")
COOK = Skill("cooking-help", "Что приготовить из того, что есть дома. «что поесть»", "тело готовки")
HIDDEN = Skill("secret-tool", "Скрытый скилл", "тело", listed=False)
NOTHING = {"added": 0, "updated": 0, "forgotten": 0, "restored": 0}


def _alive():
    return True


@pytest.fixture
def memory(mongo_db):
    qdrant = FakeQdrant()
    return make_rag_store(mongo_db, qdrant=qdrant), MemoryRepo(mongo_db), qdrant


def _facets(qdrant, mongo_id) -> set[str]:
    return {p["payload"]["facet_text"] for p in qdrant.points() if p["payload"]["mongo_id"] == str(mongo_id)}


def test_skill_sync_follows_the_skills_folder(memory):
    store, repo, qdrant = memory
    assert skill_memory.sync([RADAR, COOK, HIDDEN], store, repo, _alive) == {**NOTHING, "added": 2}
    assert skill_memory.sync([RADAR, COOK, HIDDEN], store, repo, _alive) == NOTHING
    assert repo.find_one({"tags": "skill:secret-tool"}) is None                       # скрытый не пишется

    radar = repo.find_one({"tags": "skill:radar"})
    assert radar["text"].startswith("Скилл «radar»: Где мои люди") and 'load_skill("radar")' in radar["text"]
    assert "тело радара" not in radar["text"]                                         # тело скилла не в памяти
    assert radar["tags"][:2] == ["skill", "skill:radar"]
    assert {"где кто", "где был"} <= _facets(qdrant, radar["_id"])

    edited = Skill("radar", "Где люди квартиры сейчас. «кто дома»", "")
    assert skill_memory.sync([edited, COOK], store, repo, _alive) == {**NOTHING, "updated": 1}
    radar = repo.find_one({"tags": "skill:radar"})
    assert "кто дома" in radar["text"]
    facets = _facets(qdrant, radar["_id"])
    assert "кто дома" in facets and "где кто" not in facets                         # эмбеддинг переписан

    cook = skill_memory.skill_fact(COOK)
    store.remember(cook.text, tags=cook.tags, summary=cook.summary, questions=cook.questions,
                   source_session=skill_memory.SOURCE)                                  # дубль своего факта
    assert skill_memory.sync([edited, COOK], store, repo, _alive) == {**NOTHING, "forgotten": 1}
    assert len(list(repo.find_many({"tags": "skill:cooking-help"}, limit=10))) == 1

    assert skill_memory.sync([edited], store, repo, _alive) == {**NOTHING, "forgotten": 1}
    assert repo.find_one({"tags": "skill:cooking-help"}) is None
    assert {p["payload"]["mongo_id"] for p in qdrant.points()} == {str(radar["_id"])}


def test_skill_sync_keeps_foreign_facts_and_restores_own_archived(memory):
    store, repo, _ = memory
    foreign = store.remember("Петя считает скилл радара лучшим", tags=["skill"])     # чужой факт со свободной меткой
    skill_memory.sync([RADAR], store, repo, _alive)
    own = repo.find_one({"tags": "skill:radar"})
    repo.archive(own["_id"])
    assert skill_memory.sync([RADAR], store, repo, _alive)["restored"] == 1
    assert not repo.get_by_id(own["_id"]).get("archived")
    assert repo.get_by_id(foreign) is not None                                      # чужой не забыт


def test_skill_sync_skips_when_memory_down(memory):
    store, repo, _ = memory
    assert "error" in skill_memory.sync([RADAR], store, repo, lambda: False)
    assert repo.find_one({"tags": "skill"}) is None
