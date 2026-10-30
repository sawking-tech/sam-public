"""Кто звучит: решение о реплике отдельно от звука и файлов, чтобы проверять его без звука.


Одно лицо: быстрый голос отвечает первым, наблюдатель вступает только поправкой.
Правила:
  - ответ на фразу, после которой человек уже сказал новую, не звучит (stale): он отвечает мимо;
  - после реплики наблюдателя быстрый голос не отвечает на фразы, сказанные до неё (dup); любое новое
    событие - фраза человека, таймер - снимает блок, и быстрый голос снова отвечает;
  - поправка наблюдателя (fix) отзывает реплики быстрого голоса с последней фразы человека:
    они неправда, звучать и опираться на них в истории нельзя (retracted).
"""

from __future__ import annotations

# Источник реплики - поле from в say-out и said: нужен для отзыва поправкой и отладки, в подпись говорящего
# не идёт. Всё озвученное говорит SAM, источник - короткая пометка рядом (speaker_label).
FAST = "haiku"         # быстрая модель (say.py --from haiku); имя слота осталось от прототипа
MAIN = "gateway"       # основная модель: ответ голосового хода без быстрой и поправка проверки
OBSERVER = "observer"  # ведущий - сессия Claude Code (say.py по умолчанию)

SPEAKER = "SAM"
SOURCE_NOTES = {FAST: "быстрая", MAIN: "основная", OBSERVER: "ведущий"}

AUTO_ANSWERS = frozenset({FAST, MAIN})   # ответы моделей хода; ведущий их перекрывает
STALE = "stale"
DUP = "dup"
RETRACTED = "retracted"


def speaker_label(source: str | None) -> str:
    """Подпись озвученной реплики для людей: «SAM · быстрая»; источник неизвестен - просто «SAM»."""
    note = SOURCE_NOTES.get(source or "")
    return f"{SPEAKER} · {note}" if note else SPEAKER


def is_observer(who: str | None) -> bool:
    """Реплика ведущего (или без источника, как у старого say.py): после неё ответы моделей на ту же фразу - дубль.
    Строка основной модели ведущим не считается: иначе второе предложение её ответа глушилось как дубль первого
    (12 окт: звучало только первое предложение)."""
    return who not in AUTO_ANSWERS


def is_stale(reply_to: float | None, last_phrase: float) -> bool:
    """Ответ устарел: после фразы, на которую он, пришла новая законченная фраза человека."""
    return reply_to is not None and last_phrase > reply_to


def verdict(who: str | None, reply_to: float | None, *, last_phrase: float,
            last_human: float, last_observer: float) -> str | None:
    """None - реплика звучит; иначе статус, с которым она уходит в said.jsonl."""
    if who in AUTO_ANSWERS:
        # ведущий ответил после фразы - ответ модели лишний, кто бы его ни дал (9 окт 23:29: GLM отвечала вторым
        # голосом после ведущего, потому что дубль ловился только у быстрой)
        # без reply_to (старый say.py) - по последней фразе человека, как было
        answered = reply_to if reply_to is not None else last_human
        if last_observer > answered:
            return DUP
    if is_stale(reply_to, last_phrase):
        return STALE
    return None


def retracted_rows(rows: list[tuple[str, str, bool]]) -> set[int]:
    """Номера реплик быстрого голоса, отозванных поправкой. rows - по времени: (кто, текст, поправка ли);
    кто - FAST, OBSERVER, MAIN или говорящий человек. Отзывается всё быстрое от последней фразы человека
    до поправки; поправка ведущего и основной модели отзывает одинаково, как в say_out."""
    out: set[int] = set()
    pending: list[int] = []
    for i, (who, _, fix) in enumerate(rows):
        if who == FAST:
            pending.append(i)
        elif who in (OBSERVER, MAIN):
            if fix:
                out.update(pending)
                pending = []
        else:
            pending = []
    return out
