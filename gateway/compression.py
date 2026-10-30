"""Компрессия истории сессии (SAM-36).

Скользящее окно забывания с каскадной агрегацией. Логика отделена
от agent.py — вызывается с явными зависимостями (repos + LLM caller +
event emitter), легко тестируется через mongomock + mock LLM.

Модель забывания:
  сырое сообщение → sessions
                  → (за окном keep) → SUMMARIZE → level-1 summary
                  → (level-1 ≥ limit) → MERGE → level-2 summary
                  → перед удалением → sessions_archive (никогда не теряем)

См. подробности в docs/architecture.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from datetime import datetime
from typing import Awaitable, Callable, Protocol

from . import token_usage
from .env import env_float, env_int
from .event_bus import LINGER
from .repos.session_repo import SUMMARY_CHANNEL, THOUGHTS_CHANNEL
from .runner import LINGER_TURNS
from .speech_meta import strip_meta
from .spoken_history import strip_fate_mark

# ─── Константы плавающего окна ────────────────────────────────────────────────

# Сырое окно по числу: не больше K последних. Было 10; с окном в токенах короткие реплики держатся дольше,
# а окно плюс чанк (24) остаются ниже потолка SAM_COMPRESS_CEILING (28), чтобы фоновое сжатие в паузе
# успевало раньше срочного
COMPRESS_KEEP = 20
COMPRESS_CHUNK = 4           # за окном накопилось CHUNK - сжимать; и наименьший кусок на один вызов сводки
# Потолок сводок в контексте модели (SAM-224): больше - соседние сводки сливаются попарно каскадом, от старых
SUMMARY_CEILING = env_int("SAM_COMPRESS_SUMMARY_CEILING", 4)
# Сколько предшествующих сводок вызов слияния получает фоном: линия беседы видна, вход вызова не растёт
MERGE_BACKGROUND = env_int("SAM_COMPRESS_MERGE_BACKGROUND", 3)
# Причины скрытия слитых сводок (поле ignore): сводка остаётся в истории и мониторе, в контекст не идёт
MERGED_INTO = "слита в сводку {msg_id}"
REPEATS = "повторяет сводку {msg_id}"
RANGE_SEP = " – "
MERGE_SEP = "\n---\n"   # между сводками фона в вызове слияния
# Канал мыслей (SAM-227, compression.md «Два канала»): неозвученные блоки, вызовы и тела скиллов - своё небольшое
# окно, своя цепочка сводок с меньшим потолком; разговор в приоритете и места им не уступает
TALK = "talk"
# Окно мыслей: четверть окна разговора (600) - последний ход с вызовами и его мысль дословно; тело живого скилла
# держится сверх него (linger), пока после него меньше LINGER_TURNS реплик человека
THOUGHT_KEEP_TOKENS = env_int("SAM_COMPRESS_THOUGHT_KEEP_TOKENS", 150)
# Потолок сводок мыслей: половина потолка разговора - мысли сжимаются раньше и сильнее (compression.md, «Два канала»)
THOUGHT_CEILING = env_int("SAM_COMPRESS_THOUGHT_CEILING", 2)

# Окно и порог в токенах (SAM-62): длинные сообщения уходят из сырого окна раньше, короткие
# держатся до COMPRESS_KEEP. Замер 9 окт на записях разговоров (5881 реплика, токенизатор
# Gemma 4, o200k даёт то же): фраза человека медиана 11 токенов, p90 32; ответ медиана 33, p90 79;
# 3,3 символа на токен. Окно 600 - 10 сообщений по p90 (~56 на сообщение), порог чанка 250 - 4 таких.
# Токены считаются по символам: usage поставщика (SAM-48) приходит на вызов, а не на сообщение
CHARS_PER_TOKEN = env_float("SAM_CHARS_PER_TOKEN", 3.3)
KEEP_TOKENS = env_int("SAM_COMPRESS_KEEP_TOKENS", 600)
CHUNK_TOKENS = env_int("SAM_COMPRESS_CHUNK_TOKENS", 250)
KEEP_MIN = 2                 # последний обмен (фраза и ответ) дословно при любой длине
# Потолок несжатого в базе (SAM-97): выше него сжатие идёт сразу, не дожидаясь паузы. Считается по сообщениям
# сессии в Mongo, а не по истории хода, обрезанной лимитом. 28 - окно (20) плюс чанк (4) и запас 4 на паузу,
# ниже лимита истории хода (30, runner.history_limit)
CEILING_MSGS = env_int("SAM_COMPRESS_CEILING", 28)
# Кусок на один вызов сводки (SAM-97): до 1500 токенов сообщений. По 4 сообщения хвост в 334 сообщения после
# рестарта шёл ~80 вызовами по несколько секунд, и каждый - строкой в мониторе. Вход вызова: промпт сводки
# (~630 токенов o200k, gateway/prompts/summarize.py), 3 прошлые сводки prior_chain (~450) и кусок 1500 -
# около 2,6 тыс., ответ max_tokens=600 (agent._summarize_chunk); окно любой модели роутера (от 8 тыс.) это держит
CHUNK_MAX_TOKENS = env_int("SAM_COMPRESS_CHUNK_MAX_TOKENS", 1500)
# Потолок в токенах - окно плюс полный кусок (SAM-97). Было два окна (1200): в потоке реплик срочный проход
# начинался, как только за окном набиралось ~600 токенов, и каждый такой проход платил вызов сводки, слияние
# level 1 и автозапись фактов - сжатие шло проход за проходом по свежему хвосту
CEILING_TOKENS = env_int("SAM_COMPRESS_CEILING_TOKENS", KEEP_TOKENS + CHUNK_MAX_TOKENS)
# Предел ожидания паузы (SAM-97): после него сжатие идёт без паузы. Ход, повисший внутри activity(), держит
# ворота закрытыми бесконечно; 5 минут - в живом разговоре (реплики через 5-20 с) потолок наступит раньше
IDLE_WAIT_MAX_S = env_float("SAM_COMPRESS_IDLE_MAX_S", 300.0)
# Событие хода сжатия перед каждым вызовом сводки (SAM-97): монитор держит одну строку на сжатие вместо строки
# llm_request на вызов. Этапы: куски (level 1) и слияние сводок (level 2)
# Пауза после сбоя фонового сжатия сессии (SAM-226): иначе каждая реплика снова зовёт автозапись фактов и сводку,
# которые снова упадут на той же модели; 2 минуты - несколько реплик живого разговора
FAIL_RETRY_S = env_float("SAM_COMPRESS_FAIL_RETRY_S", 120.0)
PROGRESS_EVENT = "compression_progress"
STAGE_CHUNK, STAGE_MERGE = "chunk", "merge"
DEDUP_THRESHOLD = 0.75       # TF-IDF cosine для dedup near-duplicates
DEDUP_MIN_LEN = 15           # короче этой длины — не чистим («спасибо», «ага»)


# ─── State (по сессии) ─────────────────────────────────────────────────────────

_compress_hashes: dict[str, str] = {}    # session_id → hash последнего сжатия
_compress_in_progress: set[str] = set()  # защита от параллельных запусков
_failed_at: dict[str, float] = {}        # session_id → time.monotonic() последнего сбоя фонового сжатия


# ─── Protocols (тип-документация зависимостей) ─────────────────────────────────

class SessionRepoProtocol(Protocol):
    def get_all_for_compression(self, session_id: str) -> list[dict]: ...
    def active_summaries(self, session_id: str, channel: str | None = None) -> list[dict]: ...
    def insert_summary(self, doc: dict): ...
    def hide_ids(self, ids, reason: str) -> int: ...
    def extend_summary(self, summary_id, time_to: str, time_range: str) -> None: ...
    def upsert_summary(self, msg_id, summary: str, time_from: str, time_to: str,
                       time_range: str, compression_level: int = 1) -> None: ...
    def delete_by_ids(self, ids) -> int: ...


class ArchiveRepoProtocol(Protocol):
    def archive_messages(self, session_id: str, messages, reason: str) -> int: ...


class MetaRepoProtocol(Protocol):
    def delete(self, session_id: str) -> None: ...


# Type для функции вызова LLM (avoidance circular import)
LLMSummarizer = Callable[[list, dict, str], Awaitable[str]]
"""(messages_for_llm, llm_config, prior_context) → summary_text"""

EventEmitter = Callable[[str, dict], None]
"""(event_type, data) — например event_bus.emit"""

LLMConfigGetter = Callable[[], Awaitable[dict | None]]
"""() → конфиг модели сводки или None, если поставщиков нет"""

LLMMerger = Callable[[list[str], dict, str], Awaitable[str]]
"""(тексты пары сводок, llm_config, фон - предшествующие сводки) → сводка пары или SKIP, если пара повторяет фон"""


class CompressionStatus(Protocol):
    """Кто следит за фоновым сжатием (SAM-97): ждёт паузы, идёт, закончилось с ошибкой или без."""
    def waiting(self, session_id: str) -> None: ...
    def started(self, session_id: str) -> None: ...
    def finished(self, session_id: str, error: str | None) -> None: ...


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_ts(ts, fmt: str = "%H:%M:%S") -> str:
    if ts is None:
        return "?"
    ts_n = ts.replace(tzinfo=None) if hasattr(ts, "tzinfo") and ts.tzinfo else ts
    return ts_n.strftime(fmt)


FIRST_LINE_CHARS = 80   # метка прочитанного скилла в пересказе: имя и первая строка, не тело


def _first_line(text) -> str:
    return next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")[:FIRST_LINE_CHARS]


def channel_of(m: dict) -> str:
    """Канал записи: фраза человека и ответ SAM с произнесённым текстом - разговор; ответ из одних неозвученных
    блоков, вызов инструмента и его итог - мысли (SAM-227). Канал выводится из записи, отдельного поля нет."""
    if m.get("role") == "user":
        return TALK
    if m.get("role") == "assistant" and not m.get("tool_calls"):
        return TALK if strip_meta(strip_fate_mark(m.get("content") or "")).strip() else THOUGHTS_CHANNEL
    return THOUGHTS_CHANNEL


def has_talk(chunk: list[dict]) -> bool:
    """В куске есть реплика человека или произнесённый текст SAM (канал разговора, channel_of) с непустым текстом."""
    return any(channel_of(m) == TALK and strip_fate_mark(m.get("content") or "").strip() for m in chunk)


def _linger_alive(messages: list[dict], i: int, turns: int = LINGER_TURNS) -> bool:
    """Тело скилла ещё живо в контексте хода (runner.lingering): после него меньше turns реплик человека."""
    if not messages[i].get(LINGER):
        return False
    return sum(1 for m in messages[i + 1:] if m.get("role") == "user") < turns


def _to_llm_msg(m: dict) -> dict:
    """Преобразовать MongoDB-документ в формат для LLM-prompt."""
    if m.get("role") == "tool_call":
        return {"role": "assistant", "content": f"[вызов: {m.get('tool_name', '')}]"}
    if m.get("role") == "assistant" and m.get("tool_calls"):
        names = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
        return {"role": "assistant", "content": f"[вызов инструментов: {names}]"}
    if m.get("role") == "tool" and m.get("linger"):
        # тело скилла в пересказ не идёт: он теряет точные шаги; нужно снова - load_skill
        return {"role": "user", "content": f"[прочитан {m.get('tool_name', '')}: {_first_line(m.get('content'))}]"}
    if m.get("role") == "tool":
        return {"role": "user", "content": f"[результат: {(m.get('content') or '')[:200]}]"}
    return {"role": m.get("role", "user"), "content": strip_fate_mark(m.get("content") or "")}


# ─── Окно сырых сообщений в токенах ────────────────────────────────────────────

def message_tokens(m: dict, chars_per_token: float = CHARS_PER_TOKEN) -> int:
    """Токены сообщения по символам того, что видит модель сжатия."""
    return max(1, round(len(_to_llm_msg(m)["content"]) / chars_per_token))


def split_raw_window(messages: list[dict], keep_tokens: int = KEEP_TOKENS, keep_max: int = COMPRESS_KEEP,
                     keep_min: int = KEEP_MIN, count=message_tokens,
                     thought_tokens: int = THOUGHT_KEEP_TOKENS) -> tuple[list[dict], list[dict]]:
    """(за окном, окно), порядок сохранён. У каждого канала своё окно от конца (SAM-227): разговор - последние реплики
    в пределах keep_tokens, не больше keep_max и не меньше keep_min; мысли - в пределах thought_tokens. Поток мыслей
    реплики людей из окна не вытесняет, долгая тишина с работой агентов - тоже."""
    kept: set[int] = set()
    size, used, thought_used = 0, 0, 0
    talk_open, thought_open = True, True
    for i in range(len(messages) - 1, -1, -1):
        m, tokens = messages[i], count(messages[i])
        if channel_of(m) == TALK:
            if talk_open and not (size >= keep_max or (size >= keep_min and used + tokens > keep_tokens)):
                kept.add(i)
                size, used = size + 1, used + tokens
            else:
                talk_open = False
        elif _linger_alive(messages, i):
            kept.add(i)   # живое тело скилла сжимать нельзя: ход читает его целиком (runner.lingering)
        elif thought_open and thought_used + tokens <= thought_tokens:
            kept.add(i)
            thought_used += tokens
        else:
            thought_open = False
    return ([m for i, m in enumerate(messages) if i not in kept], [m for i, m in enumerate(messages) if i in kept])


def needs_compression(messages: list[dict], chunk: int = COMPRESS_CHUNK, chunk_tokens: int = CHUNK_TOKENS,
                      count=message_tokens, **window) -> bool:
    """Есть что сжать: за окном накопился чанк по числу или по токенам. Сводки не считаются."""
    regular = [m for m in messages if m.get("role") != "summary"]
    older, _ = split_raw_window(regular, count=count, **window)
    return len(older) >= chunk or (bool(older) and sum(count(m) for m in older) >= chunk_tokens)


def pending_tail(messages: list[dict], count=message_tokens) -> tuple[int, int]:
    """(сообщений, токенов) несжатых: всё, кроме сводок."""
    regular = [m for m in messages if m.get("role") != "summary"]
    return len(regular), sum(count(m) for m in regular)


def above_ceiling(messages: list[dict], ceiling_msgs: int = CEILING_MSGS, ceiling_tokens: int = CEILING_TOKENS,
                  count=message_tokens) -> bool:
    """Несжатого в базе на потолке по числу или по токенам (SAM-97)."""
    msgs, tokens = pending_tail(messages, count)
    return msgs >= ceiling_msgs or tokens >= ceiling_tokens


def chunk_by_tokens(messages: list[dict], max_tokens: int = CHUNK_MAX_TOKENS, min_msgs: int = COMPRESS_CHUNK,
                    count=message_tokens) -> list[list[dict]]:
    """Куски на вызовы сводки: подряд, пока сумма токенов в пределах max_tokens, но не меньше min_msgs сообщений.
    Хвост короче min_msgs доливается в предыдущий кусок: отдельный вызов на одно-два сообщения не нужен."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    for m in messages:
        tokens = count(m)
        if len(current) >= min_msgs and used + tokens > max_tokens:
            chunks.append(current)
            current, used = [], 0
        current.append(m)
        used += tokens
    if current:
        if chunks and len(current) < min_msgs:
            chunks[-1].extend(current)
        else:
            chunks.append(current)
    return chunks


