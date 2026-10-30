"""Текст модели в речь и строки истории для промпта (gateway.text_utils)."""

from datetime import datetime, timedelta

import pytest

from gateway.text_utils import (
    fmt_msg,
    strip_thinking,
    voice_clean,
)

NOW = datetime(2026, 10, 12, 12, 0)   # часы fmt_msg (UTC)


@pytest.mark.parametrize("fn, raw, expected", [
    (strip_thinking, "<think>размышляю</think>Ответ", "Ответ"),
    (strip_thinking, "<|thinking|>reason<|/thinking|>Hello", "Hello"),
    (strip_thinking, "<think>\nмного\nстрок\n</think>\nОтвет", "Ответ"),
    (strip_thinking, "Окей, давай подумаем\nПривет, мир", "Привет, мир"),
    (strip_thinking, "просто ответ", "просто ответ"),
    (strip_thinking, "", ""),
    (strip_thinking, None, ""),
    (voice_clean, "посмотри https://example.com тут", "посмотри тут"),
    (voice_clean, "файл C:\\Users\\x\\file.txt здесь", "файл здесь"),
    (voice_clean, "лог /var/log/syslog тут", "лог тут"),
    (voice_clean, "[ERROR] что-то сломалось\nответ", "ответ"),
    (voice_clean, "много    пробелов  тут", "много пробелов тут"),
    (voice_clean, "", ""),
], ids=[
    "think_tags", "pipe_thinking_tags", "multiline_think", "reasoning_starter_line", "no_thinking", "strip_empty",
    "strip_none", "url", "windows_path",
    "unix_path", "error_marker", "whitespace", "voice_clean_empty",
])
def test_raw_answer_becomes_clean_speech(fn, raw, expected):
    assert fn(raw) == expected


@pytest.mark.parametrize("msg, kw, expected", [
    ({"role": "user", "content": "hi", "timestamp": NOW}, {}, "[только что] user: hi"),
    ({"role": "user", "content": "hi", "timestamp": NOW - timedelta(hours=3)}, {}, "[3ч назад] user: hi"),
    ({"role": "user", "content": "hi", "timestamp": NOW - timedelta(days=2)}, {}, "[2д назад] user: hi"),
    ({"role": "user", "content": "X" * 1000}, {"max_len": 50}, "user: " + "X" * 50),
    ({"is_summary": True, "content": "обсудили котов", "authors": ["Антон", "Ирина", "Антон"], "timestamp": NOW}, {},
     "[только что] [Антон,Ирина]: обсудили котов"),
    ({"role": "assistant", "tool_calls": [{"function": {"name": "create_task"}}, {"function": {"name": "remember"}}],
      "timestamp": NOW}, {}, "[только что] assistant→tools: create_task, remember"),
    ({"role": "tool", "tool_call_id": "abc123def", "content": "result text", "timestamp": NOW}, {},
     "[только что] tool[abc123de]: result text"),
    ({"role": "user", "content": "hi"}, {}, "user: hi"),
], ids=["just_now", "hours", "days", "cut_long", "summary_with_authors", "tool_calls", "tool_result", "no_timestamp"])
def test_history_line_for_prompt(msg, kw, expected):
    assert fmt_msg(msg, now=NOW, **kw) == expected
