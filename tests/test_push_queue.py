"""Очередь push в основную модель (gateway/push_queue.py, gateway/push_dispatch.py): через настоящий PushDispatcher
с поддельными часами; граница - deliver (модель)."""
import asyncio

import pytest

from gateway import push_dispatch as pd
from gateway.push_dispatch import QUEUED, PushDispatcher
from gateway.push_queue import (ACK_HEAD, ALONE, BATCH, DEFAULT_MAX_HOLD_S, MERGE, PART_SEPARATOR, SUMMARY_SOURCE,
                                PushQueue, from_env)


def ack_all(batch):
    """Модель в шлюзе отработала все пункты пачки и подтвердила их номерами (ADR-020, п. 13)."""
    return f"ok\n{ACK_HEAD} " + ", ".join(str(n) for n in range(1, len(batch.items()) + 1))


class Model:
    """Основная модель за deliver: запоминает пачки; reply - ответ (None - модель вне шлюза, исключение - сбой)."""

    def __init__(self, reply=ack_all):
        self.reply, self.batches = reply, []

    async def __call__(self, batch):
        self.batches.append(batch)
        reply = self.reply(batch) if callable(self.reply) else self.reply
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def sent(self):
        return [(b.session, b.text) for b in self.batches]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _dispatcher(model, hold=lambda batch: None, log=print, **queue):
    clock, events = Clock(), []
    d = PushDispatcher(PushQueue(**{"max_hold_s": 30, **queue}), model, lambda kind, ev: events.append(ev),
                       clock=clock, log=log, hold=hold)
    return d, clock, events


def _src(name):
    return f"workflow:{name}/step"


async def _play(d, clock, script):
    """Сценарий: ("push", t, имя, важность, сессия, текст, delivery) или ("flush", t)."""
    for step in script:
        clock.now = step[1]
        if step[0] == "flush":
            await d.flush_due()
        else:
            _, _, name, importance, session, text, delivery = step
            await d.push(_src(name), text or f"текст {name}", session, importance, delivery)


def _p(t, name, importance, session="voice", text=None, delivery=BATCH):
    return ("push", t, name, importance, session, text, delivery)


def _f(t):
    return ("flush", t)


STICKY_CASES = {
    "alone_at_once_buffer_untouched": (
        30, [_p(0, "sticky", 0), _p(1, "disk", 0.5, delivery=ALONE), _f(29), _f(30)],
        [("voice", "текст disk"), ("voice", "текст sticky")]),       # одно сообщение - без подписи источника
    "urgent_takes_waiting_of_its_session": (
        30, [_p(0, "memory", 0.2), _p(0, "other", 0.2, session="kitchen"), _p(3, "late", 1), _f(23), _f(24)],
        [("voice", f"[{_src('late')}] текст late{PART_SEPARATOR}[{_src('memory')}] текст memory"),
         ("kitchen", "текст other")]),
    "close_sticky_one_input_important_first": (
        30, [_p(10, "memory", 0.5), _p(5, "scan", 0), _f(24), _f(25), _f(100)],
        [("voice", f"[{_src('memory')}] текст memory{PART_SEPARATOR}[{_src('scan')}] текст scan")]),
    "merge_fuses_at_first_place": (
        10, [_p(0, "a", 0, text="проверь диск C", delivery=MERGE), _p(1, "b", 0, text="подробно"),
             _p(2, "c", 0, text="проверь диск D", delivery=MERGE), _f(10)],
        [("voice", f"проверь диск C\nпроверь диск D{PART_SEPARATOR}[{_src('b')}] подробно")]),
    "far_apart_go_separately": (
        30, [_p(0, "a", 0.9), _f(3), _p(60, "b", 0.9), _f(63)], [("voice", "текст a"), ("voice", "текст b")]),
    "sessions_ripen_in_deadline_order": (
        10, [_p(0, "late", 0, session="voice"), _p(1, "early", 0.75, session="kitchen"), _f(3.4), _f(3.5), _f(10)],
        [("kitchen", "текст early"), ("voice", "текст late")]),
    "equal_texts_one_item_with_count": (
        10, [_p(i, f"disk{i}", 0, text="диск C почти полон") for i in range(3)] + [_p(3, "mem", 0, text="запомни"),
                                                                                  _f(10)],
        [("voice", f"[{_src('disk0')} +2] диск C почти полон{PART_SEPARATOR}[{_src('mem')}] запомни")]),
}


@pytest.mark.parametrize("hold_s, script, delivered", STICKY_CASES.values(), ids=STICKY_CASES.keys())
async def test_push_strict_goes_now_and_sticky_merges_when_ripe(hold_s, script, delivered):
    model = Model()
    d, clock, events = _dispatcher(model, max_hold_s=hold_s)
    await _play(d, clock, script)
    assert model.sent() == delivered
    assert [e["merged"] for e in events] == [len(b.messages) > 1 for b in model.batches]


