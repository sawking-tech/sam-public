"""Автозапись памяти (SAM-63, ADR-015): факты извлекаются из куска истории, который сжатие уберёт.

Сжатие архивирует и удаляет старые сообщения; факты из них раньше попадали в долгую память
только через `remember`. Теперь перед удалением куска (`compression.compress_session_history`, параметр
on_forget) один вызов LLM извлекает факты; каждый факт только добавляется через `RAGStore.remember`
(Mongo - источник правды, Qdrant - оглавление) с источником: сессия и отрезок времени. Повтор
отсекается поиском по памяти: близкий факт уже есть - не пишется.

Сжатие идёт в паузах (ворота простоя, SAM-62), поэтому и извлечение в паузах. Сбой извлечения не роняет
сжатие: оригиналы куска уже лежат в архиве. Включается SAM_MEMORY_AUTOWRITE=1: промпт не проверен вживую,
а запись в память не отыгрывается назад правкой кода.

Разбор ответа модели (parse_facts) и решение о повторе отделены от вызова LLM и хранилища.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .prompts.extract import EXTRACT_FACTS_PROMPT
from .env import env_bool, env_float

ENABLED = env_bool("SAM_MEMORY_AUTOWRITE", False)
AUTO_TAG = "auto"                 # метка факта, записанного автозаписью: видно в list_memories
# Близость к уже записанному факту, с которой новый считается повтором. Не замерено на памяти SAM;
# поиск отдаёт косинусную близость эмбеддингов (rag.RAGStore.search)
DUP_SCORE = env_float("SAM_MEMORY_DUP_SCORE", 0.85)
MAX_FACTS = 10                    # с одного куска: больше - модель пересказывает разговор
TEXT_MAX_CHARS = 500
SUMMARY_MAX_CHARS = 80
QUESTIONS_MAX = 3
MESSAGE_MAX_CHARS = 1000          # одно сообщение в промпте извлечения
_JSON = re.compile(r"\{.*\}", re.S)
_THINK = re.compile(r"<think>.*?</think>", re.S)


@dataclass(frozen=True)
class Fact:
    text: str
    summary: str = ""
    questions: tuple[str, ...] = field(default_factory=tuple)


def build_prompt(messages: list[dict]) -> str:
    lines = [f"{m.get('role', '?')}: {str(m.get('content') or '')[:MESSAGE_MAX_CHARS]}" for m in messages
             if str(m.get("content") or "").strip()]
    return EXTRACT_FACTS_PROMPT.format(text="\n".join(lines))


def parse_facts(raw: str) -> list[Fact]:
    """Факты из ответа модели; не JSON, не та форма, пустые строки - ничего."""
    m = _JSON.search(_THINK.sub("", raw or ""))
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    items = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    facts = []
    for item in items[:MAX_FACTS]:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        questions = item.get("questions") if isinstance(item.get("questions"), list) else []
        facts.append(Fact(str(item["text"]).strip()[:TEXT_MAX_CHARS],
                          str(item.get("summary") or "").strip()[:SUMMARY_MAX_CHARS],
                          tuple(str(q).strip() for q in questions[:QUESTIONS_MAX] if str(q).strip())))
    return facts


def source_of(session_id: str, messages: list[dict]) -> str:
    stamps = [m.get("timestamp") for m in messages if m.get("timestamp") is not None]
    span = f" {stamps[0]:%Y-%m-%d %H:%M}-{stamps[-1]:%H:%M}" if stamps and hasattr(stamps[0], "strftime") else ""
    return f"auto:{session_id}{span}"


class AutoWriter:
    """llm(prompt) -> текст ответа; store - RAGStore (search, remember)."""

    def __init__(self, llm: Callable[[str], Awaitable[str]], store, dup_score: float = DUP_SCORE) -> None:
        self._llm, self._store, self._dup_score = llm, store, dup_score

    async def __call__(self, session_id: str, messages: list[dict]) -> list[str]:
        """Извлечь и записать факты куска; вернуть записанные тексты."""
        if not messages:
            return []
        facts = parse_facts(await self._llm(build_prompt(messages)))
        if not facts:
            return []
        # Эмбеддер на CPU и Mongo синхронны: в потоке, иначе стоят все сессии (SAM-74)
        return await asyncio.to_thread(self._write, facts, source_of(session_id, messages))

    def _write(self, facts: list[Fact], source: str) -> list[str]:
        written = []
        for fact in facts:
            if self._is_duplicate(fact):
                continue
            if self._store.remember(fact.text, tags=[AUTO_TAG], summary=fact.summary or None,
                                    questions=list(fact.questions), source_session=source):
                written.append(fact.text)
        return written

    def _is_duplicate(self, fact: Fact) -> bool:
        # Дубль - по смыслу: score - косинус вектора, первым в гибриде может встать факт, найденный только по словам
        hits = self._store.search(fact.text, top_k=1, lexical_branch=False)
        return bool(hits) and hits[0].score >= self._dup_score
