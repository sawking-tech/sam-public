"""Кому сказано (SAM-77): правила кода и прогон по размеченным фразам записи 8 окт. Голосовой ход с решением -
tests/test_voice_turn.py."""

import pytest

from gateway import addressee as ad
from gateway.addressee import BACKGROUND, ME, NOT_ME, UNSURE, Heard

@pytest.mark.parametrize("text, who, since, echo, quiet, expected", [
    ("Мурка, иди сюда", "owner", 5, False, False, NOT_ME),
    ("Барсик, слезь со стола", "owner", 5, False, False, NOT_ME),
    ("Кис-кис", "owner", None, False, False, NOT_ME),
    ("Мурка мешает, Сэм, выключи музыку", "owner", 60, False, False, ME),       # кот и имя вместе - ей
    ("Сэм, открой браузер", "owner", None, False, False, ME),
    ("Слушай, Сэм, а что на завтра?", "owner", None, False, False, ME),
    ("Сэм", "owner", None, False, False, ME),
    ("Сэм, чё молчишь", "owner", None, False, False, ME),            # «молчи» целым словом: зовут, а не просят молчать
    ("Я вчера в Сэм сделал анимацию", "owner", None, False, False, UNSURE),   # упоминание: решает модель
    # разговор людей между собой код не отличает от просьбы без имени - решает модель по смыслу
    ("Нет, всё правильно, так Антон сказал", "friend", 120, False, False, UNSURE),
    ("Она опять тормозит, потому что думает", "partner", 90, False, False, UNSURE),
    ("А почему именно так? Объясни", "owner", 8, False, False, UNSURE),
    ("Привет", "owner", None, False, False, UNSURE),                 # короткое от знакомого голоса - модели
    ("АПЛОДИСМЕНТЫ", None, None, False, False, BACKGROUND),
    ("корон", None, None, False, False, BACKGROUND),
    # фраза без слов от узнанного голоса - мусор распознавания, модели не идёт (addressee 2)
    ("...", "owner", 5, False, False, BACKGROUND),
    ("— …", "partner", None, False, False, BACKGROUND),
    ("что угодно", "claude", 1, False, False, BACKGROUND),           # своя речь по голосу
    ("любой текст", "owner", None, True, False, BACKGROUND),          # эхо
    ("Продолжай", "owner", 10, False, False, ME),                     # команда в окне беседы
    ("Стоп, а что с погодой?", "owner", 3, False, False, ME),         # «стоп» с вопросом - не просьба молчать
    # просьба помолчать исполняется молча; «Стоп» 9 окт 22:26 шёл командой модели вместо молчания
    ("Пока не озвучивай ничего, мы тут разговариваем", "owner", None, False, False, BACKGROUND),
    ("Сэм, помолчи", "owner", None, False, False, BACKGROUND),
    ("Стоп", None, 3, False, False, BACKGROUND),
    ("Стоп, стоп!", None, 3, False, False, BACKGROUND),
    ("Сэм, хватит", None, 3, False, False, BACKGROUND),
    ("Тише.", None, 3, False, False, BACKGROUND),
    # в тишине модели доходит только имя, команда в окне беседы тоже ждёт его
    ("Счёт будет раздельный", "owner", 5, False, True, BACKGROUND),
    ("Повтори ещё раз", "owner", 5, False, True, BACKGROUND),
    ("Сэм, что скажешь?", "owner", 5, False, True, ME),
])
def test_addressee_rules_table(text, who, since, echo, quiet, expected):
    assert ad.decide(Heard(text, who, echo, since, quiet)).to == expected


@pytest.mark.parametrize("text, expected", [
    ("Сэма, ты тут?", ME),
    ("Сэмик, включи свет", ME),
    ("Сэмчик, а что на ужин", ME),
    ("Семь утра уже, вставай", UNSURE),
    ("Семья спит, говори тише", UNSURE),
    ("Семёну позвони", UNSURE),
], ids=["sema", "semik", "semchik", "sem-o-clock", "family", "semyon"])
def test_name_forms_call_sam_and_words_alike_do_not(text, expected):
    """addressee 6: ласковые формы имени - обращение; «семь», «семья», «Семён» - не имя SAM."""
    assert ad.decide(Heard(text, "owner", False, None)).to == expected


@pytest.mark.parametrize("since, expected", [(29.9, ME), (30.0, ME), (30.1, UNSURE), (60, UNSURE)],
                         ids=["inside", "edge", "just-after", "minute"])
def test_command_counts_as_to_sam_only_inside_the_conversation_window(since, expected):
    """addressee 7, 12: «повтори» - команда SAM только в окне беседы SAM_CONVERSATION_WINDOW_S (30 с) после её
    реплики; позже фраза идёт модели хода, а не «мне» без вопросов."""
    assert ad.CONVERSATION_WINDOW_S == 30.0
    assert ad.decide(Heard("Повтори", "owner", False, since)).to == expected
