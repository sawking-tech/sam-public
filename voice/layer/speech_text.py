"""Текст для озвучки из потока дельт (SAM-09): речь (ADR-003) и нарезка на предложения.

Звучит весь ответ без мета-блоков в парных тегах (gateway/speech_meta.py) и служебных меток.

Чистая логика без сети и звука: мост (bridge.py) кормит её дельтами /voice/turn и пишет готовые
предложения в say-out.jsonl, чтобы первое звучало до конца генерации.
"""
from __future__ import annotations

import re

from gateway.speech_meta import stream_meta, strip_meta

# Конец предложения: знак, затем пробел. «3.5» и «т.е.» без пробела после точки не режутся.
_BOUNDARY = re.compile(r"[.!?…]+[»\"')]*\s+")
# Служебная метка в квадратных скобках («[прервано]», «[вызов: x]», «[судьба неизвестна]»): модель копирует их
# из контекста в речь, а в речи скобок нет. Длиннее порога - не метка, а текст: на нём
# поток перестаёт придерживать незакрытую скобку, и вырезание не отменяет уже сказанное.
MARK_MAX_CHARS = 40
_MARK = re.compile(rf"\[[^\[\]\n]{{1,{MARK_MAX_CHARS}}}\]\s*")
_PARTIAL_MARK = re.compile(rf"\[[^\[\]\n]{{0,{MARK_MAX_CHARS}}}\Z")
# Слово перед точкой, после которого предложение не кончается: сокращения
ABBREVIATIONS = frozenset({"т", "е", "д", "п", "др", "пр", "им", "ул", "г", "гг", "см", "напр", "т.е", "т.д", "т.п"})


class VoiceText:
    """Дельты -> новый текст речи: звучит весь ответ по мере прихода, без мета-блоков."""

    def __init__(self) -> None:
        self.raw = ""
        self.sent = 0

    def feed(self, delta: str) -> str:
        self.raw += delta
        text = spoken_transparent(self.raw)
        new, self.sent = text[self.sent:], len(text)
        return new

    def finish(self, final_voice: str) -> str:
        """Ход закончен: придержанный хвост («<», «[») больше ничего не ждёт; ничего не прозвучало - итог хода."""
        rest = spoken_transparent(self.raw, final=True)[self.sent:] if self.sent else final_voice
        self.sent += len(rest)
        return rest


def spoken_transparent(raw: str, final: bool = False) -> str:
    """Весь ответ, который можно говорить сейчас: без мета-блоков в парных тегах (незакрытые - до конца,
    gateway/speech_meta.py) и меток; хвост, похожий на начало тега или метки, ждёт."""
    if final:
        return drop_marks(strip_meta(raw))
    return _PARTIAL_MARK.sub("", drop_marks(stream_meta(raw)))


def drop_marks(text: str) -> str:
    """Текст без служебных меток в квадратных скобках; итог хода шлюза - gateway.voice_turn.drop_marks."""
    return _MARK.sub("", text)



class Sentences:
    """Текст кусками -> законченные предложения; остаток отдаёт flush."""

    def __init__(self) -> None:
        self.buf = ""

    def push(self, text: str) -> list[str]:
        self.buf += text
        out, start = [], 0
        for m in _BOUNDARY.finditer(self.buf):
            if _is_abbreviation(self.buf[start:m.start()]):
                continue
            sentence = self.buf[start:m.end()].strip()
            if sentence:
                out.append(sentence)
            start = m.end()
        self.buf = self.buf[start:]
        return out

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


def _is_abbreviation(before: str) -> bool:
    words = before.split()
    return bool(words) and words[-1].lower().rstrip(".") in ABBREVIATIONS
