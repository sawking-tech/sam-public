"""Поза на опоре выбирается вероятностно по человеку, месту и времени суток (SAM-120).

Доли поз задаёт таблица `poses` в файле привычек: у человека записи {place, poses: {поза: доля}, when: [окна как у дел]}; запись без окон действует всегда.
Таблица смешивается с копилкой привычек так же, как места: чем больше секунд наблюдённых поз в слоте, тем больше вес копилки. Выбранная поза держится
выдержку и меняется сразу при смене места. В копилку поза попадает только из свидетельства источника, который позу видит (список в конфиге):
своя выборка туда не пишется, иначе таблица кормила бы сама себя. Модуль чистый: данные, копилка и случайность приходят снаружи."""
from __future__ import annotations

import random
from datetime import datetime
from typing import Mapping

from . import presence_habit_stats as pstats
from . import presence_habits as ph

TABLE_KEY = "poses"
HOLD_KEY = "pose_hold_s"
DEFAULT_HOLD_S = 120.0          # поза держится не меньше двух минут: человек не ложится и не садится каждые несколько секунд
POSE_SOURCES_ENV = "SAM_PRESENCE_POSE_SOURCES"
DEFAULT_POSE_SOURCES: tuple[str, ...] = ()   # каналы, чья поза наблюдена, а не взята из места; пока таких нет, копилка поз пуста


def table_shares(habits: Mapping, person: str, place: str, when: datetime) -> dict[str, float]:
    """Доли поз из таблицы для человека на месте в этот момент, сумма единица; первая подходящая запись, нет записи - пусто."""
    for entry in (habits.get(TABLE_KEY) or {}).get(person) or []:
        active = not entry.get("when") or ph.any_window_active(entry["when"], when)
        if not active or str(entry.get("place", "")).lower() != place:
            continue
        total = sum(entry.get("poses", {}).values())
        if total > 0:
            return {pose: share / total for pose, share in entry["poses"].items()}
    return {}


def shares(habits: Mapping, stats: pstats.HabitStats | None, person: str, place: str, when: datetime) -> dict[str, float]:
    """Таблица со смесью копилки: копилка весит по своим секундам, таблицы нет - только копилка, нет ничего - пусто."""
    table = table_shares(habits, person, place, when)
    seen = stats.pose_prior(person, place, when) if stats is not None else None
    if not table:
        return dict(seen.places) if seen else {}
    return pstats.blend(table, seen)


def choose(shares_: Mapping[str, float], rng: random.Random) -> str:
    """Поза выборкой по долям; порядок поз по имени, чтобы один сид давал одну последовательность."""
    mark, acc = rng.random() * sum(shares_.values()), 0.0
    ordered = sorted(shares_.items())
    for pose, share in ordered:
        acc += share
        if mark < acc:
            return pose
    return ordered[-1][0]


class PoseSampler:
    """Поза человека на месте: выборка по долям, держится выдержку, на новом месте выбирается заново; без долей - None, решает место."""

    def __init__(self, habits: Mapping, stats: pstats.HabitStats | None, rng: random.Random, hold_s: float | None = None) -> None:
        self._habits, self._stats, self._rng = habits, stats, rng
        self._hold_s = float(hold_s if hold_s is not None else habits.get(HOLD_KEY, DEFAULT_HOLD_S))
        self._held: dict[str, tuple[str, str, float]] = {}       # человек -> (место, поза, с какого момента)

    def pose(self, person: str, place: str, now: float) -> str | None:
        held = self._held.get(person)
        if held and held[0] == place and now - held[2] < self._hold_s:
            return held[1]
        shares_ = shares(self._habits, self._stats, person, place, datetime.fromtimestamp(now))
        if not shares_:
            self._held.pop(person, None)
            return None
        pose = choose(shares_, self._rng)
        self._held[person] = (place, pose, now)
        return pose
