"""Душа SAM в хранилище (gateway/soul_service.py, gateway/repos/soul_repo.py): засев, правка без рестарта, откат."""
import pytest

from gateway import agent, self_config
from gateway.prompts import lead, soul, system, voice
from gateway.prompts.identity import MALE
from gateway.repos.soul_repo import SoulRepo
from gateway.soul_service import SoulService, rollback_target

LEGACY = "---\nowner: Хозяин\n---\nУтром не напоминай про спорт."


@pytest.fixture
def service(mongo_db, monkeypatch):
    """Сервис на mongomock; сборка промптов процесса читает его снимок, после теста - снова заготовка."""
    monkeypatch.setattr(self_config, "_instance", self_config.SelfConfig())   # род по умолчанию, не со стенда
    svc = SoulService(SoulRepo(mongo_db, history_limit=3), log=lambda line: None)
    soul.configure(svc.load)
    yield svc
    soul.configure(soul.read_seed)


def build_all() -> dict:
    return {"main": system.system_prompt_template(), "voice": voice.voice_prompt_template(),
            "voice_base": voice.voice_prompt_base(), "lead": lead.lead_prompt(MALE), "system": system.system_prompt()}


def test_prompts_from_the_store_equal_prompts_from_the_seed_file(service):
    from_store = build_all()
    soul.configure(soul.read_seed)
    assert from_store == build_all()


def test_seed_fills_an_empty_store_with_the_character_only(mongo_db):
    lines = []
    svc = SoulService(SoulRepo(mongo_db, 3), log=lines.append)
    inserted = svc.seed()
    seed = soul.read_seed()
    assert inserted == ["IDENTITY", "CHARACTER", "TONE"] == list(seed) and len(lines) == 1
    assert SoulRepo(mongo_db, 3).load() == seed and svc.show("IDENTITY")["author"] == "seed"


def test_seed_does_not_touch_a_filled_store(mongo_db):
    svc = SoulService(SoulRepo(mongo_db, 3), log=lambda line: None)
    svc.seed()
    assert svc.write("TONE", "Свой тон.", "owner").ok
    mongo_db[SoulRepo.COLLECTION].delete_one({"_id": "CHARACTER"})   # раздел, которого в базе нет
    again = SoulService(SoulRepo(mongo_db, 3), log=lambda line: None)
    assert again.seed() == ["CHARACTER"]
    assert SoulRepo(mongo_db, 3).load()["TONE"] == "Свой тон."


def legacy_store(mongo_db, edited: dict | None = None):
    """Хранилище, где жил весь промпт: инструкции, шапка и личный слой рядом с характером."""
    repo = SoulRepo(mongo_db, 3)
    repo.insert_missing({**soul.read_seed(), "_header": "owner: человек", "DOING": "Инструкция."}, {}, "seed")
    repo.insert_missing({"_personal": LEGACY}, {}, "data/SAM.md")
    for name, text in (edited or {}).items():
        repo.put(name, text, "owner", repo.version(name))
    return repo


def test_seed_removes_untouched_records_that_are_not_the_soul(mongo_db):
    lines = []
    legacy_store(mongo_db)
    svc = SoulService(SoulRepo(mongo_db, 3), log=lines.append)
    assert set(svc.seed()) == {"_header", "DOING", "_personal"}
    assert set(SoulRepo(mongo_db, 3).names()) == {"IDENTITY", "CHARACTER", "TONE"}
    assert any("удалены из хранилища" in line and "DOING" in line for line in lines)


def test_seed_keeps_an_edited_instruction_record_unread_and_names_it(mongo_db):
    lines = []
    legacy_store(mongo_db, edited={"DOING": "Правка человека."})
    svc = SoulService(SoulRepo(mongo_db, 3), log=lines.append)
    svc.seed()
    assert "DOING" in SoulRepo(mongo_db, 3).names() and "DOING" not in svc.load()
    assert {e["_id"] for e in svc.entries()} == {"IDENTITY", "CHARACTER", "TONE"}
    assert any("правленые записи оставлены" in line and "DOING" in line for line in lines)


