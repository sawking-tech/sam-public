"""Страховка речи перед синтезом: в движок уходят только слова (ADR-003).

Применяется ПОСЛЕ LLM — если модель забыла правило устной речи (раздел ORAL_SPEECH в SAM.md).
Silero цифры, знаки и латиницу молча пропускает, другие движки читают их каждый по-своему.

normalize_for_tts - весь проход, по порядку (голосовой слой, voice/layer/tts_client.stream):
1. Путь Windows или Unix и ссылка не зачитываются посимвольно: на их месте слово «путь» или «ссылка».
   Имя внутри пути почти всегда латиницей и всё равно не прозвучит; назвать папку словами - дело модели.
2. Даты цифрами «08.10.2026», время «14:11», «20 мая», «2026 год» - словами (normalize_numbers_for_tts).
3. Знаки словами: процент, градус, номер, и, равно, умножить на, разделить на, плюс, минус.
   «+» перед гласной - ручная разметка ударения (voice/layer/stress.py), его не трогаем.
4. Номер версии «3.11.2» - через «точка», остальные числа и дроби - словами (num2words, именительный падеж).
Латиница не трогается: её пишет кириллицей модель; транслитерация по буквам
звучит неверно («докер» из «docker» не выводится), аббревиатуры по буквам ломают имена («SAM»).

normalize_numbers_for_tts - только шаг 2 (старый клиент voice/voice_client.py).
"""
from __future__ import annotations

import re

try:
    from num2words import num2words
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


_MONTHS_GEN = {
    "январ": "января",  "феврал": "февраля", "март": "марта",
    "апрел": "апреля",  "май": "мая",        "июн": "июня",
    "июл": "июля",      "август": "августа", "сентябр": "сентября",
    "октябр": "октября","ноябр": "ноября",   "декабр": "декабря",
}
_MONTH_PATTERN = "|".join(re.escape(m) for m in _MONTHS_GEN.values())
_MONTH_NAMES = list(_MONTHS_GEN.values())     # по порядку, январь первым
# Год, который читается как год; за пределами - просто число
YEAR_MIN, YEAR_MAX = 1900, 2200

_HOUR_FORM = {
    1: "час",    21: "час",   31: "час",
    2: "часа",   3: "часа",   4: "часа",
    22: "часа",  23: "часа",  24: "часа",
}
_MIN_FORM = {
    1: "минута",   21: "минута",  31: "минута",  41: "минута",  51: "минута",
    2: "минуты",   3: "минуты",   4: "минуты",
    22: "минуты",  23: "минуты",  24: "минуты",
    32: "минуты",  33: "минуты",  34: "минуты",
    42: "минуты",  43: "минуты",  44: "минуты",
    52: "минуты",  53: "минуты",  54: "минуты",
}


def _hours_word(n: int) -> str:
    return _HOUR_FORM.get(n, "часов")


def _minutes_word(n: int) -> str:
    if n == 0:
        return "ровно"
    return _MIN_FORM.get(n, "минут")


def _num_to_ordinal_gen(n: int) -> str:
    """Число → порядковое в родительном падеже («двадцатого»).
    num2words to='ordinal' даёт И.п. — корректируем окончание."""
    word = num2words(n, lang="ru", to="ordinal")
    # двадцатый → двадцатого, пятый → пятого, первый → первого, третий → третьего
    if word.endswith("ый") or word.endswith("ой"):
        return word[:-2] + "ого"
    if word.endswith("ий"):
        return word[:-2] + "ьего"
    return word


def _replace_time(m: re.Match) -> str:
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return m.group(0)
    h_word = num2words(h, lang="ru") + " " + _hours_word(h)
    if mi == 0:
        return h_word + " ровно"
    mi_word = num2words(mi, lang="ru") + " " + _minutes_word(mi)
    return f"{h_word} {mi_word}"


def _replace_date_month(m: re.Match) -> str:
    day = int(m.group(1))
    month = m.group(2)
    if not (1 <= day <= 31):
        return m.group(0)
    return f"{_num_to_ordinal_gen(day)} {month}"


def _decline_ordinal(ord_word: str, case: str) -> str:
    """Порядковое И.п. м.р. («...двадцать шестой») -> Р.п. ('gen') или П.п. ('loc').
    Окончания И.п.: -ый (первый/пятый), -ой (шестой/восьмой), -ий (третий)."""
    def repl(m: re.Match) -> str:
        stem, soft = m.group(1), m.group(2) == "ий"
        if case == "gen":
            return stem + ("его" if soft else "ого")
        return stem + ("ем" if soft else "ом")
    return re.sub(r"(\w+?)(ый|ой|ий)\b", repl, ord_word)


def _replace_year(m: re.Match) -> str:
    year = int(m.group(1))
    case_word = m.group(2)  # год / года / году / годе
    if not (YEAR_MIN <= year <= YEAR_MAX):
        return m.group(0)
    ord_word = num2words(year, lang="ru", to="ordinal")  # «...двадцать шестой»
    if case_word == "года":     # Р.п.
        ord_word = _decline_ordinal(ord_word, "gen")
    elif case_word in ("году", "годе"):  # П.п.
        ord_word = _decline_ordinal(ord_word, "loc")
    return f"{ord_word} {case_word}"


def _replace_numeric_date(m: re.Match) -> str:
    """«08.10.2026» -> «восьмого октября две тысячи двадцать шестого года»."""
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= day <= 31 and 1 <= month <= len(_MONTH_NAMES) and YEAR_MIN <= year <= YEAR_MAX):
        return m.group(0)
    year_word = _decline_ordinal(num2words(year, lang="ru", to="ordinal"), "gen")
    return f"{_num_to_ordinal_gen(day)} {_MONTH_NAMES[month - 1]} {year_word} года"