# ─── Dedup ─────────────────────────────────────────────────────────────────────

def dedupe_recent_messages(
    session_id: str,
    recent: list[dict],
    session_repo: SessionRepoProtocol,
    archive_repo: ArchiveRepoProtocol,
    threshold: float = DEDUP_THRESHOLD,
) -> int:
    """SAM-36: убрать почти-дубли. STT pre-trigger может оставить
    повторяющиеся куски диалога в соседних user-сообщениях. Архивируем
    перед удалением. Возвращает количество удалённых.
    """
    if len(recent) < 2:
        return 0
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
    except ImportError:
        return 0

    # Пары соседей одного role
    candidates: list[tuple[int, int]] = []
    last_by_role: dict[str, int] = {}
    for i, m in enumerate(recent):
        role = m.get("role") or "user"
        if role in ("summary", "tool_call", "tool"):
            continue
        if role in last_by_role:
            candidates.append((last_by_role[role], i))
        last_by_role[role] = i

    if not candidates:
        return 0

    texts = [(m.get("content") or "").strip() for m in recent]
    to_remove: list[int] = []
    for old_i, new_i in candidates:
        old_t, new_t = texts[old_i], texts[new_i]
        if not old_t or not new_t:
            continue
        if len(old_t) < DEDUP_MIN_LEN or len(new_t) < DEDUP_MIN_LEN:
            continue
        try:
            vec = TfidfVectorizer(analyzer="word", lowercase=True, min_df=1)
            matrix = vec.fit_transform([old_t, new_t])
            sim = float(cosine_similarity(matrix[0], matrix[1])[0][0])
        except ValueError:
            continue
        if sim >= threshold:
            to_remove.append(old_i)

    if not to_remove:
        return 0

    docs_to_remove = [recent[i] for i in to_remove]
    archive_repo.archive_messages(session_id, docs_to_remove, reason="dedup_recent")
    deleted = session_repo.delete_by_ids([m["_id"] for m in docs_to_remove])
    if deleted:
        print(f"[Compress/dedup] session={session_id}: removed {deleted} near-dup")
    return deleted


