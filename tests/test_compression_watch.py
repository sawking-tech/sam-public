"""SAM-97: сжатие ставится при записи реплики в историю при любой основной модели, его состояние видно в /health."""
import asyncio
from datetime import datetime, timedelta

import pytest

from gateway import compression
from gateway.compression_watch import CompressionWatch
from gateway.event_bus import EventBus
from gateway.repos.archive_repo import ArchiveRepo
from gateway.repos.session_repo import SessionRepo
from tests.fakes_core import app_client  # noqa: F401  фикстура


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _watch(db, scheduled, busy=(), **kw):
    """Наблюдатель на mongomock; постановка записывает (сессия, сразу ли) и не запускает сжатие."""
    def _schedule(sid, urgent):
        scheduled.append((sid, urgent))
        return True
    return CompressionWatch(SessionRepo(db).get_all_for_compression, ArchiveRepo(db).last_compressed_at,
                            _schedule, in_progress=lambda sid: sid in busy, **kw)


def _bus(db, watch):
    bus = EventBus()
    bus.set_db(db)
    bus.set_history_listener(watch.on_history_write)
    return bus


def _talk(bus, session, pairs, text="коротко"):
    """Разговор без хода модели шлюза: фраза человека и ответ основной модели вне шлюза (POST .../reply)."""
    for i in range(pairs):
        bus.emit("user_message", {"content": f"{text} {i}", "session": session})
        bus.emit("assistant_reply", {"content": f"<voice>{text} {i}</voice>", "session": session,
                                     "source": "observer"})


# ─── постановка при записи в историю ──────────────────────────────────────────

def _talk_then(pairs, text="коротко", session="voice"):
    return lambda bus: _talk(bus, session, pairs, text)


def _worker_and_system(bus):
    for i in range(40):
        bus.emit("tool_call", {"name": "mcp_call", "args": {}, "session": "task:abcd1234"})
        bus.emit("user_message", {"content": f"[TASK_DONE] {i}", "session": "voice", "system": True})


@pytest.mark.parametrize("busy, write, expected", [
    # claude-code: реплики пишутся шиной (чат монитора, ответ ведущего), хода шлюза нет; 20 сообщений - всё в окне
    ((), _talk_then(10), []),
    # 24: за окном чанк - в паузе
    ((), _talk_then(12), [("voice", False)]),
    # уже ждёт паузы - вторая постановка не нужна
    ({"voice"}, _talk_then(20), []),
    # воркер и системные записи сжатие не ставят
    ((), _worker_and_system, []),
    # потолок по несжатому в базе, а не по истории хода: 28 несжатых - сразу
    ((), _talk_then(14), [("voice", False)] * 4 + [("voice", True)]),
    # потолок по токенам: 6 длинных сообщений по ~730 токенов
    ((), _talk_then(3, text="слово " * 400), "urgent-last"),
], ids=["inside-window", "chunk-beyond-window", "already-waiting", "worker-and-system", "ceiling-by-count",
        "ceiling-by-tokens"])
async def test_history_write_schedules_compression(mongo_db, busy, write, expected):
    scheduled = []
    write(_bus(mongo_db, _watch(mongo_db, scheduled, busy=busy)))
    if expected == "urgent-last":
        assert scheduled and scheduled[-1] == ("voice", True)
    else:
        assert scheduled == expected


# ─── состояние для /health ────────────────────────────────────────────────────

class _GateClock(_Clock):
    async def sleep(self, seconds):
        self.t += seconds
        await asyncio.sleep(0)


