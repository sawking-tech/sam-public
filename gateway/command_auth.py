"""Привилегированные команды (SAM-88): что требует секрета и был ли секрет в текущем ходе.

Опознание говорящего - не аутентификация, поэтому привилегированное действие (права, удаление данных, конфиги,
делегирование кода) выполняется только в ходе, где человек назвал секрет `SAM_COMMAND_SECRET`. Вход хода (голос,
чат монитора) кладёт назначения названных ключей в secret_scope (SAM-104); гард читает через secret_in_turn("command"),
слои между ними признак не тащат. Ключ другого назначения привилегированных команд не открывает.
Какой инструмент привилегирован - здесь; для mcp_call решает гард основной моделью (gateway/guard.py).
"""
from __future__ import annotations

import contextvars
import os
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Iterator

from .secret_mask import COMMAND_PURPOSE

# Без секрета хода не выполняются никогда: меняют окружение, настройки, память, душу или запускают код
PRIVILEGED_TOOLS = frozenset(
    t.strip() for t in os.getenv("SAM_PRIVILEGED_TOOLS", "set_env,tune_self,forget,forget_topic,claude_delegate,switch_main_model,soul_edit,soul_rollback").split(",")
    if t.strip())
# mcp_call то безобиден (скриншот), то удаляет файлы: решает гард по аргументам
JUDGED_TOOLS = frozenset({"mcp_call"})
# Привилегированные только при правке ядра SAM - репозитория вне папок периферии (SAM-194, gateway/core_paths.py)
CORE_SCOPED_TOOLS = frozenset({"claude_delegate"})
NEED_SECRET = "[GUARD] Нужен ключ: назови его вместе с командой."
# Агенту ключ не назвать: он идёт вне хода человека. Просьба о ключе вела к выдуманному ключу (8a70441b, 15 окт) -
# отказ однозначный, с тем, что делать вместо (SAM-212, SAM-216)
AGENT_REFUSED = ("[GUARD] Правка ядра агенту без права запрещена: подготовь разбор, план и diff и верни создателю; "
                 "право применить даётся после проверки плана, или правку сделает SAM в ходе человека.")

_secrets_in_turn: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar("secrets_in_turn", default=frozenset())


def _purposes(named: bool | Iterable[str] | None) -> frozenset[str]:
    """Назначения названных ключей; True - прежний признак одного ключа команд (SAM-88)."""
    if named is True:
        return frozenset({COMMAND_PURPOSE})
    return frozenset(named) if named and not isinstance(named, (bool, str)) else frozenset()


@contextmanager
def secret_scope(named: bool | Iterable[str] | None) -> Iterator[None]:
    """Ход, в котором названы ключи с этими назначениями (пусто - никакие); вложенные вызовы видят признак."""
    token = _secrets_in_turn.set(_purposes(named))
    try:
        yield
    finally:
        _secrets_in_turn.reset(token)


def secret_in_turn(purpose: str = COMMAND_PURPOSE) -> bool:
    """Назван ли в этом ходе ключ с таким назначением; привилегированные команды - ключ command."""
    return purpose in _secrets_in_turn.get()
