"""Основная модель SAM (SAM-91): шлюзу всё равно, кто она - он берёт выбранную и отдаёт ей ход.

Ответчиков несколько, порядок - приоритет из конфига (`SAM_MAIN_MODELS`, первый доступный отвечает); выбор
делается на каждый ход, поэтому основная модель меняется в реальном времени: пропал Claude Code - следующий ход
идёт в LLM, вернулся - обратно. Шлюз знает только контракт: имя, доступность и где ответ рождается - внутри шлюза
(вызов LLM в голосовом ходе) или снаружи (сессия Claude Code отвечает сама; no-op не отвечает никто).

Модуль чистый: проверки доступности передаются фабрикам из корня композиции (gateway/agent.py).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

ENV = "SAM_MAIN_MODELS"
DEFAULT_ORDER = "claude-code,llm"   # высший приоритет у Claude Code, дальше модель из роутера
LLM, CLAUDE_CODE, NOOP = "llm", "claude-code", "noop"


@dataclass(frozen=True)
class ConcreteModel:
    """Конкретная модель за абстракцией основной (две абстракции: реализация скрыта внутри конкретной
    и у каждой своя). in_gateway - ответ даёт вызов LLM в ходе шлюза; False - шлюз модель не зовёт."""
    name: str
    available: Callable[[], bool]
    in_gateway: bool
    # Доставка входа не из голоса (чат монитора, inject) модели, которая отвечает вне шлюза; у llm и noop - пусто
    deliver: Callable[[dict], None] = lambda message: None
    # Команды, которые модель сама сообщает монитору (SAM-99): [{"name", "description"}]; нет своих - пусто
    commands: Callable[[], list[dict]] = lambda: []
    reads_inbox: bool = False   # читает входящие (вопросы и итоги агентов): ждать её ответа есть смысл


def llm(available: Callable[[], bool] = lambda: True) -> ConcreteModel:
    return ConcreteModel(LLM, available, in_gateway=True)


def claude_code(available: Callable[[], bool], deliver: Callable[[dict], None],
                commands: Callable[[], list[dict]] = lambda: []) -> ConcreteModel:
    return ConcreteModel(CLAUDE_CODE, available, in_gateway=False, deliver=deliver, commands=commands, reads_inbox=True)


# Команды Claude Code для монитора: её личные скиллы, которыми она решила поделиться, - имена
# через запятую; пусто - своих команд у неё в мониторе нет
CLAUDE_CODE_COMMANDS_ENV = "SAM_CLAUDE_CODE_COMMANDS"


def commands_from_env(env: str = CLAUDE_CODE_COMMANDS_ENV) -> list[dict]:
    return [{"name": n.strip(), "description": ""} for n in os.getenv(env, "").split(",") if n.strip()]


def noop() -> ConcreteModel:
    return ConcreteModel(NOOP, lambda: True, in_gateway=False)


def parse_order(raw: str | None = None) -> list[str]:
    return [n.strip() for n in (raw if raw is not None else os.getenv(ENV, DEFAULT_ORDER)).split(",") if n.strip()]


class MainModel:
    """Основная модель: конкретные модели по приоритету с инертностью. Текущая остаётся, пока
    доступна: менять без причины незачем. Причина - текущая недоступна (в основном ошибка): берётся первая доступная
    по приоритету. Никто не доступен - текущая (ход не теряется молча). Сменилась - on_switch(было, стало)."""

    def __init__(self, models: dict[str, ConcreteModel], order: list[str],
                 on_switch: Callable[[str, str], None] = lambda was, now: None) -> None:
        unknown = [n for n in order if n not in models]
        if unknown:
            raise ValueError(f"{ENV}: нет модели {', '.join(unknown)}; есть: {', '.join(models)}")
        if not order:
            raise ValueError(f"{ENV} пуст: укажите хотя бы одну модель ({', '.join(models)})")
        self._models = models
        self._order = [models[n] for n in order]
        self._current = self._order[0]
        self._on_switch = on_switch

    @property
    def current(self) -> ConcreteModel:
        """Кто ведёт сейчас - без проверки доступности (ярлык в мониторе, /health)."""
        return self._current

    @property
    def order(self) -> list[str]:
        return [m.name for m in self._order]

    def prefer(self, name: str) -> str:
        """Сделать name текущей и первой по приоритету (ручное переключение, SAM-176); пусто - сделано, иначе почему нет."""
        model = self._models.get(name)   # любая зарегистрированная, даже если её не было в порядке из .env
        if model is None:
            return f"основной модели {name!r} нет; есть: {', '.join(self._models)}"
        if not model.available():
            return f"{name} сейчас недоступна: осталась {self._current.name}"
        self._order = [model] + [m for m in self._order if m is not model]
        if model is not self._current:
            self._on_switch(self._current.name, model.name)
            self._current = model
        return ""

    def pick(self) -> ConcreteModel:
        if self._current.available():
            return self._current
        nxt = next((m for m in self._order if m.available()), self._current)
        if nxt is not self._current:
            self._on_switch(self._current.name, nxt.name)
            self._current = nxt
        return self._current
