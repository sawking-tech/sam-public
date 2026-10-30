"""Чтение числовых и булевых переменных окружения с типом и значением по умолчанию (SAM-80).

Раньше каждый модуль звал int(os.getenv(...)) на импорте, и мусор (SAM_MAX_WORKERS=abc) ронял gateway
стеком ValueError без имени переменной. Теперь мусор - одна строка в консоли: имя, что пришло, какой тип
ждали и какое значение взято; процесс поднимается со значением по умолчанию, как политика tools
при битом файле (tool_policy). Пустая переменная - то же, что не заданная.

Разбор (parse_*) отделён от чтения окружения и печати: проверяется без os.environ.
"""
from __future__ import annotations

import os
from typing import Callable, Mapping, TypeVar

T = TypeVar("T")

TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
FALSE_WORDS = frozenset({"0", "false", "no", "off"})
LOG_PREFIX = "[Env]"


class EnvValueError(ValueError):
    """Значение переменной не разобрано в нужный тип; текст - одна строка с именем и ожидаемым типом."""


def parse_int(name: str, raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError:
        raise EnvValueError(f"{name}={raw!r}: ждали целое число") from None


def parse_float(name: str, raw: str) -> float:
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError:
        raise EnvValueError(f"{name}={raw!r}: ждали число") from None


def parse_bool(name: str, raw: str) -> bool:
    word = raw.strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise EnvValueError(f"{name}={raw!r}: ждали {'/'.join(sorted(TRUE_WORDS | FALSE_WORDS))}")


def read(name: str, default: T, parse: Callable[[str, str], T],
         environ: Mapping[str, str] | None = None, warn: Callable[[str], None] = print) -> T:
    """Значение из окружения; не задано или пусто - default; мусор - строка в warn и default."""
    raw = (os.environ if environ is None else environ).get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return parse(name, raw)
    except EnvValueError as e:
        warn(f"{LOG_PREFIX} {e}; беру {default!r}")
        return default


def env_int(name: str, default: int, **kw) -> int:
    return read(name, default, parse_int, **kw)


def env_float(name: str, default: float, **kw) -> float:
    return read(name, default, parse_float, **kw)


def env_bool(name: str, default: bool, **kw) -> bool:
    return read(name, default, parse_bool, **kw)
