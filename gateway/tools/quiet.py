"""quiet - тихий режим сессии текущего хода (gateway/quiet_mode.py): включить на срок или до отмены, выключить, показать.

Тишина экономит токены: фразы без обращения по имени не идут модели, реплики workflow пропускаются. Колонки не глушатся:
вход и выход подтверждаются готовой фразой без модели.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from .. import quiet_mode

ON, OFF, STATUS = "on", "off", "status"


def _until_text(until: float | None) -> str:
    if not until:
        return "до отмены"
    return "до " + datetime.fromtimestamp(until).strftime("%H:%M")


def quiet_impl(params: dict, session: str, book: quiet_mode.QuietBook,
               say: Callable[[str, str], bool] | None = None) -> str:
    """action on|off|status; minutes - срок тишины (нет или 0 - до отмены). Действует на сессию текущего хода."""
    action = str(params.get("action") or STATUS).strip().lower()
    if action == ON:
        try:
            minutes = float(params.get("minutes") or 0)
        except (TypeError, ValueError):
            return f"minutes - число минут, получено {params.get('minutes')!r}"
        until = book.on(session, minutes, quiet_mode.BY_TOOL)
        if say is not None:
            say(session, quiet_mode.ENTER_SAY)
        return f"Тихий режим в сессии {session} {_until_text(until)}: отвечаю только на обращение по имени. Подтверждение уже сказано, ответь <silent></silent>."
    if action == OFF:
        if not book.off(session, quiet_mode.BY_TOOL):
            return f"В сессии {session} тихого режима не было."
        if say is not None:
            say(session, quiet_mode.EXIT_SAY)
        return f"Тихий режим в сессии {session} снят. Подтверждение уже сказано."
    if action == STATUS:
        st = book.status(session)
        return f"Сессия {session}: {'тихо ' + _until_text(st['until']) if st['quiet'] else 'не тихо'}."
    return f"action - {ON}, {OFF} или {STATUS}, получено {action!r}"
