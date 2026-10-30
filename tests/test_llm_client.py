"""Граница gateway <-> поставщики LLM (SAM-72, SAM-73): замена поставщика при ошибке, сроки, обрыв стрима.

Подменяется только клиент OpenAI SDK (`_openai_client`) и проверка LM Studio по сети; цикл замены, сроки,
остывание и сборка стрима - настоящие.
"""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from gateway import llm_client, llm_router

LMSTUDIO = {"model": "local-7b", "model_server": "http://lm/v1", "api_key": "x", "source": "lmstudio"}
MAX_CALLS = 6  # больше вызовов - значит make_llm_call крутится по кругу на одном поставщике
SHORT_TIMEOUT_S = 0.05  # сроки в тестах короткие; «молчание» поставщика ждёт события без срока


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(llm_client, "_cooldowns", {})
    monkeypatch.setattr(llm_client, "_cooldown_hits", {})
    monkeypatch.setattr(llm_client, "_adapter", "openai")
    monkeypatch.setattr(llm_client, "_parse_fail_streak", 0)
    monkeypatch.setattr(llm_client, "CALL_TIMEOUT_S", SHORT_TIMEOUT_S)
    monkeypatch.setattr(llm_client, "FIRST_TOKEN_TIMEOUT_S", SHORT_TIMEOUT_S)
    monkeypatch.setattr(llm_client, "DELTA_TIMEOUT_S", SHORT_TIMEOUT_S)
    for var in ("LLM_SOURCE", "GEMINI_API_KEY", "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY", "ZAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    async def _lm(*a, **kw):
        return dict(LMSTUDIO)
    monkeypatch.setattr(llm_router, "_check_lmstudio", _lm)


# ─── Поставщик: клиент OpenAI SDK по адресу сервера ──────────────────────────

def _chunk(text):
    delta = SimpleNamespace(content=text, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)])


def _http_error(status):
    err = RuntimeError(f"HTTP {status}")
    err.status_code = status
    return err


async def _forever():
    """Поставщик молчит: событие никто не ставит, срок шлюза обязан сработать."""
    await asyncio.Event().wait()


class _Stream:
    """Стрим из готовых дельт; после них конец, молчание или обрыв с 500."""

    def __init__(self, pieces, then="end"):
        self._pieces = list(pieces)
        self._then = then

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._pieces:
            return _chunk(self._pieces.pop(0))
        if self._then == "silence":
            await _forever()
        if self._then == "break":
            raise _http_error(500)
        raise StopAsyncIteration


def ok():
    return "ok"


def fail(status):
    def _f():
        raise _http_error(status)
    return _f


def silent():
    return _forever()


def stream(pieces, then="end"):
    return lambda: _Stream(pieces, then)


SERVERS = {LMSTUDIO["model_server"]: "lmstudio",
           "https://generativelanguage.googleapis.com/v1beta/openai": "gemini"}


class _Provider:
    """chat.completions.create по источнику (адрес сервера из настоящего роутера); журнал источников и запросов."""

    def __init__(self, by_source, others):
        self.by_source = by_source
        self.others = others
        self.calls: list[str] = []
        self.kwargs: list[dict] = []

    def client(self, base_url, api_key):
        source = SERVERS.get(base_url, "dashscope")   # dashscope - последний в цепочке роутера

        async def create(**kwargs):
            self.calls.append(source)
            self.kwargs.append(kwargs)
            if len(self.calls) > MAX_CALLS:
                raise AssertionError(f"make_llm_call зациклился: {self.calls}")
            result = self.by_source.get(source, self.others)()
            return await result if asyncio.iscoroutine(result) else result
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _provider(monkeypatch, others=None, **by_source) -> _Provider:
    """others - поведение источников, не названных явно (по умолчанию - как у lmstudio)."""
    provider = _Provider(by_source, others or by_source["lmstudio"])
    monkeypatch.setattr(llm_client, "_openai_client", provider.client)
    return provider


# ─── Флоу ────────────────────────────────────────────────────────────────────