async def test_push_answers_strict_now_and_queues_sticky():
    model = Model(reply="Диск почищу.")
    d, clock, _ = _dispatcher(model, max_hold_s=10)
    assert await d.push("workflow:disk/tell", "диск", "voice", 1) == "Диск почищу."
    assert await d.push("workflow:a/s", "раз", "voice", 0.5) == QUEUED
    assert await d.flush_due() == 0


@pytest.mark.parametrize("queue, texts, sizes", [
    ({"max_items": 2, "max_chars": 1000}, [f"повод {i}" for i in range(5)], [2, 2, 1]),
    ({"max_items": 50, "max_chars": 10}, ["x" * 6] * 3, [1, 1, 1]),
], ids=["items_cap", "chars_cap"])
async def test_push_batch_is_capped_and_the_rest_goes_next(queue, texts, sizes):
    model = Model()
    d, clock, _ = _dispatcher(model, max_hold_s=10, **queue)
    for i, text in enumerate(texts):
        clock.now = i
        await d.push(_src(f"w{i}"), text, "voice", 0.0)
    clock.now = 100   # сроки всех прошли
    for _ in sizes:   # одно место модели: пачка за отправку, остаток ждёт следующей
        await d.flush_due()
    assert [len(b.messages) for b in model.batches] == sizes


@pytest.mark.parametrize("max_pending, pushes", [(10, 30), (3, 20)], ids=["fold_once", "fold_counts_across"])
async def test_push_backpressure_loses_nothing(max_pending, pushes):
    model = Model()
    d, clock, _ = _dispatcher(model, max_pending=max_pending)
    for i in range(pushes):
        clock.now = i * 0.01
        await d.push(_src("disk-watch" if i % 3 else "memory-review"), f"повод {i}", "voice", 0.1, message_id=f"m{i}")
    clock.now = 1.0
    await d.push(_src("alarm"), "важное", "voice", 0.9, message_id="alarm")
    clock.now = 1000
    await d.flush_due()
    [batch] = model.batches
    summary = next(m for m in batch.messages if m.source == SUMMARY_SOURCE)
    assert len(batch.messages) <= max_pending and "важное" in batch.text
    assert sum(n for _, n in summary.folded) + len(batch.messages) - 1 == pushes + 1   # ничего не потеряно
    assert "disk-watch" in summary.text and "workflow:" not in summary.text
    assert all(d.fate(f"m{i}") == pd.ACKED for i in range(pushes))   # свёрнутые - судьба сводки


async def test_busy_session_waits_for_the_answer_and_then_goes_in_one_batch():
    answered = asyncio.Event()
    sent = []

    async def deliver(batch):
        sent.append([m.source for m in batch.messages])
        if len(sent) == 1:
            await answered.wait()
        return "ok"
    d = PushDispatcher(PushQueue(max_hold_s=30), deliver, lambda *a: None, clock=lambda: 0.0)
    first = asyncio.ensure_future(d.push("workflow:a/s", "раз", "voice", 1.0))
    await asyncio.sleep(0)
    assert d.busy("voice")
    assert await d.push("workflow:b/s", "два", "voice", 1.0) == QUEUED      # срочное ждёт конца ответа
    assert await d.push("workflow:c/s", "три", "voice", 1.0) == QUEUED
    answered.set()
    await first
    await d.flush_due()
    assert sent == [["workflow:a/s"], ["workflow:b/s", "workflow:c/s"]]


NO_ACK_S = 0.01   # срок ожидания подтверждения, которого не будет


@pytest.mark.parametrize("reply, hold, ack, fate", [
    ("ответ", None, False, pd.ACKED),                      # модель в шлюзе ответила - acked
    (None, None, False, pd.NOT_ACKED),                     # модель вне шлюза, подтверждения нет за срок
    (None, None, True, pd.ACKED),                          # модель вне шлюза подтвердила id
    ("ok", "тихий режим", False, pd.DROPPED),              # тишина при доставке - модель не зовётся
    (ConnectionError("нет модели"), None, False, pd.ERROR),
], ids=["in_gateway", "outside_no_ack", "outside_ack", "quiet", "failure"])
async def test_push_message_fate(reply, hold, ack, fate):
    logged = []
    model = Model(reply=reply)
    d, _, events = _dispatcher(model, hold=lambda batch: hold, log=logged.append)
    await d.push("workflow:a/s", "x", "voice", 1.0, message_id="m1")
    if ack:
        waiting = asyncio.ensure_future(d.wait("m1", 1.0))
        await asyncio.sleep(0)
        assert d.ack(["m1", "unknown"]) == 1
        assert await waiting == fate
    else:
        assert await d.wait("m1", NO_ACK_S) == fate
    assert (model.batches == []) is bool(hold)
    if hold:
        assert events[-1][pd.SKIPPED] == hold
    if fate == pd.ERROR:   # сбой - строка в лог, диспетчер жив
        assert "не доставлен" in logged[0]
        model.reply = "ok"
        await d.push("workflow:b/s", "y", "voice", 1.0, message_id="m2")
        assert d.fate("m2") == pd.ACKED


