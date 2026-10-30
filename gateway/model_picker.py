"""Подборщик модели агенту (SAM-258): собирает входы выбора из внешнего мира и отдаёт рекомендацию и список моделей.

Решает model_choice (чистая функция), здесь только сборка: каталоги агрегаторов с ключом (model_catalog, кэш
SAM_CATALOG_TTL_S), модели из их списков переключения (<ИМЯ>_MODELS), ручные профили (model_profiles.json), книга цен,
замеры скорости из _token_usage и окно запусков из _task_runs. Сеть приходит функцией http_get.
"""
from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from . import model_learning, model_report, model_stats
from .env import env_int
from .llm_router import AGGREGATORS, aggregator_models, source_config
from .model_catalog import PARSERS, Offer, share_ratings, to_profiles
from .model_choice import choose
from .model_profiles import load_profiles
from .pricing import load_price_book
from .repos.task_runs_repo import TaskRunsRepo
from .repos.token_usage_repo import TokenUsageRepo
from . import task_class

HttpGet = Callable[[str, dict], Awaitable[dict]]
CATALOG_TTL_S = env_int("SAM_CATALOG_TTL_S", 3600)    # каталоги меняются за дни, не за минуты
USAGE_DAYS = env_int("SAM_ORCH_USAGE_DAYS", 30)       # окно замеров скорости; больше не даст TTL _token_usage
RANKING_LIMIT = env_int("SAM_ORCH_RANKING", 8)          # запасных моделей в рекомендации: хватает на отказ и повтор
_cache: dict = {"at": 0.0, "offers": []}


async def _offers(http_get: HttpGet) -> list[Offer]:
    if _cache["offers"] and time.monotonic() - _cache["at"] < CATALOG_TTL_S:
        return _cache["offers"]
    offers: list[Offer] = []
    for source, (url_env, default_url, key_env, _) in AGGREGATORS.items():
        key, parse = os.getenv(key_env), PARSERS.get(source)
        if not key or not parse:
            continue
        try:
            url = f"{os.getenv(url_env, default_url).rstrip('/')}/models"
            offers += parse(await http_get(url, {"Authorization": f"Bearer {key}"}))
        except Exception as e:
            print(f"[ModelPicker] каталог {source} не получен ({type(e).__name__}); его модели в выборе пропущены")
    _cache.update(at=time.monotonic(), offers=share_ratings(offers))
    return _cache["offers"]


async def _inputs(db, http_get: HttpGet):
    profiles, catalog_prices = to_profiles(await _offers(http_get),
                                           {s: aggregator_models(s) for s in AGGREGATORS}, load_profiles())
    book = load_price_book()
    book = replace(book, prices={**book.prices, **catalog_prices})   # цена каталога свежее таблицы
    rows = TokenUsageRepo(db).rows_since(datetime.utcnow() - timedelta(days=USAGE_DAYS))
    calls = Counter(f"{r.get('source', '')}/{r.get('model', '')}" for r in rows)
    runs = TaskRunsRepo(db).recent()
    return profiles, book, model_stats.collect(rows), model_learning.learn(runs), calls, runs


async def _available(profiles, cooling: set[str]) -> dict[str, bool]:
    """Доступна ли модель сейчас: источник не в паузе и роутер даёт её конфиг (ключ, список, загрузка LM Studio)."""
    out = {}
    for key in profiles:
        source, _, model = key.partition("/")
        out[key] = source not in cooling and await source_config(source, model) is not None
    return out


async def recommend(db, http_get: HttpGet, steps: int | None, cooling: set[str], deadline_s: float | None = None,
                    budget_usd: float | None = None, kind: str = "", env: str = "", task_id: str = "") -> dict:
    """Рекомендация модели задаче по прикидке создателя (шаги, вид работы); budget_usd - доля бюджета задачи."""
    profiles, book, stats, learned, _, runs = await _inputs(db, http_get)
    available = await _available(profiles, cooling)
    failed = model_learning.failed_models(runs, task_id) if task_id else frozenset()   # повтор задачи - к следующей модели
    for key in available:
        if key.partition("/")[2] in failed:
            available[key] = False
    factor = model_learning.steps_factor(runs, task_class.normal_kind(kind))
    real_steps = round(steps * factor) if steps else steps
    pick = choose(task_class.of(real_steps, kind, env), profiles, book, stats, lambda s, m: available.get(f"{s}/{m}", False),
                  deadline_s=deadline_s, learned=learned, budget_usd=budget_usd)
    if pick is None:
        return {"model": None, "reason": "нет доступной модели с инструментами и нужным окном"}
    return {"source": pick.source, "model": pick.model, "est_cost_usd": pick.est_cost_usd,
            "est_seconds": pick.est_seconds, "reason": pick.reason, "candidates": sum(available.values()),
            "ranking": list(pick.ranking[:RANKING_LIMIT])}


async def tier_list(db, http_get: HttpGet) -> list[dict]:
    profiles, book, stats, learned, calls, _ = await _inputs(db, http_get)
    return model_report.tier_list(profiles, book, stats, learned, dict(calls))
