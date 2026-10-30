"""Ударения перед синтезом (voice/layer/stress.py): расстановщик ставит «+» в каждом слове, ручная разметка главнее.
Настоящий Stresser; подменена модель расстановщика (пакет silero_stress) - граница модели."""
import sys
from types import SimpleNamespace

import pytest

from voice.layer import stress

# Расстановщик-фальшивка: «+» перед первой гласной каждого слова, как модель silero-stress на любом слове
VOWELS = "аеёиоуыэюя"


def _accent_first_vowel(text):
    def word(w):
        i = next((k for k, ch in enumerate(w) if ch.lower() in VOWELS), None)
        return w if i is None else w[:i] + stress.STRESS + w[i:]
    return " ".join(word(w) for w in text.split(" "))


@pytest.fixture
def stresser(monkeypatch):
    seen = []

    def accentor(text):
        seen.append(text)
        return _accent_first_vowel(text)
    monkeypatch.setitem(sys.modules, "silero_stress", SimpleNamespace(load_accentor=lambda: accentor))
    return stress.Stresser(), seen


@pytest.mark.parametrize("text, expected", [
    ("открой замок", "+открой з+амок"),                       # без ручной разметки - всё от расстановщика
    ("открой зам+ок", "+открой зам+ок"),                     # ручное ударение главнее расстановщика
    ("втор+ой раз, Сэм", "втор+ой р+аз, С+эм"),
], ids=["auto", "manual-wins", "manual-and-auto"])
def test_engine_stresses_every_word_and_manual_mark_wins(stresser, text, expected):
    """speech-text 17: сервер синтеза ставит ударение в каждом слове, слово с ручным «+» не трогает; расстановщик
    получает текст без ручной разметки."""
    call, seen = stresser
    assert call(text) == expected
    assert seen == [text.replace(stress.STRESS, "")]


def test_word_count_mismatch_keeps_the_manual_text():
    """Расстановщик вернул другое число слов - ручная разметка важнее, текст уходит как был."""
    assert stress.merge_manual("зам+ок открой", "з+амок") == "зам+ок открой"
    assert stress.plain("зам+ок") == "замок"