def test_edit_reaches_the_next_build_without_restart(service):
    assert "Говорю стихами." not in agent._system_prompt()
    result = service.write("CHARACTER", "Говорю стихами.", "sam")
    assert result.ok and result.version == 2
    assert "Говорю стихами." in agent._system_prompt()
    assert "Говорю стихами." in lead.lead_prompt(MALE)


@pytest.mark.parametrize("name, text, reason", [
    ("IDENTITY", "  \n<!-- пусто -->\n", "IDENTITY"),
    ("DOING", "Инструкция.", "DOING не раздел души"),
    ("_personal", "---\nowner: x\n---\n", "_personal не раздел души"),
], ids=["empty_section", "instruction", "personal"])
def test_broken_edit_is_refused_and_the_prompt_stays(service, name, text, reason):
    before = agent._system_prompt()
    result = service.write(name, text, "sam")
    assert not result.ok and reason in result.message and "\n" not in result.message
    assert agent._system_prompt() == before


def test_broken_record_in_the_store_logs_and_keeps_the_last_good_build(service, mongo_db, capsys):
    before = agent._system_prompt()
    mongo_db[SoulRepo.COLLECTION].update_one({"_id": "TONE"}, {"$set": {"text": ""}})   # правка мимо сервиса
    soul.invalidate()
    assert agent._system_prompt() == before
    assert "последнюю рабочую версию" in capsys.readouterr().out


def test_unknown_placeholder_is_written_with_a_warning(service):
    result = service.write("TONE", "Тон для {nobody}.", "sam")
    assert result.ok and "{nobody}" in result.message


def test_rollback_restores_the_previous_version_and_is_itself_undoable(service):
    original = service.show("TONE")["text"]
    service.write("TONE", "Первая правка.", "sam")
    service.write("TONE", "Вторая правка.", "sam")
    back = service.rollback("TONE", None, "owner")
    assert back.ok and service.show("TONE")["text"] == "Первая правка."
    assert service.show("TONE")["author"] == "owner, откат к версии 2"
    assert service.rollback("TONE", 1, "owner").ok and service.show("TONE")["text"] == original
    versions = [entry["version"] for entry in service.history("TONE")]
    assert versions == [5, 4, 3, 2]   # история - последние три версии и текущая


@pytest.mark.parametrize("history, version, expected", [
    ([{"version": 1}], None, "нет прошлых версий"),
    ([{"version": 3}, {"version": 2}], 3, "и так текущая"),
    ([{"version": 3}, {"version": 2}], 7, "нет версии 7; есть: 2"),
    ([{"version": 3}, {"version": 2}, {"version": 1}], None, {"version": 2}),
    ([{"version": 3}, {"version": 2}, {"version": 1}], 1, {"version": 1}),
], ids=["no_history", "current", "unknown", "previous", "exact"])
def test_rollback_target(history, version, expected):
    target = rollback_target(history, version)
    assert (expected in target) if isinstance(expected, str) else target == expected


class RacingCollection:
    """Коллекция, в которой между чтением записи и её правкой успевает пройти чужая правка."""

    def __init__(self, col):
        self._col = col

    def find_one(self, *args, **kwargs):
        doc = self._col.find_one(*args, **kwargs)
        self._col.update_one({"_id": doc["_id"]}, {"$inc": {"version": 1}})
        return doc

    def __getattr__(self, name):
        return getattr(self._col, name)


def test_concurrent_edit_is_refused(service, mongo_db, monkeypatch):
    monkeypatch.setattr(SoulRepo, "col", property(lambda self: RacingCollection(self._db[self.COLLECTION])))
    result = service.write("TONE", "Моя правка.", "sam", expected=1)
    assert not result.ok and result.conflict and result.message == "раздел TONE изменился (v2), перечитай"
    assert mongo_db[SoulRepo.COLLECTION].find_one({"_id": "TONE"})["text"] != "Моя правка."


