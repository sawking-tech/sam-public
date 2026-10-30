"""Кто такая SAM и в каком роде говорит (SAM-78).

SAM - цифровая сущность, живёт в компьютерах своего дома; женский голос - она, мужской - он. Род речи - настройка голоса `voice_gender` (self_config), у голоса
из реестра синтеза он может быть записан полем gender.

Промпт пишется один раз с метками `{женская форма|мужская форма}`; render собирает его под род. Два
отдельных промпта разошлись бы при первой правке.
"""
from __future__ import annotations

import re

FEMALE, MALE = "female", "male"
GENDERS = (FEMALE, MALE)
DEFAULT_GENDER = FEMALE      # голос SAM по умолчанию женский (SAM-33: «живая подруга»)
_MARKER = re.compile(r"\{([^{}|\n]+)\|([^{}|\n]+)\}")


def render(template: str, gender: str) -> str:
    if gender not in GENDERS:
        raise ValueError(f"voice_gender {gender!r}: нужно {' или '.join(GENDERS)}")
    index = 0 if gender == FEMALE else 1
    return _MARKER.sub(lambda m: m.group(index + 1), template)


def strip_markers(template: str) -> str:
    """Текст без меток: что остаётся жёстким в любом роде."""
    return _MARKER.sub("", template)


def gender_of_voice(registry: dict, voice: str) -> str | None:
    """Род голоса из реестра синтеза (поле gender), None - не записан."""
    voices = registry.get("voices")
    entry = voices.get(voice) if isinstance(voices, dict) else None   # реестр правят руками
    gender = entry.get("gender") if isinstance(entry, dict) else None
    return gender if gender in GENDERS else None
