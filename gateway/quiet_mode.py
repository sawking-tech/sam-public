"""Тихий режим на сессию: SAM слушает, но не зовёт модель и не говорит, пока её не позовут.

Одна правда на шлюз и голосовой слой: файл `quiet.json` в шине голоса (папка lead.alive и voice-in.jsonl), ключ - сессия
(`voice` у моста, имя устройства у монитора). Тишина другой сессии не трогается: в соседней комнате SAM отвечает.

Где действует - только там, где тратятся токены; колонки не глушатся:
- мост (voice/layer/bridge.py) после распознавания: фраза тихой сессии без триггера выхода в шлюз не уходит вовсе -
  ни хода, ни токенов; в voice-in.jsonl она уже лежит, просмотр памяти её видит; триггер снимает тишину и мост говорит EXIT_SAY;
- голосовой ход шлюза (gateway/voice_turn.py) - страховка: в тишине модель не зовётся, «помолчи» включает тишину
  и звучит ENTER_SAY, обращение по имени снимает (gateway/addressee.py);
- шаг workflow с репликой модели для тихой сессии пропускается до вызова модели;
- инструмент SAM `quiet` (gateway/tools/quiet.py) включает на срок или до отмены, выключает и показывает.
Подтверждения входа и выхода - готовые фразы без модели: признак, что режим сработал.

Решение (тихо ли, снимает ли фраза тишину) - чистые функции; чтение и запись файла - класс QuietBook.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

from . import addressee

QUIET_FILE = "quiet.json"
EVENT = "quiet"
BY_PHRASE, BY_NAME, BY_TOOL, BY_EXPIRY = "фраза", "имя", "инструмент", "срок"
ENTER_SAY = os.getenv("SAM_QUIET_ENTER_SAY", "Молчу.")     # готовая фраза входа в тишину, без модели
EXIT_SAY = os.getenv("SAM_QUIET_EXIT_SAY", "Слушаю.")      # готовая фраза выхода из тишины, без модели


def quiet_until(state: dict, session: str) -> float | None:
    """До какого времени тихо (epoch); 0 - до отмены; None - не тихо."""
    entry = state.get(session)
    if not entry:
        return None
    return float(entry.get("until") or 0.0)


def is_quiet(state: dict, session: str, now: float) -> bool:
    until = quiet_until(state, session)
    return until is not None and (until == 0.0 or now < until)


def lifts_quiet(text: str, names: tuple[str, ...] = addressee.NAMES) -> bool:
    """Снимает ли фраза тишину: обращение к SAM по имени или разрешение говорить (addressee.SPEAK_PHRASES)."""
    return addressee.is_vocative(text, names) or addressee.asks_speak(text)


def passes(state: dict, session: str, text: str, now: float) -> tuple[bool, bool]:
    """Решение моста по фразе: (отправить ли ходом, снять ли тишину). Не тихо - отправить; тихо - только с триггером выхода."""
    if not is_quiet(state, session, now):
        return True, False
    lift = lifts_quiet(text)
    return lift, lift


class QuietBook:
    """Файл тишины по сессиям. path None - только в памяти (тесты, шлюз без шины). Сбой файла - строка в stdout, тишины нет."""

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time,
                 emit: Callable[[str, dict], None] | None = None) -> None:
        self._path, self._clock, self._emit = path, clock, emit
        self._memory: dict = {}

    def _load(self) -> dict:
        if self._path is None:
            return dict(self._memory)
        try:
            return json.loads(self._path.read_text(encoding="utf-8")) if self._path.exists() else {}
        except (OSError, ValueError) as e:
            print(f"[Quiet] {self._path} не прочитан ({e}); считаю, что тихих сессий нет")
            return {}

    def _save(self, state: dict) -> None:
        if self._path is None:
            self._memory = dict(state)
            return
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            print(f"[Quiet] {self._path} не записан ({e}); проверь папку шины голоса")

    def is_quiet(self, session: str) -> bool:
        """Тихо ли сессии; истёкший срок снимается и пишется событием."""
        state, now = self._load(), self._clock()
        if is_quiet(state, session, now):
            return True
        if session in state:
            self._drop(state, session, BY_EXPIRY)
        return False

    def on(self, session: str, minutes: float | None = None, by: str = BY_TOOL) -> float:
        """Тишина сессии на minutes минут или до отмены (None); возвращает until (0 - до отмены)."""
        state, now = self._load(), self._clock()
        until = 0.0 if not minutes or minutes <= 0 else now + minutes * 60.0
        state[session] = {"until": until, "since": now, "by": by}
        self._save(state)
        self._tell(session, True, until, by)
        return until

    def off(self, session: str, by: str = BY_TOOL) -> bool:
        state = self._load()
        if session not in state:
            return False
        self._drop(state, session, by)
        return True

    def gate(self, session: str, text: str) -> tuple[bool, bool]:
        """Решение моста по распознанной фразе: (отправить ли ходом, сняла ли она тишину). Триггер выхода снимает тишину здесь."""
        if not self.is_quiet(session):
            return True, False
        if not lifts_quiet(text):
            return False, False
        self.off(session, BY_NAME)
        return True, True

    def status(self, session: str) -> dict:
        state = self._load()
        quiet = is_quiet(state, session, self._clock())
        return {"session": session, "quiet": quiet, "until": quiet_until(state, session) if quiet else None}

    def active(self) -> list[dict]:
        """Тихие сессии сейчас - для /health и монитора."""
        state, now = self._load(), self._clock()
        return [{"session": s, "until": quiet_until(state, s)} for s in state if is_quiet(state, s, now)]

    def _drop(self, state: dict, session: str, by: str) -> None:
        state.pop(session, None)
        self._save(state)
        self._tell(session, False, None, by)

    def _tell(self, session: str, quiet: bool, until: float | None, by: str) -> None:
        if self._emit is not None:
            self._emit(EVENT, {"session": session, "quiet": quiet, "until": until, "by": by})
