"""SAM-88, SAM-104: ключи маскируются на входе и не ложатся ни в монитор, ни в историю."""
from functools import partial

import pytest

from gateway.event_bus import EventBus
from gateway.repos import SessionRepo
from gateway.secret_mask import MASK, load_secret, load_secrets, mask, scrub

SECRET = "904317"   # тестовый, не настоящий
KEYS = {"command": "904317", "other": "123456"}
NO_KEY = frozenset()
COMMAND = frozenset({"command"})


@pytest.mark.parametrize("heard, secret, text, names", [
    ("Сэм, удали историю, ключ 904317", SECRET, f"Сэм, удали историю, ключ {MASK}", COMMAND),
    ("ключ 904 317 удали", SECRET, f"ключ {MASK} удали", COMMAND),
    ("ключ 904-317", SECRET, f"ключ {MASK}", COMMAND),
    ("ключ девятьсот четыре триста семнадцать, удали", SECRET, f"ключ {MASK}, удали", COMMAND),
    ("девятьсот четыре тысячи триста семнадцать", SECRET, MASK, COMMAND),
    ("девять ноль четыре три один семь", SECRET, MASK, COMMAND),
    ("встреча в 9 и 4 человека", SECRET, "встреча в 9 и 4 человека", NO_KEY),
    ("сорок четыре года", SECRET, "сорок четыре года", NO_KEY),
    ("позвони 31 числа", SECRET, "позвони 31 числа", NO_KEY),
    ("удали 19043175", SECRET, f"удали {MASK}", NO_KEY),
    ("удали 11234567", KEYS, f"удали {MASK}", NO_KEY),
    ("ключ 904317", "", "ключ 904317", NO_KEY),
], ids=["digits", "spaces", "hyphen", "words", "words_thousands", "digit_by_digit", "ordinary_digits",
        "ordinary_words", "ordinary_date", "longer_number_masks_without_right", "longer_number_with_a_key_inside",
        "no_secret_configured"])
def test_command_key_is_masked_in_every_spelling(heard, secret, text, names):
    got = mask(heard, secret)
    assert (got.text, got.names, got.secret) == (text, names, "command" in names)


def test_no_command_key_in_env_means_no_secret(monkeypatch):
    monkeypatch.delenv("SAM_COMMAND_SECRET", raising=False)
    assert load_secret() == ""


@pytest.mark.parametrize("got, expected", [
    (mask("ключ 904317 и ещё 123456", KEYS), (f"ключ {MASK} и ещё {MASK}", frozenset({"command", "other"}))),
    (mask("только 123456", KEYS), (f"только {MASK}", frozenset({"other"}))),
    (mask("ключ 904317", SECRET), (f"ключ {MASK}", COMMAND)),
], ids=["both_keys", "other_key", "string_secret_is_the_command_key"])
def test_keys_are_masked_and_reported_by_purpose(got, expected):
    assert (got.text, got.names) == expected


def test_secrets_load_from_env_by_purpose_and_scrub_goes_deep():
    env = {"SAM_COMMAND_SECRET": "904 317", "SAM_OTHER_SECRET": "123456", "SAM_EMPTY_SECRET": "",
           "SAM_FOO": "1", "PATH": "x", "SAM_TWO_WORDS_SECRET": "777"}
    assert load_secrets(env) == {"command": "904317", "other": "123456", "two_words": "777"}
    doc = {"content": "ключ 904317", "args": {"cmd": ["rm", "904 317"], "other": ["123456"]}, "n": 3}
    assert scrub(doc, KEYS) == {"content": f"ключ {MASK}", "args": {"cmd": ["rm", MASK], "other": [MASK]}, "n": 3}


def _through_event_bus(db):
    bus = EventBus()
    bus.set_scrubber(partial(scrub, secret=SECRET))
    bus.set_db(db)
    monitor = bus.subscribe()
    bus.emit("user_message", {"content": "удали, ключ 904317", "session": "s1"})
    return [monitor.get_nowait()]


def _through_session_repo(db):
    repo = SessionRepo(db, scrub=partial(scrub, secret=SECRET))
    repo.insert({"_id": "1", "session_id": "s1", "role": "user", "content": "ключ 904317"})
    repo.upsert_summary("1", "Антон назвал ключ 904317", "t0", "t1", "r")
    return []


@pytest.mark.parametrize("write", [_through_event_bus, _through_session_repo], ids=["event_bus", "session_repo"])
def test_secret_never_reaches_history_or_monitor(mongo_db, write):
    seen_by_monitor = write(mongo_db)
    stored = list(mongo_db.sessions.find({}))
    assert stored and MASK in str(stored)
    assert "904317" not in str(stored) + str(seen_by_monitor)
