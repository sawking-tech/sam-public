"""Класс задачи агента: вид работы от создателя и оценка в шагах (SAM-258).

Слова текста задачу не классифицируют: угадывание по словам (рефактор, коммит, .py) отправляло любую задачу кода на
самую дорогую модель (28 окт). Вид работы называет создатель (KINDS), шаги - его оценка; оценки нет - пул агента по
умолчанию. Задачу разбивает и решает, как делать, исполнитель: здесь только то, что нужно выбору модели и статистике.
"""
from __future__ import annotations

from dataclasses import dataclass

from .env import env_int

# Виды работы - общечеловеческие, по сути дела, а не по предметной области (код, текст, железо - пометка рядом)
KINDS = ("проектировать", "исследовать", "исправить", "создать", "решить", "проверить", "узнать", "сделать")
NO_KIND = ""   # создатель вид не назвал: статистика копится общей строкой модели
# Где идёт работа: внутри экосистемы SAM (её код, документы, память, настройки, стенд, изолированные приложения в ней) или снаружи (дом,
# чужие репозитории, интернет, люди). Снаружи за результат отвечает человек: увидит ошибку - попросит поправить, годится
# самая дешёвая модель. Внутри SAM тратит все ресурсы на свою целостность: ошибку там она гарантированно чинит сама,
# агентами, деньгами и лимитами, и дешёвая модель обходится дороже. Поэтому там модель не слабее INSIDE_MIN_TIER
INSIDE, OUTSIDE = "inside", "outside"
ENVS = (INSIDE, OUTSIDE)

DEFAULT_STEPS = env_int("SAM_AGENT_STEPS", 30)   # оценки нет - пул агента по умолчанию (agent_budget)
# Токены одного шага агента: контекст оборота уходит заново каждый шаг, поэтому вход большой. Замер 28 окт по
# _token_usage: glm-5.3-flash 4,3 млн входа и 0,14 млн выхода за 479 вызовов - около 9000 и 300 на вызов
STEP_IN_TOKENS = env_int("SAM_STEP_IN_TOKENS", 9000)
STEP_OUT_TOKENS = env_int("SAM_STEP_OUT_TOKENS", 300)


@dataclass(frozen=True)
class TaskClass:
    kind: str
    steps: int
    env: str           # inside | outside | "" - создатель не назвал
    why: str           # откуда оценка: идёт в причину выбора модели

    @property
    def in_tokens(self) -> int:
        return self.steps * STEP_IN_TOKENS

    @property
    def out_tokens(self) -> int:
        return self.steps * STEP_OUT_TOKENS

    @property
    def step_tokens(self) -> int:
        """Окно модели держит один оборот, не всю задачу."""
        return STEP_IN_TOKENS + STEP_OUT_TOKENS


def normal_kind(kind: str | None) -> str:
    """Вид работы из списка KINDS; другое или пусто - NO_KIND."""
    value = (kind or "").strip().lower()
    return value if value in KINDS else NO_KIND


def normal_env(env: str | None) -> str:
    value = (env or "").strip().lower()
    return value if value in ENVS else ""


def of(steps: int | None, kind: str | None = None, env: str | None = None) -> TaskClass:
    """Класс задачи по оценке шагов, виду работы и среде; steps None или 0 - оценки нет, пул по умолчанию."""
    known = bool(steps and steps > 0)
    n = steps if known else DEFAULT_STEPS
    why = f"оценка {n} шагов" if known else f"оценки нет, пул по умолчанию {n} шагов"
    kind, env = normal_kind(kind), normal_env(env)
    label = ", ".join(x for x in (kind, env) if x)
    return TaskClass(kind, n, env, f"{label}, {why}" if label else why)
