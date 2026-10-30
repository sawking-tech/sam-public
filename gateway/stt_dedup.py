"""STT context deduplication (SAM-32).

Pre-trigger буфер (60 сек) захватывает фрагменты предыдущих реплик. Каждый
запрос приходит с уже известным контекстом, агент решает что пользователь
повторяет одно и то же.

Идея: разбить новый STT-транскрипт на предложения, для каждого посчитать
TF-IDF cosine similarity с последними N сообщениями сессии. Чанки с
similarity >= порога — отбросить, остаток отдать агенту. Если ничего нового
не осталось — вернуть пустую строку (запрос игнорируется).
"""

from __future__ import annotations

import re
from .env import env_float, env_int

_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")

DEFAULT_THRESHOLD = env_float("STT_DEDUP_THRESHOLD", 0.7)
HISTORY_LOOKBACK = env_int("STT_DEDUP_LOOKBACK", 10)
# Реплика из речи помечается заголовком: дедуп - только для неё, набранный текст не трогается
INPUT_HEADER, SPEECH_INPUT = "X-SAM-Input", "speech"


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s and s.strip()]


def deduplicate_stt(
    new_text: str,
    history_messages: list[str],
    threshold: float = DEFAULT_THRESHOLD,
) -> str:
    """Удалить из new_text чанки, похожие на что-то в history_messages.

    Args:
        new_text: сырой STT-транскрипт от голосового клиента.
        history_messages: последние N сообщений сессии (user-только, тексты).
        threshold: cosine similarity, выше которого чанк считается дублем.

    Returns:
        Очищенный текст с только новыми фрагментами. Пустая строка если всё
        известно (запрос можно тихо проигнорировать на уровне gateway).
    """
    text = (new_text or "").strip()
    if not text:
        return ""
    if not history_messages:
        return text

    chunks = _split_sentences(text)
    if not chunks:
        return text

    # Импортируем sklearn лениво — модуль может загружаться и в окружениях
    # где sklearn не нужен (CI, mcp_aggregator). sklearn уже подтянут через
    # sentence-transformers в основном gateway-стеке.
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
    except ImportError:
        return text

    corpus = chunks + [m for m in history_messages if m and m.strip()]
    if len(corpus) <= len(chunks):
        return text

    try:
        vectorizer = TfidfVectorizer(analyzer="word", lowercase=True, min_df=1)
        matrix = vectorizer.fit_transform(corpus)
    except ValueError:
        # Пустой словарь — например только знаки препинания
        return text

    chunk_vecs = matrix[: len(chunks)]
    history_vecs = matrix[len(chunks) :]
    sims = cosine_similarity(chunk_vecs, history_vecs)

    kept: list[str] = []
    for i, chunk in enumerate(chunks):
        max_sim = float(sims[i].max()) if sims.shape[1] else 0.0
        if max_sim < threshold:
            kept.append(chunk)

    return " ".join(kept).strip()


def history_for_dedup(session_docs: list, limit: int = HISTORY_LOOKBACK) -> list[str]:
    """Достать тексты последних user-сообщений из MongoDB-документов сессии."""
    out: list[str] = []
    for doc in session_docs[-limit:]:
        if doc.get("role") == "user":
            content = (doc.get("content") or "").strip()
            if content:
                out.append(content)
    return out
