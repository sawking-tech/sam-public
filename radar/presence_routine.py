"""Автопилот «быт» (SAM-105): сцены домашнего дня по кругу вместо случайных мест, для отладки анимации.

Сцена задаёт каждому место; следующая выбирается случайно по весам из правдоподобных продолжений; по ходу случаются вылазки в туалет и ванную.
Сцены дома и чей компьютер чей - в data/home_routine.json (личное, вне репозитория; путь - SAM_RADAR_ROUTINE_FILE). Встроенные
DEFAULT_SCENES - нейтральный пример того же устройства (люди a, b, гость guest) на случай, когда файла нет. Чистый модуль: места
приходят строками, случайность и время - аргументами.
"""
from __future__ import annotations

import json
import random as _random
from pathlib import Path
from typing import Callable

ROUTINE_FILE_ENV = "SAM_RADAR_ROUTINE_FILE"
DEFAULT_ROUTINE_FILE = Path(__file__).resolve().parents[1] / "data" / "home_routine.json"

AWAY = None                        # None вместо места: человека нет дома
TRIP_PLACES = (("туалет", 0.6), ("ванная", 0.4))
TRIP_DWELL_S = (15.0, 45.0)        # сколько человек отсутствует в сцене по делам: от быстрого визита до долгого сидения (короче самой короткой длинной сцены)
TRIP_GAP_S = (40.0, 90.0)          # пауза между вылазками

# За чьим компьютером кто сидит: за своим - только хозяин, за компьютером второго жильца - он и хозяин первого. Остальные места - любые.
PC_USERS = {"pc-a": {"a"}, "pc-b": {"b", "a"}}

# Сцена: что делает каждый (место), сколько длится (секунд, на экране), что может случиться дальше (сцена, вес). Кого не назвали - остаётся, где был.
DEFAULT_SCENES: dict[str, dict] = {
    # Быт - переходы от сидячего или лежачего места к другому. Стоят только на кухне; вход - место, куда гость идёт при уходе, не стоянка; пришедший появляется на входе сам (ходьба) и идёт к месту.
    "сон": {"dwell_s": [50, 80], "assign": {"a": "кровать", "b": "кровать", "guest": AWAY}, "next": [["утро", 1]]},
    "утро": {"dwell_s": [30, 50], "assign": {"a": "pc-a", "b": "pc-b"}, "next": [["завтрак на кухне", 2], ["работаем", 3], ["готовка", 1], ["гость пришёл", 2]]},
    "завтрак на кухне": {"dwell_s": [40, 70], "assign": {"a": "кухня", "b": "кухня"}, "next": [["работаем", 3], ["гость пришёл", 1]]},
    "вдвоём за одним компьютером": {"dwell_s": [40, 70], "assign": {"a": "pc-b", "b": "pc-b"}, "next": [["работаем", 3], ["готовка", 1]]},
    "готовка": {"dwell_s": [40, 70], "assign": {"b": "газовая плита", "a": "pc-a"}, "next": [["работаем", 3], ["гость пришёл", 1]]},
    "работаем": {"dwell_s": [50, 90], "assign": {"a": "pc-a", "b": "pc-b"}, "next": [["готовка", 2], ["завтрак на кухне", 1], ["вдвоём за одним компьютером", 1], ["гость пришёл", 2], ["сон", 1]]},
    "гость пришёл": {"dwell_s": [20, 35], "assign": {"guest": "диван", "a": "pc-a"}, "next": [["тусим на диване в кабинете", 1]]},
    "тусим на диване в кабинете": {"dwell_s": [50, 90], "assign": {"a": "диван", "b": "диван", "guest": "диван"}, "next": [["втроём у мониторов", 2], ["тусим на кухне", 2], ["втроём на кровати", 1], ["гость уходит", 3]]},
    "тусим на кухне": {"dwell_s": [40, 70], "assign": {"a": "кухня", "b": "кухня", "guest": "кухня"}, "next": [["тусим на диване в кабинете", 2], ["втроём у мониторов", 1], ["гость уходит", 2]]},
    "втроём у мониторов": {"dwell_s": [50, 90], "assign": {"a": "pc-a", "b": "диван", "guest": "диван"}, "next": [["тусим на кухне", 1], ["втроём на кровати", 2], ["гость уходит", 2], ["ночь с гостем", 1]]},
    "втроём на кровати": {"dwell_s": [40, 70], "assign": {"a": "кровать", "b": "кровать", "guest": "кровать"}, "next": [["тусим на диване в кабинете", 2], ["гость уходит", 2], ["ночь с гостем", 2]]},
    "ночь с гостем": {"dwell_s": [50, 80], "assign": {"a": "кровать", "b": "кровать", "guest": "диван"}, "next": [["гость уходит", 1]]},
    "гость уходит": {"dwell_s": [12, 18], "assign": {"guest": "вход"}, "then": {"guest": AWAY}, "next": [["работаем", 3], ["готовка", 1], ["сон", 2]]},
}


