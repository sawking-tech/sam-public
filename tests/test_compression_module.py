"""Сжатие истории (SAM-36, SAM-62, SAM-76, SAM-97): настоящий gateway.compression с репозиториями на mongomock.

Подменён только суммаризатор - вызов LLM; признак пустой сводки - настоящий из gateway.prompts."""

import asyncio
import sys
from datetime import datetime, timedelta

import pytest

from gateway import compression, main_model
from gateway.prompts import is_trivial_summary
from gateway.repos import ArchiveRepo, SessionMetaRepo, SessionRepo

T0 = datetime(2026, 10, 10, 12, 0)
LLM = {"model": "fake"}


# ─── Утилиты ──────────────────────────────────────────────────────────────────

def _msg(_id, content, *, role="user", minute=0, session="s1", **extra):
    return {"_id": _id, "session_id": session, "role": role, "content": content,
            "timestamp": T0 + timedelta(minutes=minute), "msg_id": f"m-{_id}", **extra}


def _insert(db, n, text=lambda i: f"m{i}"):
    for i in range(n):
        db.sessions.insert_one(_msg(str(i), text(i), minute=i))


def _summarizer(answer="Сводка", calls=None):
    """Суммаризатор-фальшивка вместо LLM: ответ строкой или исключением, куски - в calls."""
    async def _summ(messages, llm_config, prior_context):
        if calls is not None:
            calls.append(messages)
        if isinstance(answer, BaseException):
            raise answer
        return answer
    return _summ


def _task(session):
    """Задача фонового сжатия сессии (имя ставит schedule_background_compression)."""
    return next(t for t in asyncio.all_tasks() if t.get_name() == f"compress:{session}")


@pytest.fixture(autouse=True)
def reset_state():
    """Хэши сжатых кусков и занятые сессии - состояние модуля на процесс."""
    compression._compress_hashes.clear()
    compression._compress_in_progress.clear()
    compression._failed_at.clear()
    yield
    compression._compress_hashes.clear()
    compression._compress_in_progress.clear()
    compression._failed_at.clear()


@pytest.fixture
def events():
    return []


@pytest.fixture
def deps(mongo_db, events):
    archive = ArchiveRepo(mongo_db)
    archive.ensure_indexes()
    return {
        "session_repo": SessionRepo(mongo_db),
        "archive_repo": archive,
        "meta_repo": SessionMetaRepo(mongo_db),
        "is_trivial_summary": is_trivial_summary,
        "event_emit": lambda kind, data: events.append((kind, data)),
    }


def _three_long(db):
    # Разные темы: иначе дедуп уберёт их как повторы
    topics = ("кот Мурка спит на окне", "рецепт борща со свёклой", "сборка дотнет проекта в райдере")
    for i, topic in enumerate(topics):
        db.sessions.insert_one(_msg(str(i), f"{i} " + f"{topic}. " * 80, minute=i))


# ─── окно, сводка и архив ─────────────────────────────────────────────────────

@pytest.mark.parametrize("fill, force, result_part, summaries, archived, raw_left", [
    (lambda db: _insert(db, 2), False, "Нечего сжимать", 0, 0, 2),
    # окно 20 коротких + чанк 4 = 24, всего 23 - окно стабильно
    (lambda db: _insert(db, 23), False, "Окно стабильно", 0, 0, 23),
    (lambda db: _insert(db, 24), False, "Сжато: 4 сообщений → 1 резюме", 1, 4, 20),
    # SAM-62: длинные уходят из окна раньше, последний обмен дословно
    (_three_long, False, "Сжато: 1 сообщений → 1 резюме", 1, 1, 2),
    # force: все 8 коротких в пределах CHUNK_MAX_TOKENS - один кусок, один вызов (SAM-97)
    (lambda db: _insert(db, 8), True, "Сжато", 1, 8, 0),
], ids=["not-enough", "window-stable", "one-chunk", "long-leave-first", "force"])
async def test_history_beyond_window_is_summarized_and_archived(
        mongo_db, deps, events, fill, force, result_part, summaries, archived, raw_left):
    fill(mongo_db)
    result = await compression.compress_session_history(
        "s1", LLM, summarizer=_summarizer("ВАЖНО: суть сжата"), force=force, **deps)

    assert result_part in result
    summary_docs = list(mongo_db.sessions.find({"is_summary": True}))
    assert len(summary_docs) == summaries
    assert mongo_db.sessions.count_documents({"is_summary": {"$ne": True}}) == raw_left
    assert mongo_db.sessions_archive.count_documents({}) == archived
    compressed = [d for kind, d in events if kind == "context_compressed"]
    if summaries:
        assert summary_docs[0]["content"] == "ВАЖНО: суть сжата" and summary_docs[0]["compression_level"] == 1
        assert compressed and compressed[0]["session"] == "s1" and compressed[0]["summaries"]
    else:
        assert compressed == []


