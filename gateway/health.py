"""Состояние компонентов для /health (SAM-80): Mongo, Qdrant, MCP-агрегатор этой машины.

Каждая проверка - синхронная функция без аргументов, которая падает, если компонент недоступен.
Проверки идут в потоке и не дольше PROBE_TIMEOUT_S: зависший компонент не держит /health.
"""
from __future__ import annotations

import asyncio
from typing import Callable
from .env import env_float

OK, DOWN, TIMEOUT = "ok", "down", "timeout"
# /health зовут часто и ждут быстро; компонент, не ответивший за секунду, для человека «лежит». Не замерено.
PROBE_TIMEOUT_S = env_float("SAM_HEALTH_PROBE_TIMEOUT_S", 1.0)
ERROR_MAX_CHARS = 120   # строка ошибки в ответе /health
NO_LLM = "ни один поставщик LLM не доступен: проверьте LLM_SOURCE и ключи в .env"


async def check(probes: dict[str, Callable[[], object]], timeout_s: float = PROBE_TIMEOUT_S) -> dict[str, str]:
    names = list(probes)
    results = await asyncio.gather(*(_one(probes[n], timeout_s) for n in names))
    return dict(zip(names, results))


async def _one(probe: Callable[[], object], timeout_s: float) -> str:
    try:
        await asyncio.wait_for(asyncio.to_thread(probe), timeout_s)
        return OK
    except asyncio.TimeoutError:
        return TIMEOUT
    except Exception as e:
        return f"{DOWN}: {_one_line(e)}"


def _one_line(e: Exception) -> str:
    """Первая строка ошибки или имя её типа: ответ pymongo бывает на десятки строк, а бывает пустым."""
    first = next((line.strip() for line in str(e).splitlines() if line.strip()), "")
    return first[:ERROR_MAX_CHARS] or type(e).__name__
