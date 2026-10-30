"""forget_topic — комплексная очистка памяти по теме.

Сценарий: SAM сама себе нарассказывала вымышленных историй (Мурка
король, Муха шпион), и они засели в chat history + summaries. Пользователь
говорит «забудь про Мурку и Муху, это были выдумки» — SAM вызывает
forget_topic(query="Мурка Муха") и оно чистит:

1. RAG-факты (_memories) — через store.forget_by_query
2. Chat history — substring match по content в sessions (case-insensitive)
3. Summaries — те же sessions с role=summary

Возвращает отчёт что удалено.
"""
from __future__ import annotations

import re
from typing import Any


def forget_topic_impl(
    params: dict,
    store,        # RAGStore для RAG facts
    db: Any,      # Mongo db для прямого удаления
    session_id: str = "",  # текущая сессия (если не указано — все)
) -> str:
    query = params.get("query", "").strip()
    if not query:
        return "Укажи query — ключевое слово темы которую забыть"

    target_session = (params.get("session") or session_id or "").strip()
    dry_run = bool(params.get("dry_run", False))

    counts = {"rag_facts": 0, "messages": 0, "summaries": 0, "archived": 0}

    # 1. RAG facts. Protected факты НЕ удаляем — сначала фильтруем через
    # Mongo prequery (skip protected=True), потом forget_by_query.
    counts["rag_protected_skipped"] = 0
    if store and db is not None:
        try:
            # Сначала посчитаем сколько protected matches есть
            protected_matched = db["_memories"].count_documents({
                "protected": True,
                "$or": [
                    {"text": {"$regex": re.escape(query), "$options": "i"}},
                    {"summary": {"$regex": re.escape(query), "$options": "i"}},
                    {"tags": {"$in": [query.lower()]}},
                ],
            })
            counts["rag_protected_skipped"] = protected_matched
            result = store.forget_by_query(query, dry_run=dry_run)
            m = re.search(r"(\d+)", result or "")
            counts["rag_facts"] = int(m.group(1)) if m else 0
        except Exception as e:
            counts["rag_facts"] = -1
            print(f"[forget_topic] RAG error: {e}")

    # 2 + 3. Chat history и summaries через substring match по content
    if db is not None:
        flt = {
            "content": {"$regex": re.escape(query), "$options": "i"},
        }
        if target_session:
            flt["session_id"] = target_session

        try:
            # Считаем по ролям
            msg_flt = {**flt, "role": {"$in": ["user", "assistant"]}}
            sum_flt = {**flt, "role": "summary"}

            n_msg = db.sessions.count_documents(msg_flt)
            n_sum = db.sessions.count_documents(sum_flt)
            counts["messages"] = n_msg
            counts["summaries"] = n_sum
            # архив читается в ход (блок к фразе и запись-ссылка, SAM-228): забытое уходит и оттуда
            counts["archived"] = db.sessions_archive.count_documents(flt)

            if not dry_run:
                db.sessions.delete_many(msg_flt)
                db.sessions.delete_many(sum_flt)
                db.sessions_archive.delete_many(flt)
        except Exception as e:
            print(f"[forget_topic] Mongo error: {e}")

    prefix = "DRY RUN: " if dry_run else "Удалено: "
    parts = [
        f"{counts['rag_facts']} RAG-фактов",
        f"{counts['messages']} сообщений",
        f"{counts['summaries']} саммари",
        f"{counts['archived']} из архива",
    ]
    scope = f" из сессии '{target_session}'" if target_session else " из всех сессий"
    suffix = f" (тема: {query!r})"
    if counts.get("rag_protected_skipped"):
        suffix += f". 🔒 Защищённых core-правил пропущено: {counts['rag_protected_skipped']}"
    return prefix + ", ".join(parts) + scope + suffix