# ─── Background compression scheduler ──────────────────────────────────────────

def schedule_background_compression(
    session_id: str,
    get_llm: LLMConfigGetter,
    *,
    session_repo: SessionRepoProtocol,
    archive_repo: ArchiveRepoProtocol,
    meta_repo: MetaRepoProtocol,
    summarizer: LLMSummarizer,
    is_trivial_summary: Callable[[str], bool],
    event_emit: EventEmitter | None = None,
    gate=None,
    urgent: bool = False,
    idle_max_s: float | None = IDLE_WAIT_MAX_S,
    status: CompressionStatus | None = None,
    compress: Callable[..., Awaitable[str]] | None = None,
    on_forget: Callable[[str, list[dict]], Awaitable[list]] | None = None,
    retry_after_s: float = FAIL_RETRY_S,
    merger: LLMMerger | None = None,
    thought_summarizer: LLMSummarizer | None = None,
) -> bool:
    """SAM-36: запустить compress_session_history в фоне.
    Возвращает True если задача поставлена, False если уже идёт или ждёт.

    gate - ворота простоя (SAM-62): сжатие ждёт паузы в разговоре, чтобы не делить GPU с живым
    ответом, но не дольше idle_max_s (SAM-97); urgent (несжатого выше потолка) - сжать сразу.
    get_llm - модель сводки, берётся после ожидания: за минуты паузы поставщик мог смениться.
    status - кто следит за сжатием (/health, SAM-97). retry_after_s - пауза после сбоя этой сессии (SAM-226)."""
    if session_id in _compress_in_progress:
        return False
    if time.monotonic() - _failed_at.get(session_id, float("-inf")) < retry_after_s:
        return False
    _compress_in_progress.add(session_id)
    compress = compress or compress_session_history

    async def _runner():
        error = None
        try:
            if gate is not None and not urgent and not gate.is_idle():
                if status is not None:
                    status.waiting(session_id)
                if event_emit is not None:
                    event_emit("compression_waiting", {"session": session_id, "max_wait_s": idle_max_s})
                if not await gate.wait_idle(max_s=idle_max_s):
                    print(f"[Compress/bg] {session_id}: паузы нет {idle_max_s:.0f} с, сжатие без паузы")
            if status is not None:
                status.started(session_id)
            llm_config = await get_llm()
            if not llm_config:
                raise RuntimeError("нет доступного поставщика LLM для сводки")
            result = await compress(
                session_id, llm_config,
                session_repo=session_repo, archive_repo=archive_repo,
                meta_repo=meta_repo, summarizer=summarizer,
                is_trivial_summary=is_trivial_summary, event_emit=event_emit,
                **({"on_forget": on_forget} if on_forget else {}),
                **({"merger": merger} if merger else {}),
                **({"thought_summarizer": thought_summarizer} if thought_summarizer else {}),
            )
            print(f"[Compress/bg] {session_id}: {result}")
            _failed_at.pop(session_id, None)
        except Exception as e:
            # Сбой суммаризации в фоне видно в мониторе, а не только в консоли (SAM-62); оригиналы
            # несжатого куска остаются в истории, хэш не пишется - следующая реплика поставит снова
            error = next((line for line in str(e).splitlines() if line.strip()), type(e).__name__)
            print(f"[Compress/bg] {session_id} ERROR: {error}")
            _failed_at[session_id] = time.monotonic()
            if event_emit is not None:
                event_emit("compression_error", {"session": session_id, "error": error})
        finally:
            _compress_in_progress.discard(session_id)
            if status is not None:
                status.finished(session_id, error)

    asyncio.create_task(_runner(), name=f"compress:{session_id}")
    return True


