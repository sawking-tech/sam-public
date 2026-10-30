"""Расход токенов (SAM-48) и трасса хода (SAM-81): usage каждого вызова LLM с сессией и ролью, агрегат по сессии,
этапы хода в записи вызова. Подменяется только клиент OpenAI SDK, путь make_llm_call -> приёмник настоящий."""
from types import SimpleNamespace

import pytest

from gateway import llm_client, llm_monitor, token_usage
from gateway.repos import TokenUsageRepo
from tests.fakes_core import FakeOpenAI, FakeStream, chunk

CFG = {"model": "glm-test", "source": "zai"}


def _resp(prompt, completion, **usage_extra):
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                            total_tokens=prompt + completion, **usage_extra)
    msg = SimpleNamespace(content="ok", tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=usage)


@pytest.fixture
def sink():
    records: list[dict] = []
    token_usage.set_sink(records.append)
    yield records
    token_usage.set_sink(None)


@pytest.fixture
def provider(monkeypatch):
    """Поставщик отвечает по очереди: provider.answers.append(...)."""
    fake = FakeOpenAI()
    monkeypatch.setattr(llm_client, "_openai_client", fake.client)
    monkeypatch.setattr(llm_client, "_adapter", "openai")
    return fake


def _usage_stream():
    """Дельта и последний чанк с usage, как при stream_options.include_usage."""
    return FakeStream([chunk("При"), SimpleNamespace(
        choices=[], usage=SimpleNamespace(prompt_tokens=50, completion_tokens=2, total_tokens=52))])


# Ответ coding-эндпоинта z.ai 9 окт: второй одинаковый вызов - 1152 из 1171 токенов входа из кэша
CACHED = {"usage": {"prompt_tokens": 1171, "completion_tokens": 76, "total_tokens": 1247,
                    "prompt_tokens_details": {"cached_tokens": 1152}}}


@pytest.mark.parametrize("answer, streamed, scoped, tokens, cached", [
    pytest.param(_resp(120, 30), False, True, (120, 30, 150), None, id="usage-object"),
    pytest.param(CACHED, False, True, (1171, 76, 1247), 1152, id="usage-dict-with-cache"),
    pytest.param(SimpleNamespace(choices=[]), False, False, (None, None, None), None, id="no-usage-outside-scope"),
    pytest.param(_usage_stream, True, True, (50, 2, 52), None, id="stream-usage-from-last-chunk"),
])
async def test_call_records_usage_with_session_and_role(sink, provider, answer, streamed, scoped, tokens, cached):
    provider.answers.append(answer)
    on_delta = (lambda t: None) if streamed else None
    if scoped:
        with token_usage.usage_scope("voice", token_usage.ROLE_MAIN):
            await llm_client.make_llm_call(dict(CFG), on_delta=on_delta, messages=[])
    else:
        await llm_client.make_llm_call(dict(CFG), on_delta=on_delta, messages=[])

    [rec] = sink
    who = ("voice", token_usage.ROLE_MAIN) if scoped else (token_usage.SESSION_UNKNOWN, token_usage.ROLE_UNKNOWN)
    assert (rec["session_id"], rec["role"]) == who
    assert (rec["source"], rec["model"]) == ("zai", "glm-test")
    assert (rec["input_tokens"], rec["output_tokens"], rec["total_tokens"]) == tokens
    assert rec["cached_input_tokens"] == cached
    assert rec["latency_ms"] >= 0


async def test_session_totals_by_role(mongo_db, provider):
    """Вызов пишет usage в Mongo, агрегат по сессии отдаёт сумму токенов по ролям."""
    repo = TokenUsageRepo(mongo_db)
    repo.ensure_indexes()
    token_usage.set_sink(repo.record)
    try:
        provider.answers.extend([_resp(100, 20), _resp(80, 10), _resp(30, 1), SimpleNamespace(choices=[]),
                                 _resp(999, 999)])
        with token_usage.usage_scope("voice", token_usage.ROLE_MAIN):
            await llm_client.make_llm_call(dict(CFG), messages=[])
            await llm_client.make_llm_call(dict(CFG), messages=[])
            with token_usage.role_scope(token_usage.ROLE_GUARD):
                await llm_client.make_llm_call(dict(CFG), messages=[])
            await llm_client.make_llm_call(dict(CFG), messages=[])
        with token_usage.usage_scope("other", token_usage.ROLE_MAIN):
            await llm_client.make_llm_call(dict(CFG), messages=[])
    finally:
        token_usage.set_sink(None)

    totals = repo.totals_by_role("voice")
    assert totals["main"] == {"calls": 3, "calls_without_usage": 1,
                              "input_tokens": 180, "cached_input_tokens": 0, "output_tokens": 30,
                              "total_tokens": 210}
    assert totals["guard"]["total_tokens"] == 31
    assert set(totals) == {"main", "guard"}


def _boom(_):
    raise RuntimeError("приёмник упал")


@pytest.mark.parametrize("observer", ["usage", "monitor"])
async def test_broken_observer_never_breaks_the_call(monkeypatch, provider, observer):
    if observer == "usage":
        token_usage.set_sink(_boom)
    else:
        monkeypatch.setattr(llm_monitor, "_sink", _boom)
    try:
        provider.answers.append(_resp(1, 1))
        answer = await llm_client.make_llm_call(dict(CFG), messages=[{"role": "user", "content": "x"}])
    finally:
        token_usage.set_sink(None)
    assert answer.choices[0].message.content == "ok"
    assert len(provider.requests) == 1


async def test_turn_trace_marks_stages_in_order(sink, provider):
    # Вне хода этапы не пишутся и в записи их нет
    token_usage.mark_stage("context")
    assert token_usage.current_stages() == {}
    assert "stages" not in token_usage.build_record(CFG, None, 10)

    provider.answers.append(_stream_two)
    with token_usage.trace_turn():
        token_usage.mark_stage("context")
        await llm_client.make_llm_call(dict(CFG), on_delta=lambda t: None, messages=[])
        stages = token_usage.current_stages()

    # первая дельта стрима отмечает first_token; этапы - миллисекунды от начала хода, не убывают
    assert list(stages) == ["context", "first_token"]
    assert 0 <= stages["context"] <= stages["first_token"]
    assert list(sink[0]["stages"]) == ["context", "first_token"]


async def _stream_two():
    return FakeStream([chunk("При"), chunk("вет")])
