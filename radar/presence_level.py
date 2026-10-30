"""Место устройства по уровню сигнала (SAM-118): из вероятностей по отпечаткам и мест плана с координатами получается список точек для свидетельств.

Чистый модуль: вероятности и места приходят параметрами, роутер и файлы сюда не заходят."""
from __future__ import annotations

TOP_PLACES = 3        # мест на одно устройство: по отпечаткам верное место в тройке лучших в 65-90% случаев, в первом только в 30-50%
MIN_PLACE_P = 0.15    # место с меньшей вероятностью по уровню в свидетельство не идёт
COVERAGE_MIN = 0.5    # доля вероятности, которая должна приходиться на места с координатами в плане; меньше - ответа нет

PLACE_LABEL, PLACE_X, PLACE_Z = "label", "x", "z"   # поля места в плане

Spot = tuple[float, float, float, str]   # x, z, вероятность, подпись места


def _coords_by_label(places: list[dict]) -> dict[str, dict]:
    return {(p.get(PLACE_LABEL) or "").lower(): p for p in places}


def _coverage(probs: dict[str, float], coords: dict[str, dict]) -> float:
    """Доля вероятности, пришедшаяся на места, у которых в плане есть координаты."""
    return sum(q for label, q in probs.items() if label in coords)


def _placed(probs: dict[str, float], coords: dict[str, dict], min_p: float) -> list[Spot]:
    return [(coords[label][PLACE_X], coords[label][PLACE_Z], q, label) for label, q in probs.items() if label in coords and q >= min_p]


def _normalised(spots: list[Spot]) -> list[Spot]:
    """Вероятности оставшихся мест пересчитаны на сумму единицу."""
    total = sum(s[2] for s in spots)
    return [(x, z, q / total, label) for x, z, q, label in spots] if total > 0 else []


def spots(probs: dict[str, float], places: list[dict], top: int = TOP_PLACES, min_p: float = MIN_PLACE_P,
          coverage_min: float = COVERAGE_MIN) -> list[Spot]:
    """Лучшие места устройства по уровню; пусто, если большая часть вероятности ушла на места без координат: делить остаток на другие места было бы ложью."""
    coords = _coords_by_label(places)
    if _coverage(probs, coords) < coverage_min:
        return []
    best = sorted(_placed(probs, coords, min_p), key=lambda s: -s[2])[:top]
    return _normalised(best)
