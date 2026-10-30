"""Автопилот маршрутов радара для отладки анимации (SAM-105): раз в период каждому человеку назначается новое место в случайной комбинации.


Чистый модуль: места и случайность приходят аргументами, хаб лишь применяет назначения как ручные вероятности.
"""
from __future__ import annotations

import random as _random

SCENARIOS = ("spread", "gather", "pair", "room")        # врозь, все в одно место, двое вместе, все в одну комнату
SCENARIO_WEIGHTS = (3, 3, 2, 2)
DEFAULT_PERIOD_S = 12.0                                  # хватает на рывок, паузу и несколько шагов


def plan(people: list[str], places: list[str], rooms: list[str], rng: _random.Random | None = None) -> dict[str, str]:
    """Новое место каждому: человек -> ключ места (из списка `places`, комнаты и мебель вместе)."""
    rng = rng or _random.Random()
    if not people or not places:
        return {}
    scenario = rng.choices(SCENARIOS, SCENARIO_WEIGHTS)[0]
    if scenario == "gather":
        spot = rng.choice(places)
        return {p: spot for p in people}
    if scenario == "room" and rooms:
        room = rng.choice(rooms)
        return {p: room for p in people}
    if scenario == "pair" and len(people) >= 2:
        pair = rng.sample(people, 2)
        shared = rng.choice(places)
        return {p: (shared if p in pair else rng.choice(places)) for p in people}
    return {p: rng.choice(places) for p in people}