@pytest.mark.parametrize("msgs, older, window, needed", [
    # ~545 токенов каждое: последний обмен дословно при любой длине, старшее за окном
    ([{"role": "user", "content": "слово " * 300}] * 3, 1, 2, True),
    ([{"role": "user", "content": "ага, понял"}] * 14, 0, 14, False),
    # короткие упираются в потолок окна по счёту
    ([{"role": "user", "content": "ага, понял"}] * (compression.COMPRESS_KEEP + 4), 4, compression.COMPRESS_KEEP,
     True),
], ids=["three-long", "fourteen-short", "capped-by-count"])
def test_raw_window_and_threshold_in_tokens(msgs, older, window, needed):
    got_older, got_window = compression.split_raw_window(msgs)
    assert (len(got_older), len(got_window)) == (older, window)
    assert compression.needs_compression(msgs) is needed


def test_summaries_are_not_counted_in_the_raw_window():
    long_summaries = [{"role": "summary", "content": "слово " * 300}] * 10
    assert not compression.needs_compression(long_summaries + [{"role": "user", "content": "ага"}] * 4)
    msgs = [{"role": "summary", "content": "x" * 3300}] + [{"role": "user", "content": "ага"}] * 3
    assert compression.pending_tail(msgs) == (3, 3)
    assert not compression.above_ceiling(msgs)


@pytest.mark.parametrize("text, trivial", [
    ("SKIP", True), (" SKIP ", True), ("skip", True), ("SKIP - ничего нового", True),
    ("", True), ("   ", True), ("\n\n", True),
    ("ВАЖНО: Ирина не пьёт кофе после 18. Сегодня говорили о работе.", False),
    ("Антон попросил включить музыку.", False),
    ("НОВОЕ: договорились созвониться в пятницу.", False),
])
def test_trivial_summary_marker(text, trivial):
    assert is_trivial_summary(text) is trivial


async def test_trivial_chunk_is_dropped_without_summary(mongo_db, deps):
    _insert(mongo_db, 24)
    await compression.compress_session_history("s1", LLM, summarizer=_summarizer("SKIP"), **deps)
    assert mongo_db.sessions.count_documents({"is_summary": True}) == 0
    assert mongo_db.sessions.count_documents({}) == 20       # кусок ушёл из истории
    assert mongo_db.sessions_archive.count_documents({}) == 4   # оригиналы в архиве


# ─── сбой и одно сжатие на сессию ─────────────────────────────────────────────

async def test_failed_summary_leaves_history_and_frees_session(mongo_db, deps, events):
    _insert(mongo_db, 24)

    # Суммаризатор упал: история не помечена сжатой, следующий вызов сжимает
    with pytest.raises(RuntimeError):
        await compression.compress_session_history("s1", LLM, summarizer=_summarizer(RuntimeError("LLM down")),
                                                   **deps)
    assert mongo_db.sessions.count_documents({}) == 24

    # Явное сжатие упало: сессия свободна
    with pytest.raises(RuntimeError):
        await compression.compress_session_exclusive("s1", LLM, summarizer=_summarizer(RuntimeError("down")),
                                                     **deps)
    assert not compression.in_progress("s1")

    # Фоновое упало: событие с первой строкой ошибки, сессия свободна, история цела
    async def _llm():
        return LLM
    assert compression.schedule_background_compression(
        "s1", _llm, summarizer=_summarizer(RuntimeError("LLM down\nTraceback lines")), **deps)
    await _task("s1")
    assert events[-1] == ("compression_error", {"session": "s1", "error": "LLM down"})
    assert not compression.in_progress("s1") and mongo_db.sessions.count_documents({}) == 24

    # Нет LLM для сводки - тоже событие ошибки
    async def _none():
        return None
    # SAM-226: сразу после сбоя сессия на паузе, повтор - когда пауза вышла (здесь 0)
    assert not compression.schedule_background_compression("s1", _none, summarizer=_summarizer(), **deps)
    assert compression.schedule_background_compression("s1", _none, summarizer=_summarizer(), retry_after_s=0,
                                                       **deps)
    await _task("s1")
    assert events[-1] == ("compression_error", {"session": "s1", "error": "нет доступного поставщика LLM для сводки"})

    # После сбоев сжатие проходит
    result = await compression.compress_session_history("s1", LLM, summarizer=_summarizer("Сжато"), **deps)
    assert "уже сжат" not in result
    assert mongo_db.sessions.count_documents({"is_summary": True}) == 1


async def test_compression_runs_once_per_session(mongo_db, deps):
    _insert(mongo_db, 24)
    await compression.compress_session_history("s1", LLM, summarizer=_summarizer("S"), **deps)
    again = await compression.compress_session_history("s1", LLM, summarizer=_summarizer("S"), **deps)
    assert "уже сжат" in again or "стабильно" in again
    assert mongo_db.sessions.count_documents({"is_summary": True}) == 1

    # Явное сжатие не идёт поверх фонового той же сессии
    calls = []
    compression._compress_in_progress.add("s1")
    result = await compression.compress_session_exclusive("s1", LLM, summarizer=_summarizer("S", calls),
                                                          force=True, **deps)
    assert "уже идёт" in result and calls == []