# Паузы поставщика по llm-router.md: 401/403 - час, 5xx и молчание (504) - 10 с, первый 429 подряд - 2 с
AUTH_PAUSE_S, SERVER_PAUSE_S, FIRST_429_PAUSE_S = 3600, 10, llm_client._429_BACKOFFS[0]
PAUSE_SLACK_S = 1   # пауза отсчитана от момента сбоя, проверка идёт чуть позже
# Часы Windows грубые (около 15 мс): сбой и проверка в один тик дают (t + 3600) - t чуть больше 3600 от округления
FLOAT_EPS_S = 1e-6
FAILS = [pytest.param({"lmstudio": fail(s)}, False, False, None, ["lmstudio", "dashscope"],
                      {"lmstudio": pause, "dashscope": pause}, id=f"{s}-no-retry")
         for s, pause in ((403, AUTH_PAUSE_S), (500, SERVER_PAUSE_S), (503, SERVER_PAUSE_S), (429, FIRST_429_PAUSE_S))]


@pytest.mark.parametrize("by_source, gemini, streamed, answer, calls, cooled", FAILS + [
    pytest.param({"lmstudio": fail(500), "gemini": ok}, True, False, "ok", ["lmstudio", "gemini"],
                 {"lmstudio": SERVER_PAUSE_S}, id="500-goes-to-gemini"),
    pytest.param({"lmstudio": silent, "gemini": ok}, True, False, "ok", ["lmstudio", "gemini"],
                 {"lmstudio": SERVER_PAUSE_S}, id="silence-times-out-to-fallback"),
    pytest.param({"lmstudio": stream([], "silence")}, False, True, None, ["lmstudio", "dashscope"],
                 {"lmstudio": SERVER_PAUSE_S}, id="stream-without-first-token"),
    pytest.param({"lmstudio": stream(["При"], "silence")}, False, True, None, ["lmstudio", "dashscope"],
                 {"lmstudio": SERVER_PAUSE_S}, id="stream-silent-between-deltas"),
    pytest.param({"lmstudio": stream(["При"], "break")}, False, True, None, ["lmstudio", "dashscope"],
                 {"lmstudio": SERVER_PAUSE_S}, id="stream-break-cools-the-real-source"),
])
async def test_provider_failure_falls_back_without_looping(monkeypatch, by_source, gemini, streamed, answer,
                                                           calls, cooled):
    """Сбой поставщика - замена без круга и пауза отказавшего той длины, что назначена его коду."""
    if gemini:
        monkeypatch.setenv("GEMINI_API_KEY", "test-do-not-use")
    provider = _provider(monkeypatch, **by_source)
    on_delta = (lambda t: None) if streamed else None

    if answer is None:
        with pytest.raises(RuntimeError, match="недоступны"):
            await llm_client.make_llm_call(dict(LMSTUDIO), on_delta=on_delta, messages=[])
    else:
        assert await llm_client.make_llm_call(dict(LMSTUDIO), on_delta=on_delta, messages=[]) == answer
    assert provider.calls == calls
    now = llm_client.time.monotonic()
    left = {source: llm_client._cooldowns.get(source, now) - now for source in cooled}
    assert all(pause - PAUSE_SLACK_S < left[source] <= pause + FLOAT_EPS_S for source, pause in cooled.items()), left
    assert "stream" not in llm_client._cooldowns


async def test_success_resets_rate_limit_escalation(monkeypatch):
    """429 после успешного ответа - снова первая, короткая пауза: счёт 429 подряд обнулён ответом поставщика."""
    script = [fail(429), ok, fail(429)]
    _provider(monkeypatch, lmstudio=lambda: script.pop(0)(), others=ok)
    for _ in range(len(script)):
        llm_client._cooldowns.clear()                    # пауза прошлого 429 истекла
        assert await llm_client.make_llm_call(dict(LMSTUDIO), messages=[]) == "ok"
    assert llm_client._cooldown_hits.get("lmstudio") == 1
    left_s = llm_client._cooldowns["lmstudio"] - llm_client.time.monotonic()
    assert left_s <= llm_client._429_BACKOFFS[0]


