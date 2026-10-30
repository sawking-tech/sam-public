"""Выбор LLM (gateway/llm_router.py): модель LM Studio по id (SAM-57), разбор вызовов из текста, z.ai по ключу,
быстрая модель голоса (SAM-56, SAM-130, SAM-170).

LM Studio подменяется на уровне сети (httpx.MockTransport), остальной путь роутера настоящий.
"""
import httpx
import pytest

from gateway import llm_router as lr

FAST = lr.FAST_LMSTUDIO_MODEL


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("LLM_SOURCE", "LLM_FAST_SOURCE", "LLM_FAST_MODEL", "LMSTUDIO_MODEL", "LMSTUDIO_URL", "GEMINI_API_KEY",
                "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY", "ZAI_API_KEY", "SAM_AGENT_BACKEND", "LLM_TEXT_TOOLS",
                lr.SERVICE_SOURCE_ENV, lr.SERVICE_MODEL_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(lr, "_lmstudio_error", "")


def _lmstudio_serves(monkeypatch, ids, loaded=()):
    """LM Studio по сети: /v1/models отдаёт ids, /api/v0/models/<id> - загружена ли модель."""
    def _handler(request):
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        model = path.removeprefix("/api/v0/models/")
        return httpx.Response(200, json={"state": lr.LMSTUDIO_LOADED if model in loaded else "not-loaded"})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_handler), **kw))


@pytest.mark.parametrize("ids, wanted, model, error_has", [
    pytest.param(["a-7b", "glm-4.6-air"], "glm-4.6-air", "glm-4.6-air", [], id="wanted-id-taken"),
    pytest.param(["a-7b", "b-9b"], "glm-4.6-air", None, ["glm-4.6-air", "a-7b"], id="wanted-missing-no-substitute"),
    pytest.param(["only-14b"], "", "only-14b", [], id="single-without-env"),
    pytest.param(["b-9b", "a-7b"], "", None, ["LMSTUDIO_MODEL"], id="several-without-env"),
    pytest.param([], "", None, [], id="no-models"),
])
def test_pick_lmstudio_model_by_id(ids, wanted, model, error_has):
    picked, err = lr.pick_lmstudio_model(ids, wanted)
    assert picked == model
    assert all(part in err for part in error_has) and bool(err) == bool(error_has)


@pytest.mark.parametrize("ids, wanted, model, prompt_format, error_has", [
    pytest.param(["first-7b", "glm-4.6-air-14b"], "glm-4.6-air-14b", "glm-4.6-air-14b", "glm", "",
                 id="env-id-not-first"),
    pytest.param(["first-7b"], "glm-4.6-air-14b", None, None, "glm-4.6-air-14b", id="missing-id-skips-and-reports"),
])
async def test_lmstudio_model_is_chosen_by_id_or_reported(monkeypatch, ids, wanted, model, prompt_format, error_has):
    _lmstudio_serves(monkeypatch, ids)
    monkeypatch.setenv("LMSTUDIO_MODEL", wanted)
    monkeypatch.setenv("LLM_SOURCE", "lmstudio")

    cfg = await lr.source_config("lmstudio")
    if model is None:
        assert cfg is None
    else:
        assert (cfg["model"], cfg["prompt_format"]) == (model, prompt_format)
    assert error_has in lr.lmstudio_error() and bool(lr.lmstudio_error()) == bool(error_has)


@pytest.mark.parametrize("env, prompt_format, expected", [
    pytest.param("0", "glm", False, id="off-by-env"),
    pytest.param("0", "harmony", True, id="harmony-always-text"),   # gpt-oss без нативных tools
    pytest.param("0", "qwen", False, id="qwen-follows-env"),          # qwen получает нативные tools
    pytest.param(None, "glm", True, id="on-by-default"),
])
async def test_text_tool_parsing_follows_env_and_format(monkeypatch, env, prompt_format, expected):
    if env is not None:
        monkeypatch.setenv("LLM_TEXT_TOOLS", env)
    model = {"glm": "glm-4.6-air-14b", "harmony": "gpt-oss-20b", "qwen": "qwen3-30b"}[prompt_format]
    _lmstudio_serves(monkeypatch, [model])

    cfg = await lr.source_config("lmstudio")
    assert cfg["prompt_format"] == prompt_format and cfg["text_tools"] is expected


@pytest.mark.parametrize("key", ["test-do-not-use", None], ids=["key", "no-key"])
async def test_provider_with_key_in_env_is_available(monkeypatch, key):
    if key:
        monkeypatch.setenv("ZAI_API_KEY", key)
    monkeypatch.setenv("LLM_SOURCE", "zai")
    _lmstudio_serves(monkeypatch, [])

    cfg = await lr.get_best_llm()
    assert ("zai" in lr.get_available_sources()) is bool(key)
    if key:
        assert (cfg["source"], cfg["api_key"], cfg["model_server"]) == ("zai", key, lr.ZAI_BASE_URL)
        assert "extra_body" not in cfg   # рассуждение по роли - gateway/reasoning.py (SAM-226)
    else:
        assert cfg is None or cfg["source"] != "zai"


