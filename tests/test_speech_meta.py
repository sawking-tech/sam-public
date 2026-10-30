"""Мета-блоки в парных тегах не звучат (gateway/speech_meta.py): итог хода и поток моста режут одинаково."""
import pytest

from gateway.speech_meta import stream_meta, strip_meta
from gateway.text_utils import transparent_speech
from voice.layer.speech_text import VoiceText, spoken_transparent


@pytest.mark.parametrize("raw, spoken", [
    ("Свет включила. <note>для монитора: зал</note>", "Свет включила. "),
    ('Готово. <wf step="2">ok</wf>Дальше сама.', "Готово. Дальше сама."),
    ("<think>план</think>Да.", "Да."),
    ("<|thinking|>секрет<|/thinking|>Да.", "Да."),
    ("<silent></silent>", ""),
    ("<silent />Скажу.", "Скажу."),
    ("Строка <br />дальше.", "Строка дальше."),
    ("Строка<br />дальше.", "Строка<br />дальше."),   # сразу после буквы - не тег, как List<String>
    ("Скажу. <tool_call>{}</tool_call>", "Скажу. "),
    ("<a>x<b>y</b>z</a>Речь.", "Речь."),
    ("<a>1<a>2</a>3</a>Речь.", "3Речь."),   # ограничение: вложенный одноимённый блок без счётчика глубины
    ("<NOTE>x</note>Речь.", "Речь."),
    ("Сейчас вечер<note>зал</note>.", "Сейчас вечер."),   # блок вплотную к слову
    ("x<y, а y>z", "x<y, а y>z"),
    ("Если a<b и c>d, то всё.", "Если a<b и c>d, то всё."),
    ("Используй List<String> тут.", "Используй List<String> тут."),
    ("три < пяти", "три < пяти"),
    ("люблю <3", "люблю <3"),
    ("<voice>Солнечно.</voice>", "Солнечно."),
    ("<voice lang=ru>Привет</voice> Ещё.", "Привет Ещё."),
    ("<voice></voice>", ""),
], ids=["note", "attrs", "think", "pipe_thinking", "silent_only", "silent_self_closing", "br", "br_after_letter", "tool_call",
        "nested_other", "nested_same", "case", "glued_block", "less_greater", "comparison", "generic", "spaced_less", "heart",
        "legacy_voice", "legacy_voice_attrs", "legacy_empty_voice"])
def test_strip_meta(raw, spoken):
    assert strip_meta(raw) == spoken


@pytest.mark.parametrize("raw, turn_end, stream", [
    ("Начало. <think>оборвано", "Начало. ", "Начало. "),
    ("Начало. <silent>оборвано", "Начало. ", "Начало. "),
    ("Начало. <quiet>оборвано", "Начало. ", "Начало. "),
    ("Нажми <Enter> и подожди.", "Нажми  и подожди.", "Нажми "),
], ids=["think", "silent", "quiet_old_name", "unknown_name_speaks_on_turn_end"])
def test_unclosed_block(raw, turn_end, stream):
    assert strip_meta(raw) == turn_end
    assert stream_meta(raw) == stream


@pytest.mark.parametrize("raw, held", [
    ("Сейчас <", "Сейчас "),
    ("Сейчас </", "Сейчас "),
    ("Сейчас <sil", "Сейчас "),
    ('Сейчас <note kind="x"', "Сейчас "),
    ("Строка<br/", "Строка"),       # ждёт и после буквы
    ("Строка <br/", "Строка "),
    ("три < пяти", "три < пяти"),
], ids=["lt", "close", "name", "attrs", "after_letter", "self_closing", "spaced_less_not_held"])
def test_partial_tag_waits(raw, held):
    assert stream_meta(raw) == held


@pytest.mark.parametrize("raw", [
    "Привет <br/> мир.",
    "Свет включила. <note>зал, лампа</note> Ещё что-то?",
    "Нажми <Enter> и подожди. <wf>шаг</wf>Готово.",
    "<voice lang=ru>Да.</voice> <silent>тихо</silent>Всё. [прервано] Дальше.",
    "a<b и c>d, <tool_call>{}</tool_call>конец <3",
    "<think>план</think>Ответ <|thinking|>x<|/thinking|> тут.",
    "<voice>Привет<voice> Петя</voice> и<note>x</note>3</a>.",
    "List<Map<String>> ok",
    "a<b<c>d</c>e",
    "См. <\nдальше [x\nещё",
    "Привет <voice=x>Да</voice> всё.",
], ids=["br", "note", "unknown_unclosed", "legacy_and_silent", "comparison_and_call", "thinking_forms",
        "tags_after_letters", "nested_generic", "glued_nested", "newline_tails", "legacy_voice_attr"])
def test_stream_by_char_never_unsays(raw):
    """Посимвольно: каждый новый кусок речи продолжает уже сказанное, и итог хода тоже."""
    said = ""
    for i in range(1, len(raw) + 1):
        now = spoken_transparent(raw[:i])
        assert now.startswith(said), (raw[:i], said, now)
        said = now
    assert spoken_transparent(raw, final=True).startswith(said)


def test_stream_matches_turn_result():
    deltas = ["Свет ", "включила. <no", "te>зал, ", "лампа</note> Ещё ", "что-то?"]
    voice = VoiceText()
    streamed = "".join(voice.feed(d) for d in deltas)
    raw = "".join(deltas)
    streamed += voice.finish(transparent_speech(raw))
    assert streamed == spoken_transparent(raw, final=True) == "Свет включила.  Ещё что-то?"
    assert transparent_speech(raw) == "Свет включила. Ещё что-то?"   # voice_clean сводит пробелы
