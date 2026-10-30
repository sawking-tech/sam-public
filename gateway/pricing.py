"""Цены моделей и стоимость вызова LLM (учёт расходов).

Цена - за 1M токенов в валюте поставщика. Книга цен лежит в gateway/prices.json, свою подставляет SAM_PRICES_FILE:
цены агрегаторов меняются быстрее кода. Модели без цены стоимость не получают (None), цену не выдумываем. Код имён
поставщиков не знает: какие источники сами присылают стоимость вызова и в какой валюте, объявляет та же книга.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

TOKENS_PER_PRICE_UNIT = 1_000_000
PRICES_FILE_ENV = "SAM_PRICES_FILE"
DEFAULT_PRICES_FILE = Path(__file__).with_name("prices.json")
WILDCARD = "*"          # "lmstudio/*": все модели поставщика с одной ценой


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    currency: str
    cached_input: float | None = None   # не задана: кэшированные токены стоят как входные


@dataclass(frozen=True)
class PriceBook:
    prices: dict[str, Price] = field(default_factory=dict)         # "поставщик/модель" -> цена
    cost_currency: dict[str, str] = field(default_factory=dict)    # источник -> валюта стоимости, которую он присылает
    fx_per_usd: dict[str, float] = field(default_factory=dict)     # валюта -> сколько её за 1 USD (для сравнения цен)


def load_price_book(path: str | os.PathLike | None = None) -> PriceBook:
    """Книга цен из файла {"cost_currency": {...}, "prices": {...}}. Битый или отсутствующий файл - ошибка с именем
    файла и подсказкой."""
    target = Path(path or os.getenv(PRICES_FILE_ENV) or DEFAULT_PRICES_FILE)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return PriceBook(prices={key: Price(**fields) for key, fields in raw.get("prices", {}).items()},
                         cost_currency=dict(raw.get("cost_currency", {})),
                         fx_per_usd={cur: float(rate) for cur, rate in raw.get("fx_per_usd", {}).items()})
    except (OSError, ValueError, TypeError, AttributeError) as e:
        raise ValueError(f"книга цен {target} не читается ({e}); поправь файл или {PRICES_FILE_ENV}") from e


def price_for(prices: dict[str, Price], source: str, model: str) -> Price | None:
    return prices.get(f"{source}/{model}") or prices.get(f"{source}/{WILDCARD}")


def call_cost(price: Price, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> float:
    """Стоимость вызова в валюте цены. Кэшированные токены входят в input_tokens (как в usage OpenAI)."""
    cached = min(cached_input_tokens, input_tokens)
    cached_price = price.input if price.cached_input is None else price.cached_input
    total = (input_tokens - cached) * price.input + cached * cached_price + output_tokens * price.output
    return total / TOKENS_PER_PRICE_UNIT