@pytest.mark.parametrize("main_source, fast_route", [
    pytest.param("lmstudio", False, id="same-model-is-main-answer"),
    pytest.param("zai", True, id="other-main-keeps-fast-route"),
])
async def test_fast_route_equal_to_main_model_is_the_main_answer(monkeypatch, main_source, fast_route):
    # SAM-170: после switch_main_model на ту же qwen, что у быстрой, ответ - основной, fast-review не нужен
    _lmstudio_serves(monkeypatch, [FAST], loaded=[FAST])
    monkeypatch.setenv("ZAI_API_KEY", "test-do-not-use")
    monkeypatch.setenv("LLM_SOURCE", main_source)

    cfg = await lr.get_best_llm(task="fast")
    assert cfg["model"] == FAST
    assert (cfg.get(lr.ROUTE_KEY) == lr.ROUTE_FAST) is fast_route


@pytest.mark.parametrize("fast_env, ids, loaded", [
    pytest.param({}, [FAST], [], id="fast-downloaded-not-loaded"),
    pytest.param({}, [], [], id="lmstudio-without-fast"),
    pytest.param({"LLM_FAST_SOURCE": "off"}, [FAST], [FAST], id="fast-off"),
])
async def test_without_a_ready_fast_model_the_main_answers(monkeypatch, fast_env, ids, loaded):
    """fast-voice 2, llm-router 1: быстрой нет, она скачана, но не загружена, или выключена - маршрут fast отдаёт
    основную без метки быстрой; голосовой ход не ждёт загрузку модели в LM Studio."""
    _lmstudio_serves(monkeypatch, ids, loaded=loaded)
    monkeypatch.setenv("ZAI_API_KEY", "test-do-not-use")
    monkeypatch.setenv("LLM_SOURCE", "zai")
    for name, value in fast_env.items():
        monkeypatch.setenv(name, value)

    cfg = await lr.get_best_llm(task="fast")
    assert cfg["source"] == "zai" and lr.ROUTE_KEY not in cfg


@pytest.mark.parametrize("task, keys, lmstudio, source", [
    pytest.param("planning", {}, ["qwen3-30b"], "lmstudio", id="planning-local-before-cli"),
    pytest.param("planning", {"GEMINI_API_KEY": "k"}, [], "gemini", id="planning-gemini-before-cli"),
    pytest.param("planning", {}, [], "claude_code", id="planning-cli-when-nothing-else"),
    pytest.param("default", {}, [], "dashscope", id="default-never-cli"),
])
async def test_claude_code_cli_is_reached_only_after_the_free_providers(monkeypatch, task, keys, lmstudio, source):
    """llm-router 5, 10: Claude Code CLI тратит лимиты подписки - в цепочке planning он после Anthropic, Gemini и
    LM Studio, в обычной цепочке его нет. Доступность CLI - граница процесса, подменена."""
    _lmstudio_serves(monkeypatch, lmstudio)
    monkeypatch.setattr(lr, "_claude_code_available", lambda: True)
    for name, value in keys.items():
        monkeypatch.setenv(name, value)

    assert (await lr.get_best_llm(task=task))["source"] == source


# ─── фаза 5: P3 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("keys, lmstudio, cli, skip, source", [
    pytest.param({"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g"}, ["qwen3-30b"], True, set(), "anthropic",
                 id="anthropic-first"),
    pytest.param({"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g"}, ["qwen3-30b"], True, {"anthropic"}, "gemini",
                 id="gemini-after-anthropic"),
    pytest.param({}, [], False, set(), "dashscope", id="dashscope-last"),
    pytest.param({}, [], True, {"claude_code"}, "dashscope", id="dashscope-after-cli"),
])
async def test_planning_chain_goes_anthropic_gemini_lmstudio_cli_dashscope(monkeypatch, keys, lmstudio, cli, skip,
                                                                         source):
    """llm-router 5: цепочка planning - Anthropic, Gemini, LM Studio, Claude Code CLI, DashScope; упавший в вызове
    поставщик (skip) пропускается."""
    _lmstudio_serves(monkeypatch, lmstudio)
    monkeypatch.setattr(lr, "_claude_code_available", lambda: cli)
    for name, value in keys.items():
        monkeypatch.setenv(name, value)
    assert (await lr.get_best_llm(task="planning", skip_sources=skip))["source"] == source


