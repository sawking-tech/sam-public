"""Остаток денег у поставщиков LLM. Поставщик с API баланса - свой разбор ответа; запрос идёт через переданную функцию,
чтобы разбор проверялся без сети. У Polza баланс отдаёт их MCP (get_balance_details, SAM-260). Нет API баланса
(z.ai по подписке) - источник в ответ не попадает."""
from __future__ import annotations

import json
import os
from typing import Awaitable, Callable

HttpGet = Callable[[str, dict], Awaitable[dict]]   # (url, заголовки) -> JSON ответа
McpTool = Callable[[str, dict, str, dict], Awaitable[dict]]   # (url, заголовки, инструмент, аргументы) -> result MCP

OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_URL_ENV = "OPENROUTER_BASE_URL"
OPENROUTER_DEFAULT_URL = "https://openrouter.ai/api/v1"
POLZA_MCP_KEY_ENV = "POLZA_MCP_KEY"
POLZA_MCP_URL_ENV = "POLZA_MCP_URL"
POLZA_MCP_DEFAULT_URL = "https://polza.ai/api/mcp"
POLZA_BALANCE_TOOL = "get_balance_details"


def parse_openrouter(credits: dict, key: dict) -> dict:
    """/credits: всего пополнено и израсходовано по аккаунту; /key: лимит и остаток у ключа SAM."""
    data, key_data = credits.get("data", {}), key.get("data", {})
    return {
        "currency": "USD",
        "balance": round(data.get("total_credits", 0) - data.get("total_usage", 0), 6),
        "key_limit": key_data.get("limit"),
        "key_remaining": key_data.get("limit_remaining"),
    }


async def openrouter_balance(http_get: HttpGet) -> dict:
    base = os.getenv(OPENROUTER_URL_ENV, OPENROUTER_DEFAULT_URL).rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ[OPENROUTER_KEY_ENV]}"}
    return parse_openrouter(await http_get(f"{base}/credits", headers), await http_get(f"{base}/key", headers))


def parse_polza(result: dict) -> dict:
    """Результат инструмента get_balance_details: JSON в текстовом блоке; суммы строками."""
    text = next((c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"), "")
    balance = json.loads(text).get("balance", {})
    return {"currency": balance.get("currency", "RUB"), "balance": float(balance.get("available", 0)),
            "reserved": float(balance.get("reservedAmount", 0)), "spent": float(balance.get("spentAmount", 0))}


async def polza_balance(mcp_tool: McpTool) -> dict:
    url = os.getenv(POLZA_MCP_URL_ENV, POLZA_MCP_DEFAULT_URL)
    headers = {"Authorization": f"Bearer {os.environ[POLZA_MCP_KEY_ENV]}"}
    return parse_polza(await mcp_tool(url, headers, POLZA_BALANCE_TOOL, {}))


def alive_sources(balances: dict, fx_per_usd: dict) -> frozenset[str]:
    """Поставщики с деньгами на счёте (остаток в рублях больше нуля): их расход считается в лимитах, их остатки
    складываются в общую сумму. Счёт с нулём (OpenRouter 28 окт) выпадает сам, пополненный возвращается."""
    return frozenset(s for s in balances if (total_rub(balances, frozenset({s}), fx_per_usd) or 0) > 0)


def total_rub(balances: dict, sources: frozenset[str], fx_per_usd: dict) -> float | None:
    """Сумма остатков поставщиков бюджета в рублях: любой источник с балансом {currency, balance} (SAM-260, счёт не привязан
    к поставщику). None - ни одного остатка не получено (ступень тогда считается по долям месяца)."""
    rub, total, known = fx_per_usd.get("RUB"), 0.0, False
    for source in sources:
        item = balances.get(source) or {}
        if "error" in item or item.get("balance") is None:
            continue
        currency = item.get("currency", "RUB")
        rate = fx_per_usd.get(currency)
        if currency != "RUB" and not (rate and rub):
            continue
        total += item["balance"] if currency == "RUB" else item["balance"] / rate * rub
        known = True
    return total if known else None


async def fetch_balances(http_get: HttpGet, mcp_tool: McpTool | None = None) -> dict:
    """{источник: остаток или {"error": одна строка}} для поставщиков с ключом в окружении."""
    out: dict[str, dict] = {}
    if os.getenv(OPENROUTER_KEY_ENV):
        try:
            out["openrouter"] = await openrouter_balance(http_get)
        except Exception as e:
            out["openrouter"] = {"error": f"баланс OpenRouter не получен ({type(e).__name__}); проверь ключ и сеть"}
    if mcp_tool is not None and os.getenv(POLZA_MCP_KEY_ENV):
        try:
            out["polza"] = await polza_balance(mcp_tool)
        except Exception as e:
            out["polza"] = {"error": f"баланс Polza не получен ({type(e).__name__}); проверь ключ MCP и сеть"}
    return out