# ─── дедуп и куски ────────────────────────────────────────────────────────────

LONG = "расскажи последние новости про искусственный интеллект сегодня"


@pytest.mark.parametrize("msgs, removed_ids", [
    ([_msg("1", LONG), _msg("2", LONG + " пожалуйста")], {"1"}),    # старший уходит, свежий остаётся
    ([_msg("1", "спасибо"), _msg("2", "спасибо")], set()),           # короткие не трогаются
    ([_msg("1", "длинное сообщение пользователя"),
      _msg("2", "длинный ответ ассистента", role="assistant")], set()),   # нет пары одной роли
    ([], set()),
    ([_msg("1", "x")], set()),
], ids=["near-duplicates", "short", "no-pair", "empty", "single"])
def test_near_duplicate_messages_are_removed_and_archived(mongo_db, msgs, removed_ids):
    if msgs:
        mongo_db.sessions.insert_many([dict(m) for m in msgs])
    archive = ArchiveRepo(mongo_db)
    removed = compression.dedupe_recent_messages("s1", msgs, SessionRepo(mongo_db), archive)
    assert removed == len(removed_ids)
    assert {d["_id"] for d in mongo_db.sessions.find({})} == {m["_id"] for m in msgs} - removed_ids
    assert {d["_orig_id"] for d in mongo_db.sessions_archive.find({})} == removed_ids


def _tail(n, tokens_each):
    return [{"role": "user", "content": str(i), "t": tokens_each} for i in range(n)]


@pytest.mark.parametrize("n, tokens_each, min_msgs, sizes", [
    # Хвост после рестарта 10 окт: 312 сообщений по ~45 токенов; по 4 сообщения было 78 вызовов
    (312, 45, compression.COMPRESS_CHUNK, [33] * 9 + [15]),
    (4, 10, compression.COMPRESS_CHUNK, [4]),          # малый хвост - один кусок, как прежде
    (8, 2000, 4, [4, 4]),                              # длинные: кусок не меньше минимума
    (9, 400, 4, [4, 5]),                               # короткий остаток присоединяется к прошлому куску
], ids=["large-tail", "small-tail", "long-keep-minimum", "short-remainder"])
def test_long_tail_is_chunked_by_tokens(n, tokens_each, min_msgs, sizes):
    msgs = _tail(n, tokens_each)
    chunks = compression.chunk_by_tokens(msgs, max_tokens=1500, min_msgs=min_msgs, count=lambda m: m["t"])
    assert [len(c) for c in chunks] == sizes
    assert [m["content"] for c in chunks for m in c] == [m["content"] for m in msgs]   # порядок сохранён


@pytest.mark.parametrize("role", ["summary", "tool_call", "tool"])
def test_dedup_leaves_summaries_and_tool_calls(mongo_db, role):
    """Сводки и вызовы инструментов с почти одинаковым текстом - не повтор реплики: дедуп их не удаляет."""
    msgs = [_msg("1", LONG, role=role), _msg("2", LONG + " пожалуйста", role=role)]
    mongo_db.sessions.insert_many([dict(m) for m in msgs])
    removed = compression.dedupe_recent_messages("s1", msgs, SessionRepo(mongo_db), ArchiveRepo(mongo_db))
    assert removed == 0
    assert mongo_db.sessions.count_documents({}) == 2 and mongo_db.sessions_archive.count_documents({}) == 0


# ─── проход: снимок, архив до удаления, слияние сводок ────────────────────────

async def test_snapshot_is_archived_as_compressed_before_anything_is_deleted(mongo_db, deps):
    """Шаг 4 прохода: на момент первого вызова сводки весь снимок уже в архиве с причиной compressed,
    а в истории ещё ничего не удалено."""
    _insert(mongo_db, 24)
    seen = []

    async def summarizer(messages, llm_config, prior_context):
        archived = list(mongo_db.sessions_archive.find({}))
        seen.append(({d["_orig_id"] for d in archived}, {d["archive_reason"] for d in archived},
                     mongo_db.sessions.count_documents({})))
        return "сводка"
    await compression.compress_session_history("s1", LLM, summarizer=summarizer, **deps)

    assert seen == [({"0", "1", "2", "3"}, {"compressed"}, 24)]


async def test_records_arriving_during_a_pass_wait_for_the_next(mongo_db, deps):
    """Шаг 2: реплика, записанная пока идёт вызов сводки, не входит в снимок - не удаляется и не архивируется."""
    _insert(mongo_db, 24)

    async def summarizer(messages, llm_config, prior_context):
        mongo_db.sessions.insert_one(_msg("fresh", "свежая реплика во время сжатия", minute=100))
        return "сводка"
    result = await compression.compress_session_history("s1", LLM, summarizer=summarizer, **deps)

    assert "Сжато: 4 сообщений" in result
    fresh = mongo_db.sessions.find_one({"_id": "fresh"})
    assert fresh["content"] == "свежая реплика во время сжатия" and not fresh.get("is_summary")
    assert mongo_db.sessions_archive.count_documents({"_orig_id": "fresh"}) == 0
    assert mongo_db.sessions.count_documents({"is_summary": {"$ne": True}}) == 21


