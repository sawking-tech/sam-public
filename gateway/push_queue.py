"""Очередь push в основную модель: сообщения workflow, которые будят модель, их склейка и порядок.

Любой workflow, у которого шаг будит основную модель, не зовёт её сам, а кладёт сообщение в очередь сессии с двумя
свойствами. Способ доставки (delivery) - с кем лететь:
- alone - сразу и своим вызовом: буфер не трогает и ни с кем не склеивается;
- batch - в общем вызове отдельным пунктом со своим источником;
- merge - в общем вызове, текст слит с другими merge без разбивки по источникам (однотипные поводы).
Важность от 0 до 1 - насколько срочно (у batch и merge):
- 1 - срочное: уходит сразу и забирает с собой ждущие в буфере сообщения своей сессии;
- i < 1 - может подождать попутчиков не дольше (1 - i) * max_hold_s. Когда срок самого раннего ждущего сообщения сессии истёк, все ждущие
  сообщения этой сессии уходят одним входом - один вызов модели вместо нескольких.

Порядок: внутри пачки - по убыванию важности, при равной - по времени прихода; слитые merge - одним блоком на месте
первого из них; одинаковые тексты - одним пунктом с подписью «первый источник +N»; отправки - в том порядке, в каком
созрели. Пачка не больше max_items пунктов и max_chars знаков: лишнее уходит следующей пачкой той же отправки
(ADR-020: потолок пачки, дедуп, порядок по важности).

Обратное давление (ADR-020, п. 4): ждущих в сессии больше max_pending - самые неважные сворачиваются в одну
строку-сводку по workflow («ещё 40 фоновых: disk-watch 12, memory-review 28»); сводка копит счёт, повод не
теряется молча. Вернуть отложенную пачку (канал занят) - requeue.

Модуль чистый: время приходит параметром, отправка - результатом (список готовых пачек); часы и доставку держит
gateway/push_dispatch.py.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .env import parse_float, parse_int, read

MAX_HOLD_ENV = "SAM_PUSH_MAX_HOLD_S"
# Предел ожидания попутчиков при важности 0: полминуты - пятнадцать тиков планировщика (TICK_S 2 с), соседние workflow
# успевают встать в ту же пачку; срочному ставят важность 1
DEFAULT_MAX_HOLD_S = 30.0
MAX_ITEMS_ENV, MAX_CHARS_ENV = "SAM_PUSH_MAX_ITEMS", "SAM_PUSH_MAX_CHARS"
# Потолок пачки. Модель 12 окт: на всплеске 10 000 workflow пачка доходила до 136 пунктов; 50 пунктов и 12 тыс. знаков
# (около 4 тыс. токенов) держат вход в окне локальной модели вместе с промптом и историей
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_CHARS = 12000
MAX_PENDING_ENV = "SAM_PUSH_MAX_PENDING"
# Ждущих в сессии до свёртки. Замер 12 окт: на 12 430 workflow за окно ожидания приходит около 400 поводов, при
# пределе 200 половина сворачивалась в сводку; 400 - поводы окна разбираются поштучно параллельными пачками, а буфер
# без предела на всплеске растёт быстрее, чем модель отвечает
DEFAULT_MAX_PENDING = 400
SUMMARY_SOURCE = "push:summary"
SUMMARY_HEAD = "Ещё {total} фоновых поводов свёрнуто (перегруз очереди): "
WORKFLOW_PREFIX = "workflow:"
PART_SEPARATOR = "\n\n"
MERGE_SEPARATOR = "\n"
# Подтверждение пачки моделью в шлюзе (ADR-020, п. 13): пункты пронумерованы, в конце ответа - строка с номерами
# отработанных; неподтверждённые уходят повтором
ACK_HEAD = "ОТРАБОТАНО:"
ACK_INSTRUCTION = (f"\n\nПункты выше пронумерованы. Последней строкой ответа напиши {ACK_HEAD} и номера пунктов, "
                   f"которые ты отработала (например {ACK_HEAD} 1, 2, 4); вслух эта строка не звучит.")
_ACK_LINE = re.compile(rf"^\s*{ACK_HEAD}\s*([\d,\s]*)\s*$", re.MULTILINE | re.IGNORECASE)
ALONE, BATCH, MERGE = "alone", "batch", "merge"
DELIVERIES = (ALONE, BATCH, MERGE)


@dataclass(frozen=True)
class PushMessage:
    source: str          # workflow:<имя>/<шаг>
    text: str
    session: str
    importance: float    # 0..1, 1 - срочно
    at: float            # когда пришло, секунды монотонных часов
    delivery: str = BATCH
    folded: tuple[tuple[str, int], ...] = ()   # сводка перегруза: (workflow, сколько поводов свёрнуто)
    id: str = ""                               # для судьбы сообщения (ack, push_dispatch)
    folded_ids: tuple[str, ...] = ()           # сводка: id свёрнутых, их судьба - судьба сводки
    attempts: int = 0                          # сколько раз уже уходило модели без подтверждения


@dataclass(frozen=True)
class Batch:
    """Одна отправка в модель: одно сообщение или пачка."""
    session: str
    messages: tuple[PushMessage, ...]

    @property
    def source(self) -> str:
        return "+".join(m.source for m in self.messages)

    @property
    def ids(self) -> tuple[str, ...]:
        """id всех сообщений пачки, свёрнутые в сводку тоже: у них одна судьба."""
        return tuple(i for m in self.messages for i in (m.id, *m.folded_ids) if i)

    def items(self) -> list[list[PushMessage]]:
        """Пункты пачки: одинаковые тексты - один пункт, слитые (merge) - один пункт."""
        items: list[list[PushMessage]] = []
        merged: list[PushMessage] = []
        for _, group in _same_text(self.messages):
            if group[0].delivery == MERGE:
                if not merged:
                    items.append(merged)   # блок слитых встаёт на место первого из них
                merged.extend(group)
            else:
                items.append(group)
        return items

    @property
    def text(self) -> str:
        return self._render(numbered=False)

    def numbered_text(self) -> str:
        """Текст для модели в шлюзе: пункты с номерами и просьба подтвердить их строкой ОТРАБОТАНО."""
        if len(self.items()) == 1:
            return self.text
        return self._render(numbered=True) + ACK_INSTRUCTION

    def _render(self, numbered: bool) -> str:
        if len(self.messages) == 1:
            return self.messages[0].text
        parts = []
        for n, group in enumerate(self.items(), 1):
            head = f"[{n}] " if numbered else ""
            if group[0].delivery == MERGE:
                parts.append(head + MERGE_SEPARATOR.join(dict.fromkeys(m.text.strip() for m in group)))
            else:
                more = f" +{len(group) - 1}" if len(group) > 1 else ""
                parts.append(f"{head}[{group[0].source}{more}] {group[0].text.strip()}")
        return PART_SEPARATOR.join(parts)


def split_ack(reply: str) -> tuple[str, set[int] | None]:
    """Ответ модели -> (текст без строки подтверждения, номера отработанных); строки нет - None."""
    found = _ACK_LINE.findall(reply or "")
    clean = _ACK_LINE.sub("", reply or "").strip()
    if not found:
        return clean, None
    return clean, {int(n) for n in re.findall(r"\d+", found[-1])}


def _workflow_of(source: str) -> str:
    """Имя workflow из источника workflow:<имя>/<шаг>; прочее - как есть."""
    name = source[len(WORKFLOW_PREFIX):] if source.startswith(WORKFLOW_PREFIX) else source
    return name.split("/", 1)[0]


def summary(session: str, counts: dict[str, int], at: float, ids: tuple[str, ...] = ()) -> PushMessage:
    """Строка-сводка свёрнутых поводов: самые частые workflow первыми."""
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    text = SUMMARY_HEAD.format(total=sum(counts.values())) + ", ".join(f"{n} {c}" for n, c in ordered)
    return PushMessage(SUMMARY_SOURCE, text, session, 0.0, at, BATCH, tuple(ordered), folded_ids=ids)


def fold(session: str, messages: list[PushMessage], max_pending: int) -> list[PushMessage]:
    """Ждущие сессии не больше max_pending: важные остаются, неважные (и прежняя сводка) - в одну сводку."""
    if len(messages) <= max_pending:
        return messages
    keep_n = max(max_pending - 1, 0)
    plain = [m for m in messages if not m.folded]
    keep = sorted(plain, key=lambda m: (-m.importance, m.at))[:keep_n]
    kept = set(map(id, keep))
    counts: dict[str, int] = {}
    ids: list[str] = []
    for m in messages:
        if id(m) in kept:
            continue
        ids += [*m.folded_ids, *([m.id] if m.id else [])]
        for name, n in (m.folded or ((_workflow_of(m.source), 1),)):
            counts[name] = counts.get(name, 0) + n
    first = min(m.at for m in messages if id(m) not in kept)
    return [m for m in messages if id(m) in kept] + [summary(session, counts, first, tuple(ids))]


def clamp(importance: float) -> float:
    return min(max(float(importance), 0.0), 1.0)


def _ordered(messages) -> tuple[PushMessage, ...]:
    """Важное первым, при равной важности - по времени прихода."""
    return tuple(sorted(messages, key=lambda m: (-m.importance, m.at)))


def _same_text(messages) -> list[tuple[str, list[PushMessage]]]:
    """Одинаковые тексты (без учёта пробелов по краям) - одной группой на месте первого."""
    groups: dict[str, list[PushMessage]] = {}
    for m in messages:
        groups.setdefault(m.text.strip(), []).append(m)
    return list(groups.items())


def _split(session: str, messages, max_items: int, max_chars: int, parallel: int = 1) -> list[Batch]:
    """Ровные пачки (ADR-020, п. 12): их не меньше мест модели (parallel) и не меньше, чем требуют пределы
    max_items и max_chars; 400 поводов при 4 местах - 4 пачки по 100 одновременно, а не одна на 400 и не 50 + 1.
    Одно длинное сообщение уходит своей пачкой."""
    messages = list(messages)
    if not messages:
        return []
    total = sum(len(m.text) for m in messages)
    count = max(-(-len(messages) // max_items), -(-total // max_chars), min(max(parallel, 1), len(messages)))
    if count == 1 or any(len(m.text) > max_chars for m in messages):
        return _greedy(session, messages, max_items, max_chars)
    # по кругу: в каждой пачке и старые, и новые поводы, а не старые в первой и новые в последней; не влезла в
    # предел знаков - пачек на одну больше
    while True:
        lanes = [messages[i::count] for i in range(count)]
        if count >= len(messages) or all(sum(len(m.text) for m in lane) <= max_chars for lane in lanes):
            return [Batch(session, _ordered(lane)) for lane in lanes if lane]
        count += 1


def _greedy(session: str, messages, max_items: int, max_chars: int) -> list[Batch]:
    batches, current, size = [], [], 0
    for m in messages:
        if current and (len(current) >= max_items or size + len(m.text) > max_chars):
            batches.append(Batch(session, tuple(current)))
            current, size = [], 0
        current.append(m)
        size += len(m.text)
    if current:
        batches.append(Batch(session, tuple(current)))
    return batches


@dataclass
class PushQueue:
    max_hold_s: float = DEFAULT_MAX_HOLD_S
    max_items: int = DEFAULT_MAX_ITEMS
    max_chars: int = DEFAULT_MAX_CHARS
    max_pending: int = DEFAULT_MAX_PENDING
    parallel: int = 1   # мест модели (ADR-020, п. 12): пачки сессии занимают их все, кроме одного под одиночное

    @property
    def lanes(self) -> int:
        """Мест под пачки: одно место из нескольких остаётся одиночному и срочному, чтобы не ждали пачек."""
        return max(self.parallel - 1, 1)
    _pending: dict[str, list[PushMessage]] = field(default_factory=dict)

    def deadline(self, message: PushMessage) -> float:
        return message.at + (1.0 - message.importance) * self.max_hold_s

    def push(self, message: PushMessage) -> list[Batch]:
        """Положить сообщение; alone уходит сразу один, срочное (важность 1) - сразу вместе с ждущими сессии."""
        message = PushMessage(message.source, message.text, message.session, clamp(message.importance), message.at,
                              message.delivery, message.folded, message.id, message.folded_ids, message.attempts)
        if message.delivery == ALONE:
            return [Batch(message.session, (message,))]
        if message.importance == 1:
            waiting = self._pending.pop(message.session, [])
            return _split(message.session, _ordered([*waiting, message]), self.max_items, self.max_chars,
                          self.lanes)
        pending = self._pending.setdefault(message.session, [])
        pending.append(message)
        if len(pending) > self.max_pending:
            self._pending[message.session] = fold(message.session, pending, self.max_pending)
        return []

    def requeue(self, batch: Batch) -> None:
        """Отложенная пачка (канал сессии занят) - обратно в ждущие; её срок уже прошёл, уйдёт первой же отправкой."""
        pending = self._pending.setdefault(batch.session, [])
        pending.extend(batch.messages)
        if len(pending) > self.max_pending:
            self._pending[batch.session] = fold(batch.session, pending, self.max_pending)

    def pending_count(self, session: str | None = None) -> int:
        if session is not None:
            return len(self._pending.get(session, []))
        return sum(len(m) for m in self._pending.values())

    def due(self, now: float) -> list[Batch]:
        """Созревшие пачки к моменту now: сессия, где срок самого раннего ждущего истёк, уходит целиком."""
        ready = []
        for session, messages in list(self._pending.items()):
            first = min(self.deadline(m) for m in messages)
            if first <= now:
                ready.append((first, _split(session, _ordered(messages), self.max_items, self.max_chars,
                                            self.lanes)))
                del self._pending[session]
        return [batch for _, batches in sorted(ready, key=lambda pair: pair[0]) for batch in batches]

    def next_deadline(self) -> float | None:
        """Ближайший срок среди ждущих; ждущих нет - None."""
        deadlines = [self.deadline(m) for messages in self._pending.values() for m in messages]
        return min(deadlines) if deadlines else None


PARALLEL_ENV = "SAM_PUSH_PARALLEL"


def from_env(environ: dict | None = None, parallel: int = 1) -> PushQueue:
    """parallel - мест очереди по умолчанию (SAM_PUSH_PARALLEL сильнее): пачки занимают все, кроме одного."""
    env = os.environ if environ is None else environ
    parallel = read(PARALLEL_ENV, parallel, parse_int, environ=env)
    return PushQueue(parallel=parallel, max_hold_s=read(MAX_HOLD_ENV, DEFAULT_MAX_HOLD_S, parse_float, environ=env),
                     max_items=read(MAX_ITEMS_ENV, DEFAULT_MAX_ITEMS, parse_int, environ=env),
                     max_chars=read(MAX_CHARS_ENV, DEFAULT_MAX_CHARS, parse_int, environ=env),
                     max_pending=read(MAX_PENDING_ENV, DEFAULT_MAX_PENDING, parse_int, environ=env))