def in_progress(session_id: str) -> bool:
    """Сжатие сессии идёт или ждёт паузы."""
    return session_id in _compress_in_progress


async def _write_facts(session_id: str, messages: list[dict], on_forget, event_emit) -> None:
    """Автозапись: её сбой не останавливает сжатие - оригиналы куска уходят в архив."""
    if on_forget is None or not messages:
        return
    try:
        written = await on_forget(session_id, [m for m in messages if m.get("role") != "summary"])
    except Exception as e:
        error = next((line for line in str(e).splitlines() if line.strip()), type(e).__name__)
        print(f"[Compress] автозапись фактов {session_id} не удалась: {error}")
        if event_emit is not None:
            event_emit("memory_autowrite_error", {"session": session_id, "error": error})
        return
    if written and event_emit is not None:
        event_emit("memory_autowrite", {"session": session_id, "facts": list(written)})


def _ids_hash(messages: list[dict]) -> str:
    """Отпечаток набора сообщений по их _id: тот же набор - история не менялась."""
    return hashlib.md5("".join(str(m["_id"]) for m in messages).encode()).hexdigest()


async def compress_session_exclusive(session_id: str, llm_config: dict, **kwargs) -> str:
    """Явное сжатие (tool compress_session, эндпоинт): не идёт, пока по сессии идёт другое.
    Фоновое ставит тот же флаг _compress_in_progress, поэтому два сжатия одной истории
    не пересекаются (одно на сессию)."""
    if session_id in _compress_in_progress:
        return "Сжатие этой сессии уже идёт, повтори позже"
    _compress_in_progress.add(session_id)
    try:
        return await compress_session_history(session_id, llm_config, **kwargs)
    finally:
        _compress_in_progress.discard(session_id)


