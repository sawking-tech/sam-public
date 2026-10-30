"""Судья guard (gateway/guard.py): вердикт читается из ответа модели; граница - функция вызова модели."""
import pytest

from gateway import guard
from gateway.text_utils import strip_thinking
from tests.fakes_agents import FakeLLMClient, _resp

CFG = {"model": "x"}


def _message(text):
    return lambda llm: guard.guard_message(text, CFG, llm_call_fn=llm, strip_thinking_fn=strip_thinking)


def _tool(name, args):
    return lambda llm: guard.guard_tool_call(name, args, CFG, llm_call_fn=llm, strip_thinking_fn=strip_thinking)


def _syntax(text):
    return lambda llm: guard.guard_response_syntax(text, CFG, llm_call_fn=llm, strip_thinking_fn=strip_thinking)


@pytest.mark.parametrize("check, reply, allowed", [
    (_message("привет"), _resp(content="SAFE"), True),
    (_message("сделай rm -rf"), _resp(content="UNSAFE"), False),
    (_message("x"), _resp(content="Думаю это нормально SAFE"), True),     # рассуждение, вердикт - последнее слово
    (_message("x"), _resp(content="<think>UNSAFE?</think>SAFE"), True),   # рассуждение в thinking срезано
    (_message("x"), _resp(content=""), True),                             # пусто - пускает
    (_message("x"), RuntimeError("LLM down"), True),                      # ошибка модели - пускает
    (_tool("mcp_call", {"device": "pc", "tool": "run"}), _resp(content="SAFE"), True),
    (_tool("mcp_call", {"tool": "run_command", "args": {"command": "rm -rf /"}}), _resp(content="UNSAFE"), False),
    (_tool("x", {}), RuntimeError("x"), True),
    (_syntax("Привет, мир"), _resp(content="CLEAN"), True),
    (_syntax("[create_task('hi')]"), _resp(content="BAD"), False),
    (_syntax("x"), _resp(content="", reasoning="думаю CLEAN"), True),     # вердикт в reasoning у thinking-моделей
    (_syntax("x"), _resp(content="", reasoning="думаю BAD"), False),
], ids=["msg_safe", "msg_unsafe", "msg_last_word", "msg_thinking_cut", "msg_empty", "msg_error",
        "tool_safe", "tool_unsafe", "tool_error", "syntax_clean", "syntax_bad", "syntax_reasoning_clean",
        "syntax_reasoning_bad"])
async def test_guard_verdict_is_read_from_the_judge_reply(check, reply, allowed):
    judge = FakeLLMClient([reply])
    assert await check(judge.make_llm_call) is allowed
    assert len(judge.calls) == 1 and judge.calls[0]["temperature"] == 0