def test_author_saw_v3_while_the_store_has_v4(service):
    """Человек открыл раздел на v3, SAM за это время записала v4: правка человека не затирает её молча."""
    service.write("TONE", "v2", "sam")
    service.write("TONE", "v3", "sam")
    seen = service.show("TONE")["version"]
    assert seen == 3
    assert service.write("TONE", "v4 от SAM", "sam", expected=seen).ok
    late = service.write("TONE", "правка человека", "owner", expected=seen)
    assert not late.ok and late.conflict and "изменился (v4), перечитай" in late.message
    assert service.show("TONE")["text"] == "v4 от SAM"
    assert not service.rollback("TONE", 2, "owner", expected=seen).ok


def test_without_mongo_edits_are_refused():
    svc = SoulService(SoulRepo(None, 3), log=lambda line: None)
    assert svc.load() is None and svc.entries() == []
    assert not svc.write("TONE", "x", "sam").ok and "недоступно" in svc.write("TONE", "x", "sam").message


def test_service_without_seed_does_not_raise(mongo_db, tmp_path):
    lines = []
    svc = SoulService(SoulRepo(mongo_db, 3), read_seed=lambda: soul.read_seed(tmp_path / "нет.md"),
                      log=lines.append)
    assert svc.seed() == [] and isinstance(svc.required(), tuple)
    assert any("нет заготовки" in line for line in lines)


def test_gateway_started_without_mongo_writes_once_mongo_is_up(mongo_db):
    from gateway.soul_service import LazyDb
    clock, state, lines = [0.0], {"up": False}, []

    def connect():
        if not state["up"]:
            raise ConnectionError("connection refused")
        return mongo_db
    svc = SoulService(SoulRepo(None, 3, connect=LazyDb(connect, 30, clock=lambda: clock[0], log=lines.append)),
                      invalidate=lambda: None, log=lambda line: None)
    assert svc.load() is None and not svc.write("TONE", "x", "sam").ok
    state["up"] = True
    assert svc.load() is None   # повтор не раньше retry_s
    clock[0] = 31
    assert svc.load()["TONE"] == soul.read_seed()["TONE"]
    assert svc.write("TONE", "Тон.", "sam").ok
    assert "недоступна" in lines[0] and "поднялась" in lines[1]


def seed_with(**changes):
    seed = dict(soul.read_seed())
    seed.update(changes)
    return lambda: seed


def test_new_seed_updates_untouched_sections_and_keeps_edited_ones(mongo_db):
    lines = []
    SoulService(SoulRepo(mongo_db, 3), log=lines.append).seed()
    edited = SoulService(SoulRepo(mongo_db, 3), log=lines.append)
    assert edited.write("CHARACTER", "Свой характер.", "owner").ok
    lines.clear()
    newer = seed_with(TONE="Новый тон из заготовки.", CHARACTER="Новый характер из заготовки.")
    svc = SoulService(SoulRepo(mongo_db, 3), read_seed=newer, log=lines.append)
    changed = svc.seed()
    assert set(changed) == {"TONE"}
    assert svc.show("TONE")["text"] == "Новый тон из заготовки." and svc.show("TONE")["author"] == "seed"
    assert svc.show("CHARACTER")["text"] == "Свой характер."
    assert "[Soul] заготовка раздела CHARACTER изменилась, в базе правка человека, не трогаю" in lines
    lines.clear()
    assert svc.seed() == [] and lines == ["[Soul] заготовка раздела CHARACTER изменилась, в базе правка человека, не трогаю"]


def test_records_seeded_before_hashes_follow_the_seed_when_untouched(mongo_db):
    """База, засеянная до хэшей (стенд 15 окт): записи без seed_hash и с автором seed идут за заготовкой."""
    svc = SoulService(SoulRepo(mongo_db, 3), log=lambda line: None)
    svc.seed()
    mongo_db[SoulRepo.COLLECTION].update_many({}, {"$unset": {"seed_hash": ""}})
    again = SoulService(SoulRepo(mongo_db, 3), read_seed=seed_with(TONE="Тон 2."),
                        log=lambda line: None)
    assert again.seed() == ["TONE"] and again.show("TONE")["text"] == "Тон 2."
    assert mongo_db[SoulRepo.COLLECTION].count_documents({"seed_hash": {"$exists": True}}) >= len(soul.read_seed())