def normalize_numbers_for_tts(text: str) -> str:
    """Fallback-нормализация: цифры в наиболее «опасных» паттернах
    превращаются в слова. Если num2words недоступен — текст не трогаем."""
    if not _AVAILABLE or not text:
        return text

    # ДД.ММ.ГГГГ - раньше времени и чисел: иначе прочиталось бы тремя числами
    text = re.sub(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", _replace_numeric_date, text)

    # HH:MM (1-2 цифры : 2 цифры)
    text = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", _replace_time, text)

    # N <месяц> (день + месяц в Р.п.)
    text = re.sub(
        rf"\b(\d{{1,2}})\s+({_MONTH_PATTERN})\b",
        _replace_date_month, text,
    )

    # YYYY год / года / году
    text = re.sub(
        r"\b(\d{4})\s+(год[ауе]?)\b",
        _replace_year, text,
    )

    return text


# ── Пути и ссылки ─────────────────────────────────────────────────────────
PATH_WORD, URL_WORD = "путь", "ссылка"
# Хвостовая пунктуация предложения - не часть пути: «лежит в C:\web.» -> «лежит в путь.»
_TRAILING = ".,;:!?)»\"'"
_URL = re.compile(r"\b(?:https?|ftp)://\S+|\bwww\.\S+", re.I)
_PATH = re.compile(
    r"\b[A-Za-z]:[\\/]\S*"                     # C:\Users\web, D:/dev
    r"|\\\\[\w.$-]+\\\S*"                      # \\server\share
    r"|(?<![\w/])~?/(?:[\w.-]+/)+[\w.-]*"      # /home/x/y, ~/dev/sam (два сегмента и больше)
    r"|\b[\w.-]+(?:\\[\w.-]+)+"                # gateway\static\monitor.html
    r"|\b[\w.-]+(?:/[\w.-]+){2,}"              # gateway/static/monitor.html; «и/или» не трогаем
)


def _spoken_as(word: str):
    def repl(m: re.Match) -> str:
        found = m.group(0)
        body = found.rstrip(_TRAILING)
        return word + found[len(body):]
    return repl


def replace_paths(text: str) -> str:
    """Путь и ссылка не зачитываются по символам: на их месте одно слово."""
    return _PATH.sub(_spoken_as(PATH_WORD), _URL.sub(_spoken_as(URL_WORD), text))


# ── Знаки ──────────────────────────────────────────────────────────────────
VOWELS = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"
_NUM = r"\d+(?:[.,]\d+)?"
PERCENT_FORMS = ("процент", "процента", "процентов")
DEGREE_FORMS = ("градус", "градуса", "градусов")
# Знак словом с пробелами вокруг, лишние пробелы потом схлопываются
_SIGN_WORDS = (
    (re.compile(r"№\s*"), "номер "),
    (re.compile(r"\s*&\s*"), " и "),
    (re.compile(r"\s*=\s*"), " равно "),
    (re.compile(r"(?<=\d)\s*[×*]\s*(?=\d)"), " умножить на "),
    (re.compile(r"(?<=\d)\s*[/÷]\s*(?=\d)"), " разделить на "),
    (re.compile(r"\+(?![" + VOWELS + r"])"), " плюс "),       # «+» перед гласной - ударение
    (re.compile(r"(?<=\d)\s+[−–]\s+(?=\d)"), " минус "),        # «5 − 3»; дефис «5 - 7» - диапазон, не трогаем
    (re.compile(r"(?<![\w.,])[−-](?=\d)"), "минус "),          # «-5»; «1-2» - диапазон
)


def plural_form(number: str, forms: tuple[str, str, str]) -> str:
    """Форма слова после числа: 1 процент, 2 процента, 5 процентов; дробь - «процента»."""
    if re.search(r"[.,]\d", number):
        return forms[1]
    n = int(number)
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _unit(forms: tuple[str, str, str]):
    return lambda m: f"{m.group(1)} {plural_form(m.group(1), forms)}"


def replace_signs(text: str) -> str:
    """Знаки словами; число остаётся цифрами - его прочитает replace_numbers."""
    text = re.sub(rf"({_NUM})\s*%", _unit(PERCENT_FORMS), text)
    text = re.sub(rf"({_NUM})\s*°\s*[CСcс]?(?!\w)", _unit(DEGREE_FORMS), text)
    for pattern, word in _SIGN_WORDS:
        text = pattern.sub(word, text)
    return text


# ── Числа ──────────────────────────────────────────────────────────────────
VERSION_JOIN = " точка "
_VERSION = re.compile(r"\b\d+(?:\.\d+){2,}\b")
_DECIMAL = re.compile(r"(?<![\d.,])(\d+)[.,](\d+)(?!\d|[.,]\d)")
_INTEGER = re.compile(r"\d+")


def _version_words(m: re.Match) -> str:
    return VERSION_JOIN.join(num2words(int(x), lang="ru") for x in m.group(0).split("."))


def replace_numbers(text: str) -> str:
    """Оставшиеся цифры словами: после этого шага цифр в речи нет."""
    text = _VERSION.sub(_version_words, text)
    text = _DECIMAL.sub(lambda m: num2words(float(f"{m.group(1)}.{m.group(2)}"), lang="ru"), text)
    return _INTEGER.sub(lambda m: num2words(int(m.group(0)), lang="ru"), text)


def _squeeze_spaces(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def normalize_for_tts(text: str) -> str:
    """Весь проход страховки перед синтезом (порядок - в шапке модуля). Без num2words - без шагов с числами."""
    if not text:
        return text
    text = replace_paths(text)
    if _AVAILABLE:
        text = normalize_numbers_for_tts(text)
    text = replace_signs(text)
    if _AVAILABLE:
        text = replace_numbers(text)
    return _squeeze_spaces(text)
