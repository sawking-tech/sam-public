"""Memory tools — remember / list_memories / update_memory / forget (SAM-40).

Все импл-функции работают через RAGStore API (уже знает про Mongo и Qdrant
синхронизацию, dedup, multi-facet indexing). Tools только форматируют
ответ для LLM.

Все функции принимают `store: RAGStore` через DI — для unit-тестов
можно подменить fake store без mongomock + Qdrant.
"""

from __future__ import annotations

import os

# Список без фильтра - обзор, хватает начала факта. По тегу читают запись целиком: трек self-tutor, карточка
# человека; 4000 знаков - запись трека с образцом задания дословно, с запасом.
LIST_PREVIEW_CHARS = 120
# Повтор факта: модели хватает начала записи, чтобы решить, менять ли её
DUPLICATE_PREVIEW_CHARS = 200
LIST_TAG_TEXT_CHARS = int(os.getenv("SAM_MEMORY_TAG_TEXT_CHARS", "4000"))
# Весь ответ по тегу: три полные записи - не больше, чем у модели уходит на один длинный инструмент.
LIST_TAG_TOTAL_CHARS = int(os.getenv("SAM_MEMORY_TAG_TOTAL_CHARS", "12000"))


def remember_impl(params: dict, store) -> str:
    """Сохранить факт в память.

    Принимает:
        fact: str (required) — полный текст
        summary: str (optional) — краткое описание
        tags: str (CSV) (optional) — теги через запятую
    """
    fact = params.get("fact", "")
    if not fact:
        return "Нужен fact — текст факта для сохранения"
    summary = params.get("summary", "")
    tags = [t.strip() for t in params.get("tags", "").split(",") if t.strip()]
    known = store.find_duplicate(fact, summary=summary or None)
    if known:
        return f"Уже помню: {known['text'][:DUPLICATE_PREVIEW_CHARS]}. Если факт изменился - update_memory"
    mongo_id = store.remember(fact, tags=tags, summary=summary or None)
    if mongo_id:
        return f"Запомнил: {summary or fact[:80]}"
    return "Не удалось сохранить (Qdrant или Mongo недоступны)"


def list_memories_impl(params: dict, store, memory_repo) -> str:
    """Поиск в памяти. Если есть query — семантический через RAG.
    Иначе — список всех фактов из MongoDB (опционально фильтр по тегу).
    """
    query_text = params.get("query", "").strip()
    tag_filter = params.get("tag", "").strip()

    if query_text:
        results = store.search(query_text, top_k=10, score_threshold=0.2)
        if not results:
            return f"Ничего похожего на '{query_text}' не найдено."
        lines = []
        for r in results:
            score = f"{r.score:.2f}"
            lines.append(f"[{score}] {r.text[:150]}")
        return "\n".join(lines)

    # Без query — список из MongoDB
    if memory_repo is None:
        return "MongoDB недоступен"
    mq = {"tags": {"$in": [tag_filter]}} if tag_filter else {}
    docs = memory_repo.find_many(mq, sort=[("created_at", -1)], limit=30)
    if not docs:
        return ("Память пуста." if not tag_filter
                else f"Нет фактов с тегом '{tag_filter}'.")
    cut = LIST_TAG_TEXT_CHARS if tag_filter else LIST_PREVIEW_CHARS
    lines = []
    total = 0
    for i, d in enumerate(docs):
        tags = ", ".join(d.get("tags", []))
        text = d.get("text", "")[:cut]
        line = f"[{tags}] {text}" if tags else text
        if tag_filter and lines and total + len(line) > LIST_TAG_TOTAL_CHARS:
            lines.append(f"... ещё {len(docs) - i} фактов не показано: возьми более узкий тег")
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def forget_impl(params: dict, store) -> str:
    """Забыть факт по query (тегу или regex по тексту)."""
    query = params.get("query", "").strip()
    if not query:
        return "Укажи query — ключевое слово или тег для поиска факта"
    return store.forget_by_query(query)


def update_memory_impl(params: dict, store, memory_repo) -> str:
    """Обновить факт. Поиск по тегу/regex, если new_text пустой — удалить."""
    if memory_repo is None:
        return "MongoDB недоступен"
    tag = params.get("tag", "").strip()
    if not tag:
        return "Нужен tag для поиска факта"
    new_text = params.get("new_text", "")
    doc = memory_repo.find_one({"$or": [
        {"tags": {"$in": [tag]}},
        {"text": {"$regex": tag, "$options": "i"}},
    ]})
    if not doc:
        return f"Факт '{tag}' не найден"
    mongo_id = str(doc["_id"])
    if not new_text:
        store.forget_by_id(mongo_id)
        return f"Удалил: {doc['text'][:80]}"
    # update с reindex=True — пересоздать vector с новым текстом
    if store.update(mongo_id, new_text=new_text, reindex=True):
        return f"Обновил: {new_text[:100]}"
    return "Не удалось обновить"
