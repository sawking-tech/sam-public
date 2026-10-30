"""Рассуждение модели по роли вызова и поставщику (SAM-226): SAM - система реального времени.

Уровень задаётся ролью вызова (token_usage): минимум - ход основной модели, сводки сжатия, шаг ask workflow,
судья реплики и ответа; низкий - воркеры и судья вызовов инструментов, им рассуждение даёт качество решений. Способ свести рассуждение к уровню - по
поставщику: LM Studio и DashScope выключают его полем запроса, z.ai не выключает, только уровни effort.

Где рассуждение не выключено, к пределу ответа добавляется запас: иначе рассуждение съедает весь предел и
ответ приходит пустым (15 окт glm-5.3-flash: 41 из 43 сводок voice - сырые куски). Поставщику, который
отключение игнорирует, в системную часть добавляется инструкция отвечать сразу - запасной путь, модель с
обязательным рассуждением всё равно сначала думает.

Модуль без сети: apply только переписывает аргументы вызова, его зовёт llm_client перед каждой попыткой.
"""
from __future__ import annotations

import os

from . import token_usage
from .env import env_int

LEVEL_MIN = "min"
LEVEL_LOW = "low"
LEVELS = (LEVEL_MIN, LEVEL_LOW)

# Уровень по роли; переопределение - SAM_REASONING_<РОЛЬ> (min | low)
ROLE_LEVELS = {
    token_usage.ROLE_MAIN: LEVEL_MIN,
    token_usage.ROLE_SUMMARIZE: LEVEL_MIN,
    token_usage.ROLE_WORKFLOW: LEVEL_MIN,
    token_usage.ROLE_JUDGE: LEVEL_MIN,
    token_usage.ROLE_WORKER: LEVEL_LOW,
    token_usage.ROLE_GUARD: LEVEL_LOW,
}
DEFAULT_LEVEL = LEVEL_MIN   # путь не размечен ролью: реальное время по умолчанию
ROLE_ENV_PREFIX = "SAM_REASONING_"

# Запас к max_tokens, где рассуждение идёт: у glm-5.3 на low рассуждение съедало и 600, и 1024 токена целиком
RESERVE_TOKENS = env_int("SAM_REASONING_RESERVE_TOKENS", 2048)

# Поставщики, которым нужна инструкция в промпте (отключение не работает): список через запятую
PROMPT_SOURCES_ENV = "SAM_REASONING_PROMPT_SOURCES"
DEFAULT_PROMPT_SOURCES = "zai"
INSTRUCTION = ("Рассуждай минимально и отвечай сразу: SAM - система реального времени, "
               "долгое рассуждение задерживает ответ и съедает предел его длины.")

_LMSTUDIO_OFF = {"chat_template_kwargs": {"enable_thinking": False}}
_DASHSCOPE_OFF = {"enable_thinking": False}
# z.ai: рассуждение glm-5.3 не выключается, только reasoning_effort low | high | max (docs.z.ai); effort уровня -
# SAM_REASONING_ZAI_<УРОВЕНЬ>, по умолчанию low для обоих
ZAI_EFFORT_ENV_PREFIX = "SAM_REASONING_ZAI_"
ZAI_DEFAULT_EFFORT = "low"


def level_for(role: str) -> str:
    """Уровень роли с учётом переопределения окружением; неизвестное значение - уровень по умолчанию роли."""
    default = ROLE_LEVELS.get(role, DEFAULT_LEVEL)
    raw = os.getenv(ROLE_ENV_PREFIX + role.upper(), "").strip().lower()
    return raw if raw in LEVELS else default


def provider_params(source: str, level: str) -> tuple[dict, bool]:
    """Поля extra_body поставщика для уровня и признак «рассуждение выключено»."""
    if source == "lmstudio":
        return (dict(_LMSTUDIO_OFF), True) if level == LEVEL_MIN else ({}, False)
    if source == "dashscope":
        return (dict(_DASHSCOPE_OFF), True) if level == LEVEL_MIN else ({}, False)
    if source == "zai":
        effort = os.getenv(ZAI_EFFORT_ENV_PREFIX + level.upper(), "").strip().lower() or ZAI_DEFAULT_EFFORT
        return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}, False
    return {}, False


def prompt_sources() -> frozenset[str]:
    raw = os.getenv(PROMPT_SOURCES_ENV, DEFAULT_PROMPT_SOURCES)
    return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())


def with_instruction(messages: list[dict]) -> list[dict]:
    """Инструкция в конец системного сообщения (начало промпта не меняется - кэш цел); его нет - новое первым."""
    if messages and messages[0].get("role") == "system" and isinstance(messages[0].get("content"), str):
        first = messages[0]
        return [{**first, "content": f"{first['content']}\n\n{INSTRUCTION}"}, *messages[1:]]
    return [{"role": "system", "content": INSTRUCTION}, *messages]


def apply(llm_config: dict, role: str, kwargs: dict, reserve: int = RESERVE_TOKENS) -> dict:
    """Аргументы вызова с рассуждением по роли: extra_body поставщика, запас к max_tokens, инструкция обхода.

    Поля политики перекрывают одноимённые у вызывающего: уровень решается здесь, а не в каждом вызове."""
    source = llm_config.get("source", "")
    level = level_for(role)
    params, off = provider_params(source, level)
    out = dict(kwargs)
    if params:
        out["extra_body"] = {**(out.get("extra_body") or {}), **params}
    if not off and isinstance(out.get("max_tokens"), int):
        out["max_tokens"] += reserve
    if level == LEVEL_MIN and source in prompt_sources() and out.get("messages"):
        out["messages"] = with_instruction(out["messages"])
    return out