# ─── Каскад сводок (SAM-224) ───────────────────────────────────────────────────

def pair_pass(count: int) -> list[tuple[int, ...]]:
    """Один проход каскада: соседние пары от старых, нечётная последняя остаётся одна. 5 → (0,1) (2,3) (4)."""
    return [tuple(range(i, min(i + 2, count))) for i in range(0, count, 2)]


def joined_range(first: dict, last: dict) -> str:
    """Отрезок сводки пары: начало первой, конец второй."""
    start = str(first.get("time_range") or first.get("time_from") or "").split(RANGE_SEP)[0]
    end = str(last.get("time_range") or last.get("time_to") or "").split(RANGE_SEP)[-1]
    return f"{start}{RANGE_SEP}{end}"


def planned_merges(count: int, ceiling: int) -> int:
    """Сколько вызовов слияния даст каскад из count сводок: для полосы прогресса монитора."""
    calls = 0
    while count > ceiling:
        pairs = count // 2
        calls, count = calls + pairs, count - pairs
    return calls


class _MonitorRows:
    """Строки живого монитора через каскад: у каждой сводки контекста - строка самой ранней исходной сводки (её
    data-mid уже в ленте) и строки поглощённых, которые надо убрать. Итог - одно событие context_compressed."""

    def __init__(self, active: list[dict]) -> None:
        self.docs = {d.get("msg_id", ""): d for d in active}
        self.row: dict[str, str] = {}
        self.gone: dict[str, list[str]] = {}

    def _row(self, doc: dict) -> str:
        mid = doc.get("msg_id", "")
        return self.row.get(mid, mid)

    def take(self, keeper: dict, row_from: dict, sources: list[dict]) -> None:
        """keeper встаёт строкой row_from; строки sources (кроме неё) и всё, что они уже поглотили, уходят."""
        kid = keeper.get("msg_id", "")
        keep = self._row(row_from)
        moved = list(self.gone.get(kid, []))
        for d in sources:
            mid = d.get("msg_id", "")
            moved += self.gone.pop(mid, [])
            if self._row(d) != keep:
                moved.append(self._row(d))
            self.docs.pop(mid, None)
        self.row[kid], self.gone[kid], self.docs[kid] = keep, moved, keeper

    def payload(self) -> list[dict]:
        return [{"agg_id": self._row(d), "del_ids": self.gone[mid], "time_from": "", "time_to": "",
                 "time_range": d.get("time_range", ""), "content": d.get("content", ""),
                 "level": d.get("compression_level", 1)}
                for mid, d in self.docs.items() if mid in self.gone]