async def test_push_hold_is_checked_at_delivery_not_at_queueing():
    quiet = set()
    model = Model()
    d, clock, events = _dispatcher(model, max_hold_s=10, hold=lambda batch: "тишина" if batch.session in quiet else None)
    assert await d.push("workflow:a/s", "раз", "voice", 0.0, message_id="q") == QUEUED
    quiet.add("voice")                                            # сессию перевели в тишину, пока пачка ждала
    clock.now = 10
    assert await d.flush_due() == 1
    assert model.batches == [] and events[-1][pd.SKIPPED] == "тишина" and d.fate("q") == pd.DROPPED


@pytest.mark.parametrize("env, hold_s", [({"SAM_PUSH_MAX_HOLD_S": "20"}, 20), ({"SAM_PUSH_MAX_HOLD_S": "полминуты"},
                                                                                DEFAULT_MAX_HOLD_S), ({}, DEFAULT_MAX_HOLD_S)],
                         ids=["from_env", "garbage", "default"])
async def test_push_numbers_from_env(env, hold_s):
    q = from_env(env)
    assert q.max_hold_s == hold_s
    model = Model()
    d = PushDispatcher(q, model, lambda *a: None, clock=lambda: 0.0)
    assert await d.push("workflow:big/s", "x", "voice", 4) == "ok"       # больше 1 - как 1, срочное
    assert await d.push("workflow:neg/s", "y", "voice", -6) == QUEUED     # меньше 0 - как 0
    assert q.next_deadline() == hold_s


# ─── фаза 5: P3 ───────────────────────────────────────────────────────────────

async def test_batch_input_names_every_source_and_the_monitor_sees_them():
    """workflows 36, 37: источник входа пачки - источники её шагов через «+»; каждая отправка - событие workflow_push
    с sources и merged."""
    model = Model()
    d, clock, events = _dispatcher(model, max_hold_s=10)
    await _play(d, clock, [_p(0, "disk", 0.0), _p(1, "memory", 0.0), _f(10), _p(11, "alarm", 1.0)])
    sources = [_src("disk"), _src("memory")]
    assert [b.source for b in model.batches] == ["+".join(sources), _src("alarm")]
    assert [(e["sources"], e["merged"]) for e in events] == [(sources, True), ([_src("alarm")], False)]


@pytest.mark.parametrize("env, items, chars, parallel", [
    ({"SAM_PUSH_MAX_ITEMS": "7", "SAM_PUSH_MAX_CHARS": "900", "SAM_PUSH_PARALLEL": "2"}, 7, 900, 2),
    ({}, None, None, 3),                                         # мест - по умолчанию от вызывающего
], ids=["from_env", "defaults"])
def test_batch_limits_and_lanes_come_from_settings(env, items, chars, parallel):
    """workflows 39, 40: пределы пачки - SAM_PUSH_MAX_ITEMS и SAM_PUSH_MAX_CHARS; мест очереди - SAM_PUSH_PARALLEL,
    без неё - то, что дал корень композиции (мест модели минус одно)."""
    from gateway import agent, llm_client
    q = from_env(env, parallel=3)
    assert (q.max_items, q.max_chars, q.parallel) == (items or q.max_items, chars or q.max_chars, parallel)
    if not env:
        assert agent._build_push_dispatcher()._queue.parallel == max(llm_client.LLM_CONCURRENCY - 1, 1)


@pytest.mark.parametrize("reply", ["понятно", None], ids=["in_gateway_no_ack_number", "outside_no_ack"])
async def test_a_single_message_is_never_repeated_by_the_buffer(reply):
    """workflows 55: одиночное сообщение буфер сам не повторяет, даже без подтверждения: повтор - решение workflow."""
    model = Model(reply=reply)
    d, clock, _ = _dispatcher(model, max_hold_s=10)
    await d.push(_src("disk"), "диск", "voice", 1.0, message_id="m1")
    for t in (10, 100, 1000):
        clock.now = t
        await d.flush_due()
    assert len(model.batches) == 1
