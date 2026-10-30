"""Граница компетенции быстрой модели (SAM-169): набор фраз - кто отвечает. Пауза и метка передачи в голосовом
ходе - tests/test_voice_turn.py."""
import pytest

from gateway.phrase_class import FAST, MAIN, responder_for


@pytest.mark.parametrize("phrase, responder", [
    # действия и факты вне разговора - основной
    ("Сэм, какая сейчас погода дома", MAIN), ("сколько стоит биткоин", MAIN), ("когда приедет Ирина", MAIN),
    ("что такое Hermes", MAIN), ("где мои ключи", MAIN), ("почему не работает интернет", MAIN),
    ("Сэм, включи свет в спальне", MAIN), ("поставь будильник на семь", MAIN), ("Сэм, напомни выпить таблетку", MAIN),
    ("найди рецепт борща", MAIN), ("Сэм, вымой посуду", MAIN), ("закажи пиццу", MAIN),
    # болтовня и вопросы по разговору - быстрой
    ("Сэм, ты тут", FAST), ("Сэм, как дела", FAST), ("привет", FAST), ("Сэм, а ты так умеешь?", FAST),
    ("и их называют агентами", FAST), ("Сэм, который час", FAST), ("какое сегодня число", FAST),
    ("сколько времени", FAST), ("спасибо, родная", FAST), ("Ну, короче, есть же разные программы", FAST),
    ("Это вообще жестко", FAST), ("", FAST),
    # формы слов не ловятся внутри других слов
    ("погодите", FAST), ("включение света было вчера", FAST),
    # вопросы к самой SAM (ревью 12 окт: «почему», «где» в болтовне запускали паузу и облачный ход основной)
    ("почему ты молчишь", FAST), ("где ты была", FAST), ("Сэм, когда тебя включили", FAST), ("сколько тебе лет", FAST),
])
def test_responder_for_phrase_table(phrase, responder):
    assert responder_for(phrase) == responder


def test_capabilities_are_grouped_from_the_real_registry():
    """ADR-027, блок 3: быстрая видит возможности группами; инструмента нет в карте - он в «прочем», а не пропал."""
    from gateway.tools.capabilities import CAPABILITIES_HEADER, OTHER_GROUP, capability_groups
    text = capability_groups({"remember": object, "forget": object, "spawn_agent": object, "new_tool": object})
    lines = text.splitlines()
    assert lines[0] == CAPABILITIES_HEADER
    assert lines[1].startswith("- память") and lines[1].endswith("(forget, remember)")
    assert lines[2].startswith("- агенты") and lines[-1] == f"- {OTHER_GROUP} (new_tool)"
    assert capability_groups({}) == ""
