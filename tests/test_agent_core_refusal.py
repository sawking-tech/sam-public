"""Правка ядра агентам запрещена (SAM-212, SAM-216): агент на привилегированный вызов получает однозначный отказ с
тем, что делать вместо, а не просьбу назвать ключ - её агент закрывал выдуманным ключом (8a70441b, 15 окт). Основной
модели в ходе без секрета текст прежний. Настоящие цикл, гард и проверка ядра шлюза; подменена модель хода."""
import dataclasses
import json
from types import SimpleNamespace as NS

import pytest

from gateway import agent, command_auth
from gateway.agent_loop import run_agent_loop
from gateway.core_paths import REPO


def _resp(calls=()):
    tcs = [NS(id=f"c{i}", type="function", function=NS(name=n, arguments=json.dumps(a))) for i, (n, a) in enumerate(calls)]
    return NS(choices=[NS(message=NS(content=None if tcs else "итог", tool_calls=tcs or None, role="assistant"),
                          finish_reason="tool_calls" if tcs else "stop")], usage=None)


class _Llm:
    """Модель: сначала правка ядра агентом кода, затем итог текстом."""

    def __init__(self):
        self.replies = [_resp([("claude_delegate", {"task": "поправь gateway/apps.py", "workdir": str(REPO)})]),
                        _resp()]

    async def make_llm_call(self, cfg, **kw):
        return self.replies.pop(0)

    def _effective_adapter(self, cfg):
        return "openai"

    def inc_parse_fail(self):
        pass


@pytest.mark.parametrize("session, text", [
    ("task:8a70441b", command_auth.AGENT_REFUSED),   # агент: ключ ему не назвать
    ("voice", command_auth.NEED_SECRET),             # основная модель в ходе без секрета: назови ключ
], ids=["agent", "main_model"])
async def test_privileged_call_without_a_secret_is_refused_by_who_asks(monkeypatch, session, text):
    async def no_llm():
        return {}
    monkeypatch.setattr(agent, "get_best_llm", no_llm)
    deps = dataclasses.replace(agent._get_agent_loop_deps(), llm_client=_Llm())
    messages = [{"role": "user", "content": "почини ядро"}]
    await run_agent_loop(deps, messages, {"source": "test", "model": "m"}, ["claude_delegate"], max_turns=3,
                         session_id=session)
    assert [m["content"] for m in messages if m.get("role") == "tool"] == [text]
