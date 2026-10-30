"""Остатки счетов для ступени бюджета (SAM-260): фоновое обновление кэша, общая сумма по любым поставщикам."""
import asyncio

from gateway import agent, balance


async def test_balances_refresh_in_the_background_and_the_first_call_does_not_wait(monkeypatch):
    calls = []

    async def fetch(http_get, mcp_tool=None):
        calls.append(1)
        return {"polza": {"currency": "RUB", "balance": 4969.5}}
    monkeypatch.setattr(balance, "fetch_balances", fetch)
    monkeypatch.setattr(agent, "_balances_cache", {"at": 0.0, "value": {}})
    assert agent._account_balances() == {}                      # первый вызов не ждёт сеть
    await asyncio.sleep(0.05)
    assert agent._account_balances()["polza"]["balance"] == 4969.5
    agent._account_balances()
    assert calls == [1]                                         # минутный кэш: второй запрос в сеть не идёт


async def test_a_failed_refresh_keeps_the_previous_value_and_does_not_hammer_the_network(monkeypatch):
    async def fetch(http_get, mcp_tool=None):
        raise RuntimeError("сеть")
    monkeypatch.setattr(balance, "fetch_balances", fetch)
    monkeypatch.setattr(agent, "_balances_cache", {"at": 0.0, "value": {"polza": {"balance": 1.0, "currency": "RUB"}}})
    agent._account_balances()
    await asyncio.sleep(0.05)
    assert agent._account_balances()["polza"]["balance"] == 1.0
    assert agent._balances_cache["at"] > 0 and not agent._balances_cache["busy"]


def test_zero_balance_provider_drops_out_and_a_topped_up_one_returns():
    fx = {"USD": 1.0, "RUB": 90.0}
    balances = {"polza": {"currency": "RUB", "balance": 4000.0}, "openrouter": {"currency": "USD", "balance": 0.0}}
    assert balance.alive_sources(balances, fx) == frozenset({"polza"})
    balances["openrouter"]["balance"] = 10.0                     # пополнил на 10 долларов
    assert balance.alive_sources(balances, fx) == frozenset({"polza", "openrouter"})
    assert balance.total_rub(balances, balance.alive_sources(balances, fx), fx) == 4000.0 + 900.0