def _absorb(session_repo, keeper: dict, first: dict, second: dict) -> dict:
    """Пара - повтор: keeper (предыдущая сводка или первая из пары) растягивает отрезок до конца пары, остальные
    скрываются с причиной «повторяет сводку»."""
    span = joined_range(keeper, second)
    session_repo.extend_summary(keeper["_id"], second.get("time_to", ""), span)
    hidden = [d["_id"] for d in (first, second) if d is not keeper]
    session_repo.hide_ids(hidden, REPEATS.format(msg_id=keeper.get("msg_id", "")))
    return {**keeper, "time_to": second.get("time_to", ""), "time_range": span}


def _replace_pair(session_id: str, session_repo, first: dict, second: dict, text: str) -> dict:
    """Сводка пары встаёт на место первой (её время), обе слитые скрываются из контекста и остаются в истории.
    Вставка идёт до скрытия: сбой между ними оставляет в контексте лишнюю сводку, а не теряет пару."""
    merged = {
        "session_id": session_id, "msg_id": str(uuid.uuid4()), "timestamp": first.get("timestamp"),
        "role": "summary", "is_summary": True, "content": text,
        "compression_level": max(first.get("compression_level", 1), second.get("compression_level", 1)) + 1,
        "time_from": first.get("time_from", ""), "time_to": second.get("time_to", ""),
        "time_range": joined_range(first, second),
        **({SUMMARY_CHANNEL: first[SUMMARY_CHANNEL]} if first.get(SUMMARY_CHANNEL) else {}),
    }
    merged["_id"] = session_repo.insert_summary(merged)
    session_repo.hide_ids([first["_id"], second["_id"]], MERGED_INTO.format(msg_id=merged["msg_id"]))
    return merged


async def fold_summaries(
    session_id: str,
    llm_config: dict,
    *,
    session_repo: SessionRepoProtocol,
    merger: LLMMerger,
    is_trivial_summary: Callable[[str], bool],
    event_emit: EventEmitter | None = None,
    ceiling: int = SUMMARY_CEILING,
    background: int = MERGE_BACKGROUND,
    channel: str | None = None,
) -> int:
    """Пока сводок контекста больше потолка - попарное слияние от старых, проход за проходом (дизайн compression.md).

    Вызов слияния получает предшествующие сводки фоном. Пару, которую модель считает повтором фона (SKIP), поглощает
    предыдущая сводка, а у самой старой пары фона нет - вторую поглощает первая. Возвращает число вызовов слияния;
    монитор получает итог одним событием и при сбое на середине."""
    active = session_repo.active_summaries(session_id, channel) if channel else session_repo.active_summaries(session_id)
    planned = planned_merges(len(active), ceiling)
    calls = 0
    rows = _MonitorRows(active)
    try:
        while len(active) > ceiling:
            out: list[dict] = []
            for group in pair_pass(len(active)):
                if len(group) == 1:
                    out.append(active[group[0]])
                    continue
                first, second = active[group[0]], active[group[1]]
                calls += 1
                if event_emit is not None:
                    event_emit(PROGRESS_EVENT, {"session": session_id, "stage": STAGE_MERGE, "call": calls,
                                                "calls": planned, "done": calls - 1, "total": planned})
                bg = MERGE_SEP.join(d.get("content", "") for d in out[-background:]) if background > 0 else ""
                with token_usage.usage_scope(session_id, token_usage.ROLE_SUMMARIZE):
                    text = await merger([first.get("content", ""), second.get("content", "")], llm_config, bg)
                if not is_trivial_summary(text):
                    merged = _replace_pair(session_id, session_repo, first, second, text)
                    rows.take(merged, first, [first, second])
                    out.append(merged)
                    continue
                keeper = out[-1] if out else first
                grown = _absorb(session_repo, keeper, first, second)
                rows.take(grown, keeper, [d for d in (first, second) if d is not keeper])
                if out:
                    out[-1] = grown
                else:
                    out.append(grown)
            active = out
    finally:
        changed = rows.payload()
        if changed and event_emit is not None:
            event_emit("context_compressed", {"session": session_id, "total_msgs": len(rows.docs),
                                              "summaries": changed})
    if calls:
        print(f"[Compress] каскад {session_id}: {calls} слияний, сводок в контексте {len(active)}")
    return calls


# ─── Главная функция компрессии ───────────────────────────────────────────────

