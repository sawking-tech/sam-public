"""Рассуждение по роли и поставщику (SAM-226, gateway/reasoning.py): способ по поставщику, запас к пределу ответа,
инструкция обхода; llm_client применяет политику по роли вызова. Сеть подменена на уровне функции вызова."""
import pytest

from gateway import llm_client, reasoning, token_usage

RESERVE = 100
OFF_LMSTUDIO = {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(reasoning.PROMPT_SOURCES_ENV, raising=False)
    for role in reasoning.ROLE_LEVELS:
        monkeypatch.delenv(reasoning.ROLE_ENV_PREFIX + role.upper(), raising=False)
    for level in reasoning.LEVELS:
        monkeypatch.delenv(reasoning.ZAI_EFFORT_ENV_PREFIX + level.upper(), raising=False)


def _apply(source, role, **kw):
    kw.setdefault("messages", [{"role": "system", "content": "СИСТЕМА"}, {"role": "user", "content": "фраза"}])
    return reasoning.apply({"source": source}, role, kw, reserve=RESERVE)


@pytest.mark.parametrize("source, role, extra, max_tokens", [
    pytest.param("lmstudio", token_usage.ROLE_SUMMARIZE, OFF_LMSTUDIO, 600, id="lmstudio-summary-off"),
    pytest.param("lmstudio", token_usage.ROLE_MAIN, OFF_LMSTUDIO, 600, id="lmstudio-voice-off"),
    pytest.param("lmstudio", token_usage.ROLE_WORKER, None, 600 + RESERVE, id="lmstudio-worker-thinks-with-reserve"),
    pytest.param("lmstudio", token_usage.ROLE_GUARD, None, 600 + RESERVE, id="lmstudio-guard-thinks-with-reserve"),
    pytest.param("lmstudio", token_usage.ROLE_JUDGE, OFF_LMSTUDIO, 600, id="lmstudio-judge-off"),
    pytest.param("dashscope", token_usage.ROLE_WORKFLOW, {"enable_thinking": False}, 600, id="dashscope-off"),
    pytest.param("zai", token_usage.ROLE_SUMMARIZE, {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
                 600 + RESERVE, id="zai-cannot-disable-low-with-reserve"),
    pytest.param("gemini", token_usage.ROLE_MAIN, None, 600 + RESERVE, id="unknown-way-reserve-only"),
])
def test_provider_way_and_reserve(source, role, extra, max_tokens):
    out = _apply(source, role, max_tokens=600)
    assert out.get("extra_body") == extra
    assert out["max_tokens"] == max_tokens


def test_instruction_for_provider_that_ignores_disable():
    """z.ai по умолчанию в списке обхода: инструкция в конце системного сообщения, начало промпта прежнее."""
    out = _apply("zai", token_usage.ROLE_SUMMARIZE)
    assert out["messages"][0]["content"] == "СИСТЕМА\n\n" + reasoning.INSTRUCTION
    assert out["messages"][1] == {"role": "user", "content": "фраза"}


def test_instruction_without_system_goes_first_and_caller_list_untouched():
    sent = [{"role": "user", "content": "кусок"}]
    out = _apply("zai", token_usage.ROLE_SUMMARIZE, messages=sent)
    assert out["messages"][0] == {"role": "system", "content": reasoning.INSTRUCTION}
    assert sent == [{"role": "user", "content": "кусок"}]


@pytest.mark.parametrize("source, role, env, has", [
    pytest.param("zai", token_usage.ROLE_WORKER, None, False, id="low-level-no-instruction"),
    pytest.param("lmstudio", token_usage.ROLE_MAIN, None, False, id="not-in-list"),
    pytest.param("gemini", token_usage.ROLE_MAIN, "gemini, zai", True, id="list-from-env"),
])
def test_instruction_only_for_min_level_and_listed_source(monkeypatch, source, role, env, has):
    if env:
        monkeypatch.setenv(reasoning.PROMPT_SOURCES_ENV, env)
    out = _apply(source, role)
    assert (reasoning.INSTRUCTION in out["messages"][0]["content"]) is has


def test_role_level_from_env_and_unknown_role_is_min(monkeypatch):
    monkeypatch.setenv(reasoning.ROLE_ENV_PREFIX + "GUARD", "min")
    assert reasoning.level_for(token_usage.ROLE_GUARD) == reasoning.LEVEL_MIN
    monkeypatch.setenv(reasoning.ROLE_ENV_PREFIX + "WORKER", "much")
    assert reasoning.level_for(token_usage.ROLE_WORKER) == reasoning.LEVEL_LOW
    assert reasoning.level_for(token_usage.ROLE_UNKNOWN) == reasoning.LEVEL_MIN


def test_zai_effort_per_level_from_env(monkeypatch):
    """Воркерам на z.ai можно дать high, ход основной остаётся на low."""
    monkeypatch.setenv(reasoning.ZAI_EFFORT_ENV_PREFIX + "LOW", "high")
    assert _apply("zai", token_usage.ROLE_WORKER)["extra_body"]["reasoning_effort"] == "high"
    assert _apply("zai", token_usage.ROLE_MAIN)["extra_body"]["reasoning_effort"] == "low"


def test_policy_overrides_caller_fields_and_keeps_the_rest():
    out = _apply("lmstudio", token_usage.ROLE_MAIN,
                 extra_body={"chat_template_kwargs": {"enable_thinking": True}, "top_k": 20})
    assert out["extra_body"] == {**OFF_LMSTUDIO, "top_k": 20}


async def test_llm_client_applies_policy_by_call_role(monkeypatch, fake_llm_response):
    """Вызов сводки на LM Studio уходит с выключенным рассуждением, вызов воркера - с запасом к пределу."""
    sent = []

    async def fake_openai(cfg, **kw):
        sent.append(kw)
        return fake_llm_response("ок")
    monkeypatch.setattr(llm_client, "_openai_call", fake_openai)
    cfg = {"source": "lmstudio", "model": "qwen", "model_server": "http://x/v1"}
    messages = [{"role": "user", "content": "кусок"}]
    with token_usage.usage_scope("voice", token_usage.ROLE_SUMMARIZE):
        await llm_client.make_llm_call(cfg, messages=messages, max_tokens=600)
    with token_usage.usage_scope("task:1", token_usage.ROLE_WORKER):
        await llm_client.make_llm_call(cfg, messages=messages, max_tokens=600)
    assert sent[0]["extra_body"] == OFF_LMSTUDIO and sent[0]["max_tokens"] == 600
    assert "extra_body" not in sent[1] and sent[1]["max_tokens"] == 600 + reasoning.RESERVE_TOKENS
