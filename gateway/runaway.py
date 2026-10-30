"""Признаки разноса генерации и обрезка хвоста (SAM-246).

Обычный ответ озвучивается, поэтому короткий: два-три предложения. Озвучиваемая часть (текст вне парных мета-блоков,
ADR-003) длиннее порога, русский текст с латиницей вместо слов, повтор одной фразы - признаки того, что модель
ушла в разнос. Чистые функции и Watch для потока: подключение к циклу и статистика снаружи.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import speech_meta
from .env import env_float, env_int

MAX_SENTENCES = env_int("SAM_REPLY_MAX_SENTENCES", 5)    # в голос не больше пяти предложений, длиннее не звучит
MAX_CHARS = env_int("SAM_REPLY_MAX_CHARS", 500)         # пять предложений по ~100 знаков; длиннее озвучиваемая часть не звучит
KEEP_CHARS = env_int("SAM_RUNAWAY_KEEP_CHARS", 300)      # сколько здравого начала остаётся после обрезки
LATIN_SHARE = 0.5            # доля латиницы среди букв: русский текст с английской кашей (замер: «Monster Boom Monitor Box»)
CJK_SHARE = env_float("SAM_RUNAWAY_CJK_SHARE", 0.02)   # доля иероглифов: порог 2%, слабая модель уходит в китайский
LATIN_MIN_LETTERS = 40       # короче - код, имена и сокращения законны
REPEAT_NGRAM = 4             # слов в повторяющейся фразе
REPEAT_TIMES = 3             # столько повторов подряд или в тексте - зацикливание
# Поток обрывается только на верном признаке: в коде и списках четыре слова повторяются законно, петля же идёт без конца,
# поэтому порог выше, а латиница не проверяется (вызов инструмента текстом - латиница в русском)
STREAM_REPEAT_TIMES = env_int("SAM_RUNAWAY_STREAM_REPEATS", 8)
# Агента никто не слушает: текстом он думает вслух, отмечает шаги и продумывает план, на это ~500 слов хватает. Код и
# длинное он пишет в файлы инструментами. Длиннее - модель спамит до предела ответа вместо действия (28 окт
# glm-5.3-flash: 15 тыс. знаков на оборот по цене выхода, до вызова инструмента не доходила)
AGENT_TEXT_MAX_CHARS = env_int("SAM_AGENT_TEXT_MAX_CHARS", 4000)
STREAM_CHECK_CHARS = env_int("SAM_RUNAWAY_CHECK_CHARS", 200)   # проверка раз в столько новых знаков: разбор идёт по всему тексту
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
WORD = re.compile(r"\w+", re.UNICODE)

TOO_LONG, LATIN, REPEATED, CJK = "spoken_too_long", "latin_in_russian", "repeated_ngrams", "hieroglyphs"
AGENT_TOO_LONG = "agent_text_too_long"
# Диапазоны CJK: унифицированные иероглифы, расширение A, каны, хангыль
CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3040, 0x30FF), (0xAC00, 0xD7AF))


@dataclass(frozen=True)
class Runaway:
    sign: str
    detail: str


def spoken(text: str) -> str:
    """Что звучит: текст без мета-блоков и тегов."""
    return speech_meta.strip_meta(text or "").strip()


def sentences(text: str) -> list[str]:
    return [p for p in SENTENCE_END.split(text.strip()) if p.strip()]


def _latin_share(text: str) -> tuple[float, int]:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0, 0
    latin = sum(1 for c in letters if c.isascii())
    return latin / len(letters), len(letters)


def _cjk_share(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if any(lo <= ord(c) <= hi for lo, hi in CJK_RANGES)) / len(chars)


def _repeats(text: str, times: int = REPEAT_TIMES) -> bool:
    words = [w.lower() for w in WORD.findall(text)]
    seen: dict[tuple[str, ...], int] = {}
    for i in range(len(words) - REPEAT_NGRAM + 1):
        gram = tuple(words[i:i + REPEAT_NGRAM])
        seen[gram] = seen.get(gram, 0) + 1
        if seen[gram] >= times:
            return True
    return False


def detect(text: str, max_sentences: int = MAX_SENTENCES, max_chars: int = MAX_CHARS,
           voice_limits: bool = True) -> Runaway | None:
    """Признак разноса озвучиваемой части ответа; None - ответ в порядке. voice_limits=False у агентов: они
    рассуждают и отчитываются подробно, длина и латиница (код, термины) не признак, остаются иероглифы и зацикливание."""
    return _signs(spoken(text), max_sentences, max_chars, voice_limits, latin=True, repeat_times=REPEAT_TIMES)


def detect_stream(text: str, voice_limits: bool = True, agent_max_chars: int = AGENT_TEXT_MAX_CHARS) -> Runaway | None:
    """Признак разноса в недописанном потоке: незакрытый мета-блок ещё не звучит и не считается (speech_meta.stream_meta).
    У агента (voice_limits=False) текст длиннее agent_max_chars - признак: он пишет текстом то, что место в файлах."""
    voice = speech_meta.stream_meta(text or "").strip()
    if not voice_limits and agent_max_chars and len(voice) > agent_max_chars:
        return Runaway(AGENT_TOO_LONG, f"{len(voice)} знаков текста при пороге {agent_max_chars}")
    return _signs(voice, MAX_SENTENCES, MAX_CHARS, voice_limits, latin=False, repeat_times=STREAM_REPEAT_TIMES)


def _signs(voice: str, max_sentences: int, max_chars: int, voice_limits: bool, latin: bool,
           repeat_times: int) -> Runaway | None:
    if not voice:
        return None
    count = len(sentences(voice))
    if voice_limits and count > max_sentences:
        return Runaway(TOO_LONG, f"{count} предложений при пороге {max_sentences}")
    if voice_limits and len(voice) > max_chars:
        return Runaway(TOO_LONG, f"{len(voice)} знаков при пороге {max_chars}")
    if (cjk := _cjk_share(voice)) > CJK_SHARE:
        return Runaway(CJK, f"иероглифы {cjk:.0%} знаков")
    share, letters = _latin_share(voice)
    cyrillic = any("а" <= c.lower() <= "я" or c.lower() == "ё" for c in voice)
    if latin and voice_limits and cyrillic and letters >= LATIN_MIN_LETTERS and share >= LATIN_SHARE:
        return Runaway(LATIN, f"латиница {share:.0%} букв в русском тексте")
    if _repeats(voice, repeat_times):
        return Runaway(REPEATED, f"фраза из {REPEAT_NGRAM} слов повторена {repeat_times} раз")
    return None


class Watch:
    """Страж потока: копит дельты и раз в STREAM_CHECK_CHARS знаков проверяет весь текст. feed отдаёт признак, когда
    поток пора оборвать; после первого признака отдаёт его же, текст не копит."""

    def __init__(self, voice_limits: bool = True, check_chars: int = STREAM_CHECK_CHARS):
        self._voice_limits, self._step = voice_limits, max(check_chars, 1)
        self._parts: list[str] = []
        self._size = self._checked = 0
        self.sign: Runaway | None = None

    def feed(self, piece: str) -> Runaway | None:
        if self.sign is None and piece:
            self._parts.append(piece)
            self._size += len(piece)
            if self._size - self._checked >= self._step:
                self._checked = self._size
                self.sign = detect_stream("".join(self._parts), self._voice_limits)
        return self.sign


def trim(text: str, keep_chars: int = KEEP_CHARS, max_sentences: int = MAX_SENTENCES) -> str:
    """Здравое начало: целые предложения, пока влезают в keep_chars и max_sentences и текст не показывает признаков
    разноса; первое предложение остаётся целиком, даже если длиннее."""
    kept: list[str] = []
    for sentence in sentences(text):
        candidate = kept + [sentence]
        joined = " ".join(candidate)
        if kept and (len(joined) > keep_chars or len(candidate) > max_sentences or detect(joined, max_sentences) is not None):
            break
        kept = candidate
    return " ".join(kept)
