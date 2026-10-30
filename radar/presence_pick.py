"""Выбор положения выборкой (SAM-118): место устройства берётся по облаку выборок из распределения, а не вершиной и не одной случайной точкой.

Одна выборка раз в несколько секунд давала шум (метка прыгала между 0,57 и 0,18): берётся DRAWS выборок, считается, сколько частиц в каждом месте, и выбирается самое населённое;
при равных почти частицах остаётся прежнее место. Вероятностная природа остаётся: при распределении 0,5 и 0,5 выбор идёт то в одно, то в другое, а вершина всегда была бы одним и тем же.
Липкость: выбранное место держится не меньше MIN_HOLD_S и только потом может смениться, а если его вероятность упала ниже KEEP_MIN_P - сменяется сразу,
иначе метка залипла бы на месте, которое оценка уже отвергла. Модуль чистый: случайность и время приходят параметрами."""
from __future__ import annotations

import random
from dataclasses import dataclass

MIN_HOLD_S = 3.0           # выбранное место держится не меньше: пять кадров в секунду не должны давать мерцание между равновероятными
KEEP_MIN_P = 0.05          # вероятность выбранного места упала ниже - выбор меняется сразу
DRAWS = 200                # выборок на один выбор: по умолчанию как облако положения в presence_cloud
SWITCH_RATIO = 2.0         # на другое место переходим, только если оно вероятнее выбранного не меньше чем во столько раз: между 0,57 и 0,18 метка не прыгает


@dataclass
class Pick:
    place: str
    at: float             # когда выбрано


class StickySampler:
    def __init__(self, rng: random.Random | None = None, min_hold_s: float = MIN_HOLD_S, keep_min_p: float = KEEP_MIN_P, switch_ratio: float = SWITCH_RATIO,
                 draws: int = DRAWS) -> None:
        self._rng, self._hold, self._keep, self._ratio, self._draws = rng or random.Random(), min_hold_s, keep_min_p, switch_ratio, draws
        self._picks: dict[str, Pick] = {}

    def pick(self, key: str, posterior: dict[str, float], now: float) -> str | None:
        """Место по вероятностям posterior (сумма около единицы); пусто - None."""
        if not posterior:
            self._picks.pop(key, None)
            return None
        held = self._picks.get(key)
        if held and held.place in posterior and posterior[held.place] >= self._keep and now - held.at < self._hold:
            return held.place
        places, weights = zip(*posterior.items())
        draws = self._rng.choices(places, weights=weights, k=self._draws)
        counts = {place: draws.count(place) for place in set(draws)}
        chosen = max(counts, key=lambda place: (counts[place], place == (held.place if held else None)))                      # самое населённое, при равенстве прежнее
        keep = held and held.place in posterior and posterior[held.place] >= self._keep
        if keep and chosen != held.place and posterior[chosen] < self._ratio * posterior[held.place]:
            self._picks[key] = Pick(held.place, now)                  # выдержка прошла, но новое место не намного вероятнее: остаёмся и отсчитываем выдержку заново
            return held.place
        self._picks[key] = Pick(chosen, now if not held or chosen != held.place else held.at)
        return chosen