def foreign_seats(scenes: dict[str, dict], pc_users: dict[str, set] | None = None) -> list[tuple[str, str, str]]:
    """Сцены, где за компьютером сидит не тот, кому можно: (сцена, человек, место)."""
    users = PC_USERS if pc_users is None else pc_users
    return [(name, person, place) for name, scene in scenes.items() for person, place in scene["assign"].items() if place in users and person not in users[place]]


START_SCENES = ("сон", "утро", "работаем", "готовка", "завтрак на кухне")      # с чего может начаться день: гостя ещё нет


def load_routine(path: str | Path | None) -> tuple[dict[str, dict], dict[str, set]]:
    """(сцены, чей компьютер чей) из файла; нет файла или битый - встроенные. Файл - {"scenes": ..., "pc_users": ...}
    или, как раньше, одни сцены."""
    if path and Path(path).is_file():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return DEFAULT_SCENES, PC_USERS
        if isinstance(data, dict) and "scenes" in data:
            return data["scenes"], {k: set(v) for k, v in (data.get("pc_users") or {}).items()}
        return data, PC_USERS
    return DEFAULT_SCENES, PC_USERS


def load_scenes(path: str | Path | None) -> dict[str, dict]:
    """Сцены из файла или встроенные; битый файл - встроенные."""
    return load_routine(path)[0]


class Routine:
    """Бесконечная смена сцен быта: step(now) возвращает назначения {человек: место или None}, когда что-то меняется, иначе пустой словарь."""

    def __init__(self, scenes: dict[str, dict] | None = None, rng: _random.Random | None = None, busy: Callable[[str], bool] | None = None, random_start: bool = False) -> None:
        self._busy = busy or (lambda person: False)        # идёт ли человек: уходящий пропадает дома только дойдя до входа
        self.scenes = scenes or DEFAULT_SCENES
        self.rng = rng or _random.Random()
        self.scene: str | None = None
        self._random_start = random_start                  # день начинается с любой сцены без гостя, а не всегда со сна
        self.until = 0.0
        self.places: dict[str, str | None] = {}         # где человек по сцене (в вылазку возвращаются сюда)
        self._then: dict[str, str | None] = {}          # что произойдёт в конце сцены («гость уходит» - сцена гостя: сначала у входа, потом нет дома)
        self._then_at = 0.0
        self._trip: tuple[str, float] | None = None     # (кто, до какого момента в вылазке)
        self._next_trip = 0.0

    def step(self, now: float) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        if self._then and now >= self._then_at and not any(self._busy(p) for p in self._then):
            out.update(self._apply(self._then)); self._then = {}
        if self.scene is None or now >= self.until:
            self._trip = None
            out.update(self._enter(self._pick_next(), now))
        elif self._trip and now >= self._trip[1]:
            person = self._trip[0]
            self._trip = None
            self._next_trip = now + self.rng.uniform(*TRIP_GAP_S)
            if self.places.get(person) is not None:
                out[person] = self.places[person]
        elif self._trip is None and now >= self._next_trip:
            person = self._trip_candidate() if now + TRIP_DWELL_S[1] < self.until else None      # вылазка кончается до смены сцены
            if person:
                place = self.rng.choices([p for p, _ in TRIP_PLACES], [w for _, w in TRIP_PLACES])[0]
                self._trip = (person, now + self.rng.uniform(*TRIP_DWELL_S))
                out[person] = place
            else:
                self._next_trip = now + self.rng.uniform(*TRIP_GAP_S)
        return out

    def _pick_next(self) -> str:
        if self.scene is None:
            return self.rng.choice([n for n in START_SCENES if n in self.scenes]) if self._random_start else "сон"
        names, weights = zip(*self.scenes[self.scene]["next"]) if self.scenes[self.scene].get("next") else (list(self.scenes), [1] * len(self.scenes))
        return self.rng.choices(list(names), list(weights))[0]

    def _enter(self, name: str, now: float) -> dict[str, str | None]:
        scene = self.scenes[name]
        self.scene = name
        self.until = now + self.rng.uniform(*scene["dwell_s"])
        self._then = dict(scene.get("then") or {})
        self._then_at = now + (self.until - now) * 0.6        # «уходит»: сначала у входа, ближе к концу сцены его нет дома
        if self._next_trip == 0.0:
            self._next_trip = now + self.rng.uniform(*TRIP_GAP_S)
        return self._apply(scene["assign"])

    def _apply(self, assign: dict[str, str | None]) -> dict[str, str | None]:
        self.places.update(assign)
        return dict(assign)

    def _trip_candidate(self) -> str | None:
        home = [p for p, place in self.places.items() if place is not None and place not in ("кровать",) and p not in self._then]      # спящего и уходящего гостя не гоняют
        return self.rng.choice(home) if home else None
