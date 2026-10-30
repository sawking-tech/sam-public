"""Выбор модели агенту (SAM-258): самая дешёвая из тех, что по факту закрывают такие задачи и влезают в бюджет.

Чистая функция: профили, цены, замеры, итоги запусков, доступность и доля бюджета приходят аргументами.
- p(м) - доля успехов модели на задачах этого вида по факту (model_learning); фактов нет - 1: доказать, что модель не
  справится, может только её провал, поэтому без данных первой идёт самая дешёвая;
- цена(м) = шаги x (токены входа шага x цена входа + токены выхода шага x цена выхода) / p(м) - с повторами;
- из моделей с p не ниже MIN_P, чья цена влезает в долю бюджета задачи (остаток лимитов / задачи, которые его делят),
  берётся самая надёжная, при равной - дешевле; не влезает никто - самая дешёвая: пауза до открытия лимита короче.
Работа внутри экосистемы SAM (inside) - только модели уровня не ниже INSIDE_MIN_TIER. Срок, если задан, отсекает
медленные; локальная модель при занятой видеокарте (идёт голос) не берётся.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .env import env_float, env_int
from .model_learning import Learned, success_p
from .model_profiles import ModelProfile
from .model_stats import ModelStat
from .pricing import PriceBook, call_cost, price_for
from .task_class import INSIDE, TaskClass

MIN_P = env_float("SAM_MODEL_MIN_P", 0.25)   # ниже - модель по факту проваливает такие задачи, не берётся даже бесплатной
# Работа внутри экосистемы SAM - только модели не ниже этого уровня профиля: самая дешёвая там не пробуется
INSIDE_MIN_TIER = env_int("SAM_INSIDE_MIN_TIER", 2)

Available = Callable[[str, str], bool]   # (поставщик, модель) -> ключ есть, не в паузе, в списке переключения


@dataclass(frozen=True)
class Choice:
    source: str
    model: str
    est_cost_usd: float | None
    est_seconds: float | None
    reason: str
    ranking: tuple[dict, ...] = ()   # допустимые модели по приоритету: первая - выбранная; запасные на случай отказа


@dataclass(frozen=True)
class _Candidate:
    key: str
    source: str
    model: str
    profile: ModelProfile
    cost_usd: float | None      # цена задачи за один проход
    seconds: float | None
    p: float                    # вероятность закрыть задачу
    facts: Learned | None

    @property
    def expected_usd(self) -> float | None:
        return None if self.cost_usd is None else self.cost_usd / self.p


def _split(key: str) -> tuple[str, str]:
    source, _, model = key.partition("/")
    return source, model


def _cost_usd(profile: ModelProfile, key: str, task: TaskClass, book: PriceBook) -> float | None:
    if profile.local:
        return 0.0
    source, model = _split(key)
    price = price_for(book.prices, source, model)
    rate = book.fx_per_usd.get(price.currency) if price else None
    if price is None or not rate:
        return None   # цена или курс неизвестны: цену не выдумываем
    return call_cost(price, task.in_tokens, 0, task.out_tokens) / rate


def _seconds(profile: ModelProfile, key: str, task: TaskClass, stats: dict[str, ModelStat]) -> float | None:
    stat = stats.get(key)
    tps = stat.tokens_per_s if stat else profile.tps_hint
    return task.out_tokens / tps if tps else None


def _describe(c: _Candidate) -> str:
    cost = "цена неизвестна" if c.expected_usd is None else ("бесплатно" if c.expected_usd == 0 else f"около ${c.expected_usd:.4f}")
    seconds = "скорость неизвестна" if c.seconds is None else f"около {c.seconds:.0f} с"
    facts = f"по факту {c.facts.runs} запусков, успехов {c.facts.success_rate:.0%}" if c.facts else "фактов нет"
    return f"{c.key}: p {c.p:.2f} ({facts}), {cost}, {seconds}"


def _by_cost(c: _Candidate) -> tuple:
    return (c.expected_usd is None, c.expected_usd or 0.0, -c.p, c.seconds is None, c.seconds or 0.0)


def _by_reliability(c: _Candidate) -> tuple:
    return (-c.p,) + _by_cost(c)


def choose(task: TaskClass, profiles: dict[str, ModelProfile], book: PriceBook, stats: dict[str, ModelStat],
           available: Available, deadline_s: float | None = None, local_busy: bool = False,
           learned: dict[tuple, Learned] | None = None, budget_usd: float | None = None) -> Choice | None:
    """None - не нашлось ни одной доступной модели с инструментами и нужным окном. budget_usd - доля бюджета этой
    задачи; None - бюджет не ограничен."""
    learned = learned or {}
    candidates = []
    for key, profile in profiles.items():
        source, model = _split(key)
        if not profile.tools or profile.context < task.step_tokens or not available(source, model):
            continue
        if local_busy and profile.local:
            continue
        p, facts = success_p(model, task.kind, task.env, learned)   # ум - у модели, у любого продавца
        cost, seconds = _cost_usd(profile, key, task, book), _seconds(profile, key, task, stats)
        if facts:                                           # по факту точнее оценки по цене и скорости
            cost = facts.median_cost_usd if facts.median_cost_usd is not None else cost
            seconds = facts.median_seconds if facts.median_seconds is not None else seconds
        candidates.append(_Candidate(key, source, model, profile, cost, seconds, p, facts))
    if not candidates:
        return None
    notes = []
    if task.env == INSIDE:
        strong = [c for c in candidates if c.profile.tier >= INSIDE_MIN_TIER]
        if strong:
            candidates = strong
        else:
            notes.append(f"внутри SAM нужен уровень {INSIDE_MIN_TIER}, доступных такого нет")
    pool = [c for c in candidates if c.p >= MIN_P]
    if not pool:
        best_p = max(c.p for c in candidates)
        pool = [c for c in candidates if c.p == best_p]
        notes.append(f"все проваливают такие задачи (p < {MIN_P}), взяты самые надёжные")
    if deadline_s is not None:
        in_time = [c for c in pool if c.seconds is None or c.seconds <= deadline_s]
        if in_time:
            pool = in_time
        else:
            notes.append(f"никто не укладывается в {deadline_s:.0f} с, срок не учтён")
    fits = [c for c in pool if budget_usd is None or (c.expected_usd is not None and c.expected_usd <= budget_usd)]
    if fits:
        ordered = sorted(fits, key=_by_reliability) + sorted((c for c in pool if c not in fits), key=_by_cost)
    else:
        ordered = sorted(pool, key=_by_cost)
        notes.append(f"в долю бюджета ${budget_usd:.4f} не влезает никто, взята самая дешёвая: пауза короче")
    best = ordered[0]
    reason = f"{task.why}; выбрана {_describe(best)}"
    if budget_usd is not None and fits:
        reason += f"; доля бюджета ${budget_usd:.4f}"
    if notes:
        reason += "; " + "; ".join(notes)
    ranking = tuple({"priority": n, "source": c.source, "model": c.model, "p": round(c.p, 3),
                     "est_cost_usd": c.expected_usd, "est_seconds": c.seconds} for n, c in enumerate(ordered, 1))
    return Choice(best.source, best.model, best.expected_usd, best.seconds, reason, ranking)