async def compress_session_history(
    session_id: str,
    llm_config: dict,
    *,
    session_repo: SessionRepoProtocol,
    archive_repo: ArchiveRepoProtocol,
    meta_repo: MetaRepoProtocol,
    summarizer: LLMSummarizer,
    is_trivial_summary: Callable[[str], bool],
    event_emit: EventEmitter | None = None,
    force: bool = False,
    on_forget: Callable[[str, list[dict]], Awaitable[list]] | None = None,
    merger: LLMMerger | None = None,
    thought_summarizer: LLMSummarizer | None = None,
) -> str:
    """Проход сжатия: сообщения за окном - в сводки, затем каскад сводок до потолка (если передан merger).

    Каскад идёт и тогда, когда сообщений сжимать нечего: накопленные сводки сворачиваются первым же проходом."""
    result = await _compress_messages(
        session_id, llm_config, session_repo=session_repo, archive_repo=archive_repo, meta_repo=meta_repo,
        summarizer=summarizer, is_trivial_summary=is_trivial_summary, event_emit=event_emit, force=force,
        on_forget=on_forget, thought_summarizer=thought_summarizer,
    )
    if merger is None:
        return result
    # Каскад по каналам (SAM-227): сводки разговора сливаются только от переполнения своими сводками
    calls = 0
    for channel, ceiling in ((TALK, SUMMARY_CEILING), (THOUGHTS_CHANNEL, THOUGHT_CEILING)):
        calls += await fold_summaries(session_id, llm_config, session_repo=session_repo, merger=merger,
                                      is_trivial_summary=is_trivial_summary, event_emit=event_emit,
                                      ceiling=ceiling, channel=channel)
    if calls:
        meta_repo.delete(session_id)
        result += f" Каскад: {calls} слияний сводок."
    return result


