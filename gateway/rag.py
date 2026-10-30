"""RAG layer — Mongo + Qdrant как TOC (SAM-40).

Архитектура:
    MongoDB  = source of truth. ВСЕГДА. Там полные данные.
    Qdrant   = семантическое ОГЛАВЛЕНИЕ (TOC):
                 summary (краткое описание факта)
               + questions (вопросы которые этот факт закрывает)
               → embedding → vector
               Воспринимать как кратковременная память системы —
               легко пересоздать через reindex_all, не критично если
               отстал, чинится в фоне (dream-job SAM-22).

Pipeline поиска (гибрид, SAM-102):
    1. вектор: query → embed → Qdrant top-K по семантике, лучший facet на mongo_id
    2. слова: BM25 по тексту, summary, тегам фактов из Mongo (gateway/lexical.py) -
       имя одним словом вектор почти не различает, слово в тексте находит
    3. слияние RRF; порог вектора режет только векторную ветку; запрос-имя (одно слово) -
       сначала найденное по словам, вектор дополняет (lexical.SHORT_QUERY_TOKENS)
    4. → MongoDB по mongo_id → актуальный полный текст, archived отброшены
    Пока эмбеддер грузится в другом потоке (прогрев на старте шлюза), поиск идёт одними словами,
    а не ждёт загрузки.

Поиск семантически матчит не только по самим фактам, но и по
**вопросам** в payload (HyDE-стиль). Это значит когда пользователь
спрашивает «что Маша не пьёт?» — мы находим факт «Маша не пьёт кофе
после 18» через вопрос-заглушку «что Маша не пьёт?» которую мы при
записи положили в questions[].

API:
    remember(text, tags?, summary?, questions?)  → mongo_id
        Если questions не переданы — embed только summary.
        Лучший паттерн: при сохранении факта LLM генерит 2-3 вопроса
        на которые этот факт отвечает, и они идут в questions.
    find_duplicate(text, summary?)      → dict | None  (уже записанный почти такой же факт)
    search(query, top_k=5, ...)         → list[RAGResult]
        Всегда подтягивает actual text из Mongo по mongo_id.
    get_fact(mongo_id)                  → dict | None  (alias для MongoDB lookup)
    update(mongo_id, new_text?, new_tags?, reindex=False)
        По умолчанию обновляет только Mongo. reindex=True если
        изменился смысл и нужно пересобрать вектор.
    forget_by_id / forget_by_query      → удаление из обоих
    prune_unused(max_age_days)          → периодическая чистка
    maintenance_check()                 → отчёт о расхождении
    reindex_all()                       → пересборка TOC из Mongo

Maintenance (приборка / reindex / dream cycle) выполняется фоновым
job-ом (SAM-22 «auto-dream»). Поэтому небольшие рассинхрон допустимы
и чинятся в фоне, а не критически на горячем пути.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from . import lexical
from .env import env_bool, env_int

log = logging.getLogger("sam.rag")


# ─── Defaults ─────────────────────────────────────────────────────────────────

EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
EMBED_DIM = 384
COLLECTION_NAME = "context"
DEFAULT_SCORE_THRESHOLD = 0.5
# Повтор факта при записи через tool remember (find_duplicate): косинус фасета не ниже - факт уже есть
DEDUP_SIM_THRESHOLD = 0.92
# Факт без summary индексируется по началу текста этой длины
SUMMARY_DEFAULT_CHARS = 200
MMR_DIVERSITY_LAMBDA = 0.7
# SAM-102: лексическая ветка поиска; 0 - только вектор, как до гибрида
LEXICAL_ENABLED = env_bool("SAM_RAG_LEXICAL", True)
# Сколько фактов читает лексическая ветка за поиск. 10 окт в памяти 4 факта; BM25 по тысячам коротких
# фактов в Python - миллисекунды, дальше нужен индекс
LEXICAL_SCAN_LIMIT = env_int("SAM_RAG_LEXICAL_SCAN", 5000)
# Кандидатов у каждой ветки - top_k * множитель: факт, который есть в обоих списках, но не в первых top_k
# одного из них, всё равно получает обе доли RRF; у вектора несколько facet-точек схлопываются в один факт
CANDIDATE_FACTOR = 3
# Свой порог векторной ветки для фактов с меткой: «метка=порог» через запятую. skill - факты скиллов
# (gateway/skill_memory.py): короткие примеры фраз как фасеты ловят бытовое. Замер 12 окт на этом эмбеддере:
# верные совпадения с вопросами скиллов 0,8-1,0, ложные («привет» к «со слов») до 0,64
TAG_MIN_SCORE_ENV = "SAM_RAG_TAG_MIN"
DEFAULT_TAG_MIN_SCORE = "skill=0.7"


def parse_tag_min_score(raw: str | None) -> dict[str, float]:
    """«skill=0.7,x=0.6» -> {"skill": 0.7, "x": 0.6}; битая пара - строка в лог и пропуск."""
    out: dict[str, float] = {}
    for pair in (raw or "").split(","):
        if not pair.strip():
            continue
        tag, _, value = pair.partition("=")
        try:
            out[tag.strip()] = float(value)
        except ValueError:
            log.error("%s: пара %r не «метка=число», пропущена", TAG_MIN_SCORE_ENV, pair)
    return out


def min_score_for(tags: list[str], base: float, tag_min: dict[str, float]) -> float:
    """Порог точки: общий или самый строгий из порогов её меток."""
    return max([base, *(tag_min[t] for t in tags or [] if t in tag_min)])


# ─── Types ────────────────────────────────────────────────────────────────────

@dataclass
class RAGResult:
    """Один результат поиска. По умолчанию text = snapshot из Qdrant.
    Если hydrate=True или явно вызван get_fact — text из Mongo."""
    mongo_id: str | None
    text: str
    summary: str
    tags: list[str]
    score: float                 # косинус вектора; 0.0 - факт найден только по словам
    qdrant_id: str               # пусто - факт найден только по словам
    created_at: str | None
    lexical: float = 0.0         # BM25 лексической ветки; 0.0 - слов запроса в факте нет


@dataclass
class _Candidate:
    """Факт-кандидат после слияния веток; summary - для MMR."""
    key: str
    mongo_id: str | None
    qdrant_id: str
    summary: str
    tags: list[str]
    created_at: str | None
    vector: float = 0.0
    lexical: float = 0.0
    fused: float = 0.0
    doc: dict | None = None


class EmbedderUnavailableError(RuntimeError):
    pass


class DimensionMismatchError(RuntimeError):
    pass


# ─── RAGStore ─────────────────────────────────────────────────────────────────

class RAGStore:
    """Memory.md-style рабочая память агента над Qdrant + опциональным Mongo.

    Все зависимости через конструктор — тесты подменяют через DI.
    """

    def __init__(
        self,
        memory_repo,
        qdrant_client_factory: Callable[[], Any] | None = None,
        embedder_factory: Callable[[], Any] | None = None,
        collection_name: str = COLLECTION_NAME,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        dedup_threshold: float = DEDUP_SIM_THRESHOLD,
    ) -> None:
        self.memory_repo = memory_repo
        self.collection_name = collection_name
        self.score_threshold = score_threshold
        self.dedup_threshold = dedup_threshold
        self._qdrant_factory = qdrant_client_factory or _default_qdrant_factory
        self._embedder_factory = embedder_factory or _default_embedder_factory
        self._embedder = None
        self.tag_min_score = parse_tag_min_score(os.getenv(TAG_MIN_SCORE_ENV, DEFAULT_TAG_MIN_SCORE))
        # Одна загрузка эмбеддера на процесс: прогрев на старте и первый поиск не грузят модель дважды
        self._embedder_lock = threading.Lock()
        self._qdrant = None
        self._collection_ready = False

    # ── lazy infra ────────────────────────────────────────────────────────────

    def _get_qdrant(self):
        if self._qdrant is None:
            self._qdrant = self._qdrant_factory()
        return self._qdrant

    def _get_embedder(self):
        if self._embedder is None:
            with self._embedder_lock:
                if self._embedder is None:
                    self._embedder = self._embedder_factory()
        return self._embedder

    def embedder_loading(self) -> bool:
        """Эмбеддер сейчас грузится в другом потоке (прогрев шлюза)."""
        return self._embedder is None and self._embedder_lock.locked()

    def embed(self, text: str) -> list[float]:
        model = self._get_embedder()
        if model is None:
            raise EmbedderUnavailableError("Sentence-transformers model not loaded")
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist() if hasattr(vec, "tolist") else list(vec)

    @property
    def embed_dim(self) -> int:
        model = self._get_embedder()
        if model is None:
            return EMBED_DIM
        if hasattr(model, "get_sentence_embedding_dimension"):
            return model.get_sentence_embedding_dimension()
        return len(self.embed("test"))

    def ensure_collection(self) -> None:
        if self._collection_ready:
            return
        try:
            from qdrant_client.models import VectorParams, Distance
        except ImportError:
            log.error("qdrant_client not installed")
            return
        client = self._get_qdrant()
        current_dim = self.embed_dim
        try:
            existing = [c.name for c in client.get_collections().collections]
            if self.collection_name in existing:
                info = client.get_collection(self.collection_name)
                cfg_dim = self._extract_collection_dim(info)
                if cfg_dim is not None and cfg_dim != current_dim:
                    raise DimensionMismatchError(
                        f"Collection '{self.collection_name}' dim={cfg_dim} "
                        f"≠ embedder dim={current_dim}. Run reindex_all() after "
                        f"dropping the collection."
                    )
            else:
                client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(size=current_dim, distance=Distance.COSINE),
                )
                log.info("Created collection '%s' dim=%d", self.collection_name, current_dim)
            self._collection_ready = True
        except DimensionMismatchError:
            raise
        except Exception as e:
            log.error("ensure_collection failed: %s", e)

    @staticmethod
    def _extract_collection_dim(info) -> int | None:
        try:
            vec_cfg = info.config.params.vectors
            if hasattr(vec_cfg, "size"):
                return int(vec_cfg.size)
            if isinstance(vec_cfg, dict):
                for v in vec_cfg.values():
                    if hasattr(v, "size"):
                        return int(v.size)
        except Exception:
            return None
        return None

    # ── memory operations (memory.md semantics) ──────────────────────────────

    def remember(
        self,
        text: str,
        tags: list[str] | None = None,
        summary: str | None = None,
        questions: list[str] | None = None,
        source_session: str | None = None,
    ) -> str | None:
        """Сохранить факт. ОДИН Mongo doc → N Qdrant points (multi-vector).

        Args:
            text: полный текст факта (Mongo, source of truth).
            tags: метки.
            summary: короткое описание (1 vector в Qdrant). Default — text[:200].
            questions: вопросы которые закрывает этот факт. Каждый = ОТДЕЛЬНЫЙ
                       vector в Qdrant. Юзер спросит «когда у Мурки ДР?» —
                       embed запроса матчит embed заглушки-вопроса.
            source_session: кто записал (для аудита).

        В Qdrant создаётся 1 + len(questions) точек, все указывают на
        один mongo_id. Search дедупит по mongo_id перед возвратом.

        Повтор здесь не проверяется: tool remember сначала зовёт find_duplicate,
        запись скиллов (skill_memory) и извлечение фактов (memory_extract)
        сверяют повторы сами.

        Возвращает mongo_id или None.
        """
        if not text or not text.strip():
            return None
        try:
            self.ensure_collection()
        except DimensionMismatchError as e:
            log.error("remember aborted: %s", e)
            return None

        # MongoDB — single source of truth
        mongo_id = None
        if self.memory_repo is not None:
            mongo_id = self.memory_repo.insert(
                text, tags=tags, summary=summary, author=source_session,
                questions=[q for q in questions or [] if q and q.strip()],
            )

        # Qdrant — multi-vector TOC. Все points → same mongo_id.
        self._index_facets(
            mongo_id=mongo_id,
            summary=summary or text[:SUMMARY_DEFAULT_CHARS],
            questions=questions or [],
            tags=tags or [],
            source_session=source_session,
        )
        return mongo_id

    # Backward compat
    add = remember

    def _index_facets(
        self,
        mongo_id: str | None,
        summary: str,
        questions: list[str],
        tags: list[str],
        source_session: str | None = None,
    ) -> int:
        """Создать в Qdrant все vector-точки для одного факта.

        Возвращает количество созданных точек.
        Каждая точка имеет facet_type: "summary" | "question".
        created_at точки - время индексации: после пересборки у точки снова есть срок до уборки (access_count с нуля).
        """
        created_at = datetime.utcnow().isoformat()
        count = 0
        facets: list[tuple[str, str]] = []
        if summary and summary.strip():
            facets.append(("summary", summary))
        for q in questions:
            if q and q.strip():
                facets.append(("question", q))

        for facet_type, text in facets:
            try:
                vec = self.embed(text)
            except EmbedderUnavailableError:
                log.error("_index_facets: embedder unavailable")
                return count
            self._upsert_point(
                qdrant_id=str(uuid.uuid4()),
                vector=vec,
                payload={
                    "mongo_id": mongo_id,
                    "facet_type": facet_type,  # "summary" or "question"
                    "facet_text": text[:500],
                    "summary": summary[:500],  # для дисплея в результатах
                    "tags": tags,
                    "source_session": source_session,
                    "created_at": created_at,
                    # Usage tracking для dream-job эволюции памяти:
                    # часто использованные facets — оставлять, новые плодить;
                    # мёртвые — удалять как мусор.
                    "access_count": 0,
                    "last_used_at": None,
                },
            )
            count += 1
        return count

    # ── dream-job memory evolution (см. SAM-22) ──────────────────────────────

    def expand_facets(
        self,
        mongo_id: str,
        new_questions: list[str],
    ) -> int:
        """Добавить дополнительные question-facets к существующему mongo_id.

        Используется dream-job: для популярных фактов (high access_count)
        LLM генерит дополнительные ракурсы / вопросы → индексируем чтобы
        чаще матчиться на разные формулировки запросов.

        НЕ дублирует — каждый question = новый vector с новым qdrant_id.
        """
        if not mongo_id or not new_questions or self.memory_repo is None:
            return 0
        doc = self.memory_repo.get_by_id(mongo_id)
        if not doc:
            return 0
        try:
            self.ensure_collection()
        except DimensionMismatchError as e:
            log.error("expand_facets skipped: %s", e)
            return 0
        added = 0
        done: list[str] = []
        created_at = datetime.utcnow().isoformat()
        for q in new_questions:
            if not q or not q.strip():
                continue
            try:
                vec = self.embed(q)
            except EmbedderUnavailableError as e:
                log.error("expand_facets stopped after %d facets: %s", added, e)
                break
            self._upsert_point(
                qdrant_id=str(uuid.uuid4()),
                vector=vec,
                payload={
                    "mongo_id": mongo_id,
                    "facet_type": "question",
                    "facet_text": q[:500],
                    "summary": doc.get("summary") or doc.get("text", "")[:200],
                    "tags": doc.get("tags", []),
                    "created_at": created_at,
                    "access_count": 0,
                    "last_used_at": None,
                },
            )
            added += 1
            done.append(q)
        if done:  # вопросы в Mongo - источник для пересборки индекса
            self.memory_repo.add_questions(mongo_id, done)
        return added

    def prune_dead_facets(
        self,
        min_access_count: int = 0,
        max_age_days: int = 90,
    ) -> dict:
        """Удалить vector-facets которые никогда не использовались.

        Условия удаления (оба должны быть True):
        - access_count ≤ min_access_count (никто не нашёл через этот facet)
        - created_at старше max_age_days (был шанс, но не сматчилось)

        Mongo doc НЕ трогаем — только индексные nodes. Если у факта удалили
        все facets, его summary всё ещё в Mongo, может быть переиндексирован.

        Возвращает {checked, deleted}.
        """
        try:
            from qdrant_client.models import (
                Filter, FieldCondition, Range, MatchValue,
            )
        except ImportError:
            log.error("prune_dead_facets: qdrant_client not installed")
            return {"checked": 0, "deleted": 0}
        cutoff_iso = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
        try:
            client = self._get_qdrant()
            # Scroll all points, фильтр в Python (qdrant range на ISO-string
            # работает не везде стандартно — простой in-memory filter).
            all_dead_ids = []
            offset = None
            while True:
                resp = client.scroll(
                    collection_name=self.collection_name,
                    limit=200, offset=offset,
                    with_payload=True, with_vectors=False,
                )
                if isinstance(resp, tuple):
                    points, offset = resp
                else:
                    points = resp.points if hasattr(resp, "points") else resp
                    offset = getattr(resp, "next_page_offset", None)
                for p in points:
                    pl = p.payload or {}
                    if pl.get("access_count", 0) > min_access_count:
                        continue
                    created = pl.get("created_at", "")
                    if created and created >= cutoff_iso:
                        continue  # ещё свежий, дать шанс
                    all_dead_ids.append(p.id)
                if offset is None or not points:
                    break

            if all_dead_ids:
                from qdrant_client.models import PointIdsList
                client.delete(
                    collection_name=self.collection_name,
                    points_selector=PointIdsList(points=all_dead_ids),
                )
            return {"checked": "all", "deleted": len(all_dead_ids)}
        except Exception as e:
            log.error("prune_dead_facets failed: %s", e)
            return {"checked": 0, "deleted": 0, "error": str(e)}

    def top_used_facets(self, limit: int = 20) -> list[dict]:
        """Топ самых используемых facets — для dream-job анализа.
        Можно генерить ещё похожих к ним через expand_facets."""
        try:
            client = self._get_qdrant()
            all_points = []
            offset = None
            while True:
                resp = client.scroll(
                    collection_name=self.collection_name,
                    limit=200, offset=offset,
                    with_payload=True, with_vectors=False,
                )
                if isinstance(resp, tuple):
                    points, offset = resp
                else:
                    points = resp.points if hasattr(resp, "points") else resp
                    offset = getattr(resp, "next_page_offset", None)
                all_points.extend(points)
                if offset is None or not points:
                    break

            scored = [
                {
                    "qdrant_id": str(p.id),
                    "mongo_id": (p.payload or {}).get("mongo_id"),
                    "facet_type": (p.payload or {}).get("facet_type"),
                    "facet_text": (p.payload or {}).get("facet_text", "")[:120],
                    "access_count": (p.payload or {}).get("access_count", 0),
                    "last_used_at": (p.payload or {}).get("last_used_at"),
                }
                for p in all_points
            ]
            scored.sort(key=lambda d: d["access_count"], reverse=True)
            return scored[:limit]
        except Exception as e:
            log.error("top_used_facets failed: %s", e)
            return []

    def search(
        self,
        query: str,
        top_k: int = 5,
        tag_filter: str | list[str] | None = None,
        score_threshold: float | None = None,
        mmr: bool = False,
        mmr_lambda: float = MMR_DIVERSITY_LAMBDA,
        lexical_branch: bool = LEXICAL_ENABLED,
    ) -> list[RAGResult]:
        """Гибридный поиск (SAM-102): вектор Qdrant + BM25 по словам фактов, слияние RRF → MongoDB.

        Mongo — source of truth, текст всегда оттуда. Порог score_threshold режет только векторную
        ветку: точное слово запроса в факте находится и при слабом векторе.

        Args:
            mmr: переранжировать для diversity (анти-дубль в выдаче).
            lexical_branch: False - только вектор (проверка дубля по смыслу, memory_extract).
        """
        if not query or not query.strip():
            return []
        tags = [tag_filter] if isinstance(tag_filter, str) else list(tag_filter or [])
        threshold = score_threshold if score_threshold is not None else self.score_threshold
        pool = top_k * CANDIDATE_FACTOR

        query_vec, vector_hits = self._vector_hits(query, pool, tags, threshold)
        lexical_hits = self._lexical_hits(query, pool, tags) if lexical_branch else []
        candidates = self._fuse(vector_hits, lexical_hits, vector_weight=0.0 if lexical.is_short(query) else 1.0)

        if mmr and query_vec is not None and len(candidates) > top_k:
            candidates = self._mmr_rerank(candidates, query_vec, top_k, mmr_lambda)

        results: list[RAGResult] = []
        for c in candidates:
            if len(results) >= top_k:
                break
            text, tags_out = c.summary, c.tags
            if c.mongo_id and self.memory_repo is not None:
                # ВСЕГДА актуальный text из Mongo (source of truth)
                doc = c.doc or self.memory_repo.get_by_id(c.mongo_id)
                if not doc:
                    log.warning("orphan vector %s — mongo_id=%s missing", c.qdrant_id, c.mongo_id)
                    continue
                if doc.get("archived"):  # SAM-45: soft-deleted не возвращаем
                    continue
                text = doc.get("text", c.summary)
                tags_out = doc.get("tags", c.tags)
            results.append(RAGResult(
                mongo_id=c.mongo_id, text=text, summary=c.summary, tags=tags_out,
                score=c.vector, qdrant_id=c.qdrant_id, created_at=c.created_at, lexical=c.lexical,
            ))
        # Tracking для dream-job:
        # 1. last_used_at в Mongo (per-fact level)
        self._touch_used([r.mongo_id for r in results if r.mongo_id])
        # 2. access_count + last_used_at в Qdrant payload (per-facet level)
        #    — какие именно vector-нотисы реально полезны, для prune_dead_facets
        self._increment_qdrant_access([r.qdrant_id for r in results if r.qdrant_id])
        return results

    def _vector_hits(self, query: str, limit: int, tags: list[str],
                     threshold: float) -> tuple[list[float] | None, list]:
        """Векторная ветка: лучший facet-point на mongo_id, по убыванию score. Эмбеддер грузится в другом
        потоке, недоступен или Qdrant упал - пусто, поиск идёт словами."""
        if self.embedder_loading():
            log.info("search: эмбеддер ещё грузится, поиск по словам")
            return None, []
        try:
            self.ensure_collection()
            query_vec = self.embed(query)
        except (DimensionMismatchError, EmbedderUnavailableError) as e:
            log.error("search vector branch skipped: %s", e)
            return None, []
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchAny
            f = Filter(must=[FieldCondition(key="tags", match=MatchAny(any=tags))]) if tags else None
            points = self._get_qdrant().query_points(
                collection_name=self.collection_name,
                query=query_vec, limit=limit,
                score_threshold=threshold, query_filter=f,
            ).points
        except Exception as e:
            log.error("search query failed: %s", e)
            return query_vec, []
        points = [p for p in points
                  if p.score >= min_score_for((p.payload or {}).get("tags", []), threshold, self.tag_min_score)]
        # Дедуп по mongo_id: один факт матчится через несколько facet-точек (summary + N questions),
        # берём лучший score за группу. Точки без mongo_id - сами по себе.
        best: dict = {}
        for p in points:
            key = (p.payload or {}).get("mongo_id") or f"qdrant:{p.id}"
            if key not in best or p.score > best[key].score:
                best[key] = p
        return query_vec, sorted(best.values(), key=lambda x: x.score, reverse=True)

    def _lexical_hits(self, query: str, limit: int, tags: list[str]) -> list[tuple[float, dict]]:
        """Лексическая ветка: (BM25, doc) по тексту, summary, вопросам и тегам активных фактов Mongo."""
        if self.memory_repo is None:
            return []
        flt: dict = {"archived": {"$ne": True}}
        if tags:
            flt["tags"] = {"$in": tags}
        try:
            docs = self.memory_repo.find_many(flt, limit=LEXICAL_SCAN_LIMIT)
        except Exception as e:
            log.error("search lexical branch skipped: %s", e)
            return []
        by_id = {str(d["_id"]): d for d in docs if d.get("_id") is not None}
        corpus = [(mid, _lexical_text(d)) for mid, d in by_id.items()]
        return [(score, by_id[mid]) for mid, score in lexical.bm25_rank(query, corpus, limit)]

    @staticmethod
    def _fuse(vector_hits: list, lexical_hits: list[tuple[float, dict]],
              vector_weight: float = 1.0) -> list[_Candidate]:
        """RRF двух веток. Лексическая идёт первой: при равной сумме выше факт со словом запроса. vector_weight 0
        (запрос-имя, lexical.SHORT_QUERY_TOKENS) - вектор только дополняет выдачу после найденного по словам."""
        cands: dict[str, _Candidate] = {}
        for score, doc in lexical_hits:
            mid = str(doc["_id"])
            created = doc.get("created_at")
            cands[mid] = _Candidate(
                key=mid, mongo_id=mid, qdrant_id="", summary=doc.get("summary") or doc.get("text", "")[:200],
                tags=doc.get("tags", []), lexical=score, doc=doc,
                created_at=created.isoformat() if hasattr(created, "isoformat") else created,
            )
        for p in vector_hits:
            payload = p.payload or {}
            key = payload.get("mongo_id") or f"qdrant:{p.id}"
            c = cands.get(key)
            if c is None:
                c = cands[key] = _Candidate(
                    key=key, mongo_id=payload.get("mongo_id"), qdrant_id=str(p.id),
                    summary=payload.get("summary", ""), tags=payload.get("tags", []),
                    created_at=payload.get("created_at"),
                )
            else:
                c.qdrant_id = str(p.id)
            c.vector = float(p.score)
        lexical_order = [str(doc["_id"]) for _, doc in lexical_hits]
        vector_order = [(p.payload or {}).get("mongo_id") or f"qdrant:{p.id}" for p in vector_hits]
        fused = lexical.rrf_fuse([lexical_order, vector_order], weights=[1.0, vector_weight])
        for key, value in fused:
            cands[key].fused = value
        return [cands[key] for key, _ in fused]

    def get_fact(self, mongo_id: str) -> dict | None:
        """Получить АКТУАЛЬНЫЙ полный факт из Mongo (source of truth).

        Главный agentic tool: LLM смотрит snapshot из search(), решает что
        ей нужно подтвердить актуальностью — дёргает этот метод по mongo_id.
        """
        if not mongo_id or self.memory_repo is None:
            return None
        doc = self.memory_repo.get_by_id(mongo_id)
        if not doc:
            return None
        return {
            "mongo_id": str(doc.get("_id", mongo_id)),
            "text": doc.get("text", ""),
            "tags": doc.get("tags", []),
            "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
            "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else None,
            "author": doc.get("author"),
        }

    def update(
        self,
        mongo_id: str,
        new_text: str | None = None,
        new_tags: list[str] | None = None,
        new_summary: str | None = None,
        new_questions: list[str] | None = None,
        reindex: bool = False,
    ) -> bool:
        """Обновить факт в Mongo. Vectors в Qdrant пересобираются если reindex=True
        (надо при изменении смысла) — удаляем все старые с этим mongo_id,
        создаём новый набор (summary + questions).

        Args:
            reindex: True → пересобрать все Qdrant points для этого mongo_id.
                     False → только Mongo обновляется (TOC может отстать;
                     dream-job это починит).
        """
        if not mongo_id or self.memory_repo is None:
            return False
        update_dict: dict = {"updated_at": datetime.utcnow()}
        if new_text is not None:
            update_dict["text"] = new_text
        if new_tags is not None:
            update_dict["tags"] = new_tags
        if new_summary is not None:
            update_dict["summary"] = new_summary
        if new_questions is not None:
            update_dict["questions"] = new_questions
        if len(update_dict) == 1:  # только updated_at
            return False
        if not self.memory_repo.update_by_id(mongo_id, update_dict):
            return False

        if reindex:
            doc = self.memory_repo.get_by_id(mongo_id)
            if not doc:
                return True  # Mongo обновлён, но doc сейчас не вытаскивается
            try:
                self.ensure_collection()
                self._delete_vectors_by_mongo_id(mongo_id)
                self._index_facets(
                    mongo_id=mongo_id,
                    summary=doc.get("summary") or doc.get("text", "")[:200],
                    questions=doc.get("questions", []),
                    tags=doc.get("tags", []),
                    source_session=doc.get("author"),
                )
            except Exception as e:
                log.error("update reindex failed: %s", e)
                return False
        return True

    def forget_by_id(self, mongo_id: str) -> bool:
        """Полное удаление из памяти. Mongo + все связанные vectors."""
        if not mongo_id:
            return False
        mongo_deleted = False
        if self.memory_repo is not None:
            mongo_deleted = self.memory_repo.delete_by_id(mongo_id)
        qdrant_deleted = self._delete_vectors_by_mongo_id(mongo_id)
        return mongo_deleted or qdrant_deleted

    def forget_by_query(self, query: str) -> str:
        """Найти и удалить факт по тегу/regex. Возвращает summary удалённого."""
        if not query or self.memory_repo is None:
            return "Память недоступна"
        doc = self.memory_repo.find_one({"$or": [
            {"tags": {"$in": [query]}},
            {"text": {"$regex": query, "$options": "i"}},
        ]})
        if not doc:
            return f"Факт '{query}' не найден"
        mongo_id = str(doc["_id"])
        text_preview = doc.get("text", "")[:80]
        ok = self.forget_by_id(mongo_id)
        return f"Забыл: {text_preview}" if ok else f"Не удалось забыть: {text_preview}"

    # ── maintenance (dream-job territory, см. SAM-22) ────────────────────────

    def prune_unused(self, max_age_days: int = 90) -> dict:
        """Удалить факты не использованные дольше max_age_days.
        Это «забывание» — естественная часть dream cycle."""
        if self.memory_repo is None:
            return {"deleted": 0, "checked": 0}
        cutoff = datetime.utcnow() - timedelta(days=max_age_days)
        query = {
            "$or": [
                {"last_used_at": {"$lt": cutoff}},
                {"$and": [
                    {"last_used_at": {"$exists": False}},
                    {"created_at": {"$lt": cutoff}},
                ]},
            ],
        }
        old_docs = self.memory_repo.find_many(query, limit=10000)
        deleted = 0
        for doc in old_docs:
            mongo_id = str(doc.get("_id", ""))
            if mongo_id and self.forget_by_id(mongo_id):
                deleted += 1
        log.info("prune_unused: deleted %d / checked %d", deleted, len(old_docs))
        return {"deleted": deleted, "checked": len(old_docs)}

    def maintenance_check(self) -> dict:
        """Health-check памяти. Для dream-job алертинга, не критичен.

        Сообщает дрифт между Qdrant snapshot'ами и Mongo фактами, но это
        не «bug consistency» — это нормально что что-то рассинхронизировано.
        Сильное расхождение → алерт оператору / SAM-22 решит чинить.
        """
        result = {
            "mongo_facts": 0,
            "qdrant_vectors": 0,
            "orphan_vectors": 0,
            "standalone_vectors": 0,  # без mongo_id — это OK (memory.md)
            "missing_vectors": 0,
        }
        if self.memory_repo is None:
            return result
        mongo_docs = self.memory_repo.find_many({}, limit=10000)
        mongo_ids = {str(d["_id"]) for d in mongo_docs}
        result["mongo_facts"] = len(mongo_ids)

        try:
            client = self._get_qdrant()
            all_points = []
            offset = None
            while True:
                resp = client.scroll(
                    collection_name=self.collection_name,
                    limit=100, offset=offset,
                    with_payload=True, with_vectors=False,
                )
                if isinstance(resp, tuple):
                    points, offset = resp
                else:
                    points = resp.points if hasattr(resp, "points") else resp
                    offset = getattr(resp, "next_page_offset", None)
                all_points.extend(points)
                if offset is None or not points:
                    break
            result["qdrant_vectors"] = len(all_points)

            qdrant_mongo_ids = set()
            for p in all_points:
                mid = (p.payload or {}).get("mongo_id")
                if not mid:
                    result["standalone_vectors"] += 1
                    continue
                qdrant_mongo_ids.add(mid)
                if mid not in mongo_ids:
                    result["orphan_vectors"] += 1
            result["missing_vectors"] = len(mongo_ids - qdrant_mongo_ids)
        except Exception as e:
            log.error("maintenance_check failed: %s", e)
        return result

    def reindex_all(self) -> dict:
        """Пересоздать все vectors из Mongo. Нужно при смене embedder модели."""
        if self.memory_repo is None:
            return {"error": "no memory_repo"}
        try:
            self._drop_collection()
        except Exception as e:
            # Коллекции может не быть; если её не удалось удалить, создание ниже скажет своё
            log.warning("reindex_all: drop collection failed: %s", e)
        self._collection_ready = False
        docs = self.memory_repo.find_many({}, limit=10000)
        reindexed = 0
        for doc in docs:
            text = doc.get("text", "")
            if not text:
                continue
            try:
                self.ensure_collection()
                self._index_facets(
                    mongo_id=str(doc["_id"]),
                    summary=doc.get("summary") or text,
                    questions=doc.get("questions", []),
                    tags=doc.get("tags", []),
                    source_session=doc.get("author"),
                )
                reindexed += 1
            except Exception as e:
                log.error("reindex %s: %s", doc.get("_id"), e)
        return {"total_docs": len(docs), "reindexed": reindexed}

    # ── private ──────────────────────────────────────────────────────────────

    def _increment_qdrant_access(self, qdrant_ids: list[str]) -> None:
        """Инкрементировать access_count + last_used_at для возвращённых
        Qdrant points. Для dream-job: какие именно vector-facets оказались
        полезны — те оставляем, плодим похожие; никогда не нужные — удалим.

        Используем set_payload для атомарного частичного обновления.
        Не падает тихо при ошибке (это аналитика).
        """
        if not qdrant_ids:
            return
        try:
            client = self._get_qdrant()
            now_iso = datetime.utcnow().isoformat()
            for qid in qdrant_ids:
                # Достать текущий access_count, инкрементировать
                try:
                    # qdrant scroll по id неудобен; читаем через query или retrieve
                    if hasattr(client, "retrieve"):
                        pts = client.retrieve(
                            collection_name=self.collection_name,
                            ids=[qid], with_payload=True, with_vectors=False,
                        )
                        current = (pts[0].payload.get("access_count", 0)
                                   if pts else 0)
                    else:
                        current = 0
                except Exception as e:
                    log.warning("access_count read failed for %s, counting from 0: %s", qid, e)
                    current = 0
                if hasattr(client, "set_payload"):
                    client.set_payload(
                        collection_name=self.collection_name,
                        payload={"access_count": current + 1,
                                 "last_used_at": now_iso},
                        points=[qid],
                    )
        except Exception as e:
            log.debug("_increment_qdrant_access (non-critical): %s", e)

    def _touch_used(self, mongo_ids: list[str]) -> None:
        if not mongo_ids or self.memory_repo is None:
            return
        now = datetime.utcnow()
        for mid in mongo_ids:
            if mid:
                try:
                    self.memory_repo.update_by_id(mid, {"last_used_at": now})
                except Exception as e:
                    log.warning("last_used_at not saved for %s: %s", mid, e)

    def find_duplicate(self, text: str, summary: str | None = None) -> dict | None:
        """Уже записанный активный факт, чей фасет почти совпадает (dedup_threshold) с summary нового факта
        (по умолчанию - начало текста, как при записи); нет такого или эмбеддер недоступен - None."""
        if not text or not text.strip():
            return None
        try:
            self.ensure_collection()
            vector = self.embed(summary or text[:SUMMARY_DEFAULT_CHARS])
        except (DimensionMismatchError, EmbedderUnavailableError) as e:
            log.warning("duplicate check skipped, saving as new: %s", e)
            return None
        mongo_id = self._find_duplicate(vector)
        doc = self.memory_repo.get_by_id(mongo_id) if mongo_id and self.memory_repo is not None else None
        if not doc or doc.get("archived"):
            return None
        return self.get_fact(mongo_id)

    def _find_duplicate(self, vector: list[float]) -> str | None:
        try:
            client = self._get_qdrant()
            points = client.query_points(
                collection_name=self.collection_name,
                query=vector, limit=1,
                score_threshold=self.dedup_threshold,
            ).points
            if points:
                mid = (points[0].payload or {}).get("mongo_id")
                if mid:
                    return mid
        except Exception as e:
            # Поиск дубля не прошёл - факт запишется отдельным, возможен дубль
            log.warning("duplicate check failed, saving as new: %s", e)
        return None

    def _upsert_point(self, qdrant_id: str, vector: list[float], payload: dict) -> None:
        try:
            from qdrant_client.models import PointStruct
            client = self._get_qdrant()
            client.upsert(
                collection_name=self.collection_name,
                points=[PointStruct(id=qdrant_id, vector=vector, payload=payload)],
            )
        except Exception as e:
            log.error("upsert failed: %s", e)

    def _delete_vectors_by_mongo_id(self, mongo_id: str) -> bool:
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            client = self._get_qdrant()
            client.delete(
                collection_name=self.collection_name,
                points_selector=Filter(must=[
                    FieldCondition(key="mongo_id", match=MatchValue(value=mongo_id))
                ]),
            )
            return True
        except Exception as e:
            log.error("delete vectors by mongo_id failed: %s", e)
            return False

    def _drop_collection(self) -> None:
        try:
            self._get_qdrant().delete_collection(collection_name=self.collection_name)
        except Exception as e:
            log.warning("drop_collection: %s", e)

    def _mmr_rerank(self, candidates: list[_Candidate], query_vec: list[float],
                    top_k: int, lam: float) -> list[_Candidate]:
        """Maximal Marginal Relevance — diversity в top-K. Релевантность - доля RRF от лучшего кандидата,
        чтобы шкала была 0..1, как у косинуса сходства."""
        if not candidates or top_k <= 0:
            return candidates[:top_k]
        try:
            candidate_vecs = [self.embed(c.summary) if c.summary else query_vec for c in candidates]
        except EmbedderUnavailableError as e:
            log.warning("MMR skipped, plain top-%d: %s", top_k, e)
            return candidates[:top_k]
        top = max(c.fused for c in candidates) or 1.0
        relevance = [c.fused / top for c in candidates]
        selected: list[int] = []
        remaining = list(range(len(candidates)))
        first = max(remaining, key=lambda i: relevance[i])
        selected.append(first)
        remaining.remove(first)
        while len(selected) < top_k and remaining:
            best_idx, best_mmr = remaining[0], -float("inf")
            for i in remaining:
                max_sim = max(
                    _cosine_sim(candidate_vecs[i], candidate_vecs[j])
                    for j in selected
                )
                score = lam * relevance[i] - (1 - lam) * max_sim
                if score > best_mmr:
                    best_mmr, best_idx = score, i
            selected.append(best_idx)
            remaining.remove(best_idx)
        return [candidates[i] for i in selected]


# ─── helpers ──────────────────────────────────────────────────────────────────

def _lexical_text(doc: dict) -> str:
    """Текст факта для лексической ветки: сам текст, summary, вопросы и теги («кот», «кличка»)."""
    parts = [doc.get("text", ""), doc.get("summary") or ""]
    parts += [q for q in doc.get("questions", []) or [] if isinstance(q, str)]
    parts += [t for t in doc.get("tags", []) or [] if isinstance(t, str)]
    return " ".join(p for p in parts if p)

def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _default_qdrant_factory():
    from qdrant_client import QdrantClient
    return QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))


# Эмбеддер-процесс (SAM-172, gateway/embedder_service.py): модель грузится раз на стенд; пусто - всегда локально
EMBEDDER_URL_ENV = "SAM_EMBEDDER_URL"
DEFAULT_EMBEDDER_URL = "http://127.0.0.1:10204"


def _default_embedder_factory():
    """Процесс эмбеддера, если отвечает; иначе модель в этом процессе (холодная загрузка - около 30 с)."""
    from .embedder_client import RemoteEmbedder, available
    url = os.getenv(EMBEDDER_URL_ENV, DEFAULT_EMBEDDER_URL).strip().rstrip("/")
    if url and available(url):
        log.info("эмбеддер: процесс %s", url)
        return RemoteEmbedder(url, _local_embedder)
    return _local_embedder()


def _local_embedder():
    try:
        from .embed_model import load_sentence_model
        return load_sentence_model(EMBED_MODEL_NAME)
    except Exception as e:
        log.warning("Embedder load failed: %s", e)
        return None


# ─── module-level backward-compat API (для agent.py) ──────────────────────────

_default_store: RAGStore | None = None


def init_default_store(memory_repo, **kwargs) -> RAGStore:
    global _default_store
    _default_store = RAGStore(memory_repo=memory_repo, **kwargs)
    return _default_store


def _store() -> RAGStore | None:
    return _default_store


def embed(text: str) -> list[float]:
    store = _store()
    if store is None:
        return []
    try:
        return store.embed(text)
    except EmbedderUnavailableError as e:
        log.error("embed: %s", e)
        return []


def ensure_collection() -> None:
    store = _store()
    if store is not None:
        store.ensure_collection()


def get_rag_context(query: str, limit: int = 5, *, memory_repo=None) -> str:
    """Backward compat — для runner.run_turn().
    Возвращает snapshot-заметки (hydrate из Mongo всегда — SAM-45 architecture)."""
    store = _store()
    if store is None:
        return ""
    try:
        results = store.search(query, top_k=limit)
    except Exception as e:
        print(f"[RAG] search failed: {e}")
        return ""
    if not results:
        return ""
    return "\n".join(f"- {r.text}" for r in results)


def get_rag_contexts(query: str, limit: int = 5) -> tuple[str, str]:
    """Один поиск - две выдачи (ADR-027): основной всё, быстрой без описаний скиллов и с обобщением вместо секрета."""
    from .secret_mark import fast_fact
    store = _store()
    if store is None:
        return "", ""
    try:
        results = store.search(query, top_k=limit)
    except Exception as e:
        print(f"[RAG] search failed: {e}")
        return "", ""
    main = "\n".join(f"- {r.text}" for r in results)
    fast = [fast_fact(r.text, r.tags, r.summary) for r in results]
    return main, "\n".join(f"- {x}" for x in fast if x)


def save_fact(text: str, tags: list[str] = None, summary: str = None,
              *, memory_repo=None) -> bool:
    store = _store()
    if store is None:
        return False
    return store.remember(text, tags=tags, summary=summary) is not None
