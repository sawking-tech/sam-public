"""Проверка внутренних ссылок документации: файл, названный в README или docs, существует.

CI зовёт это на каждый PR. Ловит ссылку вида `docs/features/apps.md` или [текст](docs/adr/021-...md),
у которой файла нет: переезд документа оставляет за собой такие ссылки, и в живом дереве они не видны.

Запуск:
  python scripts/check_docs_links.py [--root .]

Внешние ссылки (http, https, mailto) не проверяются: сеть на CI не наша забота.
Код выхода 1 - есть битые ссылки, каждая строкой; 0 - все на месте.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Где искать: корень README и весь docs/. Скиллы и CLAUDE.md тоже ссылаются на доки
SOURCES = ("README.md", "CLAUDE.md", "docs", ".claude/skills")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
# Ссылка на файл: в бэктиках или markdown-скобками. Ловим только пути с расширением - голый `docs/` не файл
MD_LINK = re.compile(r"\]\(([^)\s]+)")
INLINE = re.compile(r"`([^`\n]+)`")
FILE_EXT = (".md", ".py", ".json", ".yml", ".yaml", ".ps1", ".sh", ".html", ".txt")
# Каталоги репозитория: имя в бэктиках проверяется, только если начинается с одного из них.
# Иначе `agent_loop.py` в тексте читается как путь, хотя это имя модуля
TRACKED_DIRS = ("docs/", "gateway/", "voice/", "radar/", "scripts/", "tests/", "mcp/", "cortex/",
                "workflows/", "docker/", "n8n-workflows/", ".claude/")
# data/ - данные стенда, вне git: ссылка на файл там правильна и на CI не проверяема
UNCHECKED_PREFIXES = ("data/", "logs/", ".claude/session-context/")


def _iter_docs(root: Path):
    for name in SOURCES:
        p = root / name
        if p.is_file():
            yield p
        elif p.is_dir():
            for f in p.rglob("*.md"):
                if not any(part in SKIP_DIRS for part in f.parts):
                    yield f


def _prose_lines(text: str):
    """Строки вне блоков кода: в ```-блоке пути - пример или шаблон, ссылками они не являются."""
    fenced = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield n, line


def _targets(text: str) -> list[tuple[int, str]]:
    out = []
    for n, line in _prose_lines(text):
        for m in MD_LINK.finditer(line):
            out.append((n, m.group(1)))
        for m in INLINE.finditer(line):
            out.append((n, m.group(1)))
    return out


def _strip_target(raw: str) -> str | None:
    """Путь, который стоит проверять, или None: внешнее, якорь, шаблон и имя модуля без папки отсеиваются."""
    t = raw.split("#", 1)[0].split("?", 1)[0].strip()
    if not t or "://" in t or t.startswith(("mailto:", "/", "<", "{")):
        return None
    if t.startswith(UNCHECKED_PREFIXES):
        return None
    if not t.endswith(FILE_EXT) or "*" in t:
        return None                      # шаблон вида voice/layer/*.py - не один файл
    if "/" not in t:
        return None                      # имя файла без папки: сокращение в прозе («audio-core.md» рядом с docs/features/)
    if not t.startswith(TRACKED_DIRS):
        return None                      # путь вне известных папок: `pip install -r ...`, примеры
    return t


def check(root: Path) -> list[str]:
    """Битые ссылки строкой `файл:строка -> цель`. Цель ищется от корня репозитория и от самого файла."""
    broken = []
    for doc in _iter_docs(root):
        text = doc.read_text(encoding="utf-8", errors="replace")
        for line_no, raw in _targets(text):
            target = _strip_target(raw)
            if target is None:
                continue
            if (root / target).exists() or (doc.parent / target).exists():
                continue
            broken.append(f"{doc.relative_to(root)}:{line_no} -> {target}")
    return broken


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=Path(__file__).resolve().parents[1], type=Path)
    args = ap.parse_args()
    broken = check(args.root)
    for line in broken:
        print(line)
    sys.exit(1 if broken else 0)


if __name__ == "__main__":
    main()
