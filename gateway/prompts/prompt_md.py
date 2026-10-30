"""Формат текстов промпта SAM в markdown, как CLAUDE.md: шапка подстановок и разделы `## ИМЯ`.

Шапка - строки между `---` в начале текста, YAML `ключ: значение`. Раздел - строки от заголовка до следующего
заголовка без краевых пустых строк; комментарии `<!-- ... -->` в промпт не идут. Текст до первого раздела - пояснение
для людей, модель его не получает. Формат общий для инструкций (SAM.md, gateway/prompts/instructions.py), души
(gateway/prompts/soul.py) и личного слоя (data/SAM.md, gateway/prompts/personal.py); метка источника в строке сбоя
приходит параметром.

В тексте две метки: `{женская|мужская}` форма - род речи (identity.render), `{ключ}` - подстановка из шапки (fill),
`{КЛЮЧ}` - она же заглавными.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTMATTER = "---"
DEFAULT_TAG = "[SAM.md]"
_HEADING = re.compile(r"^## +(\S+)\s*$", re.MULTILINE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_PLACEHOLDER = re.compile(r"\{([a-z_]+|[A-Z_]+)\}")   # метка рода содержит «|»; {КЛЮЧ} - значение заглавными


class PromptError(SystemExit):
    """Промпт не собрать: SystemExit со строкой печатает только её и выходит с кодом 1."""


@dataclass(frozen=True)
class PromptFile:
    meta: dict = field(default_factory=dict)        # подстановки из шапки
    body: str = ""                                  # всё после шапки, без комментариев и краевых пробелов
    sections: dict = field(default_factory=dict)    # имя раздела -> текст


def resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def frontmatter_lines(text: str, source: str, tag: str = DEFAULT_TAG) -> tuple[str | None, str]:
    """Текст шапки (без строк ---) и остаток; шапки нет - None и весь текст."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER:
        return None, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == FRONTMATTER), None)
    if end is None:
        raise PromptError(f"{tag} {source}: шапка не закрыта строкой ---; закрой шапку или убери её")
    return "\n".join(lines[1:end]), "\n".join(lines[end + 1:])


def parse_meta(header: str, source: str, tag: str = DEFAULT_TAG) -> dict:
    """Подстановки из текста шапки YAML; не словарь или не разобрана - PromptError одной строкой."""
    try:
        meta = yaml.safe_load(header) or {}
    except yaml.YAMLError as exc:
        raise PromptError(f"{tag} {source}: шапка не разобрана ({str(exc).splitlines()[0]}); поправь YAML в шапке")
    if not isinstance(meta, dict):
        raise PromptError(f"{tag} {source}: шапка не словарь; пиши строки «ключ: значение»")
    return {str(k): str(v) for k, v in meta.items()}


def raw_sections(body: str) -> dict:
    """Разделы как написаны, с комментариями."""
    heads = list(_HEADING.finditer(body))
    ends = [h.start() for h in heads[1:]] + [len(body)]
    return {h.group(1): body[h.end():end].strip("\n") for h, end in zip(heads, ends)}


def clean(raw: str) -> str:
    """Текст раздела для промпта: без комментариев и краевых пустых строк."""
    return _COMMENT.sub("", raw).strip("\n")


def parse(text: str, source: str, tag: str = DEFAULT_TAG) -> PromptFile:
    header, body = frontmatter_lines(text, source, tag)
    meta = {} if header is None else parse_meta(header, source, tag)
    body = _COMMENT.sub("", body)
    return PromptFile(meta, body.strip(), raw_sections(body))


_reported: set[str] = set()   # ключи без значения, о которых уже сказано: строка на ключ за процесс, а не на каждый ход


def fill(text: str, values: dict, log=print) -> str:
    """Подстановки {ключ} значениями, {КЛЮЧ} - значениями заглавными. Ключа нет - строка в лог один раз и {ключ} как
    есть: сборка промпта идёт в ходе, а PromptError - SystemExit, он ронял бы шлюз из-за опечатки в разделе."""
    def value(match: re.Match) -> str:
        key = match.group(1).lower()
        if key not in values:
            if key not in _reported:
                _reported.add(key)
                log(f"{DEFAULT_TAG} нет значения для {{{match.group(1)}}}; задай {key} в шапке SAM.md или data/SAM.md")
            return match.group(0)
        return values[key] if match.group(1) == key else values[key].upper()
    return _PLACEHOLDER.sub(value, text)


def unknown_placeholders(text: str, values: dict) -> list[str]:
    """Подстановки текста без значения: правка с ними пишется, но SAM и человек видят предупреждение."""
    return sorted({m.group(1) for m in _PLACEHOLDER.finditer(text) if m.group(1).lower() not in values})
