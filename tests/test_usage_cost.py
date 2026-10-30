"""Учёт расходов на LLM: цена вызова, сводка по записям токенов, баланс поставщика."""
from datetime import datetime

import pytest

from gateway import balance, pricing, usage_cost

USD = pricing.Price(input=1.0, output=5.0, currency="USD")
CACHED = pricing.Price(input=57.0, output=285.0, cached_input=3.0, currency="RUB")


def test_call_cost_counts_cached_input_at_the_cache_price():
    assert pricing.call_cost(USD, 1_000_000, 0, 1_000_000) == pytest.approx(6.0)
    assert pricing.call_cost(CACHED, 1_000_000, 750_000, 0) == pytest.approx(0.25 * 57 + 0.75 * 3)
    assert pricing.call_cost(USD, 1_000_000, 400_000, 0) == pytest.approx(1.0)      # без цены кэша - как вход
    assert pricing.call_cost(USD, 10, 50, 0) == pytest.approx(10 / 1e6)             # кэш не больше входа


def test_price_lookup_prefers_the_model_then_the_provider_wildcard():
    prices = {"openrouter/m": USD, "lmstudio/*": pricing.Price(0, 0, "USD")}
    assert pricing.price_for(prices, "openrouter", "m") is USD
    assert pricing.price_for(prices, "lmstudio", "any-local-model").input == 0
    assert pricing.price_for(prices, "openrouter", "other") is None


def _book(prices, cost_currency=None):
    return pricing.PriceBook(prices=prices, cost_currency=cost_currency or {})


def test_bundled_price_book_loads_with_free_local_models_and_declared_cost_currency():
    book = pricing.load_price_book()
    assert pricing.price_for(book.prices, "lmstudio", "qwen/qwen3-30b-a3b-2507").output == 0
    assert pricing.price_for(book.prices, "polza", "openai/gpt-6.1-terra").cached_input is not None
    assert book.cost_currency["openrouter"] == "USD"


def test_broken_price_file_names_the_file(tmp_path):
    bad = tmp_path / "prices.json"
    bad.write_text("{не json", encoding="utf-8")
    with pytest.raises(ValueError, match="prices.json"):
        pricing.load_price_book(bad)


def _row(role="main", source="openrouter", model="m", i=1_000_000, o=0, day=1, cached=0):
    return {"role": role, "source": source, "model": model, "input_tokens": i, "output_tokens": o,
            "cached_input_tokens": cached, "timestamp": datetime(2026, 10, day)}


def test_summary_groups_cost_and_never_hides_calls_without_price_or_usage():
    rows = [_row(role="main"), _row(role="worker", o=1_000_000), _row(model="unknown"),
            {"role": "main", "source": "openrouter", "model": "m", "input_tokens": None, "output_tokens": None},
            _row(source="lmstudio", model="local")]
    out = usage_cost.summarize(rows, _book({"openrouter/m": USD, "lmstudio/*": pricing.Price(0, 0, "USD")}))
    assert out["cost"]["USD"]["total"] == pytest.approx(1.0 + 6.0)
    assert out["cost"]["USD"]["groups"]["worker"] == pytest.approx(6.0)
    assert out["calls"] == {"all": 5, "priced": 3, "provider_cost": 0, "no_usage": 1, "unpriced": 1}
    assert out["unpriced_models"] == ["openrouter/unknown"]


def test_summary_keeps_currencies_apart_and_groups_by_day():
    book = _book({"openrouter/m": USD, "polza/m": CACHED})
    out = usage_cost.summarize([_row(day=1), _row(day=2), _row(source="polza")], book, "day")
    assert set(out["cost"]) == {"USD", "RUB"}
    assert out["cost"]["USD"]["groups"] == {"2026-10-01": pytest.approx(1.0), "2026-10-02": pytest.approx(1.0)}


def test_summary_rejects_unknown_grouping():
    with pytest.raises(ValueError, match="group_by"):
        usage_cost.summarize([], _book({}), "color")


async def test_openrouter_balance_is_credits_minus_usage_with_the_key_remainder(monkeypatch):
    monkeypatch.setenv(balance.OPENROUTER_KEY_ENV, "test-do-not-use")
    seen = []

    async def http_get(url, headers):
        seen.append((url, headers["Authorization"]))
        if url.endswith("/credits"):
            return {"data": {"total_credits": 263, "total_usage": 239.5}}
        return {"data": {"limit": 23, "limit_remaining": 22.5}}

    out = await balance.fetch_balances(http_get)
    assert out == {"openrouter": {"currency": "USD", "balance": 23.5, "key_limit": 23, "key_remaining": 22.5}}
    assert {u.rsplit("/", 1)[1] for u, _ in seen} == {"credits", "key"}


