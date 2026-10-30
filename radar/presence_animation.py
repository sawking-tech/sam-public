"""Параметры анимации роя, которые подбирают ползунками страницы (SAM-113): значения живут на сервере, чтобы переживать обновление страницы и другой браузер.

Чистый модуль: разбор присланного словаря и список изменений для журнала. Файл и журнал - в хабе.
Имена и границы ползунков задаёт flight.js; сервер хранит только числа под безопасными именами.
"""
from __future__ import annotations

import math
import re

KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,23}$")      # delayEdge, durMax: имя ползунка из flight.js
MAX_KEYS = 16
VALUE_MAX = 10.0                                          # любой ползунок анимации - секунды или метры, не больше десяти


def parse(data: object) -> dict[str, float]:
    """Присланное -> {имя: число}; не словарь, плохое имя, не число или вне 0..VALUE_MAX - ValueError с причиной одной строкой."""
    if not isinstance(data, dict) or not data or len(data) > MAX_KEYS:
        raise ValueError(f"ждали от 1 до {MAX_KEYS} параметров анимации словарём")
    out = {}
    for name, raw in data.items():
        if not isinstance(name, str) or not KEY_RE.match(name):
            raise ValueError(f"плохое имя параметра анимации: {name!r}")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or not 0.0 <= raw <= VALUE_MAX:
            raise ValueError(f"{name}: ждали число от 0 до {VALUE_MAX:g}, пришло {raw!r}")
        out[name] = float(raw)
    return out


def changes(old: dict[str, float], new: dict[str, float]) -> list[str]:
    """Строки журнала по реально изменившимся значениям: «delayEdge 0.08 -> 0.1», у нового параметра «delayEdge (нет) -> 0.1»."""
    return [f"{k} {old[k]:g} -> {v:g}" if k in old else f"{k} (нет) -> {v:g}" for k, v in new.items() if old.get(k) != v]