def _summaries(db, n, session="s1"):
    """n видимых сводок уровня 1 от прошлых проходов, старше сырых сообщений; отрезок у каждой свой."""
    for i in range(n):
        db.sessions.insert_one(_msg(f"sum-{i}", f"s{i}", role="summary", minute=-100 + i, session=session,
                                    is_summary=True, compression_level=1, time_from=f"10:{i:02d}:00",
                                    time_to=f"10:{i:02d}:59", time_range=f"10:{i:02d} – 10:{i:02d}"))


def _merger(calls=None, skip=()):
    """Склейщик-фальшивка: сводка пары - склейка текстов через «+»; для пар из skip - SKIP (повтор фона)."""
    async def _merge(pair, llm_config, background):
        if calls is not None:
            calls.append((tuple(pair), background))
        joined = "+".join(pair)
        return "SKIP" if joined in skip else joined
    return _merge


def _context(db, session="s1"):
    return [d["content"] for d in SessionRepo(db).active_summaries(session)]


@pytest.mark.parametrize("count, context, merges", [
    pytest.param(4, ["s0", "s1", "s2", "s3"], 0, id="at-ceiling-untouched"),
    pytest.param(5, ["s0+s1", "s2+s3", "s4"], 2, id="five-pairs-from-oldest"),
    pytest.param(6, ["s0+s1", "s2+s3", "s4+s5"], 3, id="six-to-three"),
    pytest.param(9, ["s0+s1+s2+s3", "s4+s5+s6+s7", "s8"], 6, id="cascade-two-passes"),
])
async def test_cascade_keeps_context_under_ceiling(mongo_db, deps, count, context, merges):
    """SAM-224, дизайн «Каскадное попарное слияние»: больше потолка (4) - соседние пары от старых, проход за проходом;
    старшая сводка накапливает раннюю беседу, слитые скрыты из контекста и остаются в истории."""
    _summaries(mongo_db, count)
    calls = []
    assert await compression.fold_summaries("s1", LLM, session_repo=deps["session_repo"], merger=_merger(calls),
                                            is_trivial_summary=is_trivial_summary, ceiling=4) == merges
    assert _context(mongo_db) == context and len(calls) == merges
    hidden = mongo_db.sessions.count_documents({"session_id": "s1", "is_summary": True, "ignore": {"$exists": True}})
    assert hidden == 2 * merges
    assert mongo_db.sessions.count_documents({"session_id": "s1"}) == count + merges


async def test_merged_summary_takes_the_pair_place_level_and_range(mongo_db, deps):
    _summaries(mongo_db, 5)
    await compression.fold_summaries("s1", LLM, session_repo=deps["session_repo"], merger=_merger(),
                                     is_trivial_summary=is_trivial_summary, ceiling=4)
    first = SessionRepo(mongo_db).active_summaries("s1")[0]
    assert (first["content"], first["compression_level"], first["time_range"]) == ("s0+s1", 2, "10:00 – 10:01")
    assert first["timestamp"] == T0 + timedelta(minutes=-100)
    assert mongo_db.sessions.find_one({"_id": "sum-0"})["ignore"] == f"слита в сводку {first['msg_id']}"


async def test_merge_gets_preceding_summaries_as_background(mongo_db, deps):
    """Дизайн «Слияние пары»: вызов получает предшествующие сводки фоном; первая пара - без фона."""
    _summaries(mongo_db, 6)
    calls = []
    await compression.fold_summaries("s1", LLM, session_repo=deps["session_repo"], merger=_merger(calls),
                                     is_trivial_summary=is_trivial_summary, ceiling=4, background=3)
    assert [bg for _, bg in calls] == ["", "s0+s1", "s0+s1" + compression.MERGE_SEP + "s2+s3"]


async def test_pair_repeating_the_background_is_absorbed_by_previous(mongo_db, deps):
    """Повторов нет: пара, которую модель считает повтором фона (SKIP), поглощается предыдущей сводкой."""
    _summaries(mongo_db, 5)
    await compression.fold_summaries("s1", LLM, session_repo=deps["session_repo"], merger=_merger(skip={"s2+s3"}),
                                     is_trivial_summary=is_trivial_summary, ceiling=4)
    active = SessionRepo(mongo_db).active_summaries("s1")
    assert [d["content"] for d in active] == ["s0+s1", "s4"]
    assert active[0]["time_range"] == "10:00 – 10:03"
    assert mongo_db.sessions.find_one({"_id": "sum-2"})["ignore"] == f"повторяет сводку {active[0]['msg_id']}"


