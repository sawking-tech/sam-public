"""Доставка очереди push (gateway/push_queue.py): строгое - сразу, липкое - когда созрела пачка.

Шаг workflow кладёт сообщение (push); раз в POLL_S цикл забирает созревшие пачки и отдаёт их основной модели тем же
путём, что шаг без очереди (deliver). Решение «кто летит один, кто слипается» - в push_queue, здесь только часы и
доставка.

Канал с учётом занятости (ADR-020, п. 2, как алгоритм Нейгла в TCP): пока модель отвечает сессии, её новые пачки, и
срочные тоже, вторым вызовом не идут - возвращаются в очередь и уходят одной пачкой сразу после ответа.

Судьба сообщения (ADR-020, п. 5): queued - ждёт; acked - модель в шлюзе закончила ход по пачке; sent - ушло модели
вне шлюза, она подтверждает id инструментом стенда (ack); dropped - не доставлено (тихий режим); error - сбой
доставки. Шаг workflow может ждать судьбу своего сообщения со сроком (wait): не подтверждено за срок - not_acked.
Повтор при неудаче - решение самого workflow (условный шаг), не буфера: повтор того же повода модель приняла бы
за новый.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from dataclasses import replace

from .env import env_int
from .push_queue import ALONE, BATCH, Batch, PushMessage, PushQueue, split_ack
from .workflow_spec import PUSH_EVENT

POLL_S = 1.0          # точность срока склейки: секунда при пределе ожидания в десятки секунд
EVENT = PUSH_EVENT
SENT, QUEUED = "sent", "queued"   # ответ push без текста модели: ушло (модель вне шлюза) или ждёт попутчиков

Deliver = Callable[[Batch], Awaitable[str | None]]   # ответ модели на пачку; модель вне шлюза - None
# Почему пачку сейчас не доставлять (тихий режим сессии); None - доставлять. Проверка в момент доставки, а не постановки:
# сессию могли перевести в тишину, пока пачка ждала попутчиков
Hold = Callable[[Batch], str | None]
SKIPPED = "skipped"
ACKED, NOT_ACKED, DROPPED, ERROR = "acked", "not_acked", "dropped", "error"
FINAL = (ACKED, DROPPED, ERROR)
# Сколько последних судеб помнить: ждущий шаг спрашивает про своё сообщение минуты, а не часы
FATES_KEPT = 10000
# Попыток на пункт без подтверждения модели (ADR-020, п. 13): пропущенный пункт уходит повтором, после предела - error
MAX_ATTEMPTS = env_int("SAM_PUSH_ATTEMPTS", 3)


class PushDispatcher:
    def __init__(self, queue: PushQueue, deliver: Deliver, emit: Callable[[str, dict], Any],
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print,
                 hold: Hold = lambda batch: None) -> None:
        self._queue, self._deliver, self._emit, self._clock, self._log = queue, deliver, emit, clock, log
        self._hold = hold
        self._in_flight: dict[str, int] = {}   # ходов модели по сессии сейчас: не больше мест (queue.parallel)
        self._fates: OrderedDict[str, str] = OrderedDict()
        self._waiters: dict[str, list[asyncio.Future]] = {}

    async def push(self, source: str, text: str, session: str, importance: float, delivery: str = BATCH,
                   message_id: str | None = None) -> str:
        """Положить сообщение; alone и срочное уходят сразу. Ответ - ответ модели на отправку или QUEUED.
        message_id - чтобы потом спросить судьбу (wait); не задан - свой."""
        busy = self.busy(session)   # модель отвечает сессии: и срочное ждёт конца ответа
        message_id = message_id or uuid.uuid4().hex
        self._set_fate((message_id,), QUEUED)
        batches = self._queue.push(PushMessage(source, text, session, importance, self._clock(), delivery,
                                               id=message_id))
        replies = await self._send(batches)
        return next((r for r in replies if r), SENT) if batches and not busy else QUEUED

    async def flush_due(self) -> int:
        batches = self._queue.due(self._clock())
        await self._send(batches)
        return len(batches)

    def busy(self, session: str) -> bool:
        """Все места модели у сессии заняты: новое ждёт в очереди."""
        return self._in_flight.get(session, 0) >= self._queue.parallel

    async def _send(self, batches: list[Batch]) -> list[str]:
        """Пачки сессии уходят одновременно (ADR-020, п. 12); сессии - тоже, места делит llm_gate."""
        by_session: dict[str, list[Batch]] = {}
        for batch in batches:
            by_session.setdefault(batch.session, []).append(batch)
        if len(by_session) == 1:   # одна сессия - без лишнего шага цикла: занятость видна сразу
            [(session, group)] = by_session.items()
            return await self._send_session(session, group)
        groups = await asyncio.gather(*(self._send_session(s, group) for s, group in by_session.items()))
        return [reply for group in groups for reply in group]

    async def _send_session(self, session: str, batches: list[Batch]) -> list[str]:
        """Пачки сессии - не больше lanes мест, одиночное (alone) - любое свободное: идут вместе, сверх мест - ждут."""
        alone = all(len(b.messages) == 1 and b.messages[0].delivery == ALONE for b in batches)
        limit = self._queue.parallel if alone else self._queue.lanes
        free = limit - self._in_flight.get(session, 0)
        for batch in batches[max(free, 0):]:
            self._queue.requeue(batch)
        ready = [b for b in batches[:max(free, 0)] if not self._held(b)]
        if not ready:
            return []
        self._in_flight[session] = self._in_flight.get(session, 0) + len(ready)
        try:
            return list(await asyncio.gather(*(self._deliver_one(b) for b in ready)))
        finally:
            self._in_flight[session] -= len(ready)

    def _held(self, batch: Batch) -> bool:
        reason = self._hold(batch)
        if reason:   # модель не зовётся, токены не тратятся
            self._emit(EVENT, {**self._info(batch), SKIPPED: reason})
            self._set_fate(batch.ids, DROPPED)
        return bool(reason)

    @staticmethod
    def _info(batch: Batch) -> dict:
        return {"session": batch.session, "sources": [m.source for m in batch.messages],
                "merged": len(batch.messages) > 1}

    async def _deliver_one(self, batch: Batch) -> str:
        self._emit(EVENT, self._info(batch))
        try:
            reply = await self._deliver(batch)
        except Exception as e:   # сбой одной пачки не останавливает остальные
            self._log(f"[Push] {batch.source} не доставлен: {e}")
            self._set_fate(batch.ids, ERROR)
            return ""
        if reply is None:   # модель вне шлюза подтвердит id сама (ack)
            self._set_fate(batch.ids, SENT)
            return ""
        self._settle(batch, reply)
        return split_ack(reply)[0]

    def _settle(self, batch: Batch, reply: str) -> None:
        """Ответ модели в шлюзе: подтверждённые пункты - acked, пропущенные - повтором до MAX_ATTEMPTS."""
        items = batch.items()
        confirmed = split_ack(reply)[1]
        if len(items) == 1 or confirmed is not None and confirmed >= set(range(1, len(items) + 1)):
            self._set_fate(batch.ids, ACKED)
            return
        retry: list[PushMessage] = []
        for n, group in enumerate(items, 1):
            ids = tuple(i for m in group for i in (m.id, *m.folded_ids) if i)
            if confirmed is not None and n in confirmed:
                self._set_fate(ids, ACKED)
            elif all(m.attempts + 1 < MAX_ATTEMPTS for m in group):
                retry += [replace(m, attempts=m.attempts + 1) for m in group]
            else:
                self._log(f"[Push] {group[0].source}: модель не подтвердила за {MAX_ATTEMPTS} попытки")
                self._set_fate(ids, ERROR)
        if retry:
            self._queue.requeue(Batch(batch.session, tuple(retry)))

    def ack(self, ids) -> int:
        """Подтверждение модели вне шлюза: она закончила ход по этим сообщениям. Ответ - сколько знакомых id."""
        known = [i for i in ids if i in self._fates]
        self._set_fate(known, ACKED)
        return len(known)

    def fate(self, message_id: str) -> str | None:
        return self._fates.get(message_id)

    async def wait(self, message_id: str, timeout_s: float) -> str:
        """Судьба сообщения не позже timeout_s: итоговая (acked, dropped, error) или not_acked по сроку."""
        if self._fates.get(message_id) in FINAL:
            return self._fates[message_id]
        future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(message_id, []).append(future)
        try:
            return await asyncio.wait_for(future, timeout_s)
        except asyncio.TimeoutError:
            return NOT_ACKED
        finally:
            waiting = self._waiters.get(message_id, [])
            if future in waiting:
                waiting.remove(future)
            if not waiting:
                self._waiters.pop(message_id, None)

    def _set_fate(self, ids, fate: str) -> None:
        for message_id in ids:
            self._fates[message_id] = fate
            self._fates.move_to_end(message_id)
            if fate in FINAL:
                for future in self._waiters.get(message_id, []):
                    if not future.done():
                        future.set_result(fate)
        while len(self._fates) > FATES_KEPT:
            self._fates.popitem(last=False)

    async def run(self, poll_s: float = POLL_S) -> None:
        while True:
            await self.flush_due()
            await asyncio.sleep(poll_s)
