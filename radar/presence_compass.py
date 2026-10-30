"""Компас плана (SAM-118): стороны света относительно роутера, чтобы место называть словами («северо-западнее роутера на три метра»).

План в метрах: x, z. Куда смотрит север, берётся из плана (`compass.north` - вектор в осях плана); по умолчанию север - вдоль +z, восток - против +x
(вход и кухня у стены с большим z смотрят строго на север). Чистый модуль: разбор слов и арифметика, ничего не читает."""
from __future__ import annotations

import math

DEFAULT_NORTH = (0.0, 1.0)
DIRECTIONS = {"север": 0.0, "северо-восток": 45.0, "восток": 90.0, "юго-восток": 135.0, "юг": 180.0, "юго-запад": 225.0, "запад": 270.0, "северо-запад": 315.0}
STEM_ENDS = ("а", "у", "е", "ом", "ы", "нее")      # «севера», «югу», «востоке», «западнее»: окончания отбрасываются при разборе
NEARLY_ZERO = 1e-9


def _axes(compass: dict | None) -> tuple[tuple[float, float], tuple[float, float]]:
    nx, nz = (compass or {}).get("north") or DEFAULT_NORTH
    length = math.hypot(nx, nz)
    if length < NEARLY_ZERO:
        raise ValueError("компас: north нулевой вектор, ждём направление севера в осях плана")
    nx, nz = nx / length, nz / length
    return (nx, nz), (-nz, nx)


def bearing(dx: float, dz: float, compass: dict | None = None) -> float:
    """Азимут вектора в градусах от севера по часовой (восток 90), диапазон 0..360."""
    north, east = _axes(compass)
    return math.degrees(math.atan2(dx * east[0] + dz * east[1], dx * north[0] + dz * north[1])) % 360.0


def offset(bearing_deg: float, distance_m: float, compass: dict | None = None) -> tuple[float, float]:
    """Смещение в осях плана на distance_m по азимуту bearing_deg."""
    north, east = _axes(compass)
    c, s = math.cos(math.radians(bearing_deg)), math.sin(math.radians(bearing_deg))
    return distance_m * (c * north[0] + s * east[0]), distance_m * (c * north[1] + s * east[1])


def parse_direction(text: str) -> float | None:
    """Азимут по слову стороны света («северо-запад», «на юге»); не нашли - None."""
    for word in text.lower().replace("ё", "е").split():
        word = word.strip(".,;:!?")
        for name, degrees in DIRECTIONS.items():
            if word == name or any(word == name + end for end in STEM_ENDS):
                return degrees
    return None
