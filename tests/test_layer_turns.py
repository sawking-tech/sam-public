"""Правила «кто звучит» (voice/layer/turns.py, SAM-09)."""

from __future__ import annotations

import pytest

from voice.layer.turns import DUP, FAST, MAIN, OBSERVER, STALE, is_observer, retracted_rows, speaker_label, verdict

# Моменты разговора, секунды от условного нуля; важен только порядок.
NEVER = 0.0     # наблюдатель ещё не говорил
FIRST = 10.0    # первая фраза человека
FIX = 15.0      # реплика наблюдателя после неё
SECOND = 20.0   # следующая фраза человека
LATER = 21.0    # кусок речи после неё


@pytest.mark.parametrize("who, reply_to, last_phrase, last_human, last_observer, expected", [
    # человек спросил в FIRST, затем ещё раз в SECOND: ответ на первый вопрос уже мимо
    (FAST, FIRST, SECOND, SECOND, NEVER, STALE),
    (FAST, SECOND, SECOND, LATER, NEVER, None),
    # поправка наблюдателя в FIX после фразы в FIRST: запоздавший ответ быстрой - дубль
    (FAST, FIRST, FIRST, FIRST, FIX, DUP),
    # основная модель после ответа ведущего - тоже лишний второй голос
    (MAIN, FIRST, FIRST, FIRST, FIX, DUP),
    # после поправки человек сказал новое: ответ на него звучит
    (FAST, SECOND, SECOND, SECOND, FIX, None),
    ("observer", None, SECOND, SECOND, FIX, None),
], ids=["stale", "latest-sounds", "fast-dup-after-fix", "main-dup-after-fix", "new-event-lifts-block",
        "observer-without-reply-to"])
def test_who_sounds_verdict(who, reply_to, last_phrase, last_human, last_observer, expected) -> None:
    assert verdict(who, reply_to, last_phrase=last_phrase, last_human=last_human,
                   last_observer=last_observer) == expected


def test_main_model_lines_do_not_count_as_the_lead_answering() -> None:
    # иначе после первого предложения ответа основной модели остальные глушились как dup
    assert is_observer(OBSERVER) and is_observer(None)
    assert not is_observer(MAIN) and not is_observer(FAST)


@pytest.mark.parametrize("rows, retracted", [
    ([("owner", "вопрос 1", False), (FAST, "ответ 1", False), ("owner", "вопрос 2", False),
      (FAST, "чушь", False), (FAST, "ещё чушь", False), ("observer", "ой, поправлюсь", True)], {3, 4}),
    ([("owner", "вопрос", False), (FAST, "ответ", False), ("observer", "дополню", False)], set()),
    ([("owner", "вопрос", False), (MAIN, "ответ основной", False), (OBSERVER, "ой, поправлюсь", True)], set()),
    ([("owner", "вопрос", False), (FAST, "чушь", False), (MAIN, "поправка", True)], {1}),
], ids=["fast-since-last-phrase", "plain-observer-line", "main-answer-kept", "main-fix-retracts-fast"])
def test_fix_retracts_fast_lines_since_last_phrase(rows, retracted) -> None:
    assert retracted_rows(rows) == retracted


@pytest.mark.parametrize("who, label", [
    (FAST, "SAM · быстрая"), (MAIN, "SAM · основная"), (OBSERVER, "SAM · ведущий"), (None, "SAM"),
], ids=["fast", "main", "observer", "none"])
def test_spoken_line_label(who, label) -> None:
    assert speaker_label(who) == label
