"""Лексическая ветка поиска по памяти и слияние с векторной (SAM-102).

Эмбеддер (paraphrase-multilingual-MiniLM) почти не различает одно имя собственное: замер 10 окт на живой памяти -
«Шарик» не находил «Кот Шарик: ...» выше порога 0,5, имя человека давало правило про тон (0,515)
вместо его дня рождения. Слово запроса в тексте факта - сигнал, которого у вектора нет, поэтому поиск гибридный:
BM25 по словам фактов плюс вектор, итог - Reciprocal Rank Fusion (RRF).

BM25 свой, а не текстовый индекс Mongo: факты читаются из Mongo целиком (их единицы-сотни), `$text` не умеет
префиксы и не работает в mongomock тестов. Образец - Hermes, поиск по фактам через FTS5 (oss-ref/hermes-agent,
plugins/memory/holographic/retrieval.py): слова запроса через OR, короткие отброшены, ранг BM25.

Падежи: у слова отрезается окончание из списка (`ENDINGS`), основа не короче `STEM_MIN`; «Маша/Маши/Маше/Машей»
-> «маш», «кот/кота/котом» -> «кот». Регистр и «ё» нормализуются. Чередования основы («день/дня») не ловятся -
их добирает вектор.

Логика чистая: документы и ранги приходят параметрами, Mongo и Qdrant знает gateway/rag.py.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Hashable, Sequence

from .env import env_float, env_int

# BM25: k1 и b - общепринятые значения (Robertson, Zaragoza 2009), на памяти SAM не подбирались
BM25_K1 = env_float("SAM_RAG_BM25_K1", 1.2)
BM25_B = env_float("SAM_RAG_BM25_B", 0.75)
# RRF: k=60 - значение из работы Cormack et al. 2009, сглаживает разницу между первыми местами списков
RRF_K = env_int("SAM_RAG_RRF_K", 60)
# Запрос из стольких слов и короче - поиск по имени: вектор только дополняет выдачу после найденного по словам.
# Замер 10 окт на живой памяти: запрос-имя - день рождения этого человека у вектора последний (0,285), первым
# правило про тон (0,515), где имя упомянуто вскользь; «Шарик» - правило 0,481 выше кота 0,461. В равном RRF факт из обоих
# списков (правило) обходит факт только из лексики (день рождения)
SHORT_QUERY_TOKENS = env_int("SAM_RAG_SHORT_QUERY_TOKENS", 1)

# Слова короче - предлоги, союзы, местоимения («в», «на», «он»): совпадают у всех фактов и только шумят
MIN_TOKEN_LEN = 3
# Служебные слова длиной от MIN_TOKEN_LEN: есть почти в любом факте и только шумят. Замер 12 окт: «какая погода
# завтра» стемом «как» приносила три факта с «как» в описании. «где» и «кто» не служебные: на них держится поиск людей
STOP_WORDS = frozenset({
    "как", "какая", "какой", "какое", "какие", "каких", "каким", "какую", "что", "чем", "чего", "чтобы",
    "это", "эта", "этот", "эти", "этом", "этого", "так", "там", "тут", "вот", "все", "или", "для",
    "при", "про", "без", "над", "под", "его", "она", "они", "оно", "был", "была", "было", "были", "будет",
    "есть", "еще", "уже", "если", "тоже", "также", "только", "очень", "можно", "нужно", "надо", "мне", "меня",
    "тебя", "тебе", "себя", "свой", "своя", "свои", "наш", "ваш", "the", "and",
})
# Основа не короче: иначе «кот» и «кто» после отрезания совпали бы с чем угодно
STEM_MIN = 3
# Окончания существительных и прилагательных, длинные раньше коротких: отрезается первое подходящее
ENDINGS = (
    "иями", "ями", "ами", "его", "ого", "ему", "ому", "ыми", "ими", "иях",
    "ой", "ей", "ий", "ый", "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю", "ом", "ем",
    "ам", "ям", "ах", "ях", "ов", "ев", "ию", "ия", "ье", "ья",
    "а", "я", "о", "е", "и", "ы", "у", "ю", "ь", "й",
)
_WORD = re.compile(r"\w+", re.UNICODE)


def normalize(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def stem(word: str) -> str:
    """Основа слова: окончание из ENDINGS отрезано, если основа остаётся не короче STEM_MIN."""
    for ending in ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= STEM_MIN:
            return word[: -len(ending)]
    return word


def tokens(text: str) -> list[str]:
    """Основы слов текста: нижний регистр, ё -> е, короче MIN_TOKEN_LEN и служебные отброшены."""
    return [stem(w) for w in _WORD.findall(normalize(text)) if len(w) >= MIN_TOKEN_LEN and w not in STOP_WORDS]


def bm25_rank(query: str, docs: Sequence[tuple[Hashable, str]], limit: int,
              k1: float = BM25_K1, b: float = BM25_B) -> list[tuple[Hashable, float]]:
    """Документы (ключ, текст) по BM25 к запросу, лучшие первыми; без общего слова с запросом не попадают."""
    query_terms = set(tokens(query))
    if not query_terms or not docs or limit <= 0:
        return []
    doc_terms = [(key, Counter(tokens(text))) for key, text in docs]
    n_docs = len(doc_terms)
    avg_len = sum(sum(c.values()) for _, c in doc_terms) / n_docs or 1.0
    doc_freq = Counter(term for _, c in doc_terms for term in query_terms if term in c)
    scored = []
    for key, counts in doc_terms:
        length = sum(counts.values())
        score = 0.0
        for term in query_terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            # Вариант IDF из Lucene: всегда положителен, слово во всех фактах не уводит в минус
            idf = math.log(1 + (n_docs - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg_len))
        if score > 0:
            scored.append((key, score))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored[:limit]


def is_short(query: str) -> bool:
    """Запрос - имя или слово (не длиннее SHORT_QUERY_TOKENS основ), а не фраза."""
    return 0 < len(set(tokens(query))) <= SHORT_QUERY_TOKENS


def rrf_fuse(rankings: Sequence[Sequence[Hashable]], k: int = RRF_K,
             weights: Sequence[float] | None = None) -> list[tuple[Hashable, float]]:
    """Слияние ранжированных списков ключей: сумма вес/(k + место). Равные суммы - в порядке списков: ключ из
    более раннего списка и с лучшим местом в нём выше. Вес 0 - список только дополняет выдачу после остальных."""
    weights = list(weights) if weights is not None else [1.0] * len(rankings)
    fused: dict[Hashable, float] = {}
    first_seen: dict[Hashable, tuple[int, int]] = {}
    for list_no, ranking in enumerate(rankings):
        for place, key in enumerate(ranking):
            fused[key] = fused.get(key, 0.0) + weights[list_no] / (k + place + 1)
            first_seen.setdefault(key, (list_no, place))
    return sorted(fused.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
