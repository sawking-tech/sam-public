"""Кто живёт дома: люди и питомцы установки с ролью и краткой характеристикой.

Состав у каждого дома свой, поэтому в промптах и коде нет слотов под конкретных людей: записи лежат в каталоге
(gateway/catalog.py, типы person и pet), их заполняют анкета монитора (gateway/household_api.py) и сама SAM инструментом
`set_household_member`. Блок для промпта собирается здесь. Анкета необязательна: дом пуст - SAM спрашивает один раз.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from . import catalog
from .env import env_float

PERSON, PET = "person", "pet"
KINDS = (PERSON, PET)
MAX_NAME, MAX_ROLE, MAX_ABOUT = 60, 60, 300   # запись идёт в каждый промпт: поля короткие, абзац о человеке - в память
CACHE_S = env_float("SAM_HOUSEHOLD_CACHE_S", 60.0)   # промпт собирается на каждый ход: чаще Mongo не читается

SAVE_RULE = ("Каждого человека и питомца заноси отдельным вызовом set_household_member; сказать «записала» можно только "
             "после вызова.")
EMPTY_BLOCK = ("ДОМ: пока никто не записан. В первом разговоре один раз спроси, кто здесь живёт. " + SAVE_RULE +
               " Отказались - запомни отказ и больше не спрашивай.")
HEADER = "ДОМ (кто здесь живёт). " + SAVE_RULE
LOG_TAG = "[Household]"


class HouseholdError(ValueError):
    """Запись не принята: текст - что поправить."""


def _line(value, limit: int, field: str) -> str:
    """Поле одной строкой: запись идёт в системный промпт, переносы строк отрезали бы её от блока."""
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        raise HouseholdError(f"{field}: не больше {limit} знаков")
    return text


def clean(data: dict) -> dict:
    """Проверенная запись; поля, которых нет в data, не трогаются: SAM дописывает то, что узнала."""
    kind = data.get("kind") or PERSON
    if kind not in KINDS:
        raise HouseholdError(f"kind: {' или '.join(KINDS)}")
    name = _line(data.get("name"), MAX_NAME, "имя")
    if not name:
        raise HouseholdError("имя: нужно")
    member = {"kind": kind, "name": name}
    for field, limit in (("role", MAX_ROLE), ("about", MAX_ABOUT)):
        if field in data:
            member[field] = _line(data[field], limit, field)
    return member


def _public(doc: dict) -> dict:
    return {"kind": doc.get("type"), "key": doc.get("name"), "name": doc.get("display_name") or doc.get("name"),
            "role": doc.get("role", ""), "about": doc.get("about", "")}


def members(db) -> list[dict]:
    """Сначала люди, потом питомцы, внутри - по имени."""
    found = [_public(d) for kind in KINDS for d in catalog.list_all(db, kind)]
    return sorted(found, key=lambda m: (KINDS.index(m["kind"]), m["name"].lower()))


def save(db, data: dict) -> dict:
    member = clean(data)
    fields = {k: member[k] for k in ("role", "about") if k in member}
    return _public(catalog.upsert(db, member["kind"], member["name"], {"display_name": member["name"], **fields}))


def remove(db, kind: str, name: str) -> bool:
    return catalog.delete(db, kind, name)


def prompt_block(found: list[dict]) -> str:
    if not found:
        return EMPTY_BLOCK
    lines = []
    for m in found:
        title = m["name"] if m["kind"] == PERSON else f"{m['name']} (питомец)"
        role = f" - {m['role']}" if m["role"] else ""
        about = f": {m['about']}" if m["about"] else ""
        lines.append(f"- {title}{role}{about}")
    return HEADER + "\n" + "\n".join(lines)


class CachedBlock:
    """Блок дома для сборки промпта: чтение раз в CACHE_S, при сбое Mongo остаётся последний удачный."""

    def __init__(self, read: Callable[[], list[dict]], ttl: float = CACHE_S,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print):
        self._read, self._ttl, self._clock, self._log = read, ttl, clock, log
        self._text, self._at = "", None

    def invalidate(self) -> None:
        self._at = None

    def __call__(self) -> str:
        now = self._clock()
        if self._at is None or now - self._at >= self._ttl:
            self._at = now
            try:
                self._text = prompt_block(self._read())
            except Exception as exc:   # драйвер Mongo бросает много разных: ход без блока дома лучше упавшего хода
                self._log(f"{LOG_TAG} состав дома не прочитан ({exc}); промпт с прошлым составом, проверь Mongo")
        return self._text
