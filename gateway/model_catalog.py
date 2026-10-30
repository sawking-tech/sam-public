"""Каталоги агрегаторов -> предложения моделей для оркестратора (SAM-233).

У каждого агрегатора свой разбор ответа `/models` (чистая функция, без сети); реестр PARSERS. Новый агрегатор - парсер
здесь и строка в llm_router.AGGREGATORS, оркестратор не меняется. Рейтинг (индекс интеллекта Artificial Analysis)
OpenRouter отдаёт в `benchmarks`; одна и та же модель у разных продавцов получает один рейтинг (share_ratings).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Iterable

from .model_profiles import ModelProfile, tier_for_rating
from .pricing import Price

PER_MILLION = 1_000_000
TOOLS_PARAM = "tools"
UNRATED_TIER = 1   # рейтинга нет - сложное такой модели не доверяем; вручную поднимается в model_profiles.json


@dataclass(frozen=True)
class Offer:
    source: str
    model: str
    price: Price
    context: int
    tools: bool
    rating: float | None = None


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_openrouter(payload: dict) -> list[Offer]:
    """Цены OpenRouter - USD за токен строкой; рейтинг - benchmarks.artificial_analysis.intelligence_index."""
    offers = []
    for m in payload.get("data", []):
        pricing = m.get("pricing") or {}
        inp, out = _num(pricing.get("prompt")), _num(pricing.get("completion"))
        if inp is None or out is None:
            continue
        cached = _num(pricing.get("input_cache_read"))
        rating = ((m.get("benchmarks") or {}).get("artificial_analysis") or {}).get("intelligence_index")
        offers.append(Offer("openrouter", m["id"],
                            Price(inp * PER_MILLION, out * PER_MILLION, "USD",
                                  cached * PER_MILLION if cached is not None else None),
                            int(m.get("context_length") or 0), TOOLS_PARAM in (m.get("supported_parameters") or []),
                            _num(rating)))
    return offers


def parse_polza(payload: dict) -> list[Offer]:
    """Цены polza - за 1M в валюте top_provider.pricing; параметры - у top_provider."""
    offers = []
    for m in payload.get("data", payload if isinstance(payload, list) else []):
        top = m.get("top_provider") or {}
        pricing = top.get("pricing") or {}
        inp, out = _num(pricing.get("prompt_per_million")), _num(pricing.get("completion_per_million"))
        if inp is None or out is None:
            continue
        offers.append(Offer("polza", m["id"],
                            Price(inp, out, pricing.get("currency", "RUB"), _num(pricing.get("input_cache_read_per_million"))),
                            int(m.get("context_length") or top.get("context_length") or 0),
                            TOOLS_PARAM in (top.get("supported_parameters") or [])))
    return offers


PARSERS: dict[str, Callable[[dict], list[Offer]]] = {"openrouter": parse_openrouter, "polza": parse_polza}


def share_ratings(offers: Iterable[Offer]) -> list[Offer]:
    """Модель одна, продавцов несколько: известный у одного рейтинг получают и остальные."""
    offers = list(offers)
    known = {o.model: o.rating for o in offers if o.rating is not None}
    return [o if o.rating is not None or o.model not in known else replace(o, rating=known[o.model]) for o in offers]


def to_profiles(offers: Iterable[Offer], selected: dict[str, list[str]],
                overrides: dict[str, ModelProfile]) -> tuple[dict[str, ModelProfile], dict[str, Price]]:
    """Профили и цены моделей из списков переключения (selected: источник -> модели). Ручной профиль из
    model_profiles.json сильнее каталога; локальные модели приходят только оттуда."""
    profiles: dict[str, ModelProfile] = {k: v for k, v in overrides.items() if v.local}
    prices: dict[str, Price] = {}
    for o in offers:
        if o.model not in selected.get(o.source, []):
            continue
        key = f"{o.source}/{o.model}"
        tier = tier_for_rating(o.rating) if o.rating is not None else UNRATED_TIER
        profiles[key] = overrides.get(key) or ModelProfile(tier=tier, local=False, tools=o.tools, context=o.context,
                                                         rating=o.rating)
        prices[key] = o.price
    return profiles, prices
