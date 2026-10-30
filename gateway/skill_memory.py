"""Скиллы в долгой памяти SAM: факт скилла - описание и команда вызова, тело файла в память не кладётся.

Подбор скилла к фразе делает штатный поиск памяти на входе (recall, RAG): разговор о том, про что скилл, приносит
факт «есть такой скилл и как его вызвать», тело модель читает по команде. Запись - штатными методами памяти
(RAGStore.remember / update с reindex / forget_by_id), без своего индекса.

Факт скилла:
- текст: имя, описание и команда load_skill(имя);
- summary - описание (вектор summary), примеры фраз из описания в «ёлочках» - фасеты-вопросы памяти;
- метки: skill, skill:<имя> и skill-digest:<отпечаток>. Отпечаток - от текста факта: правка описания пересчитывает
  эмбеддинг, неизменённый скилл не трогается.

Синхронизация (sync) сверяет скиллы стенда со своими фактами в памяти (автор skills-sync): новый - добавить,
изменённый - переписать, удалённый - забыть, ушедший в архив - вернуть. Чужие записи с меткой skill (метки свободные,
remember их ставит любой) сверка не трогает. Решение (plan_sync) отделено от записи (apply_sync), чтобы его проверять без памяти.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .skills import Skill

SKILL_TAG = "skill"
NAME_TAG_PREFIX = "skill:"
DIGEST_TAG_PREFIX = "skill-digest:"
DIGEST_CHARS = 12               # 48 бит отпечатка: различить правки десятков скиллов, метка остаётся короткой
EXAMPLE_RE = re.compile(r"«([^»]{3,120})»")   # примеры фраз в описании скилла: «где кто», «что поесть»
MAX_QUESTIONS = 8               # фасетов-вопросов на скилл: каждый - отдельный вектор в Qdrant
SOURCE = "skills-sync"          # автор записи в памяти
AUTHOR, ARCHIVED = "author", "archived"   # поля записи памяти (gateway/repos/memory_repo.py)
STORED_LIMIT = 1000             # записей skill в памяти за одну сверку: с запасом над числом скиллов стенда
MEMORY_DOWN = "память недоступна - скиллы не сверены"


@dataclass(frozen=True)
class SkillFact:
    name: str
    text: str
    summary: str
    questions: list[str]
    digest: str

    @property
    def tags(self) -> list[str]:
        return [SKILL_TAG, f"{NAME_TAG_PREFIX}{self.name}", f"{DIGEST_TAG_PREFIX}{self.digest}"]


@dataclass
class SyncPlan:
    add: list[SkillFact] = field(default_factory=list)
    update: list[tuple[str, SkillFact]] = field(default_factory=list)   # (mongo_id, факт)
    forget: list[tuple[str, str]] = field(default_factory=list)          # (mongo_id, имя)
    unarchive: list[str] = field(default_factory=list)                   # mongo_id своих фактов, ушедших в архив


def command(name: str) -> str:
    return f'load_skill("{name}")'


def skill_fact(skill: Skill) -> SkillFact:
    description = skill.description.strip()
    text = f"Скилл «{skill.name}»: {description} Вызов: {command(skill.name)}."
    questions = list(dict.fromkeys(EXAMPLE_RE.findall(description)))[:MAX_QUESTIONS]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:DIGEST_CHARS]
    return SkillFact(skill.name, text, description, questions, digest)


def _tag_value(tags: Iterable[str], prefix: str) -> str | None:
    return next((t[len(prefix):] for t in tags if t.startswith(prefix)), None)


def plan_sync(skills: Iterable[Skill], stored: Iterable[dict]) -> SyncPlan:
    """Что сделать с памятью: stored - записи памяти с меткой skill ({_id, tags})."""
    facts = {s.name: skill_fact(s) for s in skills if s.listed}
    plan = SyncPlan()
    seen: set[str] = set()
    for doc in stored:
        tags = doc.get("tags") or []
        name = _tag_value(tags, NAME_TAG_PREFIX)
        mongo_id = str(doc.get("_id"))
        fact = facts.get(name or "")
        if fact is None or name in seen:      # скилл удалён или запись-дубль - забыть
            plan.forget.append((mongo_id, name or "?"))
            continue
        seen.add(name)
        if doc.get(ARCHIVED):
            plan.unarchive.append(mongo_id)
        if _tag_value(tags, DIGEST_TAG_PREFIX) != fact.digest:
            plan.update.append((mongo_id, fact))
    plan.add = [f for n, f in facts.items() if n not in seen]
    return plan


def apply_sync(plan: SyncPlan, store: Any) -> dict:
    """Записать план штатными методами памяти; итог - сколько добавлено, переписано и забыто."""
    added = sum(1 for f in plan.add
                if store.remember(f.text, tags=f.tags, summary=f.summary, questions=f.questions,
                                  source_session=SOURCE))
    updated = sum(1 for mongo_id, f in plan.update
                  if store.update(mongo_id, new_text=f.text, new_tags=f.tags, new_summary=f.summary,
                                  new_questions=f.questions, reindex=True))
    forgotten = sum(1 for mongo_id, _ in plan.forget if store.forget_by_id(mongo_id))
    restored = sum(1 for mongo_id in plan.unarchive if store.memory_repo.unarchive(mongo_id))
    return {"added": added, "updated": updated, "forgotten": forgotten, "restored": restored}


def sync(skills: Iterable[Skill], store: Any, repo: Any, available: Callable[[], bool]) -> dict:
    """Скиллы стенда -> факты в памяти. repo - MemoryRepo (поиск записей с меткой skill).

    Mongo недоступна - сверки нет: пустой ответ поиска приняли бы за «фактов нет» и добавили бы всё заново."""
    if not available():
        return {"error": MEMORY_DOWN}
    stored = repo.find_many({"tags": SKILL_TAG, AUTHOR: SOURCE}, limit=STORED_LIMIT)   # только свои записи
    return apply_sync(plan_sync(skills, stored), store)
