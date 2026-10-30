"""Причуды поставщиков (SAM-27): схемы tools для Gemini, классификация ошибок, нормализация сообщений."""

import pytest

from gateway.provider_transform import (
    classify_error,
    normalize_messages_for_provider,
    sanitize_for_gemini,
    sanitize_tools_for_provider,
)


@pytest.mark.parametrize("schema, expected", [
    pytest.param({"type": "integer", "enum": [1, 2, 3]}, {"type": "string", "enum": ["1", "2", "3"]},
                 id="integer-enum-to-string"),
    pytest.param({"type": "number", "enum": [1.5, 2.5]}, {"type": "string", "enum": ["1.5", "2.5"]},
                 id="number-enum-to-string"),
    pytest.param({"type": "string", "enum": ["a", "b"]}, {"type": "string", "enum": ["a", "b"]}, id="string-enum"),
    pytest.param({"type": "string", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
                 {"type": "string"}, id="non-object-loses-properties"),
    pytest.param({"type": "array"}, {"type": "array", "items": {"type": "string"}}, id="array-gets-items"),
    pytest.param({"type": "array", "items": {"type": "integer"}}, {"type": "array", "items": {"type": "integer"}},
                 id="array-items-kept"),
    pytest.param({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a", "ghost"]},
                 {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                 id="required-only-existing"),
    pytest.param({"type": "object", "properties": {"level": {"type": "integer", "enum": [1, 2, 3]}}},
                 {"type": "object", "properties": {"level": {"type": "string", "enum": ["1", "2", "3"]}}},
                 id="nested-enum"),
    pytest.param({"type": "string", "anyOf": [{"type": "string"}, {"type": "integer"}], "properties": {"x": {}}},
                 {"type": "string", "anyOf": [{"type": "string"}, {"type": "integer"}], "properties": {"x": {}}},
                 id="combiner-keeps-properties"),
    pytest.param(None, None, id="none"),
    pytest.param("string", "string", id="scalar-str"),
    pytest.param(42, 42, id="scalar-int"),
])
def test_gemini_schema_is_sanitized(schema, expected):
    assert sanitize_for_gemini(schema) == expected


def _tool(parameters):
    return [{"type": "function", "function": {"name": "x", "parameters": parameters}}]


INT_ENUM = {"type": "integer", "enum": [1, 2]}


@pytest.mark.parametrize("tools, source, model, expected", [
    pytest.param(_tool(INT_ENUM), "gemini", "", _tool({"type": "string", "enum": ["1", "2"]}), id="gemini-source"),
    pytest.param(_tool(INT_ENUM), "openai", "gemini-flash", _tool({"type": "string", "enum": ["1", "2"]}),
                 id="gemini-model-name"),
    pytest.param(_tool(INT_ENUM), "lmstudio", "", _tool(INT_ENUM), id="lmstudio-unchanged"),
    pytest.param(_tool({"type": "object", "properties": {}}), "anthropic", "",
                 _tool({"type": "object", "properties": {}}), id="anthropic-unchanged"),
    pytest.param([{"type": "retrieval"}], "gemini", "", [{"type": "retrieval"}], id="non-function-passthrough"),
])
def test_tools_sanitized_only_for_gemini(tools, source, model, expected):
    assert sanitize_tools_for_provider(tools, source=source, model=model) == expected


def _error(message, status=None):
    err = Exception(message)
    if status is not None:
        err.status_code = status
    return err


@pytest.mark.parametrize("error, verdict", [
    pytest.param(_error("rate limit", 429), {"kind": "rate_limit", "retryable": True, "switch_provider": True},
                 id="429"),
    pytest.param(_error("Invalid API key", 401), {"kind": "auth", "retryable": False, "cooldown_s": 3600}, id="401"),
    pytest.param(_error("input too long, context length exceeded"), {"kind": "overflow", "switch_provider": False},
                 id="overflow-by-text"),
    pytest.param(_error("Maximum context window reached"), {"kind": "overflow"}, id="overflow-maximum-context"),
    pytest.param(_error("service unavailable", 503), {"kind": "server", "retryable": True}, id="503"),
    pytest.param(_error("???"), {"kind": "unknown", "switch_provider": True}, id="unknown"),
])
def test_provider_error_is_classified(error, verdict):
    got = classify_error(error)
    assert {k: got[k] for k in verdict} == verdict


def _calls(tool_id):
    return {"role": "assistant",
            "tool_calls": [{"id": tool_id, "type": "function", "function": {"name": "x", "arguments": "{}"}}]}


@pytest.mark.parametrize("messages, source, model, expected", [
    pytest.param([_calls("call:abc/123#xyz"), {"role": "tool", "tool_call_id": "call:abc/123#xyz", "content": "r"}],
                 "anthropic", "",
                 [_calls("call_abc_123_xyz"), {"role": "tool", "tool_call_id": "call_abc_123_xyz", "content": "r"}],
                 id="claude-scrubs-tool-id"),
    # Claude не принимает пустой content в assistant без tool_calls
    pytest.param([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "   "}], "anthropic", "",
                 [{"role": "user", "content": "hi"}, {"role": "assistant", "content": " "}],
                 id="claude-fills-empty-assistant"),
    pytest.param([_calls("x:y")], "openai", "claude-sonnet-4-6", [_calls("x_y")], id="claude-by-model-name"),
    pytest.param([_calls("call_abcdef-12345_long")], "mistral", "", [_calls("callabcde")],
                 id="mistral-id-9-alnum"),
    pytest.param([{"role": "tool", "tool_call_id": "ab", "content": "x"}], "mistral", "",
                 [{"role": "tool", "tool_call_id": "ab0000000", "content": "x"}], id="mistral-pads-short-id"),
    # Mistral не принимает tool сразу после user
    pytest.param([{"role": "user", "content": "do it"}, {"role": "tool", "tool_call_id": "x", "content": "r"}],
                 "mistral", "",
                 [{"role": "user", "content": "do it"}, {"role": "assistant", "content": " "},
                  {"role": "tool", "tool_call_id": "x00000000", "content": "r"}],
                 id="mistral-assistant-between-user-and-tool"),
    pytest.param([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}], "lmstudio", "",
                 [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
                 id="lmstudio-passthrough"),
    # Gemini нормализуется через sanitize_tools, не через сообщения
    pytest.param([{"role": "user", "content": "hi"}], "gemini", "", [{"role": "user", "content": "hi"}],
                 id="gemini-passthrough"),
])
def test_messages_normalized_per_provider(messages, source, model, expected):
    assert normalize_messages_for_provider(messages, source=source, model=model) == expected


@pytest.mark.parametrize("source, model, penalties", [
    ("lmstudio", "qwen/qwen3-30b-a3b-2507", True),
    ("zai", "glm-4.6", True),
    ("gemini", "gemini-2.5-flash", False),
    ("openai", "gemini-2.5-flash", False),     # Gemini через OpenAI-совместимый вход
], ids=["lmstudio", "zai", "gemini", "gemini_by_model"])
def test_anti_repeat_penalties_are_not_sent_to_gemini(source, model, penalties):
    """Gemini отвечает 400 на frequency_penalty - повтор хода и воркер падали целиком; остальным штрафы нужны."""
    from gateway.provider_transform import ANTI_REPEAT_PENALTIES, sampling_for_provider
    assert sampling_for_provider(source, model) == (ANTI_REPEAT_PENALTIES if penalties else {})


@pytest.mark.parametrize("source, model, kept", [
    ("zai", "glm-5.3-flash", True),
    ("lmstudio", "qwen/qwen3-30b-a3b-2507", True),
    ("gemini", "gemini-2.5-flash", False),
    ("dashscope", "gemini-via-proxy", False),
], ids=["zai", "lmstudio", "gemini", "gemini_model_name"])
def test_penalties_are_stripped_when_fallback_lands_on_gemini(source, model, kept):
    """Фолбэк по лимиту zai: параметры хода собраны под zai, Gemini на них отвечал 400 (27 окт)."""
    from gateway.provider_transform import ANTI_REPEAT_PENALTIES, strip_unsupported_sampling
    kw = {"model": model, "temperature": 0.3, **ANTI_REPEAT_PENALTIES}
    out = strip_unsupported_sampling(kw, source, model)
    assert out["temperature"] == 0.3
    assert all((k in out) is kept for k in ANTI_REPEAT_PENALTIES)


# ─── Gemini (SAM-261) ──────────────────────────────────────────────────────────

def _call(cid="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {"name": "f", "arguments": "{}"}}]}


def test_gemini_midhistory_system_summary_becomes_a_user_turn():
    out = normalize_messages_for_provider([
        {"role": "system", "content": "S"}, {"role": "user", "content": "привет"},
        {"role": "system", "content": "Сводка прошлого"}, _call()], "gemini", "gemini-3.5-flash")
    assert [m["role"] for m in out] == ["system", "user", "user", "assistant"]


def test_gemini_assistant_text_before_a_call_joins_the_call_turn():
    out = normalize_messages_for_provider([
        {"role": "user", "content": "сделай"}, {"role": "assistant", "content": "Сейчас сделаю."}, _call()],
        "gemini", "gemini-3.5-flash")
    assert [m["role"] for m in out] == ["user", "assistant"] and out[1]["content"] == "Сейчас сделаю." and out[1]["tool_calls"]


def test_gemini_orphan_tool_result_is_dropped_and_a_real_one_stays():
    out = normalize_messages_for_provider([
        {"role": "user", "content": "x"}, {"role": "tool", "tool_call_id": "zz", "content": "сирота"},
        _call("c1"), {"role": "tool", "tool_call_id": "c1", "content": "готово"}], "gemini", "gemini-3.5-flash")
    assert [m["role"] for m in out] == ["user", "assistant", "tool"] and out[2]["content"] == "готово"


def test_other_providers_are_untouched_by_the_gemini_rules():
    msgs = [{"role": "user", "content": "x"}, {"role": "tool", "tool_call_id": "zz", "content": "c"}]
    assert normalize_messages_for_provider(msgs, "lmstudio", "qwen") == msgs
