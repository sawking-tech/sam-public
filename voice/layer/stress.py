"""Ударения перед синтезом: «+» перед ударной гласной в каждом слове (silero-stress).


Ударения размечаются всегда: Silero «+» слушается («втор+ой»), а сам ошибается. Расстановщик
видит предложение целиком и различает омографы («зам+ок» и «з+амок»), строка - единицы миллисекунд.
Ручная разметка главнее: слово, где «+» уже стоит (модель пометила редкое слово или имя), не трогается.
Известная ошибка расстановщика: «а все молчат» -> «вс+ё» (8 окт).
"""

from __future__ import annotations

import re

STRESS = "+"
WORD = re.compile(r"\S+")


def plain(text: str) -> str:
    """Текст без разметки ударений: разметка нужна только синтезу, модели в нити она мешает."""
    return text.replace(STRESS, "")


def merge_manual(original: str, accented: str) -> str:
    """Слова, размеченные вручную, взять из исходного текста, остальные - из расстановщика.

    Расстановщик сохраняет порядок слов; если число слов не совпало, ручная разметка важнее всего."""
    orig_words = WORD.findall(original)
    auto_words = WORD.findall(accented)
    if len(orig_words) != len(auto_words):
        return original
    words = iter(o if STRESS in o else a for o, a in zip(orig_words, auto_words))
    return WORD.sub(lambda _: next(words), accented)


class Stresser:
    def __init__(self) -> None:
        from silero_stress import load_accentor
        self._accentor = load_accentor()

    def __call__(self, text: str) -> str:
        accented = self._accentor(text.replace(STRESS, ""))
        return merge_manual(text, accented) if STRESS in text else accented