async def test_oldest_pair_repeat_is_absorbed_by_its_first(mongo_db, deps):
    """У самой старой пары фона нет: SKIP - вторую поглощает первая, каскад не встаёт."""
    _summaries(mongo_db, 5)
    await compression.fold_summaries("s1", LLM, session_repo=deps["session_repo"], merger=_merger(skip={"s0+s1"}),
                                     is_trivial_summary=is_trivial_summary, ceiling=4)
    active = SessionRepo(mongo_db).active_summaries("s1")
    assert [d["content"] for d in active] == ["s0", "s2+s3", "s4"] and active[0]["time_range"] == "10:00 – 10:01"


async def test_failed_merge_leaves_the_pair_in_context_and_reports_done_merges(mongo_db, deps, events):
    """Сбой слияния на середине - сделанные слияния в базе и в мониторе, пара сбоя остаётся видимой."""
    async def _second_fails(pair, llm_config, background):
        if pair[0] == "s2":
            raise RuntimeError("LLM down")
        return "+".join(pair)
    _summaries(mongo_db, 5)
    with pytest.raises(RuntimeError):
        await compression.fold_summaries("s1", LLM, session_repo=deps["session_repo"], merger=_second_fails,
                                         is_trivial_summary=is_trivial_summary, ceiling=4,
                                         event_emit=deps["event_emit"])
    assert _context(mongo_db) == ["s0+s1", "s2", "s3", "s4"]
    [compressed] = [d for kind, d in events if kind == "context_compressed"]
    assert [(x["agg_id"], x["del_ids"]) for x in compressed["summaries"]] == [("m-sum-0", ["m-sum-1"])]


async def test_monitor_rows_follow_the_cascade(mongo_db, deps, events):
    """Монитор (data-mid строк ленты): после двух проходов сводка стоит в строке самой ранней исходной, строки
    остальных убираются, прогресс знает число вызовов заранее."""
    _summaries(mongo_db, 9)
    await compression.fold_summaries("s1", LLM, session_repo=deps["session_repo"], merger=_merger(),
                                     is_trivial_summary=is_trivial_summary, ceiling=4, event_emit=deps["event_emit"])
    [compressed] = [d for kind, d in events if kind == "context_compressed"]
    assert [(x["agg_id"], sorted(x["del_ids"]), x["content"]) for x in compressed["summaries"]] == [
        ("m-sum-0", ["m-sum-1", "m-sum-2", "m-sum-3"], "s0+s1+s2+s3"),
        ("m-sum-4", ["m-sum-5", "m-sum-6", "m-sum-7"], "s4+s5+s6+s7"),
    ]
    progress = [d for kind, d in events if kind == compression.PROGRESS_EVENT]
    assert [p["call"] for p in progress] == [1, 2, 3, 4, 5, 6] and {p["calls"] for p in progress} == {6}


async def test_pass_folds_summaries_even_with_nothing_to_compress(mongo_db, deps, events):
    """Каскад идёт и без новых сообщений: накопленные сводки сворачиваются первым же проходом; монитор видит
    слияние событием context_compressed."""
    _summaries(mongo_db, compression.SUMMARY_CEILING + 1)
    result = await compression.compress_session_history("s1", LLM, summarizer=_summarizer(), merger=_merger(),
                                                        **deps)
    assert "Каскад: 2 слияний" in result and len(_context(mongo_db)) == 3
    compressed = [d for kind, d in events if kind == "context_compressed"][-1]
    assert [(s["agg_id"], s["del_ids"]) for s in compressed["summaries"]] == [("m-sum-0", ["m-sum-1"]),
                                                                              ("m-sum-2", ["m-sum-3"])]


def test_merge_prompt_forbids_retelling_the_background():
    """Фон без пересказа: промпт слияния даёт фон для линии беседы и запрещает его пересказывать, повтор - SKIP."""
    from gateway.prompts import SUMMARIZE_MERGE_PROMPT
    assert "НЕ пересказывай фон" in SUMMARIZE_MERGE_PROMPT and "SKIP" in SUMMARIZE_MERGE_PROMPT
    assert {"{background}", "{first}", "{second}"} <= set(__import__("re").findall(r"\{\w+\}", SUMMARIZE_MERGE_PROMPT))


async def test_empty_merge_is_a_failure(monkeypatch, fake_llm_response):
    from gateway import agent

    async def fake_call(llm_config, **kwargs):
        return fake_llm_response("")
    monkeypatch.setattr(agent.llm_client, "make_llm_call", fake_call)
    with pytest.raises(RuntimeError, match="пустое слияние"):
        await agent._merge_summaries(["a", "b"], {"model": "m"}, "")


async def test_progress_event_before_each_summary_call(mongo_db, deps, events):
    """SAM-97: перед каждым вызовом сводки - compression_progress с номером вызова и сколько сообщений позади."""
    calls = []
    # 40 сообщений по ~125 токенов, свои слова у каждого (иначе дедуп): окно держит 4, за окном три куска
    _insert(mongo_db, 40, text=lambda i: " ".join(f"w{i}x{j}" for j in range(60)))
    await compression.compress_session_history("s1", LLM, summarizer=_summarizer("сводка", calls), **deps)

    progress = [d for kind, d in events if kind == compression.PROGRESS_EVENT]
    assert len(progress) == len(calls)
    chunk_steps = [p for p in progress if p["stage"] == compression.STAGE_CHUNK]
    assert len(chunk_steps) == 3          # по 4 сообщения было 9 вызовов
    assert [p["call"] for p in progress] == list(range(1, len(calls) + 1))
    assert all(p["session"] == "s1" for p in progress)
    assert chunk_steps[0]["done"] < chunk_steps[-1]["done"] < chunk_steps[-1]["total"]
    assert events[-1][0] == "context_compressed"


