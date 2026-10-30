"""Ночная консолидация памяти (SAM-22, ADR-015): окно, пауза, блокировка, отчёт, сбой шага."""
import importlib
from datetime import datetime, timedelta

import pytest

from gateway import dream
from gateway.idle_gate import IdleGate

NIGHT = datetime(2026, 10, 9, 4, 0)


class _Clock:
    """Внедрённые часы Dream: тест двигает время сам."""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _dream(mongo_db, steps, clock, gate=None):
    events = []
    gate = gate or IdleGate(idle_s=10, clock=lambda: 1000.0)
    return dream.Dream(steps, dream.MongoLock(mongo_db), gate, lambda k, d: events.append((k, d)),
                       now=clock), events


async def test_dream_runs_once_a_night_when_nobody_talks(mongo_db):
    clock = _Clock(datetime(2026, 10, 9, 14, 0))
    talk_clock = [1000.0]
    gate = IdleGate(idle_s=10, clock=lambda: talk_clock[0])
    d, events = _dream(mongo_db, [dream.Step("report", lambda: {"mongo_facts": 3})], clock, gate)

    assert await d.tick() is None                                  # днём не идёт
    clock.now = NIGHT
    gate.touch()
    assert await d.tick() is None                                  # ночью, но люди говорят
    talk_clock[0] += 11
    assert await d.tick() == {"report": {"mongo_facts": 3}}
    assert await d.tick() is None and len(events) == 1             # второй запуск за ночь не идёт

    clock.now = NIGHT + timedelta(days=1)                          # следующая ночь
    lock = dream.MongoLock(mongo_db)
    assert lock.acquire(clock.now)                                 # другой запуск держит блокировку
    assert await d.tick() is None
    clock.now += dream.LOCK_TTL + timedelta(seconds=1)             # упавший запуск: блокировка протухла
    assert await d.tick() is not None
    assert lock.last_run() == clock.now


@pytest.mark.parametrize("now, last_run, from_h, to_h, expected", [
    (NIGHT, None, 3, 6, True),
    (datetime(2026, 10, 9, 14, 0), None, 3, 6, False),
    (NIGHT, NIGHT - timedelta(hours=2), 3, 6, False),
    (NIGHT, NIGHT - timedelta(hours=21), 3, 6, True),
    (datetime(2026, 10, 9, 23, 30), None, 23, 5, True),
], ids=["night", "day", "ran_tonight", "ran_last_night", "window_wraps_midnight"])
def test_dream_is_due_inside_the_window_once_per_interval(now, last_run, from_h, to_h, expected):
    assert dream.due(now, last_run, from_h, to_h, 20) is expected


async def test_dream_reports_each_step_and_survives_a_failure(mongo_db):
    def _boom():
        raise ConnectionError("qdrant down\nstack")
    d, events = _dream(mongo_db, [dream.Step("report", lambda: {"mongo_facts": 3}),
                                  dream.Step("prune_index", _boom),
                                  dream.Step("after", lambda: {"ok": 1})], _Clock(NIGHT))
    report = await d.tick()
    assert report == {"report": {"mongo_facts": 3}, "prune_index": {"error": "qdrant down"}, "after": {"ok": 1}}
    assert events == [("dream", {"steps": report})]
    assert dream.MongoLock(mongo_db).last_run() == NIGHT


class _RecordingStore:
    """Память на границе Mongo и Qdrant: записывает, какие методы обслуживания позвали."""

    def __init__(self):
        self.calls = []

    def maintenance_check(self):
        self.calls.append("maintenance_check")
        return {"mongo_facts": 1}

    def prune_dead_facets(self):
        self.calls.append("prune_dead_facets")
        return {"deleted": 0}

    def prune_unused(self):
        self.calls.append("prune_unused")
        return {"deleted": 0}


@pytest.fixture
def dream_module_from_env(monkeypatch):
    """Модуль dream, заново прочитавший окружение; после теста - снова по окружению процесса."""
    def load(**env):
        monkeypatch.delenv("SAM_DREAM_APPLY", raising=False)
        monkeypatch.delenv("SAM_DREAM", raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return importlib.reload(dream)
    yield load
    monkeypatch.undo()
    importlib.reload(dream)


@pytest.mark.parametrize("env, calls", [
    ({}, ["maintenance_check"]),
    ({"SAM_DREAM_APPLY": "1"}, ["maintenance_check", "prune_dead_facets"]),
], ids=["apply_unset", "apply_on"])
async def test_night_run_deletes_nothing_unless_apply_is_set(mongo_db, dream_module_from_env, env, calls):
    """memory-in-pauses: SAM_DREAM_APPLY по умолчанию выключен - ночной запуск только отчитывается; включённый
    удаляет векторы Qdrant, а факты Mongo (prune_unused) ночной запуск не удаляет никогда."""
    module = dream_module_from_env(**env)
    store = _RecordingStore()
    d = module.Dream(module.default_steps(store), module.MongoLock(mongo_db), IdleGate(idle_s=10, clock=lambda: 1000.0),
                     lambda k, data: None, now=_Clock(NIGHT))
    await d.tick()
    assert store.calls == calls


def test_index_pruning_only_when_applied():
    class _Store:
        def maintenance_check(self):
            return {}

        def prune_dead_facets(self):
            return {}
    assert [s.name for s in dream.default_steps(_Store(), apply=False)] == ["report"]
    assert [s.name for s in dream.default_steps(_Store(), apply=True)] == ["report", "prune_index"]


@pytest.mark.parametrize("env, started", [({}, False), ({"SAM_DREAM": "1"}, True)], ids=["unset", "on"])
async def test_night_job_starts_with_the_gateway_only_when_sam_dream_is_set(monkeypatch, dream_module_from_env, env,
                                                                             started):
    """memory-in-pauses 11: консолидацию включает SAM_DREAM=1 - старт шлюза запускает ночную работу; без переменной
    её нет."""
    import asyncio

    from gateway import agent, main
    dream_module_from_env(**env)
    ran = []

    class _Job:
        async def run(self):
            ran.append("dream")
    monkeypatch.setattr(agent, "build_dream", _Job)
    await main._start_dream()
    await asyncio.sleep(0)
    assert ran == (["dream"] if started else [])
