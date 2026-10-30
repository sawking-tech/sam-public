"""Карта предпочтений устройства (SAM-118): где телефон лежит чаще, независимо от уровня сигнала.

Уровень сигнала даёт равновероятные места (кровать и кухня на 5 ГГц отличаются на 0,6 дБ, диван и туалет на 2 дБ), и оценка скачет между ними.
Предпочтение простое, проще человеческих привычек: вес места для устройства; телефон в туалете или ванной лежит куда реже, чем в спальне.
Вес умножается на оценку один раз на выходе (в рекурсию он не входит, иначе росла бы степень). Модуль чистый: веса приходят словарём."""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Callable

from . import presence_habit_stats as hs
from . import presence_habits as ph



class PrefsKey(StrEnum):
    """Служебные ключи файла предпочтений; остальные ключи - части имён устройств с личными таблицами."""
    DEFAULT = "default"      # общая карта: место -> вес
    FIXED = "fixed"          # веса, заданные руками: скрипт расчёта их не трогает
    OUTLETS = "outlets"      # множители мест с розетками
    NOTE = "note"            # пояснение для человека, в расчёте не участвует


DEFAULT_KEY = PrefsKey.DEFAULT
SERVICE_KEYS = (PrefsKey.DEFAULT, PrefsKey.FIXED, PrefsKey.OUTLETS, PrefsKey.NOTE)


def place_key(name: str) -> str:
    """Единый вид названия места для сравнения: план и отпечатки пишут его по-разному (Стол, кухня, Кресло)."""
    return name.strip().lower()
UNKNOWN_WEIGHT = 1.0            # место, которого нет в карте, не поощряется и не штрафуется


def weight(prefs: dict, device_key: str, place: str) -> float:
    """Вес места для устройства: сначала его личная карта (по части ключа устройства), потом общая."""
    low = device_key.lower()
    own = next((table for name, table in prefs.items() if name not in SERVICE_KEYS and name.lower() in low), {})
    return own.get(place, prefs.get(DEFAULT_KEY, {}).get(place, UNKNOWN_WEIGHT))


def apply(posterior: dict[str, float], prefs: dict, device_key: str) -> dict[str, float]:
    """Оценка мест, умноженная на веса предпочтений и пронормированная; нет карты или оценки - как есть."""
    if not prefs or not posterior:
        return dict(posterior)
    scaled = {place: q * weight(prefs, device_key, place) for place, q in posterior.items()}
    total = sum(scaled.values())
    return {place: q / total for place, q in scaled.items()} if total > 0 else dict(posterior)


SURFACE_RADIUS_M = 0.6          # мебель дальше этого от точки места поверхностью для телефона не считается
BARE_FLOOR_FACTOR = 0.4         # места без поверхности (коридор, вход): телефон там лежит только на полу или в руке
TIME_POWER = 0.5                # время людей входит корнем: кухня и стол, где бывают мало, но кладут мимо шли, не должны проигрывать кровати в разы
MIN_WEIGHT = 0.15               # ни одно место не получает вес ниже: телефон мимо шёл и положили


NOT_A_SURFACE = ("унитаз", "ванна")          # в туалете и ванне телефон не лежит: оставить его там некуда, кроме пола


def has_surface(objects: list[dict], x: float, z: float, radius_m: float = SURFACE_RADIUS_M) -> bool:
    """Есть ли у места поверхность, где лежит телефон: мебель плана (стол, кровать, диван, полка, стиралка, кошачья этажерка) в радиусе от точки."""
    for o in objects:
        if o.get("kind") != "furniture" or any(word in str(o.get("title", "")).lower() for word in NOT_A_SURFACE):
            continue
        dx = max(o["x"] - x, 0.0, x - (o["x"] + o["w"]))
        dz = max(o["z"] - z, 0.0, z - (o["z"] + o["d"]))
        if (dx * dx + dz * dz) ** 0.5 <= radius_m:
            return True
    return False


def derive(places_xy: dict[str, tuple[float, float]], objects: list[dict], occupancy: dict[str, float], outlets: dict[str, float] | None = None) -> dict[str, float]:
    """Вес места = поверхность × где любят бывать люди: телефон лежит там, где есть поверхность, и там, куда ходят и где остаются люди (положили мимо шли и забыли).

    occupancy - доля времени людей по месту (не обязательно нормирована); outlets - множитель мест с розетками (телефон там заряжается чаще, спальня и кухня);
    вес нормируется на лучшее место и не ниже MIN_WEIGHT."""
    raw = {place: (1.0 if has_surface(objects, x, z) else BARE_FLOOR_FACTOR) * occupancy.get(place, 0.0) ** TIME_POWER * (outlets or {}).get(place, 1.0) for place, (x, z) in places_xy.items()}
    top = max(raw.values(), default=0.0)
    return {place: round(max(MIN_WEIGHT, w / top), 3) if top > 0 else 1.0 for place, w in raw.items()}


