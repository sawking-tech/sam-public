"""Поза по долям (SAM-120): выборка повторяет таблицу, держит выдержку, меняется со сменой места; копилка пишет только наблюдённую позу. Данные выдуманные."""
import random
from datetime import datetime

import pytest

from radar import presence_habit_stats as pstats
from radar import presence_hub as ph
from radar import presence_pose as ppose

EVENING = datetime(2026, 10, 12, 22, 0).timestamp()      # понедельник, 22:00
NOON = datetime(2026, 10, 12, 12, 0).timestamp()
HABITS = {"poses": {"person-a": [{"place": "sofa", "poses": {"lie": 0.45, "sit": 0.55}, "when": [{"days": "all", "from": "21:00", "to": "03:00"}]},
                                 {"place": "armchair", "poses": {"sit": 1.0}}]}}


def test_pose_sampler_follows_the_shares_and_holds_the_pose():
    """Тысяча выборок повторяет доли, поза держит выдержку, новое место - новый выбор, вне окна и для неизвестных - None."""
    sampler = ppose.PoseSampler(HABITS, None, random.Random(7), hold_s=10)
    poses = [sampler.pose("person-a", "sofa", EVENING + 10 * k) for k in range(1000)]
    assert poses.count("lie") / 1000 == pytest.approx(0.45, abs=0.04)
    assert set(poses) == {"lie", "sit"}
    sampler = ppose.PoseSampler(HABITS, None, random.Random(3), hold_s=120)
    first = sampler.pose("person-a", "sofa", EVENING)
    assert all(sampler.pose("person-a", "sofa", EVENING + s) == first for s in range(0, 120, 5))
    assert sampler.pose("person-a", "armchair", EVENING + 30) == "sit"                    # смена места - выбор заново, без выдержки
    assert ppose.PoseSampler(HABITS, None, random.Random(3)).pose("person-a", "sofa", EVENING + 10) == first     # один сид - одна выборка
    sampler = ppose.PoseSampler(HABITS, None, random.Random(1))
    assert sampler.pose("person-a", "sofa", NOON) is None
    assert sampler.pose("person-b", "sofa", EVENING) is None and sampler.pose("person-a", "bed", EVENING) is None
    assert ppose.table_shares(HABITS, "person-a", "armchair", datetime.fromtimestamp(NOON)) == {"sit": 1.0}      # запись без окон действует всегда


def test_observed_pose_from_configured_sources_feeds_the_stats_and_pulls_the_shares():
    """Хаб отмечает увиденную позу только из настроенных источников, копилка пишет только увиденную и уверенную позу, данные тянут доли по секундам и переживают JSON."""
    stats = pstats.HabitStats()
    when = datetime.fromtimestamp(EVENING)
    assert stats.observe_pose("person-a", "sofa", "lie", when, 3600, confidence=0.99)
    assert not stats.observe_pose("person-a", "sofa", "sit", when, 3600, confidence=0.5)            # неуверенное не пишется
    shares = ppose.shares(HABITS, stats, "person-a", "sofa", when)
    assert shares["lie"] == pytest.approx(0.5 * 0.45 + 0.5 * 1.0)                                     # час данных - половина веса
    restored = pstats.HabitStats.from_json(stats.to_json())
    assert restored.pose_prior("person-a", "sofa", when).places == {"lie": 1.0}
    assert ppose.shares({}, restored, "person-a", "sofa", when) == {"lie": 1.0}                        # таблицы нет - только копилка
    stats = pstats.HabitStats()
    recorder = pstats.HabitRecorder(stats, lambda x, z: "sofa", None, lambda path, text: None)
    recorder.record([{"person": "person-a", "x": 1.0, "z": 1.0, "confidence": 0.97, "pose": "lie", "pose_seen": "sit"},
                     {"person": "person-b", "x": 1.0, "z": 1.0, "confidence": 0.97, "pose": "lie"}], [], 10.0, EVENING)
    day = datetime.fromtimestamp(EVENING).strftime(pstats.DAY_FORMAT)
    assert stats.poses[day]["person-a"]["22:00"]["sofa"] == {"sit": 10.0} and "person-b" not in stats.poses[day]
    layout = {"walls": [], "objects": [], "rooms": [], "places": [{"label": "Диван", "x": 1.0, "z": 1.0}]}
    seen = {}
    for sources in (("messages",), ("camera",)):                                                      # слова видят позу / позу видит только камера
        hub = ph.PresenceHub(ph.MODE_RSSI, layout, clock=lambda: EVENING, pose_sources=sources)
        hub.set_real(True)
        hub.note_message("told", None, who="person-a", place="диван", pose="lie", now=EVENING)
        [row] = hub.identities(EVENING)
        seen[sources] = row.get("pose_seen")
    assert seen == {("messages",): "lie", ("camera",): None}
