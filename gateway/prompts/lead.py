"""Промпт SAM для Claude Code в роли основной модели (SAM-95): те же разделы, что у шлюза, без пересказа.

Бриф сессии (.claude/hooks/lead_brief.py) печатает его вместо своей версии правил: правка характера в душе доходит
до любой основной модели. Разделы про tools шлюза, речь ответа и spawn_agent (NO_LIES, ход
голоса, контент, имена задач) сюда не входят: сессия говорит командой say и действует своими инструментами, это
описывает роль в брифе; к тому же бриф не должен расти больше чем на треть (замер 10 окт: 4979 знаков до, ~6900 после).
Сборка читает снимок души при каждом вызове; прежние имена модуля отдаёт __getattr__.
"""
from __future__ import annotations

from .identity import render
from .personal import personalize
from .sections import section
from .system import join

# COMMAND_KEY - ключ команд (10 окт 05:00): система безопасности узнаёт его, как сидящего за терминалом
LEAD_ORDER = ("IDENTITY", "CHARACTER", "TONE", "HEARING", "TIME_RULES", "COMMAND_KEY",
              "ORAL_SPEECH")


def lead_prompt_template() -> str:
    return join(LEAD_ORDER)


def lead_prompt(gender: str) -> str:
    """Промпт в роде голоса SAM с личным слоем (имена, личные инструкции).
    Неизвестный род - ValueError из render."""
    return personalize(render(lead_prompt_template(), gender))


def __getattr__(name: str):
    if name == "LEAD_PROMPT_TEMPLATE":
        return lead_prompt_template()
    if name == "COMMAND_KEY":
        return section(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
