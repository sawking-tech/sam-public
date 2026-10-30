"""Секрет привилегированных команд (SAM-88): найти в тексте и замаскировать, чтобы он не утёк.

Секрет произносится голосом или пишется в чат монитора вместе с командой. Входы (слух, чат, inject) маскируют
его сразу и ставят признак `secret`: список имён названных ключей; стоки (история в Mongo, события монитора) прогоняют через scrub
всё, что пишут, - последняя линия для входа, который маскировку забыл. Опознание говорящего - не
аутентификация, признак секрета - да.

Whisper пишет число по-разному: «904317», «904 317», «904-317», «девятьсот четыре триста семнадцать»,
«девятьсот четыре тысячи триста семнадцать». Поэтому сравниваются цифры, а не текст: подряд идущие слова-числа
и группы цифр переводятся в строку цифр двумя способами - склейкой групп и разбором с тысячами.
Ключей может быть несколько (SAM-104): каждый - переменная `SAM_<НАЗНАЧЕНИЕ>_SECRET` в .env, имя назначения - её
середина в нижнем регистре (`SAM_COMMAND_SECRET` - `command`). Маскируются все, признак несёт имена названных.
Модуль чистый: секреты передаются параметром, окружение читают только load_secrets и load_secret.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

SECRET_ENV = "SAM_COMMAND_SECRET"
SECRET_ENV_PATTERN = re.compile(r"^SAM_([A-Z0-9]+(?:_[A-Z0-9]+)*)_SECRET$")
COMMAND_PURPOSE = "command"   # привилегированные команды: gateway/command_auth.py
MASK = "[секрет]"

UNITS = {"ноль": 0, "один": 1, "одна": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
         "шесть": 6, "семь": 7, "восемь": 8, "девять": 9}
TEENS = {"десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
         "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19}
TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
        "восемьдесят": 80, "девяносто": 90}
HUNDREDS = {"сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500, "шестьсот": 600,
            "семьсот": 700, "восемьсот": 800, "девятьсот": 900}
THOUSAND = ("тысяча", "тысячи", "тысяч")
WORDS = {**UNITS, **TEENS, **TENS, **HUNDREDS}
# Порядки слов-чисел для склейки: после 10-19 группа закрыта, после цифр - тоже
TEEN_RANK = 0
MAX_RANK = 4

TOKEN = re.compile(r"\d+|[а-яё]+", re.IGNORECASE)
# Между частями числа допустимы только пробелы и знаки, буквы и слова не-числа разрывают число
GAP = re.compile(r"^[\s,.\-–—:/]*$")


@dataclass(frozen=True)
class Masked:
    text: str
    secret: bool
    names: frozenset[str] = field(default_factory=frozenset)   # назначения ключей, названных точно


def load_secret() -> str:
    """Секрет команд из окружения (.env); пусто - проверка секрета выключена."""
    return re.sub(r"\D", "", os.getenv(SECRET_ENV, ""))


def load_secrets(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Все ключи из окружения: назначение -> цифры; пустые пропускаются."""
    out = {}
    for key, value in (os.environ if environ is None else environ).items():
        m = SECRET_ENV_PATTERN.match(key)
        digits = re.sub(r"\D", "", value)
        if m and digits:
            out[m.group(1).lower()] = digits
    return out


def _as_map(secret: str | Mapping[str, str]) -> dict[str, str]:
    """Один ключ строкой - ключ команд; пустые ключи отбрасываются."""
    pairs = {COMMAND_PURPOSE: secret}.items() if isinstance(secret, str) else secret.items()
    return {name: value for name, value in pairs if value}


def _is_number(tok: str) -> bool:
    low = tok.lower()
    return tok.isdigit() or low in WORDS or low in THOUSAND


def _rank(tok: str) -> int:
    """Порядок слова-числа: 1 единицы и 10-19, 2 десятки, 3 сотни; цифры - по длине."""
    low = tok.lower()
    if low in HUNDREDS:
        return 3
    if low in TENS:
        return 2
    return 1


def _glued(tokens: list[str]) -> str:
    """Склейка групп: «девятьсот четыре триста семнадцать» -> 904 и 317 -> «904317»; «ноль» - цифра 0."""
    out: list[str] = []
    group: int | None = None
    last = MAX_RANK
    for tok in tokens:
        low = tok.lower()
        if tok.isdigit():
            if group is not None:
                out.append(str(group))
            out.append(tok)
            group, last = None, MAX_RANK
            continue
        if low in THOUSAND:
            continue
        rank = _rank(tok)
        if group is not None and rank >= last:   # «триста семнадцать» - продолжение, «семнадцать триста» - новая группа
            out.append(str(group))
            group = None
        group = (group or 0) + WORDS[low]
        last = TEEN_RANK if low in TEENS else rank
    if group is not None:
        out.append(str(group))
    return "".join(out)


def _parsed(tokens: list[str]) -> str:
    """Разбор с тысячами: «девятьсот четыре тысячи триста семнадцать» -> «904317»."""
    if any(t.isdigit() for t in tokens):
        return ""
    total, group = 0, 0
    for tok in tokens:
        low = tok.lower()
        if low in THOUSAND:
            total += (group or 1) * 1000
            group = 0
        else:
            group += WORDS[low]
    return str(total + group)


def _runs(text: str) -> list[tuple[int, int, list[str]]]:
    """Подряд идущие числа во фразе: (начало, конец, токены)."""
    runs, cur, start, end = [], [], 0, 0
    for m in TOKEN.finditer(text):
        tok = m.group()
        joined = cur and GAP.match(text[end:m.start()])
        if _is_number(tok) and (joined or not cur):
            if not cur:
                start = m.start()
            cur.append(tok)
            end = m.end()
            continue
        if cur:
            runs.append((start, end, cur))
        cur = [tok] if _is_number(tok) else []
        start, end = (m.start(), m.end()) if cur else (0, 0)
    if cur:
        runs.append((start, end, cur))
    return runs


def mask(text: str, secret: str | Mapping[str, str]) -> Masked:
    """Фраза без секретов: число, в цифрах которого есть секрет, заменяется на MASK целиком.

    Признак и names - только при точном совпадении числа с секретом: «19043175» маскируется, но командой не правит."""
    secrets = _as_map(secret)
    if not secrets or not text:
        return Masked(text, False)
    spans, names = [], set()
    for start, end, toks in _runs(text):
        glued, parsed = _glued(toks), _parsed(toks)
        hit = False
        for name, value in secrets.items():
            if value in glued or value == parsed:
                hit = True
                if value in (glued, parsed):
                    names.add(name)
        if hit:
            spans.append((start, end))
    for start, end in reversed(spans):
        text = text[:start] + MASK + text[end:]
    return Masked(text, bool(names), frozenset(names))


def scrub(value: Any, secret: str | Mapping[str, str]) -> Any:
    """Копия значения без секрета: строки маскируются, словари и списки - вглубь, прочее как есть."""
    if not _as_map(secret):
        return value
    if isinstance(value, str):
        return mask(value, secret).text
    if isinstance(value, dict):
        return {k: scrub(v, secret) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(scrub(v, secret) for v in value)
    return value