async def test_loaded_skill_is_marked_not_summarized(mongo_db, deps):
    # Тело прочитанного скилла не пересказывается: в сводку идёт метка с именем и первой строкой
    mongo_db.sessions.insert_one(_msg("skill", "Скилл radar: где кто\nшаг 1: radar_where(person)\nшаг 2: ...",
                                      role="tool", minute=-1, tool_name="load_skill", linger=True))
    for i in range(4):   # свежие мысли занимают окно мыслей: скилл уходит за окно и сжимается
        mongo_db.sessions.insert_one(_msg(f"fresh-{i}", "<think>" + f"думаю о своём {i} " * 30 + "</think>",
                                          role="assistant", minute=40 + i))
    _insert(mongo_db, 24)
    calls, thoughts = [], []
    await compression.compress_session_history("s1", LLM, summarizer=_summarizer("S", calls),
                                               thought_summarizer=_summarizer("Решила: радар", thoughts), **deps)
    talk = [m["content"] for chunk in calls for m in chunk]
    sent = [m["content"] for chunk in thoughts for m in chunk]
    # SAM-227: прочитанный скилл - канал мыслей, в сводку разговора не идёт
    assert "[прочитан load_skill: Скилл radar: где кто]" in sent and not any("прочитан" in c for c in talk)
    assert not any("radar_where" in c for c in sent)
    thought = mongo_db.sessions.find_one({"summary_channel": "thoughts"})
    assert thought["content"] == "Решила: радар" and thought["is_summary"]


def _thought(_id, minute, text="<think>смотрю</think>"):
    return _msg(_id, text, role="assistant", minute=minute)


def test_channel_of_record():
    """SAM-227: разговор - фраза человека и ответ SAM с речью; мысли - одни неозвученные блоки, вызовы, итоги."""
    assert compression.channel_of({"role": "user", "content": "привет"}) == compression.TALK
    assert compression.channel_of({"role": "assistant", "content": "Привет. <note>x</note>"}) == compression.TALK
    for m in ({"role": "assistant", "content": "<think>жду</think>"}, {"role": "tool_call", "content": ""},
              {"role": "tool", "content": "тело", "linger": True}):
        assert compression.channel_of(m) == "thoughts"


def test_thought_flood_does_not_push_talk_out_of_the_window():
    """SAM-227: долгий поток мыслей после реплик - реплики людей остаются в окне дословно, старые мысли уходят."""
    talk = [_msg(f"t{i}", f"реплика {i}", minute=i) for i in range(6)]
    flood = [_thought(f"h{i}", 10 + i, "<think>" + "жду реплику " * 10 + "</think>") for i in range(30)]
    older, window = compression.split_raw_window(talk + flood)
    assert all(m in window for m in talk)
    assert len([m for m in window if m in flood]) < len(flood) and flood[-1] in window and flood[0] in older


async def test_thought_summaries_cascade_apart_from_talk(mongo_db, deps):
    """SAM-227: сводки мыслей сливаются своим каскадом и своим потолком; сводки разговора они не трогают."""
    _summaries(mongo_db, 4)                                  # разговор на потолке - не сливается
    for i in range(3):                                       # мыслей больше потолка мыслей (2)
        mongo_db.sessions.insert_one(_msg(f"th-{i}", f"мысль {i}", role="summary", minute=-50 + i, is_summary=True,
                                          compression_level=1, summary_channel="thoughts",
                                          time_range=f"11:0{i} – 11:0{i}"))
    await compression.compress_session_history("s1", LLM, summarizer=_summarizer(), merger=_merger(), **deps)
    repo = SessionRepo(mongo_db)
    assert [d["content"] for d in repo.active_summaries("s1", "talk")] == ["s0", "s1", "s2", "s3"]
    thoughts = repo.active_summaries("s1", "thoughts")
    assert [d["content"] for d in thoughts] == ["мысль 0+мысль 1", "мысль 2"]
    assert thoughts[0]["summary_channel"] == "thoughts"


# ─── фаза 4: цепочка сводок, кэш, место сводки ────────────────────────────────

