"""Бюджет SAM на тяжёлую работу (SAM-260): расход по окнам, ступени деградации, решение «можно ли запускать агента».

Чистые функции: записи вызовов, цены, время и остатки приходят аргументами, к базе и сети модуль не ходит. Деньги
считаются в рублях: стоимость вызова даёт поставщик или таблица цен (gateway/usage_cost.row_cost), другие валюты
переводятся по курсу книги цен. Тяжёлая работа сейчас - вызовы агентов (роль worker); быт (голос, ответы, судьи,
сжатие) окна не занимает и не режется.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .env import env_float, env_int
from .pricing import PriceBook
from .usage_cost import row_cost

HEAVY_ROLES = frozenset({"worker"})   # роли записей токенов, чей расход идёт в окно тяжёлой работы
BUDGET_SOURCES_ENV = "SAM_BUDGET_SOURCES"   # источники, чьи деньги защищает лимит (счёт, на котором лежит бюджет)
DEFAULT_BUDGET_SOURCES = "polza"            # расход других поставщиков (OpenRouter с пустым счётом) бюджет не расходует


def budget_sources(environ=None) -> frozenset[str]:
    raw = (environ if environ is not None else os.environ).get(BUDGET_SOURCES_ENV) or DEFAULT_BUDGET_SOURCES
    return frozenset(s.strip() for s in raw.split(",") if s.strip())

RUB = "RUB"
DEFAULT_AGENT_STEPS = env_int("SAM_AGENT_STEPS", 30)   # оценки нет - пул агента по умолчанию (agent_budget)
LIGHT_TASK_STEPS = env_int("SAM_LIGHT_TASK_STEPS", 10)   # пул задачи до порога - лёгкая работа: тренажёры, утилиты, мелкие страницы
WINDOW_H = env_int("SAM_DEV_WINDOW_H", 5)                  # скользящее окно на рабочую сессию, как у Claude (часы)
WINDOW_RUB = env_float("SAM_DEV_WINDOW_RUB", 40.0)         # расчёт SAM-260: 3000 ₽ тяжёлой работы на месяц при 5000 ₽
DAY_RUB = env_float("SAM_DEV_DAY_RUB", 150.0)
HOUR_RUB = env_float("SAM_DEV_HOUR_RUB", 15.0)             # за час: модель не сожжёт бюджет разом
WEEK_RUB = env_float("SAM_DEV_WEEK_RUB", 0.0)              # 0 - неделя выключена: сутки и месяц её перекрывают
MONTH_RUB = env_float("SAM_DEV_MONTH_RUB", 3000.0)
MONTHLY_CAP_RUB = env_float("SAM_MONTHLY_BUDGET_RUB", 5000.0)   # потолок на всё за месяц
DAYS_IN_WEEK, DAYS_IN_MONTH = 7, 30

# Ступени по доле остатка месячного бюджета тяжёлой работы
STAGE_SAVING = env_float("SAM_DEGRADE_SAVING", 0.35)       # ниже: основная модель берёт дешевле, тяжёлое по слову
STAGE_MINIMAL = env_float("SAM_DEGRADE_MINIMAL", 0.15)     # ниже: самая дешёвая рабочая модель, тяжёлого нет
NORMAL, SAVING, MINIMAL, FLOOR = "normal", "saving", "minimal", "floor"
STAGES = (NORMAL, SAVING, MINIMAL, FLOOR)                  # от лучшей к худшей
FLOOR_RUB = env_float("SAM_HOUSEHOLD_FLOOR_RUB", 500.0)    # остаток счёта: ниже - только быт на самой дешёвой модели
RECOVER_HOLD_MIN = env_int("SAM_RECOVER_HOLD_MIN", 30)     # выдержка перед подъёмом на следующую ступень
NO_HEAVY = "Ступень {stage}: тяжёлая работа остановлена, остаётся быт. Задача встанет в очередь и запустится сама, когда ступень поднимется."

REFUSED = ("Лимит тяжёлой работы: {what} {spent:.0f} из {limit:.0f} ₽. {when} Задача встанет в очередь и запустится сама, когда лимит "
           "откроется (бытовое и лёгкое идёт сейчас).")
WHEN_WINDOW = "Окно откроется через {minutes} мин."
WHEN_LATER = "Лимит сбросится позже ({what}); срочное - только по слову человека."


@dataclass(frozen=True)
class Limits:
    hour_rub: float = HOUR_RUB
    window_rub: float = WINDOW_RUB
    day_rub: float = DAY_RUB
    week_rub: float = WEEK_RUB
    month_rub: float = MONTH_RUB
    window_h: int = WINDOW_H


@dataclass(frozen=True)
class Spent:
    hour: float
    window: float
    day: float
    week: float
    month: float
    oldest_in_window: datetime | None   # когда окно освободится: самая старая запись окна + длина окна
    oldest_in_hour: datetime | None = None


def _to_rub(cost: float, currency: str, book: PriceBook) -> float | None:
    if currency == RUB:
        return cost
    rate_rub, rate_cur = book.fx_per_usd.get(RUB), book.fx_per_usd.get(currency)
    if not rate_rub or not rate_cur:
        return None
    return cost / rate_cur * rate_rub


def spent_heavy(rows: Iterable[dict], book: PriceBook, now: datetime, limits: Limits = Limits(),
                sources: frozenset[str] | None = None, light_sessions: frozenset[str] = frozenset()) -> Spent:
    """Расход тяжёлой работы (роли HEAVY_ROLES) за окно, день, неделю, месяц в рублях; вызов без цены не считается.
    sources: чьи деньги считаются (по умолчанию SAM_BUDGET_SOURCES, то есть счёт с бюджетом); light_sessions: сессии
    лёгких задач (пул до LIGHT_TASK_STEPS), окно они не занимают, расход виден в общем счёте."""
    sources = budget_sources() if sources is None else sources
    edges = {"hour": now - timedelta(hours=1), "window": now - timedelta(hours=limits.window_h), "day": now - timedelta(days=1),
             "week": now - timedelta(days=DAYS_IN_WEEK), "month": now - timedelta(days=DAYS_IN_MONTH)}
    totals = dict.fromkeys(edges, 0.0)
    oldest: datetime | None = None
    oldest_hour: datetime | None = None
    for doc in rows:
        stamp = doc.get("timestamp")
        if doc.get("role") not in HEAVY_ROLES or doc.get("source") not in sources or stamp is None or stamp > now:
            continue
        if doc.get("session_id") in light_sessions:
            continue
        priced = row_cost(doc, book)
        rub = _to_rub(priced[0], priced[1], book) if priced else None
        if rub is None:
            continue
        for name, edge in edges.items():
            if stamp >= edge:
                totals[name] += rub
        if stamp >= edges["window"] and (oldest is None or stamp < oldest):
            oldest = stamp
        if stamp >= edges["hour"] and (oldest_hour is None or stamp < oldest_hour):
            oldest_hour = stamp
    return Spent(totals["hour"], totals["window"], totals["day"], totals["week"], totals["month"], oldest, oldest_hour)


def is_heavy(description: str, steps: int | None = None) -> bool:
    """Тяжёлая ли задача при создании агента: по оценке создателя в шагах, больше порога лёгкой работы. Оценки нет -
    пул по умолчанию, тяжёлая. description остаётся в подписи для вызывающих."""
    return (steps if steps and steps > 0 else DEFAULT_AGENT_STEPS) > LIGHT_TASK_STEPS


def spent_all_month(rows: Iterable[dict], book: PriceBook, now: datetime, sources: frozenset[str] | None = None) -> float:
    """Расход всех ролей (быт и тяжёлая работа вместе) за 30 дней в рублях по счетам бюджета: против общего потолка."""
    sources = budget_sources() if sources is None else sources
    edge, total = now - timedelta(days=DAYS_IN_MONTH), 0.0
    for doc in rows:
        stamp = doc.get("timestamp")
        if doc.get("source") not in sources or stamp is None or stamp < edge or stamp > now:
            continue
        priced = row_cost(doc, book)
        rub = _to_rub(priced[0], priced[1], book) if priced else None
        total += rub or 0.0
    return total


def share_rub(spent: Spent, limits: Limits = Limits(), competing: int = 0) -> float | None:
    """Доля бюджета новой задачи в рублях: наименьший остаток часа, окна и суток (нулевой потолок - лимит выключен),
    поделённый на неё и задачи, которые идут или ждут в очереди (competing). None - все лимиты выключены."""
    left = [cap - used for used, cap in ((spent.hour, limits.hour_rub), (spent.window, limits.window_rub),
                                          (spent.day, limits.day_rub)) if cap > 0]
    if not left:
        return None
    return max(min(left), 0.0) / (1 + max(competing, 0))


def stage(month_spent: float, limits: Limits = Limits()) -> str:
    """Ступень по доле остатка месячного бюджета тяжёлой работы; пол быта задаётся отдельным остатком счёта."""
    left = max(limits.month_rub - month_spent, 0.0) / limits.month_rub if limits.month_rub else 0.0
    if left <= 0:
        return MINIMAL
    if left < STAGE_MINIMAL:
        return MINIMAL
    return SAVING if left < STAGE_SAVING else NORMAL


def target_stage(month_spent: float, balance_rub: float | None, limits: Limits = Limits(),
                 total_spent: float = 0.0) -> str:
    """Ступень, которую требуют цифры сейчас: пол быта (по остатку счёта или по общему потолку месяца) главнее долей
    месячного бюджета тяжёлой работы."""
    if balance_rub is not None and balance_rub <= FLOOR_RUB:
        return FLOOR
    if total_spent >= MONTHLY_CAP_RUB - FLOOR_RUB:
        return FLOOR
    return stage(month_spent, limits)


class Stager:
    """Текущая ступень: вниз сразу, вверх на одну ступень за раз после выдержки (сброс окна, пополнение). Время приходит
    аргументом, состояние в памяти шлюза: после перезапуска ступень пересчитывается по цифрам."""

    def __init__(self, hold_min: int = RECOVER_HOLD_MIN):
        self._hold = timedelta(minutes=hold_min)
        self.current = NORMAL
        self._since = None   # когда цифры впервые потребовали ступень лучше текущей

    def update(self, target: str, now: datetime) -> tuple[str, bool]:
        """(ступень, сменилась ли)."""
        cur, want = STAGES.index(self.current), STAGES.index(target)
        if want > cur:                      # хуже: сразу
            self.current, self._since = target, None
            return self.current, True
        if want < cur:                      # лучше: на шаг после выдержки
            self._since = self._since or now
            if now - self._since >= self._hold:
                self.current, self._since = STAGES[cur - 1], None
                return self.current, True
            return self.current, False
        self._since = None
        return self.current, False


def gate(spent: Spent, now: datetime, limits: Limits = Limits(), emergency: bool = False, current: str = NORMAL) -> tuple[bool, str]:
    """(можно ли создавать тяжёлого агента, причина отказа одной строкой). Авария снимает все лимиты; на ступенях
    minimal и floor тяжёлого нет вовсе."""
    if emergency:
        return True, ""
    if current in (MINIMAL, FLOOR):
        return False, NO_HEAVY.format(stage=current)
    checks = (("час", spent.hour, limits.hour_rub), ("окно", spent.window, limits.window_rub),
              ("сутки", spent.day, limits.day_rub), ("неделя", spent.week, limits.week_rub),
              ("месяц", spent.month, limits.month_rub))
    # Закрыто несколько лимитов - назвать тот, что откроется позже всех: очередь ждёт все (час открылся, окно ещё нет)
    latest: tuple[float, str, float, float] | None = None
    for what, used, cap in checks:
        if cap > 0 and used >= cap:   # нулевой потолок - лимит выключен
            first, length_h = ((spent.oldest_in_hour, 1) if what == "час" else (spent.oldest_in_window, limits.window_h))
            if what in ("час", "окно") and first is not None:
                minutes = float(max(int((first + timedelta(hours=length_h) - now).total_seconds() // 60), 1))
            else:
                minutes = float("inf")
            if latest is None or minutes > latest[0]:
                latest = (minutes, what, used, cap)
    if latest is None:
        return True, ""
    minutes, what, used, cap = latest
    when = WHEN_WINDOW.format(minutes=int(minutes)) if minutes != float("inf") else WHEN_LATER.format(what=what)
    return False, REFUSED.format(what=what, spent=used, limit=cap, when=when)
