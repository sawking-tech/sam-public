"""Ворота быстрого голоса (ADR-020, п. 11; ADR-014): workflow fast-voice решает, отвечает ли на фразу быстрая модель.

Голосовой ход в точке выбора модели спрашивает workflow (`on.consult: fast_voice`); его ворота - функция этого модуля.
Быстрая не отвечает, пока сессия холодная (первые COLD_TURNS голосовых ходов после старта шлюза: у неё ещё нет
контекста разговора) и пока идёт откат по поправкам основной (gateway/fast_backoff.py). Тогда ворота пусты - noop, и
отвечает основная. Модуль без глобалов шлюза: счётчик и откат приходят параметрами.
"""
from __future__ import annotations

from typing import Callable

from .env import env_int

POINT = "fast_voice"   # точка решения в голосовом ходе (on.consult в workflows/fast-voice.yml)
SESSION_KEY = "session"
# Холодных ходов: первые два после старта отвечает основная, быстрая говорит с третьего, уже зная разговор
COLD_TURNS = env_int("SAM_FAST_COLD_TURNS", 2)
READY = "быстрая отвечает: сессия прогрета, отката нет"


class ColdStart:
    """Голосовые ходы сессии с холодного старта (с запуска шлюза); в памяти процесса, рестарт - снова холодно."""

    def __init__(self) -> None:
        self._turns: dict[str, int] = {}

    def turns(self, session: str) -> int:
        return self._turns.get(session, 0)

    def note(self, session: str) -> None:
        self._turns[session] = self.turns(session) + 1


def fast_voice_ready(session: str, cold: ColdStart, backoff_remaining: Callable[[str], float],
                     cold_turns: int = COLD_TURNS, rebuilding: Callable[[str], bool] = lambda s: False) -> str:
    """Вывод ворот: непусто - быстрая отвечает; пусто - noop, отвечает основная. rebuilding - основная перестраивает
    линию после смены темы (ADR-027, «Когда отвечает основная модель»)."""
    if cold.turns(session) < cold_turns:
        return ""
    if backoff_remaining(session) > 0:
        return ""
    if rebuilding(session):
        return ""
    return READY