WEEK_START = datetime(2026, 10, 5)            # любой понедельник: неделя целиком
STATS_STRENGTH_S = 7 * 24 * 3600.0            # столько секунд копилки уравнивают её со словами: за неделю наблюдений копилка весит половину


def weekly_occupancy(habits: dict) -> dict[str, float]:
    """Доля времени людей по месту за неделю по словам-привычкам: сумма вероятностей мест по слотам и людям; «нет дома» не считается."""
    step = habits.get("step_min", ph.DEFAULT_STEP_MIN)
    total: dict[str, float] = {}
    for person in habits.get("people", {}):
        for k in range(7 * ph.MINUTES_PER_DAY // step):
            for place, q in ph.place_prior(habits, person, WEEK_START + timedelta(minutes=k * step)).items():
                if place != ph.AWAY:
                    total[place_key(place)] = total.get(place_key(place), 0.0) + q
    return total


def mixed_occupancy(words: dict[str, float], stats_seconds: dict[str, float], strength_s: float = STATS_STRENGTH_S) -> dict[str, float]:
    """Время людей по месту: слова-привычки и копилка наблюдений, оба нормированы на единицу, вес копилки растёт с секундами копилки."""
    def norm(d: dict[str, float]) -> dict[str, float]:
        total = sum(d.values())
        return {place_key(k): v / total for k, v in d.items()} if total > 0 else {}
    seen = sum(stats_seconds.values())
    weight = seen / (seen + strength_s)
    w, st = norm(words), norm(stats_seconds)
    return {place: (1.0 - weight) * w.get(place, 0.0) + weight * st.get(place, 0.0) for place in set(w) | set(st)}


OTHERS_SHARE = 0.3            # доля времени остальных людей: телефон ходит за хозяином, но кладут и другие (мимо шли)


def slot_occupancy(habits: dict, stats: "hs.HabitStats | None", owner: str | None, when: datetime) -> dict[str, float]:
    """Где в этот слот времени бывает хозяин устройства (слова-привычки смешаны с копилкой) и, с малой долей, остальные люди; «нет дома» не считается.

    Пока хозяин спит, это кровать, когда ест, это кухня: время суток входит в вес места."""
    def person(name: str) -> dict[str, float]:
        prior = ph.place_prior(habits, name, when)
        mixed = hs.blend(prior, stats.prior(name, when) if stats else None)
        return {place_key(place): q for place, q in mixed.items() if place != ph.AWAY}
    people = list(habits.get("people", {}))
    others = [p for p in people if p != owner]
    out: dict[str, float] = {}
    if owner in people:
        for place, q in person(owner).items():
            out[place] = out.get(place, 0.0) + q
    for name in others:
        for place, q in person(name).items():
            out[place] = out.get(place, 0.0) + q * OTHERS_SHARE / max(1, len(others))
    return out


SLOT_KEEP = 128                  # сколько слотов времени держим в памяти: сутки на хозяина с запасом


class DevicePrefs:
    """Карта предпочтений устройств на данный момент: вес места = поверхность x где в этот слот бывает хозяин (привычки и копилка) x розетки; заданное руками сверху.

    Зависимости приходят снаружи (привычки, копилка, мебель, места, поиск хозяина): класс не знает хаба, файлов и часов. Таблица считается раз в слот на хозяина."""

    def __init__(self, content: dict, habits: dict, stats: "hs.HabitStats | None", objects: list[dict], places_xy: dict[str, tuple[float, float]],
                 owner_of: Callable[[str], str | None], step_min: int = hs.DEFAULT_STEP_MIN, keep: int = SLOT_KEEP) -> None:
        self._fixed = content.get(PrefsKey.FIXED) or {}
        self._outlets = content.get(PrefsKey.OUTLETS) or {}
        self._personal = {k: v for k, v in content.items() if k not in SERVICE_KEYS}
        self._static = {PrefsKey.DEFAULT: content.get(PrefsKey.DEFAULT) or {}, **self._personal}
        self._habits, self._stats, self._objects, self._places = habits, stats, objects, places_xy
        self._owner_of, self._step, self._keep = owner_of, step_min, keep
        self._slots: dict[tuple[str | None, int], dict] = {}

    def table(self, device_key: str, now: float) -> dict:
        """Таблица весов устройства на момент now; без привычек или мест - заданная файлом."""
        if not self._habits or not self._places:
            return self._static
        owner, when = self._owner_of(device_key), datetime.fromtimestamp(now)
        slot = (owner, when.hour * 60 + when.minute // self._step * self._step)
        if slot not in self._slots:
            occupancy = slot_occupancy(self._habits, self._stats, owner, when)
            derived = derive(self._places, self._objects, occupancy, self._outlets) if occupancy else None
            self._slots[slot] = {PrefsKey.DEFAULT: {**derived, **self._fixed}, **self._personal} if derived else self._static
            if len(self._slots) > self._keep:
                self._slots.pop(next(iter(self._slots)))
        return self._slots[slot]
