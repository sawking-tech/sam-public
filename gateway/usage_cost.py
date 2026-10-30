"""Сводка расходов на LLM по записям токенов (_token_usage) и таблице цен.

Чистая функция: записи и цены приходят аргументами, к базе и сети она не ходит.
"""
from __future__ import annotations

from typing import Iterable

from .pricing import PriceBook, call_cost, price_for

GROUPS = ("role", "model", "source", "day")


def _day(timestamp) -> str:
    return timestamp.strftime("%Y-%m-%d") if timestamp else ""


def _group_key(doc: dict, group_by: str) -> str:
    if group_by == "day":
        return _day(doc.get("timestamp"))
    if group_by == "model":
        return f"{doc.get('source', '')}/{doc.get('model', '')}"
    return doc.get(group_by) or ""


def row_cost(doc: dict, book: PriceBook) -> tuple[float, str, bool] | None:
    """(стоимость, валюта, прислал ли её поставщик) одного вызова или None, если цены нет. Общая для сводки расходов
    и для лимитов (gateway/dev_budget.py)."""
    provider_cost, currency = doc.get("cost"), book.cost_currency.get(doc.get("source", ""))
    if provider_cost is not None and currency:   # стоимость от поставщика; валюту источника объявляет книга цен
        return provider_cost, currency, True
    if (price := price_for(book.prices, doc.get("source", ""), doc.get("model", ""))) is not None:
        cost = call_cost(price, doc.get("input_tokens") or 0, doc.get("cached_input_tokens") or 0,
                         doc.get("output_tokens") or 0)
        return cost, price.currency, False
    return None


def summarize(rows: Iterable[dict], book: PriceBook, group_by: str = "role") -> dict:
    """{currency: {total, groups: {ключ: стоимость}}} плюс счётчики вызовов: all, priced (вошли в стоимость), provider_cost
    (из них стоимость прислал сам поставщик, иначе по таблице цен), no_usage, unpriced. Вызов без usage и без цены в
    стоимость не входит, но считается: итог не выдаёт неполное за полное."""
    if group_by not in GROUPS:
        raise ValueError(f"group_by: {', '.join(GROUPS)}")
    by_currency: dict[str, dict] = {}
    counts = {"all": 0, "priced": 0, "provider_cost": 0, "no_usage": 0, "unpriced": 0}
    unpriced_models: set[str] = set()
    for doc in rows:
        counts["all"] += 1
        if doc.get("input_tokens") is None and doc.get("output_tokens") is None:
            counts["no_usage"] += 1
            continue
        priced = row_cost(doc, book)
        if priced is None:
            counts["unpriced"] += 1
            unpriced_models.add(f"{doc.get('source', '')}/{doc.get('model', '')}")
            continue
        cost, price_currency, from_provider = priced
        counts["provider_cost"] += int(from_provider)
        bucket = by_currency.setdefault(price_currency, {"total": 0.0, "groups": {}})
        bucket["total"] += cost
        key = _group_key(doc, group_by)
        bucket["groups"][key] = bucket["groups"].get(key, 0.0) + cost
        counts["priced"] += 1
    return {"group_by": group_by, "cost": by_currency, "calls": counts, "unpriced_models": sorted(unpriced_models)}
