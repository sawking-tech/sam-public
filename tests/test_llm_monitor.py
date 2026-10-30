"""Что уходит в LLM - монитору: событие llm_request перед вызовом поставщика.

Подменяется клиент OpenAI SDK; публикация идёт настоящим путём make_llm_call -> llm_monitor.publish."""
from types import SimpleNamespace

import pytest

from gateway import llm_client, llm_monitor, llm_router, reasoning, token_usage
from tests.fakes_core import FakeOpenAI

MAIN = {"model": "glm-test", "source": "zai"}
FAST = {"model": "qwen-fast", "source": "lmstudio", llm_router.ROUTE_KEY: llm_router.ROUTE_FAST}
TOOLS = [{"type": "function", "function": {"name": "spawn_agent", "parameters": {}}},
         {"name": "remember"}]


def _resp():
    msg = SimpleNamespace(content="ok", tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None)


@pytest.fixture
def events(monkeypatch):
    """Приёмник на время теста; monkeypatch вернёт тот, что подключил корень композиции."""
    got: list[dict] = []
    monkeypatch.setattr(llm_monitor, "_sink", got.append)
    return got


@pytest.fixture
def provider(monkeypatch):
    fake = FakeOpenAI(_resp())
    monkeypatch.setattr(llm_client, "_openai_client", fake.client)
    monkeypatch.setattr(llm_client, "_adapter", "openai")
    return fake


@pytest.mark.parametrize("cfg, role, fast, tools", [
    pytest.param(FAST, token_usage.ROLE_MAIN, True, ["spawn_agent", "remember"], id="fast-route"),
    pytest.param(MAIN, token_usage.ROLE_SUMMARIZE, False, [], id="summarize-on-main"),
])
async def test_call_publishes_request_with_role_route_messages_tools(events, provider, cfg, role, fast, tools):
    messages = [{"role": "system", "content": "ты SAM"}, {"role": "user", "content": "привет"}]
    with token_usage.usage_scope("voice", role):
        await llm_client.make_llm_call(dict(cfg), messages=messages, tools=TOOLS if tools else None)

    [ev] = events
    assert (ev["session"], ev["role"], ev["fast"]) == ("voice", role, fast)
    assert (ev["source"], ev["model"]) == (cfg["source"], cfg["model"])
    # монитор показывает отправленное: инструкция обхода рассуждения для z.ai уже в нём (SAM-226)
    assert ev["messages"] == reasoning.apply(cfg, role, {"messages": messages})["messages"]
    assert ev.get("tools", []) == tools
    assert len(provider.requests) == 1


MAX_CHARS = 20   # длиннее смешанного сообщения и короче длинного
_ASSISTANT = SimpleNamespace(role="assistant", content=None,
                             tool_calls=[SimpleNamespace(function=SimpleNamespace(name="get_devices"))])
_MIXED = [
    {"role": "user", "content": [{"type": "text", "text": "что тут"},
                                 {"type": "image_url", "image_url": {"url": "data:..."}}]},
    _ASSISTANT,
    {"role": "tool", "name": "get_devices", "content": "[]"},
]


@pytest.mark.parametrize("cfg, messages, expected", [
    pytest.param(MAIN, [{"role": "system", "content": "a" * 50}], None, id="long-cut-for-main"),
    pytest.param(FAST, [{"role": "system", "content": "a" * 50}], [{"role": "system", "content": "a" * 50}],
                 id="long-whole-for-fast"),
    pytest.param(MAIN, _MIXED, [
        {"role": "user", "content": "что тут\n" + llm_monitor.IMAGE_PART},
        {"role": "assistant", "content": "", "tool_calls": ["get_devices"]},
        {"role": "tool", "name": "get_devices", "content": "[]"},
    ], id="images-tool-calls-provider-objects"),
])
def test_request_event_is_readable_text(cfg, messages, expected):
    msgs = llm_monitor.build_event(cfg, {"messages": messages}, max_chars=MAX_CHARS,
                                   fast_max_chars=llm_monitor.NO_LIMIT)["messages"]
    if expected is None:
        # основная: начало длиной max_chars и число исходных символов
        assert msgs[0]["content"].startswith("a" * MAX_CHARS + "\n") and "50" in msgs[0]["content"]
    else:
        assert msgs == expected
