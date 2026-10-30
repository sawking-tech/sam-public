"""Что звучит (ADR-003, пересмотр 14 окт): звучит весь ответ без рассуждения и мета-блоков; шлюз
(итог хода для истории и отчёта о судьбе) и мост (поток дельт в озвучку) говорят одно и то же."""
from pathlib import Path

import pytest

from gateway import agent, quiet_mode, voice_turn
from gateway.prompts.lead import LEAD_PROMPT_TEMPLATE
from gateway.prompts.system import HEARD, SYSTEM_PROMPT_TEMPLATE
from gateway.prompts.voice import VOICE_PROMPT_TEMPLATE
from gateway.text_utils import transparent_speech
from gateway.tools.quiet import quiet_impl
from voice.layer.speech_text import VoiceText

WORKFLOWS = Path(__file__).resolve().parent.parent / "workflows"
QUIET_ON = quiet_impl({"action": "on"}, "kitchen", quiet_mode.QuietBook(None))
PROMPTS = [SYSTEM_PROMPT_TEMPLATE, VOICE_PROMPT_TEMPLATE, LEAD_PROMPT_TEMPLATE, voice_turn.LISTENER_HINT,
           agent.QuietTool.description, QUIET_ON, *(f.read_text(encoding="utf-8") for f in WORKFLOWS.glob("*.yml"))]


def _bridge(raw: str, final: str) -> str:
    """Речь моста: ответ дельтами по знаку, затем итог хода; пробелы между кусками не слышны."""
    text = VoiceText()
    spoken = "".join(text.feed(ch) for ch in raw) + text.finish(final)
    return " ".join(spoken.split())


@pytest.mark.parametrize("raw, spoken", [
    ('<think>план</think>Ирина в зале. <voice>Да.</voice> <tool_call>{"name": "x"}</tool_call>', "Ирина в зале. Да."),
    ("Ирина в зале. [вызов: x] Да!", "Ирина в зале. Да!"),
    ("Пять. [судьба неизвестна] Шесть. [прервано]", "Пять. Шесть."),     # метки модель копирует из контекста
    ("Массив [1, 2, 3] не метка?", "Массив не метка?"),
    ("Начало [" + "а" * 50, "Начало [" + "а" * 50),                       # длинная скобка - текст, не метка
    ("<silent>это не ко мне</silent>", ""),
    ("Скажу. <silent>пометка себе</silent> Всё.", "Скажу. Всё."),
    ('<silent reason="не мне"></silent>', ""),
    ("<silent>молчу", ""),                                                 # незакрытый тег тоже не звучит
    ('<quiet action="on"></quiet>', ""),                                   # старый тег тишины
    ("<voice><voice></voice>", ""),                  # 12 окт: «пустой <voice></voice>» внутри открытого тега
    ("<voice>Привет<voice> Петя</voice>", "Привет Петя"),
    ("Если x < y, то", "Если x < y, то"),                                  # придержанный «<» отпускается в конце
])
def test_gateway_and_bridge_speak_the_same_in_transparent_mode(raw, spoken):
    gateway = voice_turn.drop_marks(transparent_speech(raw)).strip()
    assert gateway == spoken and _bridge(raw, gateway) == spoken


def test_transparent_prompts_teach_silent_not_voice_tag():
    for prompt in PROMPTS:
        assert "<voice" not in prompt and "</voice" not in prompt and "пустым текстом" not in prompt
    assert HEARD in SYSTEM_PROMPT_TEMPLATE and HEARD in VOICE_PROMPT_TEMPLATE
    assert "<silent></silent>" in voice_turn.LISTENER_HINT
    assert all("ответь <silent></silent>" in t for t in (agent.QuietTool.description, QUIET_ON))