async def test_next_chunks_get_the_three_previous_summaries_as_prior(mongo_db, deps):
    """Шаг 5: первый кусок без прошлого (промпт BASE), каждый следующий - с тремя последними сводками (DIFF)."""
    # 20 сообщений по ~400 токенов, свои слова у каждого: пять кусков по 4
    _insert(mongo_db, 20, text=lambda i: " ".join(f"w{i}x{j}" for j in range(190)))
    priors = []
    n = iter(range(100))

    async def summarizer(messages, llm_config, prior_context):
        priors.append(prior_context)
        return f"сводка {next(n)}"
    await compression.compress_session_history("s1", LLM, summarizer=summarizer, force=True, **deps)

    assert priors == ["", "сводка 0", "сводка 0\n---\nсводка 1", "сводка 0\n---\nсводка 1\n---\nсводка 2",
                      "сводка 1\n---\nсводка 2\n---\nсводка 3"]


@pytest.mark.parametrize("prior, head", [
    ("", "SUMMARIZE_BASE_PROMPT"), ("прошлая сводка", "SUMMARIZE_DIFF_PROMPT"),
], ids=["first-base", "next-diff"])
async def test_summary_prompt_is_diff_when_there_is_a_prior(monkeypatch, fake_llm_response, prior, head):
    """Сводка шлюза: с прошлыми сводками - промпт DIFF и прошлое в нём, без них - BASE. Подменён вызов LLM."""
    from gateway import agent, prompts
    sent = []

    async def fake_call(llm_config, **kwargs):
        sent.append(kwargs["messages"][0]["content"])
        return fake_llm_response("ок")
    monkeypatch.setattr(agent.llm_client, "make_llm_call", fake_call)
    await agent._summarize_chunk([{"role": "user", "content": "кусок"}], {"model": "m"}, prior)

    template = getattr(prompts, head)
    assert sent[0].startswith(template.split("{")[0])
    assert prior in sent[0] and "user: кусок" in sent[0]


async def test_pass_drops_the_cached_overall_summary_of_its_session(mongo_db, deps):
    """Шаг 7: после прохода кэш сводной сводки (_sessions_meta) своей сессии сброшен, чужой - на месте."""
    deps["meta_repo"].upsert_summary_cache("s1", "старая сводная", 3)
    deps["meta_repo"].upsert_summary_cache("s2", "чужая сводная", 3)
    _insert(mongo_db, 24)
    await compression.compress_session_history("s1", LLM, summarizer=_summarizer("новая"), **deps)

    assert deps["meta_repo"].get("s1") is None
    assert deps["meta_repo"].get("s2")["summary"] == "чужая сводная"


async def test_summary_takes_the_place_of_its_chunk_with_time_range(mongo_db, deps):
    """Шаг 5: сводка встаёт на место первого сообщения куска (его _id и время) с отрезком времени куска; модель
    видит её с этим отрезком, а сырые реплики окна - после неё по порядку."""
    from gateway import runner
    from tests.fakes_core import runner_deps
    _insert(mongo_db, 24)
    await compression.compress_session_history("s1", LLM, summarizer=_summarizer("говорили о коте"), **deps)

    history = deps["session_repo"].get_recent("s1", 50)
    assert history[0]["_id"] == "0" and history[0]["role"] == "summary"
    assert history[0]["timestamp"] == T0 and history[0]["time_range"] == "12:00 – 12:03"
    assert [m["content"] for m in history[1:]] == [f"m{i}" for i in range(4, 24)]
    msgs = runner.build_messages(runner_deps(), "s1", "привет", history, "", {"prompt_format": "standard"})
    assert "[12:00 – 12:03] говорили о коте" in msgs[0]["content"]
    assert [m["content"] for m in msgs[1:-1]] == [f"m{i}" for i in range(4, 24)]


# ─── фаза 5: P3 ───────────────────────────────────────────────────────────────

def test_without_scikit_learn_dedup_is_skipped(mongo_db, monkeypatch):
    """compression 8: нет scikit-learn - дедуп пропускается, почти-дубли остаются, проход не падает."""
    monkeypatch.setitem(sys.modules, "sklearn.feature_extraction.text", None)   # import даёт ImportError
    msgs = [_msg("1", LONG), _msg("2", LONG + " пожалуйста")]
    mongo_db.sessions.insert_many([dict(m) for m in msgs])
    assert compression.dedupe_recent_messages("s1", msgs, SessionRepo(mongo_db), ArchiveRepo(mongo_db)) == 0
    assert mongo_db.sessions.count_documents({}) == 2 and mongo_db.sessions_archive.count_documents({}) == 0


@pytest.mark.parametrize("lead", [main_model.LLM, main_model.CLAUDE_CODE])
async def test_summary_model_is_the_service_model_whatever_leads(monkeypatch, lead):
    """compression 19 (SAM-226): сводку пишет служебная модель роутера (локальная или основная роутера), а не ход
    основной модели SAM, при любой основной модели."""
    from gateway import agent, main
    scheduled = []
    monkeypatch.setattr(main, "main_models", main_model.MainModel(
        {main_model.LLM: main_model.llm(), main_model.CLAUDE_CODE: main_model.claude_code(lambda: True, print)},
        [lead]))

    async def service_llm():
        return {"source": "lmstudio", "model": "service"}
    monkeypatch.setattr(agent, "get_service_llm", service_llm)
    monkeypatch.setattr(agent, "_mongo_ok", True)
    monkeypatch.setattr(compression, "schedule_background_compression",
                        lambda session_id, get_llm, **kw: scheduled.append(get_llm) or True)
    assert agent.schedule_background_compression("voice")
    assert await scheduled[0]() == {"source": "lmstudio", "model": "service"}


