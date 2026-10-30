"""Словарь произношения (voice/layer/lexicon.py) без звука и серверов."""

from __future__ import annotations

from pathlib import Path

import pytest

from voice.layer import tts_client
from voice.layer.lexicon import Live, LexiconError, load, parse

STACK = "стек = стэк"
CASES = ("стека", "стеку", "стеком", "стеке", "стеки", "стеков", "стекам", "стеками", "стеках")


@pytest.mark.parametrize("table, text, spoken", [
    (STACK, "положим в стек", "положим в стэк"),
    *[(STACK, f"про {form}.", f"про стэ{form[3:]}.") for form in CASES],
    (STACK, "Стек растёт, СТЕК растёт", "Стэк растёт, СТЭК растёт"),
    (STACK, "стекло стекает, вода истекает, протекло, стеклянный",
     "стекло стекает, вода истекает, протекло, стеклянный"),
    (STACK, "стек-трейс и колл-стек", "стэк-трейс и колл-стэк"),
    (STACK, "ст+ек", "ст+ек"),
    # своё перечисление окончаний: форма вне списка не меняется
    ("кэш = кеш; а у", "кэш, кэша и кэшу, кэшем", "кеш, кеша и кешу, кэшем"),
    # фраза без окончаний меняется только целиком
    ("на связи = на св+язи", "Я на  связи. На связи!", "Я на св+язи. На св+язи!"),
    ("на связи = на св+язи", "в связи с тем, нет связи, на связиста", "в связи с тем, нет связи, на связиста"),
    ("# заголовок\n\nстек = стэк  # комментарий\n", "стек", "стэк"),
], ids=["word", *[f"case-{f}" for f in CASES], "keeps-register", "similar-words-untouched", "hyphenated",
        "manual-stress", "custom-endings", "phrase-whole", "phrase-only-whole", "comments-and-blank-lines"])
def test_lexicon_replaces_word_forms_only(table, text, spoken):
    assert parse(table)(text) == spoken


def test_bad_lexicon_line_is_named():
    with pytest.raises(LexiconError, match="lex.txt:2"):
        parse("стек = стэк\nстек стэк", "lex.txt")


def test_live_lexicon_rereads_and_keeps_last_good(tmp_path):
    assert load(tmp_path / "нет-такого-словаря.txt")("стек") == "стек"   # нет файла - пустой словарь
    path = tmp_path / "lexicon.txt"
    path.write_text("стек = стэк", encoding="utf-8")
    live = Live(path)
    assert live.get()("стек") == "стэк"
    path.write_text("стек = стэк\nкэш = кеш", encoding="utf-8")
    assert live.get()("кэш") == "кеш"
    path.write_text("сломано", encoding="utf-8")
    assert live.get()("кэш") == "кеш"


@pytest.mark.parametrize("backend", ["silero", "cosy"])
def test_every_tts_engine_gets_replaced_text(monkeypatch, backend):
    # Замена в tts_client.stream: через него идёт реплика к любому движку, до ударений на сервере
    sent = []
    monkeypatch.setattr(tts_client, "_stream_at", lambda address, text, voice: sent.append(text) or iter(()))
    monkeypatch.setattr(tts_client, "tts_backend", lambda voice=None: backend)
    list(tts_client.stream("Стек на 2 кадра"))
    assert len(sent) == 1 and sent[0].startswith("Стэк ")