@pytest.mark.parametrize("model_id, chosen", [
    ("qwen3-1.7b", False), ("gemma-3-1b-it", False), ("qwen3-4b", True), ("qwen3-30b-a3b", True),
    ("mistral-nemo-instruct", True),            # размер не назван - не пропускается
])
async def test_lmstudio_model_under_four_billion_is_skipped(monkeypatch, model_id, chosen):
    """llm-router 12: модель LM Studio меньше 4 млрд параметров по имени пропускается, цепочка идёт дальше."""
    _lmstudio_serves(monkeypatch, [model_id])
    cfg = await lr.get_best_llm()
    assert (cfg["source"] == "lmstudio") is chosen and (cfg["model"] == model_id) is chosen


async def test_zai_is_not_in_the_auto_chain_without_llm_source(monkeypatch):
    """llm-router 9: ключ z.ai сам по себе поставщика в авто-цепочку не ставит - только LLM_SOURCE или
    LLM_FAST_SOURCE."""
    monkeypatch.setenv("ZAI_API_KEY", "test-do-not-use")
    _lmstudio_serves(monkeypatch, [])
    monkeypatch.setattr(lr, "_claude_code_available", lambda: True)
    for task in ("default", "planning"):
        chain, skip = [], set()
        for _ in lr.SOURCES:                            # цепочка не длиннее списка поставщиков
            cfg = await lr.get_best_llm(task=task, skip_sources=skip)
            if cfg is None or cfg["source"] in skip:
                break
            chain.append(cfg["source"])
            skip.add(cfg["source"])
        assert "zai" not in chain and chain, task


@pytest.mark.parametrize("loaded, source, model", [
    pytest.param([FAST, "main-14b"], "", FAST, id="local-qwen-loaded"),
    pytest.param(["main-14b"], "", "main-14b", id="qwen-not-loaded-falls-back-to-main"),
    pytest.param([FAST, "main-14b"], "off", "main-14b", id="off-uses-main"),
])
async def test_service_llm_for_history_calls(monkeypatch, loaded, source, model):
    """SAM-226: сводки истории - на загруженной локальной qwen; не загружена или off - основная модель."""
    monkeypatch.setenv("LMSTUDIO_MODEL", "main-14b")
    if source:
        monkeypatch.setenv(lr.SERVICE_SOURCE_ENV, source)
    _lmstudio_serves(monkeypatch, [FAST, "main-14b"], loaded=loaded)
    cfg = await lr.get_service_llm()
    assert (cfg["source"], cfg["model"]) == ("lmstudio", model)



async def test_lmstudio_check_is_cached_for_seconds(monkeypatch):
    """ADR-027: голосовой ход не спрашивает LM Studio на каждом обращении - проверка в кэше SAM_LMSTUDIO_CACHE_S."""
    hits = []
    real = httpx.AsyncClient

    def _handler(request):
        hits.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": FAST}]})
        return httpx.Response(200, json={"state": lr.LMSTUDIO_LOADED})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(_handler), **kw))
    clock = [100.0]
    monkeypatch.setattr(lr.time, "monotonic", lambda: clock[0])
    first = await lr._check_lmstudio(FAST, loaded_only=True)
    again = await lr._check_lmstudio(FAST, loaded_only=True)
    assert first == again and first["model"] == FAST and len(hits) == 2
    clock[0] += lr.LMSTUDIO_CACHE_S + 1
    await lr._check_lmstudio(FAST, loaded_only=True)
    assert len(hits) == 4


@pytest.mark.parametrize("source, key_env, model_env, url", [
    ("openrouter", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "https://openrouter.ai/api/v1"),
    ("polza", "POLZA_API_KEY", "POLZA_MODEL", "https://polza.ai/api/v1"),
], ids=["openrouter", "polza"])
async def test_aggregator_needs_key_and_model_and_is_not_in_the_auto_chain(monkeypatch, source, key_env, model_env, url):
    """Агрегатор: ключ и модель из окружения (имя модели в коде устарело бы); без модели - None; в авто-цепочку не ставится."""
    _lmstudio_serves(monkeypatch, [])
    for var in (key_env, model_env, f"{model_env}S"):    # настоящие ключи и модели из .env тесту не нужны
        monkeypatch.delenv(var, raising=False)
    assert await lr.source_config(source) is None and source not in lr.get_available_sources()

    monkeypatch.setenv(key_env, "test-do-not-use")
    assert source in lr.get_available_sources()
    assert await lr.source_config(source) is None            # ключ есть, модели нет

    monkeypatch.setenv(model_env, "vendor/some-model")
    cfg = await lr.source_config(source)
    assert (cfg["source"], cfg["model"], cfg["model_server"], cfg["api_key"]) == (source, "vendor/some-model", url, "test-do-not-use")
    assert source in lr.SOURCES

    monkeypatch.setattr(lr, "_claude_code_available", lambda: True)
    for task in ("default", "planning"):
        chain, skip = [], set()
        for _ in lr.SOURCES:
            nxt = await lr.get_best_llm(task=task, skip_sources=skip)
            if nxt is None or nxt["source"] in skip:
                break
            chain.append(nxt["source"])
            skip.add(nxt["source"])
        assert source not in chain, task