async def test_compression_status_is_shown_in_health(mongo_db, app_client, monkeypatch):
    # Ожидание, ошибка и последнее сжатие по ручным часам наблюдателя
    clock, now = _Clock(), datetime(2026, 10, 10, 1, 0)
    watch = _watch(mongo_db, [], clock=clock, now=lambda: now)
    _talk(_bus(mongo_db, watch), "voice", 15)
    ArchiveRepo(mongo_db).col.insert_one({"session_id": "voice", "archive_reason": "compressed",
                                          "archived_at": now - timedelta(minutes=37)})
    watch.waiting("voice")
    clock.t += 40
    voice = watch.status()["sessions"]["voice"]
    assert (voice["pending_msgs"], voice["above_ceiling"], voice["waiting_s"]) == (30, True, 40)
    assert voice["last_ok"] == "2026-10-10T00:23:00Z" and voice["last_ok_ago_s"] == 37 * 60
    watch.started("voice")
    watch.finished("voice", "LLM down")
    voice = watch.status()["sessions"]["voice"]
    assert (voice["waiting_s"], voice["running_s"], voice["last_error"]) == (None, None, "LLM down")
    watch.finished("voice", None)
    assert watch.status()["sessions"]["voice"]["last_error"] is None

    # Через настоящий планировщик: ворота закрыты - «ждёт», после предела - сжато и не ждёт
    from gateway.idle_gate import IdleGate
    gate_clock = _GateClock()
    gate = IdleGate(idle_s=60, clock=gate_clock, poll_s=1, sleep=gate_clock.sleep, timer=gate_clock)

    async def _llm():
        return {"model": "m"}

    async def _compress(*a, **kw):
        return "ok"

    def _schedule(sid, urgent):
        return compression.schedule_background_compression(
            sid, _llm, session_repo=None, archive_repo=None, meta_repo=None, summarizer=None,
            is_trivial_summary=lambda s: False, gate=gate, urgent=urgent, idle_max_s=120,
            status=real, compress=_compress)
    real = CompressionWatch(SessionRepo(mongo_db).get_all_for_compression,
                            ArchiveRepo(mongo_db).last_compressed_at, _schedule)
    gate.touch()
    _talk(_bus(mongo_db, real), "s-real", 12)
    await asyncio.sleep(0)
    assert real.status()["sessions"]["s-real"]["waiting_s"] is not None
    await next(t for t in asyncio.all_tasks() if t.get_name() == "compress:s-real")
    assert real.status()["sessions"]["s-real"]["waiting_s"] is None
    assert not compression.in_progress("s-real")

    # Раздел compression в /health
    from gateway import main

    async def _best_llm(*a, **kw):
        return {"source": "test", "model": "m"}
    client, _ = app_client
    monkeypatch.setattr(main, "get_best_llm", _best_llm)
    monkeypatch.setattr(main, "_health_probes", lambda: {})
    monkeypatch.setattr(main, "compression_watch", watch)
    body = client.get("/health").json()
    assert body["compression"]["ceiling_msgs"] == compression.CEILING_MSGS
    assert body["compression"]["sessions"]["voice"]["pending_msgs"] == 30


# ─── лавина реплик ────────────────────────────────────────────────────────────

async def _avalanche(mongo_db, writes: int, every_s: float, **watch_kw) -> list[int]:
    """Поток записей через реальную постановку и сжатие без ворот (худший случай: каждый проход сразу);
    проход успевает закончиться до следующей записи. Возвращает размеры кусков в вызовах сводки."""
    from gateway.repos.meta_repo import SessionMetaRepo
    clock, calls = _Clock(), []
    repo, archive = SessionRepo(mongo_db), ArchiveRepo(mongo_db)
    archive.ensure_indexes()

    async def _llm():
        return {"model": "m"}

    async def _summ(messages, llm_config, prior):
        calls.append(len(messages))
        return f"сводка {len(calls)}"

    def _schedule(sid, urgent):
        return compression.schedule_background_compression(
            sid, _llm, session_repo=repo, archive_repo=archive, meta_repo=SessionMetaRepo(mongo_db),
            summarizer=_summ, is_trivial_summary=lambda s: False, urgent=urgent, status=watch)
    watch = CompressionWatch(repo.get_all_for_compression, archive.last_compressed_at, _schedule,
                             clock=clock, **watch_kw)
    bus = _bus(mongo_db, watch)
    try:
        for i in range(writes):
            words = " ".join(f"w{i}x{j}" for j in range(50))   # ~100 токенов, свои слова: дедуп не трогает
            bus.emit("user_message" if i % 2 else "assistant_reply", {"content": words, "session": "voice"})
            while compression.in_progress("voice"):
                await asyncio.sleep(0)
            clock.t += every_s
    finally:
        compression._compress_in_progress.clear()
        compression._failed_at.clear()
        compression._compress_hashes.clear()
    return calls


@pytest.mark.parametrize("watch_kw, few", [
    # 200 записей по ~100 токенов каждые 3 с (~20 тыс. токенов): проход берёт за окном кусок, 19 вызовов
    ({}, True),
    # без паузы между проходами (как до SAM-97): проход на каждые 2-3 записи, 94 вызова
    ({"min_gap_s": 0}, False),
], ids=["with-gap", "without-gap"])
async def test_avalanche_of_writes_compresses_in_few_passes(mongo_db, watch_kw, few):
    calls = await _avalanche(mongo_db, 200, every_s=3.0, **watch_kw)
    if few:
        assert len(calls) <= 25
        pending, _ = compression.pending_tail(SessionRepo(mongo_db).get_all_for_compression("voice"))
        assert pending < compression.CEILING_MSGS
    else:
        assert len(calls) > 60
