"""Очередь отложенных задач (SAM-260): принято как есть, запуск сам, когда лимит пускает, по порядку."""
import mongomock
import pytest

from gateway import deferred_tasks as dt


@pytest.fixture
def db():
    return mongomock.MongoClient()["sam_test"]


def _params(n):
    return {"description": f"сделай рефакторинг {n}", "steps": 20}


async def test_a_task_waits_while_the_limit_is_closed_and_starts_when_it_opens(db):
    dt.enqueue(db, _params(1))
    launched = []

    async def launch(params):
        launched.append(params)
        return {"status": "ok", "task_id": "abc"}
    assert await dt.run_ready(db, lambda d, s: (False, "лимит"), launch) == [] and launched == []
    assert len(dt.pending(db)) == 1
    started = await dt.run_ready(db, lambda d, s: (True, ""), launch)
    assert len(started) == 1 and launched == [_params(1)] and dt.pending(db) == []


async def test_oldest_goes_first_and_one_per_pass_by_default(db):
    for n in (1, 2, 3):
        dt.enqueue(db, _params(n))
    order = []

    async def launch(params):
        order.append(params["description"])
        return {"status": "ok", "task_id": "x"}
    await dt.run_ready(db, lambda d, s: (True, ""), launch)
    assert order == ["сделай рефакторинг 1"] and len(dt.pending(db)) == 2


async def test_a_busy_gateway_keeps_the_task_waiting_and_a_crash_marks_it_failed(db):
    dt.enqueue(db, _params(1))

    async def busy(params):
        return {"status": "error", "error": "все воркеры заняты"}
    assert await dt.run_ready(db, lambda d, s: (True, ""), busy) == [] and len(dt.pending(db)) == 1

    async def crash(params):
        raise RuntimeError("упало")
    await dt.run_ready(db, lambda d, s: (True, ""), crash)
    assert dt.pending(db) == [] and db[dt.COLLECTION].find_one()["status"] == dt.FAILED
