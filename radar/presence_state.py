"""Состояние радара между перезапусками (SAM-118): переключатели панели, последние сообщения и кто был на карте.

Без него перезапуск сбрасывал автопилот и реальное определение, забывал слова «где кто» и встречал всех у входа как только что пришедших.
Слова слабеют по сроку полураспада (`TOLD_HALF_LIFE_S`, `TOLD_REST_HALF_LIFE_S`), поэтому сообщения старше `MAX_MESSAGE_AGE_S` при чтении отбрасываются. Модуль чистый: файл пишет и читает хаб."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

MAX_MESSAGE_AGE_S = 6 * 3600.0     # старше слова уже не свидетельство: вес слов упал в разы за несколько периодов полураспада
MAX_HOME_AGE_S = 12 * 3600.0       # на карте был давно - после такого перерыва человек мог уйти и вернуться, встреча у входа допустима


@dataclass
class SavedState:
    saved_at: float = 0.0
    real: bool = False                                               # реальное определение включено
    autopilot: dict = field(default_factory=dict)                    # on, period_s, mode
    messages: list[dict] = field(default_factory=list)               # поля pmsg.Message
    home: list[str] = field(default_factory=list)                    # кто был на карте при последней записи


def to_json(state: SavedState) -> str:
    return json.dumps(asdict(state), ensure_ascii=False, indent=1)


def from_json(text: str) -> SavedState:
    """Читает файл; чужие или лишние поля пропускаются, чтобы старый файл не ронял радар после правки формата."""
    raw = json.loads(text)
    known = SavedState.__dataclass_fields__
    return SavedState(**{k: v for k, v in raw.items() if k in known})


def fresh_messages(messages: list[dict], now: float, max_age_s: float = MAX_MESSAGE_AGE_S) -> list[dict]:
    """Сообщения не старше max_age_s и не из будущего: часы шли назад, такой записи верить нельзя."""
    return [m for m in messages if 0.0 <= now - m.get("t", -1e18) <= max_age_s]


def home_after_restart(state: SavedState, now: float, max_age_s: float = MAX_HOME_AGE_S) -> list[str]:
    """Кто считается уже бывшим дома: записанные на карте, если запись не старше max_age_s."""
    return list(state.home) if 0.0 <= now - state.saved_at <= max_age_s else []


def snapshot(now: float, real: bool, autopilot: dict, messages: list, home: list[str]) -> SavedState:
    """Состояние для записи: сообщения приходят объектами с полями Message, в файл идут словарями."""
    return SavedState(now, real, autopilot, [asdict(m) for m in messages], home)


class StateStore:
    """Файл состояния: чтение с допуском на испорченный файл и запись; путь и запись приходят снаружи."""

    def __init__(self, path: Path | None, write: Callable[[Path | None, str], None]) -> None:
        self._path, self._write = path, write

    def load(self) -> SavedState | None:
        """Сохранённое состояние; нет файла или он испорчен - None (радар начинает с чистого, причина одной строкой)."""
        if not self._path or not self._path.exists():
            return None
        try:
            return from_json(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as e:
            print(f"[Presence] состояние не прочитано из {self._path}: {e}; начинаю с чистого")
            return None

    def save(self, state: SavedState) -> None:
        self._write(self._path, to_json(state))
