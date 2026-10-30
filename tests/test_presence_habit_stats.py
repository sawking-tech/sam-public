"""Копилка привычек (SAM-118): уверенность, день недели, слот, гости, окно дат, смесь со словами и файл."""
from datetime import datetime

import pytest

from radar import presence_habit_stats as hs
from radar import presence_habits as ph

FIRST, SECOND, GUEST = "first", "second", "guest"
MON = datetime(2026, 10, 5, 10, 7)          # понедельник, слот 10:00
MON_BEFORE = datetime(2026, 9, 21, 10, 12)  # понедельник за 14 дней до


NEXT_MON = datetime(2026, 10, 12, 10, 5)    # следующий понедельник, тот же слот


def test_habit_prior_by_weekday_slot_guests_and_window():
    stats = hs.HabitStats()
    # в копилку идёт только уверенное наблюдение с местом и положительным временем
    assert stats.observe(FIRST, "кухня", MON, 900, confidence=0.99)
    assert not stats.observe(FIRST, "кухня", MON, 600, confidence=0.9)
    assert not stats.observe(FIRST, "", MON, 600) and not stats.observe(FIRST, "кухня", MON, 0)
    stats.observe(FIRST, "стол", MON, 300)
    # приор - доли секунд по местам в тот же день недели и слот
    prior = stats.prior(FIRST, NEXT_MON)
    assert prior.places == {"кухня": 0.75, "стол": 0.25} and prior.seconds == 1200 and prior.exact_guests
    assert stats.prior(FIRST, datetime(2026, 10, 6, 10, 5)) is None          # вторник: данных нет
    assert stats.prior(FIRST, datetime(2026, 10, 12, 11, 5)) is None         # другой слот
    small = hs.HabitStats()
    small.observe(FIRST, "кухня", MON, 120)
    assert small.prior(FIRST, MON) is None                                   # мало данных

    # набор гостей сопоставляется точно, на тонких данных - все наборы
    guests = hs.HabitStats()
    guests.observe(FIRST, "стол", MON, 900)
    guests.observe(FIRST, "диван", MON, 900, guests=[GUEST])
    assert guests.prior(FIRST, MON, guests=[GUEST]).places == {"диван": 1.0}
    assert guests.prior(FIRST, MON, guests=[]).places == {"стол": 1.0}
    fallback = guests.prior(FIRST, MON, guests=[GUEST, "гость"])
    assert not fallback.exact_guests and fallback.places == {"стол": 0.5, "диван": 0.5}

    # окно дат держит только недавние дни, отсчёт от as_of
    window = hs.HabitStats()
    window.observe(FIRST, "кухня", MON_BEFORE, 900)
    window.observe(FIRST, "стол", MON, 900)
    assert window.prior(FIRST, NEXT_MON).places == {"кухня": 0.5, "стол": 0.5}
    assert window.prior(FIRST, NEXT_MON, window_days=14).places == {"стол": 1.0}
    assert window.prior(FIRST, NEXT_MON, window_days=30, as_of=datetime(2026, 10, 5, 12, 0)).places == {"кухня": 0.5, "стол": 0.5}


def test_habit_stats_blend_with_words():
    words = {"стол": 0.5, "кухня": 0.3, ph.AWAY: 0.2}
    assert hs.blend(words, None) == words
    stats = hs.StatPrior({"диван": 1.0}, hs.BLEND_STRENGTH_S, True)           # вес копилки 0,5
    out = hs.blend(words, stats)
    assert out[ph.AWAY] == 0.2 and out["стол"] == pytest.approx(0.25) and out["диван"] == pytest.approx(0.5 * 0.8)


def test_habit_stats_survive_json_round_trip():
    stats = hs.HabitStats(step_min=30)
    stats.observe(SECOND, "диван", MON, 600, guests=[GUEST])
    again = hs.HabitStats.from_json(stats.to_json())
    assert again.step_min == 30 and again.days == stats.days