async def test_summary_prompt_keeps_stable_facts_apart_from_episodes(monkeypatch, fake_llm_response):
    """compression 20: промпт сводки, который уходит модели, отделяет устойчивые факты (блок «ВАЖНО:» / «НОВОЕ:»,
    только сказанное человеком) от эпизодов (их можно забыть)."""
    from gateway import agent
    sent = []

    async def fake_call(llm_config, **kwargs):
        sent.append(kwargs["messages"][0]["content"])
        return fake_llm_response("ок")
    monkeypatch.setattr(agent.llm_client, "make_llm_call", fake_call)
    for prior in ("", "прошлая сводка"):
        await agent._summarize_chunk([{"role": "user", "content": "кусок"}], {"model": "m"}, prior)
    base, diff = sent
    assert "Блок «ВАЖНО:» только если во фрагменте реально есть устойчивые факты" in base
    assert "ЧТО МОЖНО ЗАБЫТЬ:\n- Эпизодика без долгосрочного значения" in base
    assert "Блок «НОВОЕ:» или «ИЗМЕНЕНИЕ:»" in diff and "ЧТО НЕ ВКЛЮЧАТЬ:" in diff and "- Эпизодика" in diff


@pytest.mark.parametrize("reply", ["", "<think>долго думала</think>", "   "], ids=["empty", "only-thinking", "blank"])
async def test_empty_summary_is_a_failure_not_a_raw_chunk(monkeypatch, fake_llm_response, reply):
    """SAM-226: пустая сводка - сбой сжатия, сырой кусок вместо сводки не возвращается (41 из 43 сводок voice 15 окт)."""
    from gateway import agent

    async def fake_call(llm_config, **kwargs):
        return fake_llm_response(reply)
    monkeypatch.setattr(agent.llm_client, "make_llm_call", fake_call)
    with pytest.raises(RuntimeError, match="пустая сводка"):
        await agent._summarize_chunk([{"role": "user", "content": "кусок"}], {"model": "m", "source": "zai"}, "")


async def test_chunk_of_only_tool_calls_gets_no_summary(mongo_db, deps):
    """SAM-219: служебные вызовы без реплик (ночные sync_skills) сводки «разговора не было» не дают."""
    for i in range(24):
        mongo_db.sessions.insert_one(_msg(str(i), "", role="tool_call", minute=i, tool_name="sync_skills"))
    calls = []
    await compression.compress_session_history("s1", LLM, summarizer=_summarizer("Разговора не было", calls),
                                               force=True, **deps)
    assert calls == [] and mongo_db.sessions.count_documents({"is_summary": True}) == 0
    assert mongo_db.sessions_archive.count_documents({}) == 24     # оригиналы в архиве


def test_talk_is_a_human_line_or_sam_text_not_a_call():
    assert compression.has_talk([_msg("1", "", role="tool_call"), _msg("2", "привет")])
    assert not compression.has_talk([_msg("1", "", role="tool_call"), _msg("2", "[результат]", role="tool"),
                                     _msg("3", "", role="assistant", tool_calls=[{"function": {"name": "x"}}])])


def test_live_skill_body_stays_in_the_window_beyond_the_thought_budget():
    """Ревью SAM-227: тело скилла с linger держится в окне, пока ход его читает (runner.lingering), даже когда свежие
    мысли заняли бюджет окна мыслей; после LINGER_TURNS реплик человека уходит за окно, как обычная мысль."""
    skill = _msg("skill", "тело скилла " * 200, role="tool", minute=0, tool_name="load_skill", linger=True)
    fresh = [_thought(f"h{i}", 50 + i, "<think>" + "думаю " * 60 + "</think>") for i in range(3)]
    talk = [_msg(f"u{i}", f"реплика {i}", minute=1 + i) for i in range(2)]
    _, window = compression.split_raw_window([skill, *talk, *fresh])
    assert skill in window
    many = [_msg(f"u{i}", f"реплика {i}", minute=1 + i) for i in range(compression.LINGER_TURNS)]
    older, _ = compression.split_raw_window([skill, *many, *fresh])
    assert skill in older


def test_context_summaries_are_taken_per_channel(mongo_db):
    """Ревью SAM-227: свежие сводки мыслей не вытесняют сводки разговора из контекста хода."""
    _summaries(mongo_db, 2)
    for i in range(5):
        mongo_db.sessions.insert_one(_msg(f"th-{i}", f"мысль {i}", role="summary", minute=-10 + i, is_summary=True,
                                          summary_channel="thoughts"))
    got = SessionRepo(mongo_db).get_recent("s1", 5, summaries=4, thought_summaries=2)
    assert [d["content"] for d in got] == ["s0", "s1", "мысль 3", "мысль 4"]
