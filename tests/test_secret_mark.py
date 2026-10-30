"""Секреты для быстрой модели (ADR-027, gateway/secret_mark.py): пометка фразы кодом и факты памяти без содержания."""
import pytest

from gateway import secret_mark as sm


@pytest.mark.parametrize("text, secret", [
    ("Сэм, по секрету: хочу котёнка", True),
    ("Только  никому   не говори, ладно?", True),
    ("Сэм, какая погода", False),
    ("Какое расстояние между нами и соседями", False),
])
def test_phrase_is_marked_by_trigger_words(text, secret):
    assert sm.is_secret_phrase(text) is secret


def test_trigger_words_from_env(monkeypatch):
    monkeypatch.setenv("SAM_SECRET_TRIGGERS", "тсс")
    assert sm.is_secret_phrase("тсс, это личное") and not sm.is_secret_phrase("по секрету")


def test_fast_fact_hides_secret_content_and_skill_descriptions():
    assert sm.fast_fact("Партнёр хочет котёнка", [sm.SECRET_TAG], "у партнёра есть личная тема") == \
        sm.SECRET_FACT.format(summary="у партнёра есть личная тема")
    assert sm.fast_fact("Партнёр хочет котёнка", [sm.SECRET_TAG], "") is None
    assert sm.fast_fact("Партнёр хочет котёнка", ["Secret"], "Партнёр хочет котёнка") is None   # summary по умолчанию
    assert sm.fast_fact("Скилл «radar»: ...", ["skill", "skill:radar"], "радар") is None
    assert sm.fast_fact("Человек любит чай", ["habit"], "") == "Человек любит чай"


def test_secret_phrase_is_marked_when_written_to_history(mongo_db):
    """Правило кода при записи: фраза с триггером ложится в историю с полем secret, не дожидаясь основной."""
    from gateway.event_bus import EventBus
    bus = EventBus()
    bus._db = mongo_db
    bus._persist("user_message", "voice", {"content": "test-do-not-use: по секрету, хочу котёнка", "msg_id": "m1"})
    bus._persist("user_message", "voice", {"content": "test-do-not-use: привет", "msg_id": "m2"})
    docs = {d["msg_id"]: d for d in mongo_db.sessions.find()}
    assert docs["m1"].get(sm.FIELD) is True and sm.FIELD not in docs["m2"]
