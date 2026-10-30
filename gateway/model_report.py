"""Список моделей со статистикой (SAM-258): чистая функция, всё приходит аргументами.

Строка на модель из профилей: успехи по видам работы по факту, измеренная скорость, цена, число вызовов. Порядок -
дешевле выше: без фактов первой идёт самая дешёвая, как и в выборе (model_choice).
"""
from __future__ import annotations

from .model_learning import Learned, MIN_RUNS
from .model_profiles import ModelProfile
from .model_stats import ModelStat
from .pricing import PriceBook, price_for


def _price_usd(book: PriceBook, key: str, profile: ModelProfile) -> dict | None:
    if profile.local:
        return {"input": 0.0, "output": 0.0}
    source, _, model = key.partition("/")
    price = price_for(book.prices, source, model)
    rate = book.fx_per_usd.get(price.currency) if price else None
    if not price or not rate:
        return None   # цена или курс неизвестны
    return {"input": round(price.input / rate, 4), "output": round(price.output / rate, 4)}


def tier_list(profiles: dict[str, ModelProfile], book: PriceBook, stats: dict[str, ModelStat],
              learned: dict[tuple, Learned], calls: dict[str, int]) -> list[dict]:
    rows = []
    for key, profile in profiles.items():
        by_kind = {" ".join(x or "-" for x in group[1:]): {"runs": seen.runs, "success_rate": round(seen.success_rate, 2),
                                                          "median_seconds": seen.median_seconds,
                                                          "median_cost_usd": seen.median_cost_usd}
                   for group, seen in learned.items() if group[0] == key.partition("/")[2] and seen.runs >= MIN_RUNS}
        stat = stats.get(key)
        rows.append({
            "model": key, "local": profile.local, "rating": profile.rating, "by_kind": by_kind,
            "price_usd_per_1m": _price_usd(book, key, profile),
            "tokens_per_s": round(stat.tokens_per_s, 1) if stat else profile.tps_hint,
            "calls": calls.get(key, 0)})
    rows.sort(key=lambda r: (r["price_usd_per_1m"] is None, (r["price_usd_per_1m"] or {}).get("input", 0.0)))
    return rows
