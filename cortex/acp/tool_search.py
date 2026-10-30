"""Поиск инструментов Cortex: модель видит базовые инструменты и мост tool_search, а не весь список MCP.

Полный список стенда (70 инструментов) - 37 тыс. знаков описаний на каждый вызов модели (замер 12 окт). Идея как у
tool_search Hermes Agent: в описании моста - имена всех инструментов, поиск BM25 по имени и описанию. Отличие:
найденный инструмент со следующего шага приходит модели полной нативной схемой, без косвенного tool_call, - локальной
модели так проще. Активными становятся и инструмент, вызванный по имени без поиска, и инструменты, названные в
итоге вызова: скилл (load_skill) говорит, каким инструментом пользоваться, и тот приходит со следующего шага.
"""
from __future__ import annotations

import re
from typing import Iterable

import snowballstemmer
from rank_bm25 import BM25Okapi

SEARCH_NAME = "tool_search"
TOKEN = re.compile(r"[^\W_]+", re.UNICODE)   # слова и числа; подчёркивание делит radar_where на radar и where
CYRILLIC = re.compile(r"[а-яё]")
NAME_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")   # имена инструментов MCP - латиница с подчёркиванием
# Основы слов: «радар» в запросе находит «по радару» в описании, files - file
_RU, _EN = snowballstemmer.stemmer("russian"), snowballstemmer.stemmer("english")
FOUND_NONE = "Ничего не найдено. Уточни запрос словами из имени или назначения инструмента."


def tokens(text: str) -> list[str]:
    words = [t.lower() for t in TOKEN.findall(text or "")]
    return [(_RU if CYRILLIC.search(w) else _EN).stemWord(w) for w in words]


class ToolCatalog:
    """BM25 по инструментам: имя и описание. Точное имя в запросе - первым."""

    def __init__(self, descriptions: dict[str, str]) -> None:
        self._names = list(descriptions)
        self._descriptions = descriptions
        docs = [tokens(f"{name} {descriptions[name]}") for name in self._names]
        self._bm25 = BM25Okapi(docs) if docs else None

    def search(self, query: str, limit: int) -> list[str]:
        exact = [n for n in self._names if n == query.strip()]
        if self._bm25 is None:
            return exact
        scores = self._bm25.get_scores(tokens(query))
        ranked = [n for s, n in sorted(zip(scores, self._names), key=lambda p: -p[0]) if s > 0]
        return (exact + [n for n in ranked if n not in exact])[:limit]

    def describe(self, name: str) -> str:
        return (self._descriptions.get(name) or "").strip().split("\n")[0]


def bridge_schema(names: Iterable[str]) -> dict:
    listing = ", ".join(names)
    return {"type": "function", "function": {
        "name": SEARCH_NAME,
        "description": (
            "Найти инструменты по задаче: ключевые слова (что сделать и с чем) или точное имя. Найденные "
            "становятся доступны со следующего шага. Уже доступные искать не нужно. Все инструменты: " + listing),
        "parameters": {"type": "object", "properties": {
            "queries": {"type": "array", "items": {"type": "string"},
                        "description": "Запросы, по одному на нужную возможность, например ['радар где', 'файл прочитать']"},
        }, "required": ["queries"]},
    }}


class ToolSearch:
    """Активные инструменты сессии: базовые, найденные поиском и вызванные по имени."""

    def __init__(self, catalog: ToolCatalog, all_names: Iterable[str], core: Iterable[str], limit: int) -> None:
        self._catalog = catalog
        self._all = list(all_names)
        self._limit = limit
        self.active: list[str] = [n for n in self._all if n in set(core)]

    def activate(self, names: Iterable[str]) -> None:
        for name in names:
            if name in self._all and name not in self.active:
                self.active.append(name)

    def activate_mentioned(self, text: str) -> None:
        """Инструменты, названные в тексте итога точным именем, - в активные."""
        words = set(NAME_WORD.findall(text or ""))
        self.activate(n for n in self._all if n in words)

    def bridge(self) -> dict:
        return bridge_schema(n for n in self._all if n not in self.active)

    async def call(self, args: dict) -> str:
        queries = args.get("queries") or args.get("query") or args.get("input") or []
        if isinstance(queries, str):
            queries = [queries]
        found: list[str] = []
        for query in queries:
            found += [n for n in self._catalog.search(str(query), self._limit) if n not in found]
        if not found:
            return FOUND_NONE
        self.activate(found)
        lines = [f"- {name}: {self._catalog.describe(name)}" for name in found]
        return "Доступны со следующего шага:\n" + "\n".join(lines)
