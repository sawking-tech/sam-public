"""Постановка сжатия при записи в историю и его состояние (SAM-97).

Сжатие ставится там, где реплика легла в историю (шина событий после записи в `sessions`), а не в ходе модели
шлюза: при основной модели вне шлюза (claude-code) хода в шлюзе нет, а история растёт. Все пути - голосовой
ход, чат монитора, inject, ответ ведущего - проходят через on_history_write.

Состояние для /health: сколько несжатого в базе, когда последнее сжатие, ждёт ли паузы и сколько, последняя
ошибка. Модуль не знает Mongo и шину: чтение истории, время последнего сжатия и постановка приходят параметрами.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from . import compression
from .env import env_float
from .token_usage import WORKER_SESSION_PREFIX

# Сессии воркеров (task:XXXX) не сжимаются: их история живёт в worker_session (SAM-38), а в sessions лишь
# вызовы инструментов для монитора; до SAM-97 сжатие ставил только ход основной сессии
SKIP_PREFIXES = (WORKER_SESSION_PREFIX,)
# Пауза после прохода сжатия, в которой запись не ставит новый проход, пока несжатое ниже потолка. Без неё
# в потоке реплик каждый проход брал то, что вышло из окна за время прошлого (1-3 сообщения), и платил вызов
# сводки, слияние сводок и автозапись фактов - сжатие шло проход за проходом по свежему хвосту. Минута - в живом
# разговоре (реплики через 5-20 с) это 3-12 реплик, один кусок сводки; потолок прерывает паузу раньше
MIN_GAP_S = env_float("SAM_COMPRESS_MIN_GAP_S", 60.0)


@dataclass
class _State:
    waiting_since: float | None = None
    running_since: float | None = None
    finished_at: float | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None


class CompressionWatch:
    def __init__(self,
                 load_messages: Callable[[str], list[dict]],
                 last_compressed_at: Callable[[str], datetime | None],
                 schedule: Callable[[str, bool], bool],
                 ceiling_msgs: int = compression.CEILING_MSGS,
                 ceiling_tokens: int = compression.CEILING_TOKENS,
                 in_progress: Callable[[str], bool] = compression.in_progress,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = datetime.utcnow,
                 skip_prefixes: tuple[str, ...] = SKIP_PREFIXES,
                 min_gap_s: float = MIN_GAP_S) -> None:
        self._load, self._last_ok, self._schedule = load_messages, last_compressed_at, schedule
        self._ceiling_msgs, self._ceiling_tokens = ceiling_msgs, ceiling_tokens
        self._in_progress, self._clock, self._now, self._skip = in_progress, clock, now, skip_prefixes
        self._min_gap_s = min_gap_s
        self._states: dict[str, _State] = {}

    # ── постановка ───────────────────────────────────────────────────────────

    def on_history_write(self, session_id: str) -> bool:
        """Реплика легла в историю: есть что сжать за окном - поставить; несжатого выше потолка - сразу.
        Сразу после прохода ниже потолка не ставит (MIN_GAP_S): хвост копится до куска. True - сжатие поставлено."""
        if not session_id or session_id.startswith(self._skip):
            return False
        state = self._states.setdefault(session_id, _State())
        if self._in_progress(session_id):
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            print(f"[Compress/watch] {session_id}: запись не из цикла событий, сжатие поставит следующая")
            return False
        messages = self._load(session_id)
        if not compression.needs_compression(messages):
            return False
        urgent = compression.above_ceiling(messages, self._ceiling_msgs, self._ceiling_tokens)
        if not urgent and self._just_finished(state):
            return False
        msgs, tokens = compression.pending_tail(messages)
        print(f"[Compress/watch] {session_id}: несжатых {msgs} ({tokens} ток.), "
              f"{'сразу' if urgent else 'в паузе'}")
        return self._schedule(session_id, urgent)

    def _just_finished(self, state: _State) -> bool:
        since = _since(self._clock(), state.finished_at)
        return since is not None and since < self._min_gap_s

    # ── состояние (compression.CompressionStatus) ────────────────────────────

    def waiting(self, session_id: str) -> None:
        self._states.setdefault(session_id, _State()).waiting_since = self._clock()

    def started(self, session_id: str) -> None:
        state = self._states.setdefault(session_id, _State())
        state.waiting_since, state.running_since = None, self._clock()

    def finished(self, session_id: str, error: str | None) -> None:
        state = self._states.setdefault(session_id, _State())
        state.waiting_since = state.running_since = None
        state.finished_at = self._clock()
        if error:
            state.last_error, state.last_error_at = error, self._now()
        else:
            state.last_error = state.last_error_at = None

    # ── /health ──────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Раздел compression для /health: потолки и сессии, писавшие в историю с запуска шлюза."""
        return {
            "ceiling_msgs": self._ceiling_msgs,
            "ceiling_tokens": self._ceiling_tokens,
            "sessions": {sid: self._session_status(sid, state) for sid, state in self._states.items()},
        }

    def _session_status(self, session_id: str, state: _State) -> dict:
        msgs, tokens = compression.pending_tail(self._load(session_id))
        last_ok = self._last_ok(session_id)
        now, tick = self._now(), self._clock()
        return {
            "pending_msgs": msgs,
            "pending_tokens": tokens,
            "above_ceiling": msgs >= self._ceiling_msgs or tokens >= self._ceiling_tokens,
            "last_ok": last_ok.isoformat() + "Z" if last_ok else None,
            "last_ok_ago_s": round((now - last_ok).total_seconds()) if last_ok else None,
            "waiting_s": _since(tick, state.waiting_since),
            "running_s": _since(tick, state.running_since),
            "last_error": state.last_error,
            "last_error_at": state.last_error_at.isoformat() + "Z" if state.last_error_at else None,
        }


def _since(tick: float, start: float | None) -> int | None:
    return None if start is None else round(tick - start)
