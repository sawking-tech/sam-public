"""Одна сессия - один ход (SAM-01, ADR-023): фразы во время хода копятся и уходят следующим ходом одним сообщением.

Порядок задают события asyncio, а не паузы: ход держится, пока тест не отпустит его."""

import asyncio

import pytest

from gateway import session_lock


@pytest.fixture(autouse=True)
def reset_state():
    """Глобальные словари замка общие на процесс: каждый тест начинает с пустых."""
    for state in (session_lock._locks, session_lock._queues, session_lock._busy, session_lock._active_tasks):
        state.clear()
    yield
    for state in (session_lock._locks, session_lock._queues, session_lock._busy, session_lock._active_tasks):
        state.clear()


class _HeldTurn:
    """Обработчик хода: записывает фразы, первый ход держит до release, падает на фразах из fail."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)
        self.started, self._release = asyncio.Event(), asyncio.Event()

    async def __call__(self, msg: str) -> str:
        self.calls.append(msg)
        if len(self.calls) == 1:
            self.started.set()
            await self._release.wait()
        if msg in self.fail:
            raise RuntimeError("LLM недоступна")
        return f"r:{msg}"

    def release(self):
        self._release.set()


async def _first_turn(session: str, handler: _HeldTurn, msg: str = "m1") -> asyncio.Task:
    """Запустить первый ход и дождаться, что он внутри обработчика."""
    task = asyncio.create_task(session_lock.acquire_and_run(session, msg, handler))
    await handler.started.wait()
    return task


async def test_phrases_during_a_turn_merge_into_the_next_turn():
    # Одна фраза идёт сразу и отпускает сессию
    alone = _HeldTurn()
    alone.release()
    assert await session_lock.acquire_and_run("solo", "hello", alone) == "r:hello"
    assert not session_lock.is_busy("solo")

    # Три фразы подряд: первая отдельным ходом, две пришедшие во время него склеены во второй
    handler = _HeldTurn()
    first = await _first_turn("s1", handler)
    assert await session_lock.acquire_and_run("s1", "m2", handler) == ""
    assert await session_lock.acquire_and_run("s1", "m3", handler) == ""
    assert session_lock.queue_size("s1") == 2

    # Другая сессия не ждёт занятой
    other = _HeldTurn()
    other.release()
    assert await session_lock.acquire_and_run("s2", "ma", other) == "r:ma"

    handler.release()
    assert await first == "r:m2 m3"          # итог - последний проход
    assert handler.calls == ["m1", "m2 m3"]
    assert other.calls == ["ma"]


@pytest.mark.parametrize("edit, expected_calls", [
    (lambda sid: session_lock.clear_queue(sid), ["first"]),
    (lambda sid: session_lock.replace_queue(sid, "edited"), ["first", "edited"]),
], ids=["clear", "replace"])
async def test_pending_queue_can_be_cleared_or_replaced(edit, expected_calls):
    handler = _HeldTurn()
    first = await _first_turn("s1", handler, "first")
    await session_lock.acquire_and_run("s1", "old1", handler)
    await session_lock.acquire_and_run("s1", "old2", handler)
    assert edit("s1") == 2                   # обе накопленные сброшены или заменены
    handler.release()
    await first
    assert handler.calls == expected_calls


async def test_failed_turn_frees_session_and_flushes_queue():
    # Ход упал: накопленное за него уходит следующим ходом сразу, а не ждёт новой фразы
    handler = _HeldTurn(fail={"m1"})
    first = await _first_turn("s1", handler)
    await session_lock.acquire_and_run("s1", "m2", handler)
    handler.release()
    assert await first == "r:m2"
    assert handler.calls == ["m1", "m2"] and session_lock.get_queue("s1") == []

    # Единственный упавший ход отдаёт ошибку и отпускает сессию
    lone = _HeldTurn(fail={"x"})
    lone.release()
    with pytest.raises(RuntimeError):
        await session_lock.acquire_and_run("s3", "x", lone)
    assert not session_lock.is_busy("s3")


async def test_exclusive_turn_orders_with_human_turns_and_is_cancellable():
    order = []
    human_inside, release_human = asyncio.Event(), asyncio.Event()

    async def human(msg):
        order.append(f"человек:{msg}")
        if msg == "раз":
            human_inside.set()
            await release_human.wait()
        return msg

    # Ход workflow ждёт конца хода человека, следующая фраза не склеивается с ним
    async def workflow():
        async with session_lock.exclusive("s-excl"):
            order.append("workflow")

    first = asyncio.create_task(session_lock.acquire_and_run("s-excl", "раз", human))
    await human_inside.wait()
    wf = asyncio.create_task(workflow())
    await asyncio.sleep(0)                   # workflow дошёл до замка и ждёт
    assert order == ["человек:раз"]
    release_human.set()
    await asyncio.gather(first, wf)
    assert await session_lock.acquire_and_run("s-excl", "два", human) == "два"
    assert order == ["человек:раз", "workflow", "человек:два"]

    # Фраза во время хода workflow не встаёт в очередь склейки, а идёт своим ходом после него
    order.clear()
    release_wf, inside = asyncio.Event(), asyncio.Event()

    async def long_workflow():
        async with session_lock.exclusive("s-excl2"):
            order.append("workflow")
            inside.set()
            await release_wf.wait()

    wf = asyncio.create_task(long_workflow())
    await inside.wait()
    phrase = asyncio.create_task(session_lock.acquire_and_run("s-excl2", "три", human))
    await asyncio.sleep(0)
    assert order == ["workflow"] and session_lock.queue_size("s-excl2") == 0
    release_wf.set()
    assert await phrase == "три" and order == ["workflow", "человек:три"]
    await wf

    # «Стоп» отменяет ход workflow
    inside3 = asyncio.Event()

    async def stuck_workflow():
        async with session_lock.exclusive("s-excl3"):
            inside3.set()
            await asyncio.Event().wait()

    stuck = asyncio.create_task(stuck_workflow())
    await inside3.wait()
    assert session_lock.cancel_current_turn("s-excl3") is True
    with pytest.raises(asyncio.CancelledError):
        await stuck
    assert "s-excl3" not in session_lock._active_tasks


async def test_background_turns_run_together_and_a_human_turn_waits_for_the_last():
    # Пачки очереди push одной сессии (ADR-020, п. 12): вместе под одним захватом, ход человека - после них
    sid, order = "test-do-not-use-bg", []
    inside, release = [asyncio.Event() for _ in range(3)], asyncio.Event()

    async def batch(i):
        async with session_lock.background(sid):
            inside[i].set()
            await release.wait()
            order.append(f"batch{i}")

    async def human():
        async with session_lock.exclusive(sid):
            order.append("human")

    batches = [asyncio.create_task(batch(i)) for i in range(3)]
    for ev in inside:
        await ev.wait()                      # все три пачки внутри одновременно
    person = asyncio.create_task(human())
    await asyncio.sleep(0)
    assert order == []                       # человек ждёт последнюю пачку
    release.set()
    await asyncio.gather(*batches, person)
    assert order[-1] == "human" and len(order) == 4


async def test_monitor_edits_the_queue_and_cancels_every_turn(monkeypatch):
    """session-queue 4, 6, 7, 8: эндпоинты монитора в том же цикле (ASGI) - снимок очереди, снять последнюю фразу,
    «стоп всему»; монитор видит очередь и занятость событиями queue_*, session_busy, session_idle, turn_cancelled."""
    import httpx

    from gateway import main
    from gateway.event_bus import event_bus
    seen = []
    real_emit = event_bus.emit
    monkeypatch.setattr(event_bus, "emit", lambda kind, data: (seen.append((kind, data)), real_emit(kind, data)))

    def events(kind, session):
        return [d for k, d in seen if k == kind and d.get("session") == session]
    voice, chat = _HeldTurn(), _HeldTurn()
    first_voice, first_chat = await _first_turn("voice", voice, "раз"), await _first_turn("chat", chat, "привет")
    await session_lock.acquire_and_run("voice", "два", voice)
    await session_lock.acquire_and_run("voice", "три", voice)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testclient") as c:
        assert (await c.get("/sam/session/voice/queue")).json()["queue"] == ["два", "три"]
        popped = (await c.post("/sam/session/voice/pop-last", json={})).json()
        assert (popped["removed"], popped["queue_size"]) == ("три", 1)
        await asyncio.wait_for(_polled(lambda: events("session_busy", "voice")), 2)
        assert (await c.post("/sam/cancel-all", json={})).json() == {"cancelled": 2}

    assert await first_voice == "[отменено]" and await first_chat == "[отменено]"
    assert voice.calls == ["раз"] and chat.calls == ["привет"]                  # «два» не ушло после отмены
    assert not session_lock.is_busy("voice") and not session_lock.is_busy("chat")
    assert [d["queue"] for d in events("queue_added", "voice")] == [["два"], ["два", "три"]]
    assert events("queue_popped", "voice")[0]["removed"] == "три"
    assert events("session_busy", "voice")[0]["busy"] is True
    assert events("turn_cancelled", "voice") and events("turn_cancelled", "chat")
    assert events("session_idle", "voice")[-1]["busy"] is False and events("session_idle", "chat")


async def _polled(condition):
    while not condition():
        await asyncio.sleep(0.05)
