"""Фильтр по времени для места устройства (SAM-118): где устройство могло оказаться за прошедшее время, исходя из прошлой оценки и путей по плану.

Уровень сигнала сам называет место с точностью 30-50%, зато из одной комнаты за секунды не попасть в дальнюю. Прошлая оценка размазывается по достижимым
местам и служит априором для следующего отсчёта. Модуль чистый: время и расстояния между местами приходят параметрами."""
from __future__ import annotations

import math
from typing import Callable

WALK_SPEED_MPS = 1.2     # обычный шаг по квартире; дальше этого за секунду не уйти
STAY_PER_S = 0.97        # доля того, что устройство за секунду остаётся на месте: лежащий телефон почти не двигается
SLACK_M = 1.0            # допуск на размер места: оценка места это не точка
FALLOFF_M = 1.5          # на сколько метров дальше досягаемости вес перехода падает в e раз
FLOOR_SHARE = 0.02       # доля равномерного априора: ошибочная прошлая оценка не закрывает остальные места
DECORRELATION_S = 5.0     # столько секунд между отсчётами, чтобы их считать независимыми: секундные отсчёты почти повторяют друг друга (окно медианы то же)
KNOWN_SHARE = 0.9         # вероятность названного человеком места сразу после слов; остальное делится между другими местами устройства
STILL_STAY_PER_S = 0.998  # лежащий телефон (уровень ровный десятки секунд) за секунду остаётся на месте почти наверняка: оценка не расплывается по соседним местам на одинаковом уровне
FORGET_AFTER_S = 120.0   # оценка старше этого забыта: с ней не понять, где устройство теперь

Distance = Callable[[str, str], float]


def _reach_weight(distance_m: float, reach_m: float) -> float:
    """1, пока место в пределах досягаемости; дальше вес падает экспоненциально."""
    return 1.0 if distance_m <= reach_m else math.exp(-(distance_m - reach_m) / FALLOFF_M)


def _move_shares(place: str, places: list[str], distance: Distance, reach_m: float) -> dict[str, float]:
    """Куда уходит устройство из места: доли по остальным местам, сумма единица."""
    weights = {other: _reach_weight(distance(place, other), reach_m) for other in places if other != place}
    total = sum(weights.values())
    return {other: w / total for other, w in weights.items()} if total > 0 else {}


def _mix_floor(probs: dict[str, float]) -> dict[str, float]:
    uniform = FLOOR_SHARE / len(probs)
    return {place: (1.0 - FLOOR_SHARE) * q + uniform for place, q in probs.items()}


def predict(prev: dict[str, float], distance: Distance, dt_s: float, stay_per_s: float = STAY_PER_S) -> dict[str, float]:
    """Априор мест через dt_s секунд: часть остаётся, остальное расходится по достижимым местам; сумма единица."""
    stay = stay_per_s ** max(0.0, dt_s)
    reach_m = WALK_SPEED_MPS * max(0.0, dt_s) + SLACK_M
    places = list(prev)
    out = {place: 0.0 for place in places}
    for place, q in prev.items():
        out[place] += q * stay
        for other, share in _move_shares(place, places, distance, reach_m).items():
            out[other] += q * (1.0 - stay) * share
    return _mix_floor(out)


class PlaceTracker:
    """Оценка места одного устройства во времени: на каждый новый отсчёт прошлая оценка проходит через predict и сводится с уровнем."""

    def __init__(self) -> None:
        self.posterior: dict[str, float] = {}
        self.observed_at = -math.inf

    def set_known(self, place: str, places: list[str], now: float) -> None:
        """Человек назвал место устройства: оценка становится этим местом, дальше она держится по переходам, а уровень отмечает уход."""
        rest = (1.0 - KNOWN_SHARE) / max(1, len(places) - 1)
        self.posterior = {p: KNOWN_SHARE if p == place else rest for p in places}
        self.observed_at = now

    def update(self, observed_at: float, distance: Distance, likelihood: Callable[[dict[str, float] | None, float], dict[str, float]],
               stay_per_s: float = STAY_PER_S) -> dict[str, float]:
        """Новая оценка по отсчёту; тот же отсчёт второй раз не считается, иначе одно наблюдение усиливало бы себя.
        likelihood(prior, power): power = доля независимости отсчёта, пропорциональна времени с прошлого; перемножение секундных отсчётов давало ложные 99%."""
        if observed_at <= self.observed_at:
            return self.posterior
        dt_s = observed_at - self.observed_at
        prior = predict(self.posterior, distance, dt_s, stay_per_s) if self.posterior and dt_s <= FORGET_AFTER_S else None
        power = min(1.0, dt_s / DECORRELATION_S)
        self.posterior, self.observed_at = likelihood(prior, power), observed_at
        return self.posterior
