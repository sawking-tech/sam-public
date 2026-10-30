"""Временные слоты проходок (SAM-118): когда по привычкам людей место или цепочка мест правдоподобны.

Проходки записаны в один произвольный час, а приор зависит от времени суток. Модуль для каждой стоянки находит окна недели, где место вероятно
у человека, и для цепочек стоянок - время старта, при котором все места подряд правдоподобны; по этим слотам проходку прогоняют как будто в тот час.
Источник вероятностей - `presence_habits.place_prior`, подмена его не требует правки модуля. Модуль чистый: данные параметрами, файлы читает вызывающий."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, Protocol

from radar import presence_habits as ph

REFERENCE_MONDAY = datetime(2026, 10, 5)     # любая неделя: понедельник даёт порядок дней
MATCH_RADIUS_M = 1.5                         # стоянка в этом радиусе от места привычек - это место
MIN_PRIOR = 0.08                             # слабее этого вероятность места уже ровный фон привычного поля
PEAK_SHARE = 0.4                             # окно держит слоты не слабее этой доли пика места у человека
TIMING_TOLERANCE_SLOTS = 1                   # время места в цепочке может сдвинуться на столько шагов привычек: подъём и туалет идут встык на границе окон
BEST_STARTS = 3


class PriorSource(Protocol):
    def __call__(self, habits: dict, person: str, when: datetime) -> dict[str, float]: ...


@dataclass(frozen=True)
class Window:
    days: tuple[str, ...]
    start: str                    # ЧЧ:ММ
    end: str
    prior: float                  # пик вероятности места в окне


@dataclass(frozen=True)
class Start:
    days: tuple[str, ...]
    clock: str
    score: float                  # среднее геометрическое вероятностей мест цепочки


@dataclass(frozen=True)
class LegClass:
    activity: str                 # дело из привычек, ради которого человек идёт в место назначения
    share: float                  # доля времени дела в его окне, 0 у базовых дел
    when: tuple[str, ...]         # окна дела как в файле привычек: «дни ЧЧ:ММ-ЧЧ:ММ»


@dataclass(frozen=True)
class Room:
    x: float
    z: float
    w: float
    d: float

    def contains(self, xy: tuple[float, float]) -> bool:
        return self.x <= xy[0] <= self.x + self.w and self.z <= xy[1] <= self.z + self.d


class HabitPlaceMatcher:
    """Сопоставляет точку проходки месту привычек: ближайшее в радиусе, иначе единственное место привычек в той же комнате."""

    def __init__(self, place_xy: dict[str, tuple[float, float]], rooms: Iterable[Room] = (), radius_m: float = MATCH_RADIUS_M) -> None:
        self._places, self._rooms, self._radius = place_xy, tuple(rooms), radius_m

    def match(self, xy: tuple[float, float]) -> str | None:
        near = min(self._places, key=lambda p: math.dist(self._places[p], xy), default=None)
        if near is not None and math.dist(self._places[near], xy) <= self._radius:
            return near
        room = next((r for r in self._rooms if r.contains(xy)), None)
        inside = [p for p, pxy in self._places.items() if room is not None and room.contains(pxy)]
        return inside[0] if len(inside) == 1 else None


def week_slots(step_min: int) -> list[tuple[str, datetime]]:
    """Все слоты недели: день и момент."""
    return [(ph.WEEKDAYS[d], REFERENCE_MONDAY + timedelta(days=d, minutes=m)) for d in range(7) for m in range(0, ph.MINUTES_PER_DAY, step_min)]


def _clock(when: datetime) -> str:
    return when.strftime("%H:%M")


def place_windows(habits: dict, person: str, place: str, prior_source: PriorSource = ph.place_prior,
                  min_prior: float = MIN_PRIOR, peak_share: float = PEAK_SHARE) -> list[Window]:
    """Окна недели, где место вероятно у человека; одинаковые окна разных дней сливаются в одно."""
    step = habits.get("step_min", ph.DEFAULT_STEP_MIN)
    values = [(day, when, prior_source(habits, person, when).get(place, 0.0)) for day, when in week_slots(step)]
    peak = max((v for _, _, v in values), default=0.0)
    threshold = max(min_prior, peak_share * peak)
    per_day: dict[str, list[tuple[str, str, float]]] = {}
    for day in ph.WEEKDAYS:
        run: list[tuple[datetime, float]] = []
        for d, when, v in values + [(None, None, 0.0)]:
            if d == day and v >= threshold:
                run.append((when, v))
            elif run:
                per_day.setdefault(day, []).append((_clock(run[0][0]), _clock(run[-1][0] + timedelta(minutes=step)), max(x for _, x in run)))
                run = []
    merged: dict[tuple[str, str, float], list[str]] = {}
    for day, found in per_day.items():
        for start, end, top in found:
            merged.setdefault((start, end, round(top, 3)), []).append(day)
    return sorted((Window(tuple(days), s, e, top) for (s, e, top), days in merged.items()), key=lambda w: (-w.prior, w.start))


def _tolerant_prior(habits: dict, person: str, place: str, when: datetime, step_min: int, prior_source: PriorSource, slots: int) -> float:
    """Вероятность места с допуском по времени: максимум по соседним шагам."""
    return max(prior_source(habits, person, when + timedelta(minutes=k * step_min)).get(place, 0.0) for k in range(-slots, slots + 1))


def chain_starts(habits: dict, person: str, chain: list[tuple[str, int]], prior_source: PriorSource = ph.place_prior,
                 min_prior: float = MIN_PRIOR, tolerance_slots: int = TIMING_TOLERANCE_SLOTS, top: int = BEST_STARTS) -> list[Start]:
    """Время старта цепочки (место, секунды от старта): каждое место должно быть правдоподобно в своё время, иначе старт отбрасывается.

    Среднее геометрическое вероятностей мест - оценка старта; лучший старт каждого дня, одинаковые дни сливаются. Место, невозможное в это время
    (туалет, пока человек спит), не сглаживается: цепочка с ним не проходит."""
    step = habits.get("step_min", ph.DEFAULT_STEP_MIN)
    best: dict[str, tuple[float, datetime]] = {}
    for day, start in week_slots(step):
        values = [_tolerant_prior(habits, person, place, start + timedelta(seconds=offset), step, prior_source, tolerance_slots) for place, offset in chain]
        if min(values) < min_prior:
            continue
        score = math.exp(sum(math.log(v) for v in values) / len(values))
        if day not in best or score > best[day][0]:
            best[day] = (score, start)
    merged: dict[tuple[str, float], list[str]] = {}
    for day, (score, start) in best.items():
        merged.setdefault((_clock(start), round(score, 3)), []).append(day)
    ranked = sorted((Start(tuple(days), clock, score) for (clock, score), days in merged.items()), key=lambda s: -s.score)
    return ranked[:top]


def subchains(stands: list[dict], min_len: int = 2) -> list[list[dict]]:
    """Все цепочки подряд идущих стоянок длиной от min_len."""
    return [stands[i:j] for i in range(len(stands)) for j in range(i + min_len, len(stands) + 1)]


def classify_leg(habits: dict, person: str, destination: str) -> list[LegClass]:
    """Классы перехода: дела человека, в местах которых есть место назначения; нет таких дел - переход этому человеку не свойственен."""
    found = []
    for activity in habits.get("people", {}).get(person, []):
        if destination in activity["places"]:
            when = tuple(f"{w.get('days', 'all')} {w['from']}-{w['to']}" for w in activity["when"])
            found.append(LegClass(activity["id"], 0.0 if activity.get("base") else float(activity["share"]), when))
    return sorted(found, key=lambda c: -c.share)


def label_walk(habits: dict, stands: list[dict], matcher: HabitPlaceMatcher, places_xy: dict[str, tuple[float, float]],
               persons: Iterable[str], legs: list[dict] = (), chain_min_len: int = 2) -> dict:
    """Слоты проходки: окна каждой стоянки, классы каждого перехода и лучшие старты каждой цепочки подряд, по каждому человеку."""
    habit_place = {s["place"]: matcher.match(places_xy[s["place"]]) for s in stands}
    out: dict = {"matched": habit_place, "persons": {}}
    for person in persons:
        stand_windows = {p: place_windows(habits, person, hp) for p, hp in habit_place.items() if hp}
        chains = []
        for part in subchains(stands, chain_min_len):
            if any(habit_place[s["place"]] is None for s in part):
                continue
            offsets = [(habit_place[s["place"]], s["t0"] - part[0]["t0"]) for s in part]
            chains.append({"from": part[0]["place"], "to": part[-1]["place"], "stands": len(part), "starts": chain_starts(habits, person, offsets)})
        classes = [{"from": leg["from"], "to": leg["to"], "classes": classify_leg(habits, person, habit_place[leg["to"]]) if habit_place.get(leg["to"]) else []}
                   for leg in legs]
        out["persons"][person] = {"stands": stand_windows, "legs": classes, "chains": chains}
    return out
