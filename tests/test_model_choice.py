"""Оркестратор моделей агента (SAM-233): класс задачи, замеры скорости, профили, выбор."""
import pytest

from gateway import model_stats, task_class
from gateway.model_choice import choose
from gateway.model_profiles import ModelProfile, load_profiles
from gateway.pricing import Price, PriceBook

LOCAL = ModelProfile(tier=1, local=True, tools=True, context=16_000)
CHEAP = ModelProfile(tier=2, local=False, tools=True, context=1_000_000)
STRONG = ModelProfile(tier=3, local=False, tools=True, context=1_000_000)
PROFILES = {"lmstudio/local": LOCAL, "polza/cheap": CHEAP, "openrouter/strong": STRONG}
BOOK = PriceBook(prices={"polza/cheap": Price(5, 10, "RUB"), "openrouter/strong": Price(1, 5, "USD")},
                 fx_per_usd={"USD": 1.0, "RUB": 90.0})
EVERYONE = lambda source, model: True  # noqa: E731


def _row(source="polza", model="cheap", out=100, ms=1000):
    return {"source": source, "model": model, "output_tokens": out, "latency_ms": ms}


def test_speed_is_measured_by_long_answers_and_needs_enough_calls():
    rows = [_row(out=200, ms=2000)] * model_stats.MIN_CALLS + [_row(out=5, ms=5000)]   # короткий ответ скорость не мерит
    stat = model_stats.collect(rows)["polza/cheap"]
    assert stat.tokens_per_s == pytest.approx(100.0) and stat.calls == model_stats.MIN_CALLS
    assert model_stats.collect([_row()] * (model_stats.MIN_CALLS - 1)) == {}


def test_bundled_profiles_load_and_are_consistent_with_the_price_book():
    from gateway.pricing import load_price_book, price_for
    profiles, book = load_profiles(), load_price_book()
    assert profiles
    for key, profile in profiles.items():
        source, _, model = key.partition("/")
        assert profile.local or price_for(book.prices, source, model), f"у {key} нет цены в prices.json"