async def test_balance_failure_is_one_line_and_no_key_means_no_entry(monkeypatch):
    async def down(url, headers):
        raise ConnectionError("нет сети")

    monkeypatch.delenv(balance.OPENROUTER_KEY_ENV, raising=False)
    assert await balance.fetch_balances(down) == {}
    monkeypatch.setenv(balance.OPENROUTER_KEY_ENV, "test-do-not-use")
    assert "ConnectionError" in (await balance.fetch_balances(down))["openrouter"]["error"]


def test_repo_returns_only_rows_since_the_moment():
    import mongomock
    from gateway.repos import TokenUsageRepo
    repo = TokenUsageRepo(mongomock.MongoClient()["t"])
    repo.record({"role": "main", "timestamp": datetime(2026, 10, 1)})
    repo.record({"role": "worker", "timestamp": datetime(2026, 10, 20)})
    assert [d["role"] for d in repo.rows_since(datetime(2026, 10, 10))] == ["worker"]
    assert TokenUsageRepo(None).rows_since(datetime(2026, 10, 10)) == []


def test_cost_sent_by_the_provider_wins_over_the_price_table():
    """Источник, чью валюту объявляет книга, присылает стоимость: берём её, таблица - когда стоимости нет."""
    book = _book({"openrouter/m": USD}, {"openrouter": "USD"})
    out = usage_cost.summarize([{**_row(), "cost": 0.5}, _row()], book)
    assert out["cost"]["USD"]["total"] == pytest.approx(0.5 + 1.0)
    assert out["calls"]["provider_cost"] == 1 and out["calls"]["priced"] == 2


def test_cost_from_a_source_without_declared_currency_is_not_trusted():
    """Валюту не угадываем: стоимость от источника, которого нет в cost_currency, идёт по таблице цен."""
    book = _book({"openrouter/m": USD})
    out = usage_cost.summarize([{**_row(), "cost": 0.5}], book)
    assert out["cost"]["USD"]["total"] == pytest.approx(1.0) and out["calls"]["provider_cost"] == 0


def test_any_provider_cost_field_is_recorded_without_naming_the_provider():
    from types import SimpleNamespace
    from gateway import token_usage
    response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=8, completion_tokens=8, total_tokens=16, cost=1.5e-6))
    for source in ("openrouter", "polza", "anything-new"):
        assert token_usage.build_record({"source": source, "model": "m"}, response, 10)["cost"] == 1.5e-6
    free = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2))
    assert "cost" not in token_usage.build_record({"source": "openrouter", "model": "m"}, free, 10)


# ─── Баланс Polza через MCP (SAM-260) ─────────────────────────────────────────

import pytest as _pytest

from gateway import balance as _balance


def test_polza_balance_is_parsed_from_the_mcp_text_block():
    result = {"content": [{"type": "text", "text": '{"balance": {"available": "4969.50", "reservedAmount": "1.00", '
                                                   '"spentAmount": "30.50", "currency": "RUB"}, "autoTopUp": null}'}]}
    assert _balance.parse_polza(result) == {"currency": "RUB", "balance": 4969.5, "reserved": 1.0, "spent": 30.5}


async def test_fetch_balances_adds_polza_only_with_the_mcp_key_and_reports_errors_in_one_line(monkeypatch):
    seen = []

    async def mcp_tool(url, headers, tool, args):
        seen.append((url, tool, "Bearer" in headers["Authorization"]))
        return {"content": [{"type": "text", "text": '{"balance": {"available": "10", "currency": "RUB"}}'}]}

    async def no_http(url, headers):
        raise AssertionError("OpenRouter без ключа не опрашивается")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("POLZA_MCP_KEY", raising=False)
    assert await _balance.fetch_balances(no_http, mcp_tool) == {}
    monkeypatch.setenv("POLZA_MCP_KEY", "test-do-not-use")
    out = await _balance.fetch_balances(no_http, mcp_tool)
    assert out["polza"]["balance"] == 10.0 and seen == [("https://polza.ai/api/mcp", "get_balance_details", True)]

    async def broken(url, headers, tool, args):
        raise RuntimeError("секрет в тексте ошибки")
    out = await _balance.fetch_balances(no_http, broken)
    assert "RuntimeError" in out["polza"]["error"] and "секрет" not in out["polza"]["error"]


def test_budget_account_is_the_sum_of_any_provider_balances_in_rubles():
    fx = {"USD": 1.0, "RUB": 90.0}
    balances = {"polza": {"currency": "RUB", "balance": 4969.5}, "openrouter": {"currency": "USD", "balance": 2.0},
                "zai": {"error": "нет"}}
    assert _balance.total_rub(balances, frozenset({"polza"}), fx) == 4969.5                 # один счёт
    assert _balance.total_rub(balances, frozenset({"polza", "openrouter"}), fx) == 4969.5 + 180.0   # 2 USD = 180 RUB
    assert _balance.total_rub(balances, frozenset({"zai"}), fx) is None                       # остатка нет: по долям месяца
    assert _balance.total_rub({}, frozenset({"polza"}), fx) is None
