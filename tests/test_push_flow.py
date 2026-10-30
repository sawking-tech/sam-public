"""Флоу очереди push (ADR-020, п. 12-13) на настоящих очереди и диспетчере; подменена только модель (deliver)."""
import asyncio

import pytest

from gateway.push_dispatch import ACKED, ERROR, MAX_ATTEMPTS, PushDispatcher
from gateway.push_queue import ACK_HEAD, ALONE, PushQueue, split_ack


class _Model:
    """Модель за deliver: отвечает с задержкой, считает одновременные вызовы, подтверждает пункты по правилу."""

    def __init__(self, confirm=lambda batch: range(1, len(batch.items()) + 1), delay=0.02):
        self.confirm, self.delay = confirm, delay
        self.batches, self.in_flight, self.max_in_flight = [], 0, 0

    async def __call__(self, batch):
        self.batches.append(batch)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(self.delay)
        self.in_flight -= 1
        numbers = ", ".join(map(str, self.confirm(batch)))
        return f"Разобрала.\n{ACK_HEAD} {numbers}"


def _dispatcher(model, parallel=4, max_items=50):
    queue = PushQueue(max_hold_s=0, max_items=max_items, max_pending=1000, parallel=parallel)
    return PushDispatcher(queue, model,
                          lambda e, d: None, clock=lambda: 1000.0)


async def _push_many(d, count, session="voice"):
    for i in range(count):
        await d.push(f"workflow:wf{i}/step", f"повод {i}", session, 0.2, message_id=f"m{i}")


async def test_five_places_get_four_even_mixed_batches_at_once():
    model = _Model()
    d = _dispatcher(model, parallel=5, max_items=150)   # четыре места под пачки, пятое - одиночному
    await _push_many(d, 400)
    await d.flush_due()
    sizes = sorted(len(b.messages) for b in model.batches)
    assert sizes == [100, 100, 100, 100] and model.max_in_flight == 4
    first = {m.text for m in model.batches[0].messages}
    assert "повод 0" in first and "повод 396" in first   # в пачке и старые, и новые поводы
    assert all(d.fate(f"m{i}") == ACKED for i in range(400))


async def test_limit_splits_evenly_not_fifty_plus_one():
    model = _Model()
    d = _dispatcher(model, parallel=1, max_items=50)
    await _push_many(d, 51)
    await d.flush_due()
    await d.flush_due()   # одно место: вторая пачка - следующим проходом
    assert sorted(len(b.messages) for b in model.batches) == [25, 26]


async def test_a_single_message_goes_while_batches_are_in_flight():
    model = _Model(delay=0.05)
    d = _dispatcher(model, parallel=4)
    await _push_many(d, 30)
    flushing = asyncio.create_task(d.flush_due())   # пачки пошли и держат места
    await asyncio.sleep(0.01)
    reply = await d.push("workflow:alarm/step", "датчик дыма", "voice", 0.5, ALONE, message_id="alone")
    await flushing
    assert reply.startswith("Разобрала") and d.fate("alone") == ACKED and model.max_in_flight <= 4


async def test_unconfirmed_items_are_resent_then_fail():
    # модель подтверждает диск, а память и радар раз за разом пропускает
    model = _Model(confirm=lambda batch: [n for n, g in enumerate(batch.items(), 1) if g[0].text == "диск"])
    d = _dispatcher(model, parallel=1)
    for text in ("диск", "память", "радар"):
        await d.push(f"workflow:{text}/step", text, "voice", 0.2, message_id=text)
    for _ in range(MAX_ATTEMPTS + 1):
        await d.flush_due()
    assert d.fate("диск") == ACKED and d.fate("память") == ERROR and d.fate("радар") == ERROR
    assert len(model.batches) == MAX_ATTEMPTS   # первая отправка и повторы, затем ошибка


@pytest.mark.parametrize("reply, clean, numbers", [
    ("Сделала.\nОТРАБОТАНО: 1, 3", "Сделала.", {1, 3}),
    ("Сделала.", "Сделала.", None),
    ("отработано: 2\nконец", "конец", {2}),
])
def test_ack_line_is_parsed_and_never_spoken(reply, clean, numbers):
    assert split_ack(reply) == (clean, numbers)
