"""Кому сказана фраза (SAM-77): мне, не мне, фон или «не уверен».

SAM слушает всегда, поэтому в шину идёт всё, что звучит дома. Код решает только ясные случаи,
остальное - UNSURE: такую фразу видит модель хода вместе с тем, кто говорит и сколько прошло после
ответа SAM, и вправе промолчать. Правила по порядку, первое сработавшее решает:

1. эхо своей озвучки, свой голос или фраза без слов («...») - фон;
2. обращение к питомцу в начале фразы (клички из SAM_CAT_NAMES, «кис») - не мне;
3. разрешение говорить («можно говорить», «выключи тихий режим») - фон: тишина снимается, звучит готовое «слушаю», модель не нужна;
4. просьба помолчать или «включи тихий режим» - фон: SAM молча слушается, а не отвечает «хорошо»; одно «стоп» («Сэм, хватит!») - тоже просьба;
5. имя SAM обращением (в начале фразы, отдельным словом с запятой или вся фраза) - мне;
6. команда (продолжай, повтори, «стоп, а ...») в окне беседы - мне;
7. просили помолчать - фон до обращения по имени: ни вызова модели;
8. обрывок: одно-два слова от неузнанного голоса вне окна беседы - фон («привет» человека идёт модели);
9. остальное - UNSURE.

Тишина - состояние сессии в gateway/quiet_mode.py (файл шины, общий с мостом): просьба помолчать включает её (next_quiet),
обращение по имени и разрешение говорить снимают.

Чего код не решает, проверено на записях разговоров (журнал conversation): «голос не узнан - фон» ошибается,
потому что опознание пропускает и своих (333 из 1572 фраз без who); «нет обращения на ты -
не мне» ошибается на просьбах без местоимения («Повтори ещё раз»). Это решает модель по смыслу.

Окно беседы - CONVERSATION_WINDOW_S после последней реплики SAM. Имена SAM и питомцев - из env.
Логика без звука и сети: на вход текст, кто сказал и сколько прошло, на выход решение с причиной.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from .env import env_float

ME, NOT_ME, BACKGROUND, UNSURE = "me", "not_me", "background", "unsure"
SELF_PROFILE = os.getenv("SAM_SELF_PROFILE", "claude")
# Окно беседы: после реплики SAM ответ человека скорее ей. Не замерено; в записях разговоров беседа держится репликами
# через 5-20 с (voice-in.jsonl)
CONVERSATION_WINDOW_S = env_float("SAM_CONVERSATION_WINDOW_S", 30.0)


def _words(env: str, default: str) -> tuple[str, ...]:
    return tuple(w.strip().lower() for w in os.getenv(env, default).split(",") if w.strip())


# Как зовут SAM: основы имени; обращение - основа с одним из окончаний NAME_ENDINGS («Сэм», «Сэма», «Сэмик»).
# Не «любое слово с этой основы»: тогда «Семь утра» и «Семья спит» были бы обращением (rough-edges-2)
NAMES = _words("SAM_NAMES", "сэм,сем,sam")
NAME_ENDINGS = ("", "а", "у", "ом", "е", "ик", "ика", "ику", "иком", "чик", "ушка", "s")
_WORD = r"\w[\w-]*"   # слово начинается с буквы или цифры: «-» и «...» - не слова
# Обращения к домашним питомцам: основы кличек - в .env (SAM_CAT_NAMES), «кис» - любой кот
CAT_WORDS = _words("SAM_CAT_NAMES", "кис")
# Просьба помолчать: SAM молчит и не зовёт модель, пока её не позовут по имени
# Сверка по целым словам: «молчи» не ловит «чё молчишь» - это зовут, а не просят молчать
QUIET_PHRASES = _words("SAM_QUIET_PHRASES", "помолчи,молчи,замолчи,замолкни,умолкни,не озвучивай,не отвечай,"
                                            "не встревай,не мешай,мы разговариваем,мы тут разговариваем,тишина,"
                                            "тихий режим,включи тихий режим,режим тишины,не слушай")
# Разрешение говорить снимает тишину без имени; проверяется раньше просьбы помолчать: «выключи тихий режим» содержит
# «тихий режим». Слова короче фразы («говори») не берутся: «говори громче» тишину не снимает (12 окт, gateway/quiet_mode.py)
SPEAK_PHRASES = _words("SAM_QUIET_EXIT_PHRASES", "можно говорить,можешь говорить,отомри,хватит молчать,"
                                                 "выключи тихий режим,отключи тихий режим,выйди из тихого режима")
# Остановка, сказанная одна (можно с именем): «Стоп», «Сэм, хватит!» - тоже просьба помолчать. «Стоп» раньше
# шёл командой модели, и она ответила «Всё, замерла. Что скажешь?», а потом отвечала на разговор без имени
STOP_WORDS = _words("SAM_STOP_WORDS", "стоп,стой,хватит,тише")
COMMANDS = ("стоп", "стой", "погоди", "подожди", "хватит", "тише", "продолжай", "дальше", "повтори", "ещё раз", "еще раз")
SHORT_WORDS = 2


@dataclass(frozen=True)
class Heard:
    text: str
    who: str | None = None                  # профиль голоса (SAM-30) или None - не узнан
    echo: bool = False
    since_sam_s: float | None = None        # сколько секунд назад звучала SAM; None - не звучала
    quiet: bool = False                     # сессия в тишине: просили помолчать


@dataclass(frozen=True)
class Verdict:
    to: str
    reason: str


def decide(h: Heard, names: tuple[str, ...] = NAMES, window_s: float = CONVERSATION_WINDOW_S) -> Verdict:
    words = re.findall(_WORD, h.text.lower())
    in_window = h.since_sam_s is not None and h.since_sam_s <= window_s
    if h.echo or h.who == SELF_PROFILE:
        return Verdict(BACKGROUND, "своя речь")
    if not words:   # «...» и тире - мусор распознавания, модель на него не зовётся
        return Verdict(BACKGROUND, "нет слов")
    if _starts(words[0], CAT_WORDS) and not any(_is_name(w, names) for w in words):
        return Verdict(NOT_ME, "обращение к коту")
    if asks_speak(h.text):
        return Verdict(BACKGROUND, "разрешили говорить")
    if asks_quiet(h.text, names=names):
        return Verdict(BACKGROUND, "просьба помолчать")
    if _vocative(h.text, words, names):
        return Verdict(ME, "имя обращением")
    if h.quiet:
        return Verdict(BACKGROUND, "просили помолчать")
    if in_window and _is_command(h.text):
        return Verdict(ME, "команда в беседе")
    if not in_window and h.who is None and len(words) <= SHORT_WORDS and not _is_command(h.text):
        return Verdict(BACKGROUND, "обрывок вне беседы")
    return Verdict(UNSURE, "решает модель хода")


def _has_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    t = text.lower()
    return any(re.search(rf"(?<!\w){re.escape(p)}(?!\w)", t) for p in phrases)


def asks_speak(text: str, phrases: tuple[str, ...] = SPEAK_PHRASES) -> bool:
    """Разрешение говорить («можно говорить», «выключи тихий режим»): снимает тишину без обращения по имени."""
    return _has_phrase(text, phrases)


def asks_quiet(text: str, phrases: tuple[str, ...] = QUIET_PHRASES, stops: tuple[str, ...] = STOP_WORDS,
               names: tuple[str, ...] = NAMES) -> bool:
    """Просьба помолчать: одна из фраз целыми словами или фраза из одних остановок и имени; разрешение говорить - не просьба."""
    if asks_speak(text):
        return False
    if _has_phrase(text, phrases):
        return True
    rest = [w for w in re.findall(_WORD, text.lower()) if not _is_name(w, names)]
    return bool(rest) and all(w in stops for w in rest)


def next_quiet(quiet: bool, text: str, verdict: Verdict) -> bool:
    """Тишина после фразы: разрешение говорить и обращение по имени снимают, просьба помолчать включает, остальное не меняет."""
    if asks_speak(text):
        return False
    if asks_quiet(text):
        return True
    return False if verdict.to == ME else quiet


def is_vocative(text: str, names: tuple[str, ...] = NAMES) -> bool:
    """Имя SAM обращением в фразе (то же правило, что в decide)."""
    return _vocative(text, re.findall(_WORD, text.lower()), names)


def _starts(word: str, stems: tuple[str, ...]) -> bool:
    return any(word.startswith(s) for s in stems)


def _vocative(text: str, words: list[str], names: tuple[str, ...]) -> bool:
    """Имя обращением: первое слово фразы или слово между запятыми («Слушай, Сэм, ...», «..., Сэм?»).
    Имя внутри фразы без запятых - упоминание («в Сэм я сделал анимацию»), не обращение."""
    if words and _is_name(words[0], names):
        return True
    return any(_is_name(m, names) for m in re.findall(rf",\s*({_WORD})\s*(?:[,!?.]|$)", text.lower()))


def _is_name(word: str, names: tuple[str, ...]) -> bool:
    return any(word == stem + ending for stem in names for ending in NAME_ENDINGS)


def _is_command(text: str) -> bool:
    t = text.lower().strip(" !?.")
    return any(t == c or t.startswith(c + " ") or t.startswith(c + ",") for c in COMMANDS)
