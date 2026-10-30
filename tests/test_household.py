"""Состав дома: запись, блок промпта, анкета и инструмент; число людей не ограничено."""
import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import household, household_api
from gateway.prompts import personal
from gateway.tools.catalog import set_household_member_impl


@pytest.fixture
def db():
    return mongomock.MongoClient()["sam"]


@pytest.fixture
def client(db):
    changes = []
    app = FastAPI()
    app.include_router(household_api.make_router(lambda: db, lambda: changes.append(1)))
    test_client = TestClient(app)
    test_client.changes = changes
    return test_client


@pytest.mark.parametrize("size", [1, 2, 7])
def test_any_size_of_household_reaches_the_prompt(db, size):
    for i in range(size):
        household.save(db, {"name": f"Человек {i}", "role": f"роль {i}", "about": f"заметка {i}"})
    block = household.prompt_block(household.members(db))
    assert all(f"- Человек {i} - роль {i}: заметка {i}" in block for i in range(size))


def test_empty_home_asks_once_and_accepts_a_refusal():
    block = household.prompt_block([])
    assert "один раз" in block and "больше не спрашивай" in block


def test_people_go_first_and_a_pet_is_marked(db):
    household.save(db, {"name": "Кличка", "kind": household.PET})
    household.save(db, {"name": "Яна", "role": "сестра"})
    assert household.prompt_block(household.members(db)).splitlines()[1:] == ["- Яна - сестра", "- Кличка (питомец)"]


def test_partial_update_keeps_known_fields(db):
    household.save(db, {"name": "Яна", "role": "сестра", "about": "любит чай"})
    household.save(db, {"name": "яна", "about": "любит кофе"})
    assert [(m["role"], m["about"]) for m in household.members(db)] == [("сестра", "любит кофе")]


@pytest.mark.parametrize("data, field", [
    ({"name": ""}, "имя"),
    ({"name": "Я", "kind": "robot"}, "kind"),
    ({"name": "Я", "about": "x" * (household.MAX_ABOUT + 1)}, "about"),
])
def test_bad_member_is_refused_with_the_field_name(data, field):
    with pytest.raises(household.HouseholdError, match=field):
        household.clean(data)


def test_multiline_value_stays_one_line_of_the_block():
    assert household.clean({"name": "Яна", "about": "первая\n\nвторая"})["about"] == "первая вторая"


def test_questionnaire_round_trip(client):
    assert client.get("/sam/household").json() == {"members": []}
    saved = client.put("/sam/household", json={"name": "Яна", "role": "сестра", "about": "любит чай"}).json()
    assert client.get("/sam/household").json()["members"] == [saved]
    assert client.delete(f"/sam/household/person/{saved['key']}").status_code == 200
    assert client.get("/sam/household").json() == {"members": []} and len(client.changes) == 2


def test_questionnaire_errors(client):
    assert client.put("/sam/household", json={"name": " "}).status_code == 400
    assert client.delete("/sam/household/person/nobody").status_code == 404


def test_no_store_says_what_to_check():
    app = FastAPI()
    app.include_router(household_api.make_router(lambda: None))
    answer = TestClient(app).get("/sam/household")
    assert answer.status_code == 503 and "Mongo" in answer.json()["detail"]


def test_tool_writes_and_reports(db):
    changes = []
    assert set_household_member_impl({"name": "Яна", "role": "сестра"}, db, lambda: changes.append(1)) == "Записано: Яна"
    assert set_household_member_impl({"name": ""}, db).startswith("Не записано") and len(changes) == 1
    assert set_household_member_impl({"name": "Яна"}, None) == "MongoDB недоступен"


def test_cached_block_reads_once_keeps_last_on_failure_and_rereads_after_change():
    now, reads = [0.0], [[{"kind": "person", "name": "Яна", "role": "", "about": ""}]]
    def read():
        if isinstance(reads[0], Exception):
            raise reads[0]
        calls.append(1)
        return reads[0]
    calls = []
    block = household.CachedBlock(read, ttl=60, clock=lambda: now[0], log=lambda _: None)
    assert "Яна" in block() and "Яна" in block() and len(calls) == 1
    now[0], reads[0] = 61.0, RuntimeError("no mongo")
    assert "Яна" in block()
    reads[0] = []
    block.invalidate()
    assert block() == household.EMPTY_BLOCK


def test_block_sits_in_the_prompt_before_personal_instructions(monkeypatch, tmp_path):
    personal_file = tmp_path / "SAM.md"
    personal_file.write_text("своё правило", encoding="utf-8")
    monkeypatch.setenv(personal.PERSONAL_ENV, str(personal_file))
    monkeypatch.setattr(personal, "_household", lambda: "ДОМ: блок")
    prompt = personal.personalize("база")
    assert prompt.index("база") < prompt.index("ДОМ: блок") < prompt.index("своё правило")


@pytest.mark.parametrize("claim", ["Все занесены в состав дома.", "Все записаны."])
def test_claim_of_saving_without_a_call_counts_as_an_action(claim):
    from gateway.runner import ACTION_VERBS
    assert any(verb in claim.lower() for verb in ACTION_VERBS)
