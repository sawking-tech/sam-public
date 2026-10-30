"""Измеренная скорость моделей по записям токенов (_token_usage): чистая функция, к базе не ходит.

По факту, а не по паспорту: токенов выхода в секунду и задержка вызова берутся из того, что модель делала в SAM.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Iterable

MS_PER_SECOND = 1000
MIN_OUTPUT_TOKENS = 20   # короткий ответ почти весь состоит из задержки до первого токена: скорость по нему не мерим
MIN_CALLS = 5            # меньше замеров - медиане не верим, скорость считается неизвестной


@dataclass(frozen=True)
class ModelStat:
    calls: int
    tokens_per_s: float          # медиана токенов выхода в секунду по длинным ответам
    median_latency_s: float      # медиана полного времени вызова


def collect(rows: Iterable[dict]) -> dict[str, ModelStat]:
    """Ключ "поставщик/модель" -> замер; модели с меньше чем MIN_CALLS подходящими вызовами в результат не входят."""
    speeds: dict[str, list[float]] = {}
    latencies: dict[str, list[float]] = {}
    for doc in rows:
        out, ms = doc.get("output_tokens"), doc.get("latency_ms")
        if out is None or not ms or ms <= 0:
            continue
        key = f"{doc.get('source', '')}/{doc.get('model', '')}"
        latencies.setdefault(key, []).append(ms / MS_PER_SECOND)
        if out >= MIN_OUTPUT_TOKENS:
            speeds.setdefault(key, []).append(out / (ms / MS_PER_SECOND))
    return {key: ModelStat(len(values), statistics.median(values), statistics.median(latencies[key]))
            for key, values in speeds.items() if len(values) >= MIN_CALLS}
