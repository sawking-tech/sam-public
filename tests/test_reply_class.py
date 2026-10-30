"""Класс реплики быстрой модели (SAM-56): срочное проверяется сразу, разговорное ждёт пачки."""
import pytest

from gateway import reply_class as rc

URGENT = {rc.ACTION_UNBACKED: True, rc.CHATTER: False}


@pytest.mark.parametrize("voice, tools, cls", [
    ("Открыла калькулятор.", [], rc.ACTION_UNBACKED),
    ("Поставлю будильник на утро.", [], rc.ACTION_UNBACKED),
    ("Записал: купить молоко.", [], rc.ACTION_UNBACKED),
    ("Перезапускаю сервис, секунда.", [], rc.ACTION_UNBACKED),
    ("Сохраню после разбора.", [], rc.ACTION_UNBACKED),
    ("Проверим документацию.", [], rc.ACTION_UNBACKED),
    ("Открываю калькулятор.", ["spawn_agent"], rc.ACTION),
    ("Будильник на 7:30.", None, rc.FACT),
    ("Через две минуты.", None, rc.FACT),
    ("Это полчаса работы.", None, rc.FACT),
    ("Смотри в task.cs.", None, rc.FACT),
    ("Сто процентов.", None, rc.FACT),
    ("Поставила на семь.", None, rc.ACTION_UNBACKED),   # действие и число: решает действие
    ("Ага, слушаю.", None, rc.CHATTER),
    ("Ха, есть такое!", None, rc.CHATTER),
    ("Хороший вопрос, дай соберу мысль…", None, rc.CHATTER),
    ("Понял, Антон, расширение потом.", None, rc.CHATTER),
    ("Сейчас скажу, опять просто шучу.", None, rc.CHATTER),   # «сейчас», «опять», «просто»
    ("Поставщик тот же, записи на месте.", None, rc.CHATTER),   # слова с основы глагола - не действие
    ("Ладно, дальше.Да", None, rc.CHATTER),                     # конец предложения без пробела - не имя файла
    ("", None, rc.CHATTER),
], ids=["opened", "will_set", "wrote", "restarting", "will_save", "will_check", "backed_by_spawn", "clock_time",
        "minutes", "half_hour", "file_name", "percent", "action_wins_over_fact", "listening", "laugh", "thinking",
        "later", "joke_words", "verb_stem_words", "no_space_after_dot", "empty"])
def test_fast_reply_class_decides_urgency(voice, tools, cls):
    got = rc.classify(voice) if tools is None else rc.classify(voice, tools)
    assert got == cls
    if cls in URGENT:
        assert rc.is_urgent(cls) is URGENT[cls]