def test_profile_with_a_bad_tier_names_the_model(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text('{"models": {"a/x": {"tier": 9, "local": false, "tools": true, "context": 1}}}', encoding="utf-8")
    with pytest.raises(ValueError, match="a/x"):
        load_profiles(bad)


# ─── обучение по статистике запусков (скользящее окно) ───────────────────────────────────────────

from gateway import model_learning  # noqa: E402


def _run(model, kind, status="done", n=1, duration=10.0, cost=0.01, t0=0, source="polza"):
    return [{"source": source, "model": model, "kind": kind, "status": status, "finished_at": t0 + i,
             "duration_s": duration, "cost_usd": cost} for i in range(n)]


def test_learn_counts_success_rate_and_medians_over_the_window_only():
    old_failures = _run("cheap", "создать", status="failed", n=10, t0=0)
    recent_ok = _run("cheap", "создать", n=model_learning.WINDOW_RUNS, t0=1000)
    seen = model_learning.learn(old_failures + recent_ok)[("cheap", "создать")]
    assert seen.runs == model_learning.WINDOW_RUNS and seen.success_rate == 1.0      # старые провалы вышли из окна
    assert seen.median_seconds == 10.0 and seen.median_cost_usd == 0.01



# ─── тирлист и хранилище запусков ────────────────────────────────────────────────────────────────

from gateway import model_report  # noqa: E402


def test_tier_list_has_no_price_for_a_model_with_an_unknown_one():
    rows = model_report.tier_list({"polza/unpriced": CHEAP}, BOOK, {}, {}, {})
    assert rows[0]["price_usd_per_1m"] is None


def test_task_runs_repo_returns_newest_first_and_survives_no_database():
    import mongomock
    from gateway.repos.task_runs_repo import TaskRunsRepo
    repo = TaskRunsRepo(mongomock.MongoClient()["t"])
    repo.record({"task_id": "a", "finished_at": 1})
    repo.record({"task_id": "b", "finished_at": 2})
    assert [r["task_id"] for r in repo.recent()] == ["b", "a"] and repo.recent(limit=1)[0]["task_id"] == "b"
    assert TaskRunsRepo(None).recent() == []


# ─── рейтинг, голос как частный случай агента, флаг «только хост» ────────────────────────────────

from gateway.model_profiles import tier_for_rating  # noqa: E402


@pytest.mark.parametrize("rating, tier", [(55, 3), (44, 3), (43, 2), (38, 2), (37.9, 1), (10, 1)])
def test_tier_comes_from_the_public_rating_by_bands(rating, tier):
    assert tier_for_rating(rating) == tier


def test_bundled_profiles_hold_local_models_and_manual_fixes_only():
    profiles = load_profiles()
    assert profiles["lmstudio/qwen/qwen3-30b-a3b-2507"].local and profiles["lmstudio/qwen/qwen3-30b-a3b-2507"].tier == 1
    assert all(p.local or p.rating is None for p in profiles.values())   # рейтинг агрегаторов - из каталога


def test_profile_without_tier_and_rating_names_the_problem(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text('{"models": {"a/x": {"local": false, "tools": true, "context": 1}}}', encoding="utf-8")
    with pytest.raises(ValueError, match="не читаются"):
        load_profiles(bad)


# ─── каталоги агрегаторов ────────────────────────────────────────────────────────────────────────

from gateway import model_catalog  # noqa: E402

OR_PAYLOAD = {"data": [
    {"id": "z-ai/glm-5.3-flash", "context_length": 1048576, "supported_parameters": ["tools", "seed"],
     "pricing": {"prompt": "0.00000015", "completion": "0.0000005", "input_cache_read": "0.00000003"},
     "benchmarks": {"artificial_analysis": {"intelligence_index": 42.1}}},
    {"id": "no/price", "pricing": {}},
    {"id": "x/no-tools", "context_length": 1000, "supported_parameters": [], "pricing": {"prompt": "0", "completion": "0"}}]}
POLZA_PAYLOAD = {"data": [
    {"id": "z-ai/glm-5.3-flash", "context_length": 1048576,
     "top_provider": {"supported_parameters": ["tools"], "pricing": {"prompt_per_million": "4.76", "completion_per_million": "8.81",
                                                                     "input_cache_read_per_million": "3.43", "currency": "RUB"}}},
    {"id": "only/polza", "context_length": 10, "top_provider": {"pricing": {"prompt_per_million": "1", "completion_per_million": "2"}}}]}


def test_each_aggregator_catalog_parses_into_offers_with_price_context_tools_and_rating():
    orr = {o.model: o for o in model_catalog.parse_openrouter(OR_PAYLOAD)}
    assert set(orr) == {"z-ai/glm-5.3-flash", "x/no-tools"}                       # без цены - не предложение
    glm = orr["z-ai/glm-5.3-flash"]
    assert (glm.price.input, glm.price.output, glm.price.cached_input, glm.price.currency) == pytest.approx((0.15, 0.5, 0.03, "USD"))
    assert glm.rating == 42.1 and glm.tools and not orr["x/no-tools"].tools
    pz = {o.model: o for o in model_catalog.parse_polza(POLZA_PAYLOAD)}
    assert pz["z-ai/glm-5.3-flash"].price == Price(4.76, 8.81, "RUB", 3.43) and pz["z-ai/glm-5.3-flash"].tools
    assert pz["only/polza"].price.currency == "RUB" and not pz["only/polza"].tools


def test_same_model_at_two_sellers_shares_the_rating_and_only_listed_models_enter_the_pool():
    offers = model_catalog.share_ratings(model_catalog.parse_openrouter(OR_PAYLOAD) + model_catalog.parse_polza(POLZA_PAYLOAD))
    assert next(o for o in offers if o.source == "polza" and o.model == "z-ai/glm-5.3-flash").rating == 42.1
    overrides = {"lmstudio/local": LOCAL, "polza/only/polza": ModelProfile(tier=3, local=False, tools=True, context=10)}
    profiles, prices = model_catalog.to_profiles(
        offers, {"openrouter": ["z-ai/glm-5.3-flash"], "polza": ["z-ai/glm-5.3-flash", "only/polza"]}, overrides)
    assert set(profiles) == {"lmstudio/local", "openrouter/z-ai/glm-5.3-flash", "polza/z-ai/glm-5.3-flash", "polza/only/polza"}
    assert profiles["polza/z-ai/glm-5.3-flash"].tier == 2 and profiles["polza/z-ai/glm-5.3-flash"].rating == 42.1
    assert profiles["polza/only/polza"].tier == 3                                  # ручной профиль сильнее каталога
    assert prices["polza/z-ai/glm-5.3-flash"].currency == "RUB" and "lmstudio/local" not in prices
    unrated, _ = model_catalog.to_profiles(model_catalog.parse_polza(POLZA_PAYLOAD), {"polza": ["only/polza"]}, {})
    assert unrated["polza/only/polza"].tier == model_catalog.UNRATED_TIER

