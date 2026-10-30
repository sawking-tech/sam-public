"""Привычное поле (SAM-118): где человек бывает в это время суток и день недели, из распорядка дел.

У человека набор дел. У дела окна времени, места с весами и доля времени, которую он занимает в окне (туалет 12 раз по 3 минуты за 16 часов - около 4%).
Дела с долей занимают своё; остаток времени окна отдаётся базовым делам (работа, диван, сон) по их весам. Место `AWAY` - человека нет дома.
Модуль чистый: данные приходят словарём (файл читает вызывающий), время параметром. Личный файл привычек лежит в data/ вне git."""
from __future__ import annotations

from datetime import datetime

import numpy as np

AWAY = "__away__"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_GROUPS = {"all": WEEKDAYS, "weekdays": WEEKDAYS[:5], "weekend": WEEKDAYS[5:]}
MINUTES_PER_DAY = 1440
DEFAULT_STEP_MIN = 15          # шаг карты по времени
DEFAULT_FLOOR_SHARE = 0.12     # ровный фон: необычное поведение не должно обнуляться
DEFAULT_SIGMA_M = 0.8          # размер пятна места на полу


def _days(spec) -> set[str]:
    if isinstance(spec, str):
        return set(DAY_GROUPS.get(spec, (spec,)))
    return set(spec)


def _minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def _in_window(window: dict, weekday: str, minute: int) -> bool:
    """Окно действует в этот день недели и минуту; окно через полночь считается днём начала."""
    start, end = _minutes(window["from"]), _minutes(window["to"])
    days = _days(window.get("days", "all"))
    if start <= end:
        return weekday in days and start <= minute < end
    if minute >= start:
        return weekday in days
    previous = WEEKDAYS[(WEEKDAYS.index(weekday) - 1) % 7]
    return minute < end and previous in days


def any_window_active(windows: list[dict], when: datetime) -> bool:
    """Хоть одно окно {days, from, to} действует в этот момент; окна через полночь считаются днём начала."""
    return any(_in_window(w, WEEKDAYS[when.weekday()], when.hour * 60 + when.minute) for w in windows)


def _slot_minute(when: datetime, step_min: int) -> int:
    return (when.hour * 60 + when.minute) // step_min * step_min


def _normalised(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    return {place: w / total for place, w in weights.items()} if total > 0 else {}


def _active(activities: list[dict], weekday: str, minute: int) -> list[dict]:
    return [a for a in activities if any(_in_window(w, weekday, minute) for w in a["when"])]


def place_prior(habits: dict, person: str, when: datetime) -> dict[str, float]:
    """Вероятность мест (и AWAY) для человека в шаге времени, сумма не больше единицы: незаданный остаток уходит в ровный фон при смешивании."""
    activities = habits.get("people", {}).get(person, [])
    step = habits.get("step_min", DEFAULT_STEP_MIN)
    weekday, minute = WEEKDAYS[when.weekday()], _slot_minute(when, step)
    active = _active(activities, weekday, minute)
    fixed = [a for a in active if not a.get("base")]
    base = [a for a in active if a.get("base")]
    scale = min(1.0, 1.0 / max(sum(a["share"] for a in fixed), 1e-9))
    out: dict[str, float] = {}
    for a in fixed:
        for place, q in _normalised(a["places"]).items():
            out[place] = out.get(place, 0.0) + a["share"] * scale * q
    remainder = max(0.0, 1.0 - sum(a["share"] for a in fixed) * scale)
    for a, weight in zip(base, _normalised({i: b.get("weight", 1.0) for i, b in enumerate(base)}).values()):
        for place, q in _normalised(a["places"]).items():
            out[place] = out.get(place, 0.0) + remainder * weight * q
    return out


def away_probability(prior: dict[str, float]) -> float:
    """Вероятность, что человека нет дома: доля AWAY среди заданного."""
    total = sum(prior.values())
    return prior.get(AWAY, 0.0) / total if total > 0 else 0.0


def cell_field(prior: dict[str, float], cells_xy: np.ndarray, place_xy: dict[str, tuple[float, float]], sigma_m: float = DEFAULT_SIGMA_M,
               floor_share: float = DEFAULT_FLOOR_SHARE, zones: dict[str, np.ndarray] | None = None) -> np.ndarray:
    """Привычное поле по клеткам пола: доля каждого известного места ложится на его зону (маска клеток из zones: подошва мебели или комната сверху) поровну по клеткам,
    а без зоны - пятном нормального вида вокруг точки места; плюс ровный фон; сумма единица. Нет привычки - ровное поле."""
    uniform = np.full(len(cells_xy), 1.0 / len(cells_xy))
    known = {p: q for p, q in prior.items() if p != AWAY and p in place_xy}
    total = sum(known.values())
    if total <= 0:
        return uniform
    field = np.zeros(len(cells_xy))
    for place, q in known.items():
        zone = None if zones is None else zones.get(place)
        if zone is not None and zone.any():
            field += (q / total) * zone / zone.sum()
            continue
        x, z = place_xy[place]
        blob = np.exp(-0.5 * ((cells_xy[:, 0] - x) ** 2 + (cells_xy[:, 1] - z) ** 2) / sigma_m ** 2)
        field += (q / total) * blob / max(blob.sum(), 1e-12)
    return (1.0 - floor_share) * field + floor_share * uniform
