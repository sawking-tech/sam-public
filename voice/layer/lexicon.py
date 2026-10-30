"""Словарь произношения: слово -> написание для синтеза («стек» -> «стэк»), до запроса к любому движку.


Таблица - voice/layer/lexicon.txt (SAM_VOICE_LEXICON), формат описан в шапке файла. Применяет tts_client.stream:
через него идёт каждая реплика к Silero, CosyVoice и прочим, и сервер ставит ударения уже по заменённому слову.
Замена по целому слову с окончанием из списка: «стекло» и «истекать» не трогаются. Слово с ручной разметкой
ударения («ст+ек») не трогается: ручная разметка главнее (stress.py).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import LEXICON_FILE
from .stress import STRESS

LEXICON = LEXICON_FILE
LABEL = "[словарь]"
COMMENT = "#"
ASSIGN = "="
ENDINGS_SEP = ";"
# Падежи существительного мужского рода на твёрдую согласную, ед. и мн. число: стек, стека, стеку, стеком,
# стеке, стеки, стеков, стекам, стеками, стеках. Пустая строка - сама основа.
DEFAULT_ENDINGS = ("", "а", "у", "ом", "е", "и", "ов", "ам", "ами", "ах")
# Фраза из нескольких слов («на связи») - без окончаний: ударение зависит от соседнего слова, а не от падежа
PHRASE_ENDINGS = ("",)
# Граница слова: буква, цифра, «_» или знак ударения рядом - значит, это часть другого слова или ручная разметка
EDGE = rf"[\w{re.escape(STRESS)}]"


class LexiconError(ValueError):
    """Строка словаря не разобрана; текст - где и что не так."""


@dataclass(frozen=True)
class Entry:
    word: str
    spelling: str
    endings: tuple[str, ...] = DEFAULT_ENDINGS


def _match_case(sample: str, replacement: str) -> str:
    """Регистр замены как у слова в реплике: «Стек» -> «Стэк», «СТЕК» -> «СТЭК»."""
    if len(sample) > 1 and sample.isupper():
        return replacement.upper()
    if sample[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


class Lexicon:
    def __init__(self, entries: list[Entry]):
        self._spelling = {e.word.lower(): e.spelling for e in entries}
        self._pattern = self._compile(entries) if entries else None

    @staticmethod
    def _compile(entries: list[Entry]) -> re.Pattern:
        # Длинные основы раньше коротких: из двух подходящих берётся полное слово
        alts = []
        for e in sorted(entries, key=lambda e: -len(e.word)):
            endings = "|".join(re.escape(x) for x in sorted(e.endings, key=len, reverse=True))
            word = r"\s+".join(re.escape(part) for part in e.word.split())
            alts.append(f"(?P<w{len(alts)}>{word})(?:{endings})")
        return re.compile(rf"(?<!{EDGE})(?:{'|'.join(alts)})(?!{EDGE})", re.IGNORECASE)

    def __call__(self, text: str) -> str:
        if not self._pattern:
            return text
        return self._pattern.sub(self._replace, text)

    def _replace(self, m: re.Match) -> str:
        stem_group = next(name for name, value in m.groupdict().items() if value is not None)
        stem = m.group(stem_group)
        tail = m.group(0)[len(stem):]
        return _match_case(stem, self._spelling[" ".join(stem.lower().split())]) + tail


def parse(text: str, source: str = "словарь") -> Lexicon:
    entries = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split(COMMENT, 1)[0].strip()
        if not line:
            continue
        pair, _, endings = line.partition(ENDINGS_SEP)
        word, sep, spelling = (part.strip() for part in pair.partition(ASSIGN))
        if not sep or not word or not spelling:
            raise LexiconError(f"{source}:{number}: ждал «слово = написание [; окончания]», а там «{raw.strip()}»")
        word = " ".join(word.split())
        default = PHRASE_ENDINGS if " " in word else DEFAULT_ENDINGS
        entries.append(Entry(word, spelling, ("", *endings.split()) if endings.strip() else default))
    return Lexicon(entries)


def load(path: Path = LEXICON) -> Lexicon:
    """Словарь из файла; файла нет - пустой словарь: замены необязательны."""
    if not path.exists():
        return Lexicon([])
    try:
        return parse(path.read_text(encoding="utf-8"), str(path))
    except OSError as exc:
        raise LexiconError(f"словарь {path} не прочитан: {exc}") from exc


class Live:
    """Словарь, который перечитывается при правке файла. Сломанная правка не роняет синтез:
    остаётся последний разобранный словарь, в лог одна строка."""

    def __init__(self, path: Path = LEXICON, label: str = LABEL):
        self.path, self.label = path, label
        self._stamp: tuple[int, int] | None = None
        self._lexicon: Lexicon | None = None

    def get(self) -> Lexicon:
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != self._stamp or self._lexicon is None:
            self._stamp = stamp
            try:
                self._lexicon = load(self.path)
            except LexiconError as exc:
                print(f"{self.label} {exc}; остаюсь на прежнем словаре", file=sys.stderr, flush=True)
                self._lexicon = self._lexicon or Lexicon([])
        return self._lexicon
