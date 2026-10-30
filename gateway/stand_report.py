"""Отчёты надзора стенда (SAM-195): отбивка scripts/stand_watch.py и смена состояния компонентов.

Надзор - отдельный процесс: шлёт POST REPORT_PATH с состоянием компонентов раз в отбивку и сразу, когда оно
поменялось. Смену считает шлюз - от состояний последней принятой отбивки, а не надзор: повтор отчёта, на который шлюз
не успел ответить, смены не даёт, а отчёт, не дошедший до шлюза, её не теряет. Смена уходит в шину событием EVENT: на
него подписан workflow stand-health, основная модель узнаёт, что упало или поднялось. При старте шлюза последняя
отбивка поднимается из файла, поэтому смена самого шлюза и его сжатия за время простоя событием не становится.

Время последней отбивки - в памяти процесса и в файле BEAT_FILE_ENV. Файл, а не Mongo: он переживает рестарт шлюза,
и после подъёма возраст отбивки настоящий, а не «ещё не было»; его читают процесс ворот-команды и Cortex без HTTP и
без Mongo. Сами ворота молчания идут планировщиком workflow, а его аренда и дедуп ворот живут в Mongo: при лежащей
Mongo тревога молчания не сработает. Запись - раз в отбивку, полкилобайта.

Решение (что в событии) отделено от шины и файла: emit и save приходят параметрами.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .core_paths import REPO
from .restart import GATEWAY as RESTART_GATEWAY, restart_name

EVENT = "stand_change"            # тип события шины: на него подписан workflows/stand-health.yml
REPORT_PATH = "/sam/stand/watch"  # ручка шлюза; её же зовёт надзор
UP = "up"                         # состояние живого компонента; остальное (down, x2, stalled) - поломка
GATEWAY = "gateway"               # компонент «/health шлюза ответил»; без ответа раздела compression нет
COMPRESS_PREFIX = "compress:"     # компонент сжатия истории сессии: compress:<сессия>
BEAT_FILE_ENV = "SAM_STAND_WATCH_BEAT_FILE"
DEFAULT_BEAT_FILE = REPO / "data" / "stand_watch_beat.json"
CHANGE_SEPARATOR = "; "
LIST_SEPARATOR = ","


@dataclass(frozen=True)
class Change:
    component: str
    before: str | None   # None - компонент в отчёте впервые
    now: str


@dataclass(frozen=True)
class Beat:
    at: float            # время отбивки, секунды эпохи
    states: dict[str, str]


def change_line(change: Change) -> str:
    """«bridge up -> down»; впервые увиденный - «bridge down»."""
    if change.before is None:
        return f"{change.component} {change.now}"
    return f"{change.component} {change.before} -> {change.now}"


def transitions(before: dict[str, str], now: dict[str, str]) -> list[Change]:
    """Смены от прошлой отбивки. Новый компонент - смена, только если он не up; пропавший из отчёта (сессия сжатия
    ушла из /health шлюза) - подъём, если был не up и /health шлюза ответил: без ответа пропажа ничего не значит."""
    moved = [Change(k, before.get(k), v) for k, v in now.items() if before.get(k) != v
             and not (k not in before and v == UP)]
    if now.get(GATEWAY) != UP:
        return moved
    return moved + [Change(k, b, UP) for k, b in before.items() if k not in now and b != UP]


def change_event(changes: list[Change]) -> dict | None:
    """Поля события смены: строки смен, что упало (не up), что поднялось и что перезапускать (имена
    restart_component); смен нет - None. Пустые поля не пишутся. Шлюз в restart не попадает: раз отчёт дошёл, он жив,
    а дубль шлюза (gateway x2) его выход не уберёт."""
    if not changes:
        return None
    down = [c.component for c in changes if c.now != UP]
    restart = dict.fromkeys(n for c in down if (n := restart_name(c)) and n != RESTART_GATEWAY)
    fields = {
        "changes": CHANGE_SEPARATOR.join(change_line(c) for c in changes),
        "down": LIST_SEPARATOR.join(down),
        "up": LIST_SEPARATOR.join(c.component for c in changes if c.now == UP),
        "restart": LIST_SEPARATOR.join(restart),
    }
    return {k: v for k, v in fields.items() if v}


class StandReports:
    """Приём отчёта: смена от прошлой отбивки - событием в шину, отбивку запомнить и сохранить.
    last - прошлая отбивка: при старте шлюза её подаёт корень композиции из файла."""

    def __init__(self, emit: Callable[[str, dict], object], save: Callable[[Beat], None],
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = print,
                 last: Beat | None = None) -> None:
        self._emit, self._save, self._clock, self._log = emit, save, clock, log
        self.last = last

    def accept(self, states: dict[str, str]) -> dict | None:
        """Поля ушедшего события или None. Файл не записался - строка в лог, событие всё равно уходит."""
        changes = transitions(self.last.states if self.last else {}, states)
        self.last = Beat(self._clock(), dict(states))
        try:
            self._save(self.last)
        except OSError as e:
            self._log(f"[Стенд] отбивка надзора не записана в файл: {e}; ворота молчания надзора увидят старую")
        event = change_event(changes)
        if event:
            self._emit(EVENT, event)
        return event

    def status(self) -> dict:
        """Для /health: время последней отбивки и её возраст в секундах; отбивок не было - None."""
        if self.last is None:
            return {"last_beat": None, "age_s": None}
        return {"last_beat": _iso(self.last.at), "age_s": round(self._clock() - self.last.at)}


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")


def beat_file() -> Path:
    return Path(os.getenv(BEAT_FILE_ENV) or DEFAULT_BEAT_FILE)


def save_beat(path: Path, beat: Beat) -> None:
    """Целиком или никак: временный файл и замена, читатель не увидит половину."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"at": beat.at, "ts": _iso(beat.at), "states": beat.states}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_beat(path: Path) -> Beat | None:
    """Последняя отбивка из файла; файла нет или он битый - None (надзор не отбивался)."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return Beat(float(doc["at"]), dict(doc.get("states") or {}))
    except (OSError, ValueError, KeyError, TypeError):
        return None