async def _compress_messages(
    session_id: str,
    llm_config: dict,
    *,
    session_repo: SessionRepoProtocol,
    archive_repo: ArchiveRepoProtocol,
    meta_repo: MetaRepoProtocol,
    summarizer: LLMSummarizer,
    is_trivial_summary: Callable[[str], bool],
    event_emit: EventEmitter | None = None,
    force: bool = False,
    on_forget: Callable[[str, list[dict]], Awaitable[list]] | None = None,
    thought_summarizer: LLMSummarizer | None = None,
) -> str:
    """
    Сжатие сообщений сессии. Скользящее окно с архивом, куски по токенам, разговор и мысли - раздельно (SAM-227).

    Args:
        session_id: чью сессию сжимать
        llm_config: для вызова summarizer
        session_repo / archive_repo / meta_repo: MongoDB-доступ
        summarizer: async (messages, llm_config, prior_context) → summary
        is_trivial_summary: фильтр SKIP-маркера от LLM
        event_emit: callback (event_type, data) — обычно event_bus.emit
        force: True = сжать всё, False = только за keep-окном
    """
    calls = 0

    async def _summarize(*args, done: int, total: int, planned: int, stage: str, fn: LLMSummarizer | None = None):
        # Ход сжатия - событием на вызов, монитор обновляет одну строку на месте (SAM-97)
        nonlocal calls
        calls += 1
        if event_emit is not None:
            event_emit(PROGRESS_EVENT, {"session": session_id, "stage": stage, "call": calls,
                                        "calls": planned, "done": done, "total": total})
        # Токены сжатия записываются на сессию с ролью summarize (SAM-48)
        with token_usage.usage_scope(session_id, token_usage.ROLE_SUMMARIZE):
            return await (fn or summarizer)(*args)

    all_msgs = session_repo.get_all_for_compression(session_id)
    regular_msgs = [m for m in all_msgs if m.get("role") != "summary"]
    total = len(regular_msgs)
    # Без force сжимается только то, что за окном, а в окне всегда KEEP_MIN последних
    least = COMPRESS_CHUNK if force else KEEP_MIN + 1

    if total < least:
        return f"Нечего сжимать: {total} сообщений (нужно ≥ {least})"

    # Dedup ПЕРЕД суммаризацией
    removed = dedupe_recent_messages(session_id, regular_msgs, session_repo, archive_repo)
    if removed:
        all_msgs = session_repo.get_all_for_compression(session_id)
        regular_msgs = [m for m in all_msgs if m.get("role") != "summary"]
        total = len(regular_msgs)
        if total < least:
            return f"После дедупа осталось {total} сообщ., нечего сжимать"

    # Идемпотентность: не сжимать одно и то же дважды. Хэш пишется только после
    # успешного сжатия: упавшая суммаризация не должна выдавать историю за сжатую (SAM-76)
    if _compress_hashes.get(session_id) == _ids_hash(regular_msgs):
        return "Контекст уже сжат (нет новых сообщений)"

    # Что компрессить: снимок всего, что старше окна, одним проходом (SAM-97). Записи, пришедшие во время
    # прохода, ждут следующего. Остаток от деления на 4 больше не уходит отдельной сводкой level 2: кусок по
    # токенам берёт его с собой, level 2 даёт только рекурсивное слияние
    window: list[dict] = []
    if force:
        to_compress = regular_msgs
    else:
        # Окно сырых в токенах (SAM-62): длинные сообщения покидают его раньше
        older, window = split_raw_window(regular_msgs)
        if not needs_compression(regular_msgs):
            return f"Окно стабильно: {total} сообщений, за окном {len(older)}"
        to_compress = older

    # Факты куска - в долгую память до того, как он уйдёт из истории (SAM-63)
    with token_usage.usage_scope(session_id, token_usage.ROLE_SUMMARIZE):
        await _write_facts(session_id, to_compress, on_forget, event_emit)

    # Архивируем оригиналы ПЕРЕД компрессией
    archive_repo.archive_messages(session_id, to_compress, reason="compressed")

    # Куски по токенам (SAM-97): хвост в сотни сообщений - единицы вызовов, prior_chain тянется через них
    # Куски по каналам (SAM-227): разговор и мысли - свои цепочки сводок, мысли без своего суммаризатора не сводятся
    talk_part = [m for m in to_compress if channel_of(m) == TALK]
    thought_part = [m for m in to_compress if channel_of(m) != TALK]
    chunks = [(TALK, c) for c in chunk_by_tokens(talk_part)] + \
             [(THOUGHTS_CHANNEL, c) for c in (chunk_by_tokens(thought_part) if thought_part else [])]

    # Чанки → level-1 summary, prior_chain накапливается своя у каждого канала
    summary_docs = []
    prior_chains: dict[str, list[str]] = {TALK: [], THOUGHTS_CHANNEL: []}
    done = 0
    for channel, chunk in chunks:
        prior_chain = prior_chains[channel]
        fn = summarizer if channel == TALK else thought_summarizer
        if fn is None or (channel == TALK and not has_talk(chunk)):
            why = "мысли без суммаризатора" if fn is None else "без реплик"
            # только вызовы и их итоги: сводка вышла бы «разговора не было, вызывала sync_skills» (SAM-219), а
            # оригиналы уже в архиве
            session_repo.delete_by_ids([m["_id"] for m in chunk])
            done += len(chunk)
            print(f"[Compress] {why}, кусок без сводки ({len(chunk)} msgs)")
            continue
        llm_msgs = [_to_llm_msg(m) for m in chunk]
        prior_text = "\n---\n".join(prior_chain[-3:]) if prior_chain else ""
        summary_text = await _summarize(llm_msgs, llm_config, prior_text, done=done, total=len(to_compress),
                                        planned=len(chunks), stage=STAGE_CHUNK, fn=fn)
        done += len(chunk)

        first = chunk[0]
        rest = chunk[1:]
        ts_end = chunk[-1]["timestamp"]
        time_from = _fmt_ts(first["timestamp"])
        time_to = _fmt_ts(ts_end)
        time_range = f"{_fmt_ts(first['timestamp'], '%H:%M')} – {_fmt_ts(ts_end, '%H:%M')}"

        if is_trivial_summary(summary_text):
            session_repo.delete_by_ids([m["_id"] for m in chunk])
            print(f"[Compress] SKIP marker, dropped chunk ({len(chunk)} msgs)")
            continue

        prior_chain.append(summary_text)
        session_repo.upsert_summary(
            first["_id"], summary_text, time_from, time_to, time_range,
            compression_level=1, **({"channel": THOUGHTS_CHANNEL} if channel != TALK else {}),
        )
        session_repo.delete_by_ids([m["_id"] for m in rest])
        summary_docs.append({
            "agg_id": first.get("msg_id", ""),
            "del_ids": [m.get("msg_id", "") for m in rest],
            "time_from": time_from, "time_to": time_to,
            "time_range": time_range, "content": summary_text,
            **({SUMMARY_CHANNEL: THOUGHTS_CHANNEL} if channel != TALK else {}),
        })


    # Сбросить кеш _sessions_meta — он теперь устарел
    meta_repo.delete(session_id)

    # Event для UI/monitor
    new_total = len(summary_docs) if force else len(window) + len(summary_docs)
    all_summary_payload = summary_docs
    if event_emit is not None:
        event_emit("context_compressed", {
            "session": session_id,
            "total_msgs": new_total,
            "summaries": [
                {
                    "agg_id": d["agg_id"], "del_ids": d["del_ids"],
                    "time_from": d["time_from"], "time_to": d["time_to"],
                    "time_range": d["time_range"], "content": d["content"],
                    "level": d.get("level", 1),
                    **({SUMMARY_CHANNEL: d[SUMMARY_CHANNEL]} if d.get(SUMMARY_CHANNEL) else {}),
                }
                for d in all_summary_payload
            ],
        })
    remaining = session_repo.get_all_for_compression(session_id)
    _compress_hashes[session_id] = _ids_hash([m for m in remaining if m.get("role") != "summary"])
    return (
        f"Сжато: {len(to_compress)} сообщений → {len(summary_docs)} резюме. "
        f"В MongoDB: {new_total} записей."
    )
