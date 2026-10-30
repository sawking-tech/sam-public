"""POST /sam/memory (SAM-94): основная модель вне шлюза пишет факт в долгую память; секрет команд маскируется.

Путь настоящий: эндпоинт, mask_input с ключом из окружения, _impl_remember, RAGStore; подменены только Mongo
(mongomock), Qdrant и эмбеддер."""
from functools import partial

import pytest

from gateway import agent, main
from gateway.secret_mask import load_secrets, mask
from tests.fakes_core import FakeQdrant, app_client, make_rag_store  # noqa: F401 - фикстура

SECRET = "904317"


@pytest.fixture
def memory(app_client, monkeypatch):
    client, db = app_client
    monkeypatch.setenv("SAM_COMMAND_SECRET", SECRET)
    monkeypatch.setattr(main, "mask_input", partial(mask, secret=load_secrets()))
    qdrant = FakeQdrant()
    monkeypatch.setattr(agent, "rag_store", make_rag_store(db, qdrant=qdrant))
    return client, db, qdrant


def test_outside_model_writes_fact_to_memory(memory):
    client, db, qdrant = memory
    r = client.post("/sam/memory", json={"fact": f"Роутер дома - Routerich AX3000, ключ {SECRET} назвали",
                                         "summary": f"роутер {SECRET}", "tags": "дом,роутер"})
    assert r.status_code == 200 and r.json()["result"].startswith("Запомнил")
    doc = db["_memories"].find_one()
    stored = str(doc)
    assert "Routerich AX3000" in stored and SECRET not in stored
    assert set(doc["tags"]) == {"дом", "роутер"}
    assert qdrant.points() and SECRET not in str(qdrant.points())


def test_repeated_fact_is_not_written_twice(memory):
    """Повтор факта (сходство не ниже RAGStore.dedup_threshold) - ответ «уже помню» с текстом записи, второй
    записи нет; другой факт пишется."""
    client, db, qdrant = memory
    fact = {"fact": "Ирина пьёт кофе без сахара", "tags": "ирина"}
    first = client.post("/sam/memory", json=fact).json()["result"]
    points = len(qdrant.points())
    again = client.post("/sam/memory", json=fact).json()["result"]
    assert first.startswith("Запомнил")
    assert again.startswith("Уже помню") and fact["fact"] in again
    assert db["_memories"].count_documents({}) == 1 and len(qdrant.points()) == points

    other = client.post("/sam/memory", json={"fact": "Роутер стоит в коридоре на полке"}).json()["result"]
    assert other.startswith("Запомнил") and db["_memories"].count_documents({}) == 2


def test_empty_fact_is_refused_with_a_reason(memory):
    client, db, qdrant = memory
    r = client.post("/sam/memory", json={"fact": "   "})
    assert r.status_code == 400 and "fact" in r.json()["detail"]
    assert db["_memories"].count_documents({}) == 0 and qdrant.points() == []