@pytest.mark.parametrize("broken, seen_expected", [
    pytest.param(["При"], ["При", "<reset>", "Привет"], id="break-after-delta-resets"),
    pytest.param([], ["Привет"], id="break-before-delta-no-reset"),
])
async def test_stream_retry_resets_what_was_shown(monkeypatch, broken, seen_expected):
    monkeypatch.setenv("GEMINI_API_KEY", "test-do-not-use")
    _provider(monkeypatch, lmstudio=stream(broken, "break"), gemini=stream(["Привет"]))
    seen: list[str] = []

    answer = await llm_client.make_llm_call(dict(LMSTUDIO), on_delta=seen.append,
                                            on_reset=lambda: seen.append("<reset>"), messages=[])
    assert seen == seen_expected
    assert answer.choices[0].message.content == "Привет"


TOOLS = [{"type": "function", "function": {"name": "radar_where", "parameters": {"type": "object"}}}]


@pytest.mark.parametrize("adapter, prompt_format, native", [
    pytest.param("openai", "standard", True, id="openai-native"),
    pytest.param("custom", "standard", True, id="custom-native"),
    pytest.param("openai", "harmony", False, id="harmony-text-only"),  # gpt-oss зовёт инструменты текстом
])
async def test_tools_go_in_the_format_the_model_understands(monkeypatch, adapter, prompt_format, native):
    monkeypatch.setattr(llm_client, "_adapter", adapter)
    provider = _provider(monkeypatch, lmstudio=ok)

    await llm_client.make_llm_call({**LMSTUDIO, "prompt_format": prompt_format}, messages=[], tools=TOOLS,
                                   tool_choice="auto")
    sent = provider.kwargs[0]
    if native:
        assert sent["tools"] == TOOLS
    else:
        assert "tools" not in sent and "tool_choice" not in sent


def test_auto_adapter_falls_back_to_text_format_after_parse_failures():
    llm_client.set_adapter("auto")
    try:
        assert llm_client._effective_adapter(dict(LMSTUDIO)) == "openai"
        for _ in range(llm_client.PARSE_FAIL_THRESHOLD):
            llm_client.inc_parse_fail()
        assert llm_client._effective_adapter(dict(LMSTUDIO)) == "custom"
        with pytest.raises(ValueError, match="openai"):
            llm_client.set_adapter("bogus")
    finally:
        llm_client.reset_parse_fail()


def _overflow(status):
    def _f():
        err = RuntimeError("This model's maximum context length is 4096 tokens, input too long")
        err.status_code = status
        raise err
    return _f


