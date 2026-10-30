"""Поводы своей инициативы (SAM-49): повод проверяет код, реплику делает основная модель шагом workflow (SAM-96)."""
from datetime import datetime

import pytest

from gateway import heartbeat as hb

NIGHT = datetime(2026, 10, 9, 3, 10)
DAY = datetime(2026, 10, 9, 15, 0)


@pytest.mark.parametrize("hours, now, since_human_s, reason", [
    ((2, 6), NIGHT, 60, True),
    ((2, 6), NIGHT, None, False),                          # никого не слышно - спит
    ((2, 6), NIGHT, hb.ACTIVE_S + 1, False),               # давно не слышно
    ((2, 6), DAY, 60, False),
    ((23, 6), datetime(2026, 10, 9, 0, 30), 10, True),      # окно через полночь
    ((23, 6), datetime(2026, 10, 9, 23, 5), 10, True),
    ((23, 6), datetime(2026, 10, 9, 12, 0), 10, False),
    ((23, 6), datetime(2026, 10, 9, 6, 0), 10, False),
], ids=["night_awake", "nobody_heard", "heard_long_ago", "day", "wrap_after_midnight", "wrap_before_midnight",
        "wrap_day", "wrap_end_hour"])
def test_late_night_reason(monkeypatch, hours, now, since_human_s, reason):
    monkeypatch.setattr(hb, "LATE_FROM_H", hours[0])
    monkeypatch.setattr(hb, "LATE_TO_H", hours[1])
    got = hb.late_night(now, since_human_s=since_human_s)
    if reason:
        # 10 окт в 02:00 модель сказала «три часа ночи»: повод несёт точное время, его и называть
        assert f"ровно {now:%H:%M}" in got
    else:
        assert got == ""


def test_human_activity_counts_from_last_phrase():
    t = [100.0]
    human = hb.HumanActivity(clock=lambda: t[0])
    assert human.since_s() is None
    human.note()
    t[0] += 42
    assert human.since_s() == 42
