"""Страж разноса генерации (SAM-246): признаки на озвучиваемой части и обрезка хвоста."""
import pytest

from gateway import runaway

SHORT = "Включила свет. Ещё что-нибудь?"
LONG = ("Сегодня утром я проверила почту. Писем почти не было. Пришёл счёт за свет. Оплата до пятницы. "
        "Потом открыла календарь. На завтра стоит звонок врачу. В среду приезжает мастер. Вечером ничего не запланировано.")
BREAD = "Monster Boom Monitor Box announced the new Monitor Box with Boom Monster features today"
LOOP = "Я проверю это сейчас. " * 4


@pytest.mark.parametrize("text, sign", [
    (SHORT, None),
    ("", None),
    (LONG, runaway.TOO_LONG),
    (f"<think>{LONG}</think>Готово.", None),                    # длинное рассуждение в мета-блоке законно
    (f"{SHORT}<plan>{LONG}</plan>", None),
    (BREAD + " " + BREAD + " Привет", runaway.LATIN),
    ("Ключ API_KEY=abc123 лежит в файле config.py", None),      # короткая латиница в русском тексте законна
    (LOOP, runaway.REPEATED),
], ids=["short", "empty", "long", "think-block", "meta-block", "latin", "code-names", "loop"])
def test_detect(text, sign):
    found = runaway.detect(text)
    assert (found.sign if found else None) == sign


def test_few_but_very_long_sentences_are_too_long_by_characters():
    wall = "Слово " * 100 + "конец."
    found = runaway.detect(wall, max_chars=500)
    assert found and found.sign == runaway.TOO_LONG and "знаков" in found.detail


def test_agent_reply_may_be_long_but_not_looping_or_garbled():
    """Агенты рассуждают подробно: длина для них не признак, зацикливание и каша остаются."""
    assert runaway.detect(LONG, voice_limits=False) is None
    assert runaway.detect(LOOP * 3, voice_limits=False).sign == runaway.REPEATED
    assert runaway.detect(BREAD + " " + BREAD + " Привет", voice_limits=False) is None      # латиница у агента законна
    garbled = "Проверил файл. 模型开始输出无意义的中文内容，已经偏离任务。 Дальше исправляю."
    assert runaway.detect(garbled, voice_limits=False).sign == runaway.CJK


def test_hieroglyphs_share_is_a_runaway_for_voice_too():
    assert runaway.detect("Хорошо. 模型开始输出无意义的中文内容，已经偏离任务。").sign == runaway.CJK
    assert runaway.detect("Открыла файл настроек и нашла в нём 日本 один раз.").sign == runaway.CJK      # 2 знака из 45 - выше порога
    assert runaway.detect("Открыла файл настроек и нашла в нём одну строку.") is None


def test_threshold_follows_the_argument():
    assert runaway.detect(LONG, max_sentences=8, max_chars=10_000) is None        # 8 предложений при пороге 8


def test_trim_keeps_whole_sentences_within_the_limit():
    out = runaway.trim(LONG, keep_chars=100)
    assert out.endswith(".") and len(out) <= 100 and out.startswith("Сегодня утром я проверила почту.")


def test_trim_keeps_the_first_sentence_even_when_it_is_longer_than_the_limit():
    first = "Очень длинное первое предложение " + "слово " * 30 + "конец."
    assert runaway.trim(first + " Второе.", keep_chars=20) == first


# ─── Страж потока (SAM-246): обрыв на лету ───────────────────────────────────

def _feed(watch, text, step=50):
    sign = None
    for i in range(0, len(text), step):
        sign = watch.feed(text[i:i + step]) or sign
    return sign


def test_watch_cuts_a_stream_full_of_hieroglyphs():
    sign = _feed(runaway.Watch(check_chars=100), "Отвечаю по делу. " + "这是一个测试" * 20)
    assert sign is not None and sign.sign == runaway.CJK


def test_watch_waits_while_an_unclosed_meta_block_streams():
    """Рассуждение в незакрытом <think> ещё не звучит: его длина и язык поток не обрывают."""
    assert _feed(runaway.Watch(check_chars=100), "<think>" + "long english reasoning " * 200) is None


def test_agent_text_over_the_limit_is_cut_but_a_short_report_is_not():
    agent = runaway.Watch(voice_limits=False, check_chars=100)
    sign = _feed(agent, " ".join(f"шаг{i} проверяю ветку номер {i}." for i in range(300)))
    assert sign is not None and sign.sign == runaway.AGENT_TOO_LONG
    assert _feed(runaway.Watch(voice_limits=False, check_chars=10), "Готово, коммит abc1234, 12 passed.") is None


def test_voice_stream_over_five_sentences_is_cut():
    sign = _feed(runaway.Watch(check_chars=50), "Это предложение. " * 8)
    assert sign is not None and sign.sign == runaway.TOO_LONG


@pytest.mark.parametrize("times, cut", [(runaway.STREAM_REPEAT_TIMES - 1, False), (runaway.STREAM_REPEAT_TIMES + 1, True)])
def test_stream_repeat_threshold_is_higher_than_the_final_one(times, cut):
    text = "Начало. " + "одна и та же фраза снова " * times
    sign = _feed(runaway.Watch(voice_limits=False, check_chars=20), text)
    assert (sign is not None and sign.sign == runaway.REPEATED) is cut


def test_watch_keeps_the_first_sign_and_stops_collecting():
    watch = runaway.Watch(check_chars=10)
    first = _feed(watch, "这是一个测试" * 10)
    assert watch.feed("ещё текст") is first