@pytest.mark.parametrize("status", [400, 500], ids=["400", "500"])
async def test_context_overflow_goes_to_the_caller_without_switch_or_pause(monkeypatch, status):
    """llm-router 17: контекст не влез - ошибка вызывающему, другой поставщик не зовётся, паузы нет: ход урезает
    контекст, а не перебирает цепочку."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-do-not-use")
    provider = _provider(monkeypatch, lmstudio=_overflow(status), gemini=ok)

    with pytest.raises(Exception, match="context length"):
        await llm_client.make_llm_call(dict(LMSTUDIO), messages=[])
    assert provider.calls == ["lmstudio"] and llm_client._cooldowns == {}


# ─── фаза 5: P3 ───────────────────────────────────────────────────────────────

async def test_rate_limit_pause_grows_with_each_429_in_a_row(monkeypatch):
    """llm-router 14: 429 подряд - пауза 2, 5, 10, 20 с и дальше 20; замена отвечает сразу."""
    pauses = []
    for _ in range(5):
        provider = _provider(monkeypatch, lmstudio=fail(429), others=ok)
        llm_client._cooldowns.clear()                    # пауза прошлого 429 истекла
        assert await llm_client.make_llm_call(dict(LMSTUDIO), messages=[]) == "ok"
        assert provider.calls == ["lmstudio", "dashscope"]
        pauses.append(llm_client._cooldowns["lmstudio"] - llm_client.time.monotonic())
    expected = [2, 5, 10, 20, 20]
    assert all(p - PAUSE_SLACK_S < left <= p + FLOAT_EPS_S for p, left in zip(expected, pauses)), pauses


async def test_other_provider_error_pauses_a_minute_and_skips_the_source(monkeypatch):
    """llm-router 18: прочая ошибка поставщика - пауза 60 с, до конца вызова источник пропускается, замена."""
    provider = _provider(monkeypatch, lmstudio=ok, others=ok)

    async def teapot(cfg, **kw):
        raise llm_client.LLMProviderError("lmstudio", 418, "I'm a teapot")
    monkeypatch.setitem(llm_client._source_calls, "lmstudio", teapot)
    assert await llm_client.make_llm_call(dict(LMSTUDIO), messages=[]) == "ok"
    assert provider.calls == ["dashscope"]
    left = llm_client._cooldowns["lmstudio"] - llm_client.time.monotonic()
    assert OTHER_PAUSE_S - PAUSE_SLACK_S < left <= OTHER_PAUSE_S + FLOAT_EPS_S


OTHER_PAUSE_S = 60
QUEUE_HOLD_S = SHORT_TIMEOUT_S * 6   # очередь к месту в разы дольше срока ответа


async def test_answer_deadline_starts_after_the_queue_for_a_slot(monkeypatch):
    """llm-router 21: срок ответа считается после очереди к местам - ожидание своих же вызовов не молчание
    поставщика: поставщик не остывает и не заменяется."""
    from gateway.llm_gate import PriorityGate
    monkeypatch.setattr(llm_client, "_llm_sem", PriorityGate(1))
    provider = _provider(monkeypatch, lmstudio=ok, others=fail(500))
    gate = llm_client._get_sem()
    async with gate:                                   # место занято другим вызовом
        call = asyncio.ensure_future(llm_client.make_llm_call(dict(LMSTUDIO), messages=[]))
        await asyncio.sleep(QUEUE_HOLD_S)
        assert not call.done()
    assert await call == "ok"
    assert provider.calls == ["lmstudio"] and llm_client._cooldowns == {}


async def test_litellm_adapter_calls_litellm_with_the_provider_prefix_and_falls_back(monkeypatch):
    """agent-loop 4: LLM_ADAPTER=litellm - вызов идёт через litellm.acompletion: модель с префиксом поставщика,
    адрес и ключ из конфига роутера, пустой список tools не передаётся; 429 от litellm - замена поставщика, как у
    openai-адаптера. Подменена сама библиотека (граница поставщика)."""
    sent = []

    async def acompletion(**kwargs):
        sent.append(kwargs)
        if len(sent) == 1:
            err = RuntimeError("rate limited")
            err.status_code = 429
            raise err
        return "ok"
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    monkeypatch.setattr(llm_client, "_adapter", "litellm")
    assert await llm_client.make_llm_call(dict(LMSTUDIO), messages=[{"role": "user", "content": "x"}], tools=[],
                                          tool_choice="auto") == "ok"
    first = sent[0]
    assert (first["model"], first["api_base"], first["api_key"]) == ("openai/local-7b", "http://lm/v1", "x")
    assert "tools" not in first and "tool_choice" not in first and first["messages"][0]["content"] == "x"
    assert sent[1]["model"].startswith("openai/") and "lmstudio" in llm_client._cooldowns


async def test_fallback_to_gemini_drops_the_repeat_penalties_of_the_first_provider(monkeypatch):
    """27 окт: zai ответил 429, ход ушёл на Gemini с frequency_penalty из параметров zai, Gemini отвечал 400."""
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    provider = _provider(monkeypatch, lmstudio=fail(429), gemini=ok)
    penalties = {"frequency_penalty": 0.4, "presence_penalty": 0.2}
    assert await llm_client.make_llm_call(dict(LMSTUDIO), messages=[], **penalties) == "ok"
    assert provider.calls == ["lmstudio", "gemini"]
    assert all(k in provider.kwargs[0] for k in penalties)
    assert not any(k in provider.kwargs[1] for k in penalties)


async def test_failed_model_goes_to_the_next_one_of_the_orchestrator_ranking(monkeypatch):
    """SAM-233: у конфига агента список приоритетов оркестратора; первая модель отказала - берётся следующая из списка,
    остывший источник в нём пропускается без проб, а не общий выбор роутера."""
    called = []

    async def fake_source_config(source, model=""):
        return {"source": source, "model": model, "model_server": f"http://{source}/v1", "api_key": "x"}
    monkeypatch.setattr(llm_router, "source_config", fake_source_config)

    async def by_source(cfg, **kw):
        called.append((cfg["source"], cfg["model"]))
        if cfg["source"] == "polza" and cfg["model"] == "first":
            raise llm_client.LLMProviderError("polza", 500, "boom")
        return "ok"
    for source in ("polza", "openrouter", "zai"):
        monkeypatch.setitem(llm_client._source_calls, source, by_source)
    first = {"source": "polza", "model": "first", "model_server": "http://polza/v1", "api_key": "x",
             "fallbacks": [{"source": "openrouter", "model": "dead"}, {"source": "zai", "model": "alive"}]}
    monkeypatch.setitem(llm_client._cooldowns, "openrouter", llm_client.time.monotonic() + 3600)
    assert await llm_client.make_llm_call(first, messages=[]) == "ok"
    assert called == [("polza", "first"), ("zai", "alive")]


async def test_server_error_of_one_model_goes_to_the_next_model_of_the_same_source(monkeypatch):
    """SAM-261: 504 у одной модели агрегатора не молчит SAM: берётся следующая модель того же источника, хотя источник
    ушёл в паузу; на 403 (ключ) источник закрыт целиком и запасные его моделей пропускаются."""
    async def fake_source_config(source, model=""):
        return {"source": source, "model": model, "model_server": f"http://{source}/v1", "api_key": "x"}
    monkeypatch.setattr(llm_router, "source_config", fake_source_config)
    called = []

    async def by_model(cfg, **kw):
        called.append(cfg["model"])
        if cfg["model"] == "slow":
            raise llm_client.LLMProviderError("polza", 504, "gateway timeout")
        if cfg["model"] == "locked":
            raise llm_client.LLMProviderError("polza", 403, "forbidden")
        return "ok"
    monkeypatch.setitem(llm_client._source_calls, "polza", by_model)
    base = {"model_server": "http://polza/v1", "api_key": "x", "source": "polza",
            "fallbacks": [{"source": "polza", "model": "alive"}]}
    assert await llm_client.make_llm_call({**base, "model": "slow"}, messages=[]) == "ok"
    assert called == ["slow", "alive"]
    called.clear()
    monkeypatch.setitem(llm_client._cooldowns, "polza", 0.0)
    llm_client._cooldowns.pop("polza", None)
    async def best(*a, **k):
        return None
    monkeypatch.setattr(llm_router, "get_best_llm", best)
    with pytest.raises(RuntimeError):
        await llm_client.make_llm_call({**base, "model": "locked"}, messages=[])
    assert called == ["locked"]                     # 403: запасная того же источника не пробуется



# ─── Обрыв потока стражем разноса (SAM-246) ──────────────────────────────────

class _ClosableStream(_Stream):
    def __init__(self, pieces):
        super().__init__(pieces)
        self.closed = False

    async def close(self):
        self.closed = True


async def test_stream_is_cut_when_the_guard_fires_and_the_provider_stream_is_closed():
    stream = _ClosableStream(["раз ", "два ", "СТОП ", "лишнее"])
    seen = []
    sign = object()
    result = await llm_client._stream_collect(stream, seen.append, stop_when=lambda piece: sign if "СТОП" in piece else None)
    assert result.choices[0].message.content == "раз два СТОП "
    assert result.choices[0].finish_reason == llm_client.RUNAWAY_FINISH
    assert result.runaway is sign and stream.closed
    assert seen == ["раз ", "два ", "СТОП "]


async def test_stream_without_a_guard_sign_runs_to_the_end():
    result = await llm_client._stream_collect(_Stream(["а", "б"]), lambda p: None, stop_when=lambda p: None)
    assert result.choices[0].message.content == "аб" and result.runaway is None


def test_agent_calls_use_the_agents_key_of_the_source_and_others_the_main_one():
    cfg = {"source": "polza", "api_key": "main"}
    env = {"POLZA_AGENTS_API_KEY": "agents"}
    assert llm_client.api_key_for(cfg, "worker", env) == "agents"
    assert llm_client.api_key_for(cfg, "main", env) == "main" and llm_client.api_key_for(cfg, "guard", env) == "main"
    assert llm_client.api_key_for(cfg, "worker", {}) == "main"                          # ключа агентов нет - основной
    assert llm_client.api_key_for({"source": "zai", "api_key": "z"}, "worker", env) == "z"
