"""Планировщик лёгких workflow стенда (SAM-96, ADR-020): тик, ворота, шаги, at-most-once.

Раз в TICK_S шлюз смотрит, каким workflow пора. Образец - крон Hermes Agent (MIT,
oss-ref/hermes-agent):
- at-most-once: следующий запуск записывается в хранилище до старта шагов; не записался - запуск пропущен
  (cron/scheduler_tick.py:60-70), упавший посреди шагов процесс не повторит их;
- догоняющий запуск: опоздание в пределах половины периода (не меньше GRACE_MIN_S, не больше GRACE_MAX_S)
  запускает один раз, дальше - пропуск до следующего слота (cron/jobs.py:937-946);
- ворота без LLM: пустой вывод или тот же, что в прошлый раз, - модель не зовётся (tools/cronjob_tools.py,
  поле monitor); вывод ворот уходит шагу основной модели как повод.
Один процесс на стенд - аренда в Mongo (как блокировка gateway/dream.py): второй шлюз тикает вхолостую.

Запуск по событию (on.event): шина стенда зовёт on_event на каждом событии; workflow с подходящим событием и полями
запускается, если не идёт и с прошлого запуска по событию прошла пауза cooldown. Событие - повод шагу основной
модели, как вывод ворот. Шина бывает и в других потоках: запуск уходит в цикл планировщика потокобезопасно.

Решение «пора ли» (plan) и порядок шагов отделены от часов, Mongo и основной модели: всё приходит параметрами.

Тысячи workflow (ADR-020, п. 7, 9, 10): состояние читается из хранилища целиком при взятии аренды и дальше живёт в
памяти; ближайшие запуски - в куче, тик вынимает только созревшие; сдвиги созревших пишутся одной пачкой до старта
шагов (at-most-once прежний); событие находит подписчиков по словарю; выровненный cron получает постоянный сдвиг
в пределах минуты по имени workflow; замер тика - в status_tick() для /health.
"""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import string
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from . import radar_switch
from .env import FALSE_WORDS, env_bool, env_float, env_int
from .proc_tree import NEW_GROUP, kill_tree
from .workflow_spec import (ASK, BUILTIN_KEYS, COUNT_KEY, EVENT_PREFIX, MAIN_MODEL, PLACEHOLDER, REASON_KEY, RESULT_CONDITION, SCHEDULER_EVENT, Gate, Schedule,
                            Step, Workflow)

ENABLED = env_bool("SAM_WORKFLOWS", True)
# Путь к папке workflow; по умолчанию workflows/ в корне репозитория, как .github/workflows
DIR_ENV = "SAM_WORKFLOWS_DIR"
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "workflows"
# Тик раз в 2 с: пустой тик - одна запись аренды (сроки в куче, состояние в памяти), а частый тик разносит пики по
# сдвигу. Мок-тест 12 окт, 10 000 workflow: тик 30 с - пик начала часа держал цикл 237 мс, тик 2 с - 39 мс на p99
TICK_S = env_float("SAM_WORKFLOW_TICK_S", 2.0)
LEASE_S = TICK_S * 3          # аренда переживает два пропущенных тика, потом её берёт другой процесс
GRACE_MIN_S, GRACE_MAX_S = 120.0, 7200.0   # Hermes cron/jobs.py:_compute_grace_seconds
GATE_TIMEOUT_S = env_float("SAM_WORKFLOW_GATE_TIMEOUT_S", 10.0)   # ворота дешёвые: дольше - сбой ворот
# Сдвиг выровненного cron: тысячи «*/5» не стреляют в одну секунду (ADR-020, п. 7); меньше минуты - разрешения cron.
# Читает корень композиции, у планировщика по умолчанию 0
JITTER_S = env_float("SAM_WORKFLOW_JITTER_S", 50.0)
# Сколько ворот-команд идут одновременно (ADR-020, п. 8): каждая - процесс; на всплеске тысячи процессов разом
# кладут машину. Читает корень композиции, у планировщика по умолчанию - этот же предел
GATE_CONCURRENCY = env_int("SAM_WORKFLOW_GATE_CONCURRENCY", 4)
MS_PER_SECOND = 1000
# Тик отдаёт цикл событий каждые столько созревших: в начале часа разом созревают тысячи, и без уступки голосовой
# ход ждал бы весь тик (мок-тест 12 окт: 6 тыс. запусков - 237 мс подряд)
TICK_YIELD_EVERY = 256
EVENT = SCHEDULER_EVENT
SOURCE_PREFIX = "workflow"
REASON_HEADER = "Повод (проверка шлюза без LLM):"
EVENT_REASON_HEADER = "Событие стенда:"
EVENT_REASON_CHARS = 600      # поля события в поводе: имя, сессия и начало текста, не весь документ
# Сколько событий копит workflow с on.event.collect до следующего запуска: дальше старые теряются, пачка не растёт
# без конца, пока модель занята. 50 - как SAM_PUSH_MAX_ITEMS у очереди push
COLLECT_MAX = env_int("SAM_WORKFLOW_COLLECT_MAX", 50)
REPLY_PREVIEW = 300           # событие монитора несёт начало повода и ответа, не всё
COLLECTION = "_workflows"
LEASE_ID = "__lease__"


OFF_ENV = "SAM_WORKFLOWS_OFF"   # через запятую: имя workflow, t:<метка>, c:<категория> - выключить на этой машине
# Приставки выключения: короткая и полная равны (t:night = tag:night)
TAG_OFF, CATEGORY_OFF = ("t:", "tag:"), ("c:", "category:")
# До SAM-96 поздний час выключал SAM_HEARTBEAT=0 (10 окт ночью так выключен на стенде): 0 по-прежнему выключает его
LEGACY_OFF = {"SAM_HEARTBEAT": "late-night"}


def disabled(environ, warn: Callable[[str], None] = print) -> set[str]:
    names = {n.strip() for n in (environ.get(OFF_ENV) or "").split(",") if n.strip()}
    for var, name in LEGACY_OFF.items():
        if (environ.get(var) or "").strip().lower() in FALSE_WORDS:
            warn(f"[Workflow] {var}=0 устарел, выключает {name}; замена - {OFF_ENV}={name}")
            names.add(name)
    return names


def _off_values(off: set[str], prefixes: tuple[str, ...]) -> set[str]:
    return {item[len(p):].strip() for item in off for p in prefixes if item.startswith(p)}


def is_off(wf: Workflow, off: set[str]) -> bool:
    """Выключен ли workflow: по полному имени, по одной из меток (t:x, tag:x) или по категории (c:x, category:x)."""
    return (wf.name in off or bool(set(wf.tags) & _off_values(off, TAG_OFF))
            or wf.category in _off_values(off, CATEGORY_OFF) - {""})


def hidden(wf: Workflow, environ) -> bool:
    """Workflow категории радара при выключенном радаре не регистрируется: его как будто нет."""
    return wf.category == radar_switch.COMPONENT and not radar_switch.enabled(environ)


def labels(wf: Workflow) -> dict:
    """Категория и метки для /health и монитора; пустые не пишутся."""
    return {**({"category": wf.category} if wf.category else {}), **({"tags": list(wf.tags)} if wf.tags else {})}


def source(workflow: str, step: str) -> str:
    """Источник входа основной модели: видно, что это срабатывание и какого шага (ADR-020)."""
    return f"{SOURCE_PREFIX}:{workflow}/{step}"


def grace_s(schedule: Schedule, now: datetime) -> float:
    return max(GRACE_MIN_S, min(schedule.period_s(now) / 2, GRACE_MAX_S))


@dataclass(frozen=True)
class Plan:
    fire: bool
    next_run: datetime


def plan(schedule: Schedule, next_run: datetime | None, now: datetime) -> Plan:
    """Пора ли и когда следующий раз. Нового workflow не запускает сразу: рестарт шлюза не стреляет всеми."""
    if next_run is None:
        return Plan(False, schedule.next_after(now))
    if now < next_run:
        return Plan(False, next_run)
    late = (now - next_run).total_seconds()
    # окно догоняющего запуска не меньше GRACE_MIN_S: обычное опоздание на тик - запуск без расчёта периода (croniter)
    return Plan(late <= GRACE_MIN_S or late <= grace_s(schedule, now), schedule.next_after(now))


def event_reason(event_type: str, data: dict) -> str:
    """Событие шины - текст повода: имя и поля одной строкой JSON, без служебного msg_id."""
    fields = {k: v for k, v in data.items() if k != "msg_id"}
    return f"{event_type} {json.dumps(fields, ensure_ascii=False, default=str)}"[:EVENT_REASON_CHARS]


def gate_hash(output: str) -> str:
    return hashlib.sha256(output.encode("utf-8")).hexdigest()


def event_count(trigger: str | None) -> int:
    """Сколько событий в поводе: поводы пачки идут строками, а поля события - одной строкой JSON без переводов."""
    return len(trigger.splitlines()) if trigger else 0


def fill(value: Any, results: dict[str, str], reason: str | None) -> Any:
    """{{шаг}} - результат прежнего шага, {{reason}} - повод запуска (недоверенный текст события); строки внутри
    словарей и списков тоже. Ссылки проверены при разборе файла."""
    if isinstance(value, dict):
        return {k: fill(v, results, reason) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, results, reason) for v in value]
    if not isinstance(value, str):
        return value
    values = {COUNT_KEY: "0", **results, REASON_KEY: reason or ""}
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), value)


def referenced(step: Step) -> set[str]:
    """Прежние шаги, на результат которых ссылается шаг ({{шаг}} в тексте и with)."""
    texts = [step.text, repr(step.args or {})]   # YAML отдаёт даты объектами: json.dumps упал бы
    return {n for t in texts for n in PLACEHOLDER.findall(t) if n not in BUILTIN_KEYS and not n.startswith(EVENT_PREFIX)}


def missing_input(step: Step, results: dict[str, str]) -> str:
    """Шаг ссылается на пустой или пропущенный результат - его имя: такой шаг не идёт, иначе инструмент получил бы
    пустой текст (основная модель недоступна, шаг пропущен своим if)."""
    return next((name for name in sorted(referenced(step)) if not results.get(name, "").strip()), "")


def fill_step(step: Step, results: dict[str, str], reason: str | None) -> Step:
    """Шаг с подставленными результатами в тексте и в with."""
    return replace(step, text=fill(step.text, results, reason), args=fill(step.args, results, reason))


def step_text(step: Step, reason: str | None) -> str:
    return f"{step.text}\n\n{REASON_HEADER}\n{reason}" if reason else step.text


@dataclass(frozen=True)
class GateContext:
    now: datetime
    last_run: datetime | None     # прошлое срабатывание этого workflow; None - впервые
    # Снимок тика, общий для всех ворот одного тика (ADR-020, п. 8): дорогое чтение (журнал, Mongo) ворота кладут
    # сюда под своим ключом, и соседние ворота того же тика берут готовое
    shared: dict = field(default_factory=dict, compare=False, repr=False)
    event: dict = field(default_factory=dict, compare=False, repr=False)   # поля точки решения (on.consult)


@dataclass(frozen=True)
class StepOutcome:
    target: str                   # кому ушёл шаг: имя основной модели или инструмента
    reply: str | None = None
    fate: str = ""                # судьба сообщения шага с wait_ack: acked, not_acked, dropped, error


ACKED = "acked"
SKIPPED_STEP = "skipped"
NOOP = "noop"   # решение on.consult: ворота сказали не работать


TRAILING_MARKS = string.whitespace + ".!"   # «OK.» и «ok!» - то же, что «OK»

def _same(reply: str | None, wanted: str) -> bool:
    return (reply or "").strip(TRAILING_MARKS).casefold() == wanted.strip(TRAILING_MARKS).casefold()


def condition_met(condition: str, previous_fate: str, results: dict[str, str] | None = None) -> bool:
    """if шага: по судьбе сообщения предыдущего (acked - подтверждено; not_acked - была судьба, и не подтверждено)
    или по результату прежнего шага («verdict == OK», «verdict != OK»)."""
    if not condition:
        return True
    if m := RESULT_CONDITION.match(condition):
        name, op, wanted = m.groups()
        return _same((results or {}).get(name), wanted) == (op == "==")
    if condition == ACKED:
        return previous_fate == ACKED
    return bool(previous_fate) and previous_fate != ACKED


class Store(Protocol):
    def acquire_lease(self, owner: str, now: datetime, ttl_s: float) -> bool: ...
    def get(self, name: str) -> dict: ...
    def advance(self, name: str, next_run: datetime, last_run: datetime | None) -> None: ...
    def set_gate(self, name: str, digest: str) -> None: ...
    def load_all(self) -> dict[str, dict]: ...
    def advance_many(self, items: list[tuple[str, datetime, datetime | None]]) -> None: ...


class MemoryStore:
    """Состояние в памяти процесса: тесты и шлюз без Mongo (аренда тогда только на этот процесс)."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    def acquire_lease(self, owner, now, ttl_s) -> bool:
        return True

    def get(self, name):
        return dict(self.docs.get(name, {}))

    def advance(self, name, next_run, last_run):
        self.docs.setdefault(name, {}).update(next_run=next_run, last_run=last_run)

    def set_gate(self, name, digest):
        self.docs.setdefault(name, {})["gate"] = digest

    def load_all(self):
        return {name: dict(doc) for name, doc in self.docs.items()}

    def advance_many(self, items):
        for name, next_run, last_run in items:
            self.advance(name, next_run, last_run)


class MongoStore:
    """Документ на workflow (next_run, last_run, хэш ворот) и аренда тика - в коллекции _workflows."""

    def __init__(self, db) -> None:
        self._col = db[COLLECTION]

    def acquire_lease(self, owner, now, ttl_s) -> bool:
        from datetime import timedelta
        from pymongo.errors import DuplicateKeyError
        try:
            self._col.update_one(
                {"_id": LEASE_ID, "$or": [{"owner": owner}, {"until": {"$lt": now}}, {"until": None}]},
                {"$set": {"owner": owner, "until": now + timedelta(seconds=ttl_s)}}, upsert=True)
            return True
        except DuplicateKeyError:   # аренда у другого процесса и не истекла
            return False

    def get(self, name):
        doc = self._col.find_one({"_id": name}) or {}
        return {k: _aware(v) for k, v in doc.items()}

    def advance(self, name, next_run, last_run):
        self._col.update_one({"_id": name}, {"$set": {"next_run": next_run, "last_run": last_run}}, upsert=True)

    def set_gate(self, name, digest):
        self._col.update_one({"_id": name}, {"$set": {"gate": digest}}, upsert=True)

    def load_all(self):
        return {doc["_id"]: {k: _aware(v) for k, v in doc.items()}
                for doc in self._col.find({"_id": {"$ne": LEASE_ID}})}

    def advance_many(self, items):
        """Сдвиги пачкой: один запрос на тик вместо запроса на workflow."""
        if not items:
            return
        from pymongo import UpdateOne
        self._col.bulk_write([UpdateOne({"_id": n}, {"$set": {"next_run": nr, "last_run": lr}}, upsert=True)
                              for n, nr, lr in items], ordered=False)


def _aware(value):
    """Mongo отдаёт время без пояса (в UTC); планировщик считает в местном с поясом."""
    if isinstance(value, datetime) and value.tzinfo is None:
        from datetime import timezone
        return value.replace(tzinfo=timezone.utc).astimezone()
    return value


async def run_command_gate(command: str, timeout_s: float = GATE_TIMEOUT_S) -> str:
    """Ворота-команда: stdout - вывод; ненулевой код или таймаут - RuntimeError одной строкой."""
    proc = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE, **NEW_GROUP)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except asyncio.TimeoutError:
        kill_tree(proc.pid)          # оболочка и сама команда: иначе команда живёт дальше и держит каналы
        await proc.wait()
        raise RuntimeError(f"ворота {command!r} не ответили за {timeout_s:.0f} с") from None
    if proc.returncode:
        first = next((ln for ln in err.decode("utf-8", "replace").splitlines() if ln.strip()), "")
        raise RuntimeError(f"ворота {command!r} вышли с кодом {proc.returncode}: {first}")
    return out.decode("utf-8", "replace").strip()


def jitter_for(name: str, max_s: float) -> float:
    """Постоянный сдвиг workflow в [0, max_s) по имени: тот же после рестарта, у разных имён разный."""
    if max_s <= 0:
        return 0.0
    return int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:8], 16) % int(max_s * MS_PER_SECOND) / MS_PER_SECOND


def _jitter_span(schedule: Schedule, jitter_s: float) -> float:
    """Предел сдвига: у cron - jitter_s (в пределах минуты), у интервала - не больше периода."""
    return jitter_s if schedule.every_s is None else min(jitter_s, schedule.every_s)


@dataclass(frozen=True)
class Jittered:
    """Расписание со сдвигом. Cron: слоты плюс offset_s. Интервал: сдвигается только первый срок, дальше фаза
    держится сама; без сдвига все интервалы, заведённые одной загрузкой, стреляли бы в один тик всегда."""
    base: Schedule
    offset_s: float = 0.0

    def first_after(self, now: datetime) -> datetime:
        """Первый срок после загрузки: у интервала - со сдвигом, у cron - как следующий."""
        if self.base.every_s is not None:
            return self.base.next_after(now) + timedelta(seconds=self.offset_s)
        return self.next_after(now)

    def next_after(self, now: datetime) -> datetime:
        if not self.offset_s or self.base.every_s is not None:
            return self.base.next_after(now)
        offset = timedelta(seconds=self.offset_s)
        return self.base.next_after(now - offset) + offset

    def period_s(self, now: datetime) -> float:
        return self.base.period_s(now)


GateFn = Callable[[GateContext], str]
StepRunner = Callable[[Step, str, str, str], Awaitable[StepOutcome]]   # (шаг, источник, текст, сессия)


class Decisions:
    """Точки решения в коде (on.consult, ADR-020, п. 11): работать ли сейчас, решают ворота workflow этой точки.
    Без хранилища и цикла: решение синхронное, работает и без Mongo."""

    def __init__(self, workflows: list[Workflow], gates: dict[str, GateFn], emit: Callable[[str, dict], None],
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone(), log: Callable[[str], None] = print) -> None:
        self._gates, self._emit, self._now, self._log = gates, emit, now, log
        self._by_point: dict[str, list[Workflow]] = {}
        for wf in workflows:
            if not wf.consult:
                continue
            if wf.gate.fn not in gates:
                log(f"[Workflow] {wf.source_file} пропущен: нет ворот {wf.gate.fn}; есть: {', '.join(gates)}")
                continue
            self._by_point.setdefault(wf.consult, []).append(wf)

    def consult(self, point: str, data: dict) -> bool:
        """Пустой вывод любых ворот точки - noop, событие в шину; workflow на точке нет - решать нечего, работать."""
        ctx = GateContext(self._now(), None, event=dict(data))
        for wf in self._by_point.get(point, ()):
            try:
                out = (self._gates[wf.gate.fn](ctx) or "").strip()
            except Exception as e:   # сломанные ворота не решают за флоу: работать, но не молча
                self._log(f"[Workflow] {wf.name}: ворота {wf.gate.fn} упали ({e}), решение - работать")
                continue
            if not out:   # поля точки - первыми: служебные ключи события они не перетирают
                self._emit(EVENT, {**{k: str(v) for k, v in data.items()}, "workflow": wf.name, "consult": point,
                                   NOOP: True})
                return False
        return True

    def status(self) -> dict[str, list[str]]:
        """Для /health (ADR-020, п. 10): точка решения -> её workflow."""
        return {point: [wf.name for wf in wfs] for point, wfs in self._by_point.items()}


class Scheduler:
    """Тик: аренда, план каждого workflow, сдвиг следующего запуска до старта, запуск шагов отдельной задачей."""

    def __init__(self, workflows: list[Workflow], store: Store, gates: dict[str, GateFn],
                 run_step: StepRunner, emit: Callable[[str, dict], Any],
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 command_gate: Callable[[str], Awaitable[str]] = run_command_gate,
                 log: Callable[[str], None] = print, jitter_s: float = 0.0,
                 gate_concurrency: int = GATE_CONCURRENCY, clock: Callable[[], float] = time.monotonic) -> None:
        self._store, self._gates, self._run_step, self._emit = store, gates, run_step, emit
        self._now, self._command_gate, self._log = now, command_gate, log
        self._owner = uuid.uuid4().hex
        self._running: dict[str, asyncio.Task] = {}
        self._event_fired: dict[str, float] = {}   # имя -> monotonic последнего запуска по событию
        self._collected: dict[str, list[str]] = {}  # имя -> поводы событий, пришедших во время запуска или паузы
        self._flush_planned: set[str] = set()        # пачке уже назначен запуск: один таймер на workflow, не на событие
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clock = clock   # монотонные часы: пауза запуска по событию и замер тика
        # Решения on.consult - не запуски: их держит Decisions, планировщику они не нужны
        self.workflows = [wf for wf in workflows if not wf.consult and self._known_gate(wf)]
        self._by_name = {wf.name: wf for wf in self.workflows}
        self._schedules = {wf.name: Jittered(wf.schedule, jitter_for(wf.name, _jitter_span(wf.schedule, jitter_s)))
                           for wf in self.workflows if wf.schedule is not None}
        self._by_event: dict[str, list[Workflow]] = {}
        for wf in self.workflows:
            if wf.event is not None:
                self._by_event.setdefault(wf.event.name, []).append(wf)
        self._state: dict[str, dict] | None = None   # в памяти после взятия аренды (ADR-020, п. 9)
        self._heap: list[tuple[datetime, str]] = []
        self._tick_stats: dict = {}
        self._gate_concurrency = max(1, gate_concurrency)
        self._gate_slots: asyncio.Semaphore | None = None   # создаётся в цикле планировщика

    def _known_gate(self, wf: Workflow) -> bool:
        if wf.gate and wf.gate.fn and wf.gate.fn not in self._gates:
            self._log(f"[Workflow] {wf.source_file} пропущен: нет ворот {wf.gate.fn}; есть: {', '.join(self._gates)}")
            return False
        return True

    async def tick(self) -> list[str]:
        """Имена запущенных на этом тике workflow."""
        started = self._clock()
        now = self._now()
        if not await asyncio.to_thread(self._store.acquire_lease, self._owner, now, LEASE_S):
            self._state = None   # аренда у другого процесса: вернётся - состояние читается заново
            return []
        if self._state is None:
            await self._load(now)
        due = self._pop_due(now)
        updates, starts, shared = [], [], {}
        for i, wf in enumerate(due, 1):
            if i % TICK_YIELD_EVERY == 0:
                await asyncio.sleep(0)
            st = self._state.setdefault(wf.name, {})
            decision = plan(self._schedules[wf.name], st.get("next_run"), now)
            updates.append((wf.name, decision.next_run, now if decision.fire else st.get("last_run")))
            if decision.fire:
                starts.append((wf, GateContext(now, st.get("last_run"), shared), st.get("gate")))
        try:   # at-most-once: без записанного сдвига запуска нет
            if updates:
                await asyncio.to_thread(self._store.advance_many, updates)
        except Exception as e:
            self._log(f"[Workflow] сдвиги не записаны ({len(updates)}), запуски пропущены: {e}")
            for wf in due:   # прежний срок: следующий тик решит снова
                self._push(wf.name, self._state[wf.name].get("next_run"))
            updates, starts = [], []
        for name, next_run, last_run in updates:
            self._state[name].update(next_run=next_run, last_run=last_run)
            self._push(name, next_run)
        for i, (wf, ctx, last_gate) in enumerate(starts, 1):
            if i % TICK_YIELD_EVERY == 0:
                await asyncio.sleep(0)
            self._running[wf.name] = asyncio.create_task(self._guarded(wf, ctx, last_gate), name=f"workflow:{wf.name}")
        self._tick_stats = {"at": _iso(now), "due": len(due), "fired": len(starts), "queued": len(self._heap),
                            "ms": round((self._clock() - started) * MS_PER_SECOND, 1)}
        return [wf.name for wf, _, _ in starts]

    async def _load(self, now: datetime) -> None:
        """Состояние всех workflow одним чтением; новым - первый срок (не сразу) одной записью; куча сроков."""
        docs = await asyncio.to_thread(self._store.load_all)
        self._state = {wf.name: dict(docs.get(wf.name, {})) for wf in self.workflows}
        init, self._heap = [], []
        for name, schedule in self._schedules.items():
            st = self._state[name]
            if st.get("next_run") is None:   # рестарт шлюза не стреляет всеми: новый ждёт своего слота
                st["next_run"] = schedule.first_after(now)
                init.append((name, st["next_run"], st.get("last_run")))
            self._heap.append((st["next_run"], name))
        heapq.heapify(self._heap)
        try:
            if init:
                await asyncio.to_thread(self._store.advance_many, init)
        except Exception as e:   # первый срок - не запуск: не записался - посчитается при следующей загрузке
            self._log(f"[Workflow] первые сроки не записаны ({len(init)}): {e}")

    def _push(self, name: str, next_run: datetime | None) -> None:
        if next_run is not None:
            heapq.heappush(self._heap, (next_run, name))

    def _pop_due(self, now: datetime) -> list[Workflow]:
        """Созревшие workflow; идущий ещё раз - срок остаётся в куче, тик решит после его конца."""
        due, busy = [], []
        while self._heap and self._heap[0][0] <= now:
            item = heapq.heappop(self._heap)
            (busy if item[1] in self._running else due).append(item)
        for item in busy:
            heapq.heappush(self._heap, item)
        return [self._by_name[name] for _, name in due]

    def tick_status(self) -> dict:
        """Замер последнего тика для /health (ADR-020, п. 10): созрело, запущено, сроков в куче, мс."""
        return dict(self._tick_stats)

    def on_event(self, event_type: str, data: dict) -> list[str]:
        """Слушатель шины: имена workflow, запущенных этим событием. Вне цикла планировщика - ничего."""
        if self._loop is None:
            return []
        fired = []
        for wf in self._by_event.get(event_type, ()):
            if not wf.event.matches(event_type, data):
                continue
            last = self._event_fired.get(wf.name)
            if wf.name in self._running or (last is not None and self._clock() - last < wf.event.cooldown_s):
                if wf.event.collect:   # занят или пауза - событие ждёт следующего запуска в пачке
                    self._loop.call_soon_threadsafe(self._collect, wf, event_reason(event_type, data))
                continue
            self._event_fired[wf.name] = self._clock()
            self._loop.call_soon_threadsafe(self._start_event, wf, event_reason(event_type, data), data)
            fired.append(wf.name)
        return fired

    def _collect(self, wf: Workflow, reason: str) -> None:
        pending = self._collected.setdefault(wf.name, [])
        pending.append(reason)
        del pending[:-COLLECT_MAX]
        if wf.name not in self._running:   # пауза без идущего запуска: пачка уйдёт по её концу
            self._schedule_collected(wf)

    def _schedule_collected(self, wf: Workflow) -> None:
        if wf.name in self._flush_planned:
            return
        self._flush_planned.add(wf.name)
        last = self._event_fired.get(wf.name)
        delay = 0.0 if last is None else max(0.0, wf.event.cooldown_s - (self._clock() - last))
        self._loop.call_later(delay, self._flush_collected, wf)

    def _flush_collected(self, wf: Workflow) -> None:
        """Накопленные поводы - одним запуском; занят или пусто - ничего (конец запуска позовёт снова)."""
        self._flush_planned.discard(wf.name)
        if wf.name in self._running or not self._collected.get(wf.name):
            return
        reasons = self._collected.pop(wf.name)
        self._event_fired[wf.name] = self._clock()
        self._start_event(wf, "\n".join(reasons))

    def _start_event(self, wf: Workflow, reason: str, event: dict | None = None) -> None:
        if wf.name in self._running:
            return
        ctx = GateContext(self._now(), None)
        self._running[wf.name] = asyncio.create_task(self._guarded(wf, ctx, None, reason, event),
                                                     name=f"workflow:{wf.name}")

    async def _guarded(self, wf: Workflow, ctx: GateContext, last_gate: str | None,
                       trigger: str | None = None, event: dict | None = None) -> None:
        try:
            if trigger:   # запуск по событию тоже виден в /health: last_run пишется, следующий плановый не трогается
                state = self._state_of(wf.name)
                await asyncio.to_thread(self._store.advance, wf.name, state.get("next_run"), ctx.now)
                if self._state is not None:
                    self._state.setdefault(wf.name, {})["last_run"] = ctx.now
            await self.run_once(wf, ctx, last_gate, trigger, event)
        except Exception as e:   # сбой workflow не останавливает другие и следующий тик
            self._log(f"[Workflow] {wf.name} не выполнен: {e}")
            self._emit(EVENT, {"workflow": wf.name, "error": str(e).splitlines()[0] if str(e) else type(e).__name__})
        finally:
            self._running.pop(wf.name, None)
            if self._collected.get(wf.name) and self._loop is not None:
                self._schedule_collected(wf)

    async def run_once(self, wf: Workflow, ctx: GateContext, last_gate: str | None = None,
                       trigger: str | None = None, event: dict | None = None) -> list[StepOutcome]:
        """Ворота, затем шаги по очереди. Пустые или прежние ворота - [] без вызова модели.
        trigger - событие, запустившее workflow: повод шагу модели перед выводом ворот; event - его поля
        ({{event.поле}}), только у одиночного события."""
        reason = f"{EVENT_REASON_HEADER} {trigger}" if trigger else None
        if wf.gate:
            gate_out = await self._check_gate(wf.gate, ctx)
            if not gate_out:
                return []
            digest = gate_hash(gate_out)
            if digest == last_gate:
                return []
            await asyncio.to_thread(self._store.set_gate, wf.name, digest)
            if self._state is not None:
                self._state.setdefault(wf.name, {})["gate"] = digest
            reason = "\n".join(r for r in (reason, gate_out) if r)
        outcomes, previous_fate = [], ""
        results = {COUNT_KEY: str(event_count(trigger)),
                   **{f"{EVENT_PREFIX}{k}": str(v) for k, v in (event or {}).items() if isinstance(v, (str, int, float, bool))}}
        for step in wf.steps:
            src = source(wf.name, step.name)
            if not condition_met(step.condition, previous_fate, results):   # условный шаг (ADR-020, п. 6)
                self._emit(EVENT, {"workflow": wf.name, "step": step.name, "source": src,
                                   SKIPPED_STEP: f"if: {step.condition}, судьба прошлого шага: {previous_fate or 'нет'}"})
                continue
            if empty := missing_input(step, results):
                self._emit(EVENT, {"workflow": wf.name, "step": step.name, "source": src,
                                   SKIPPED_STEP: f"нет результата шага {empty}"})
                continue
            step = fill_step(step, results, reason)
            text = step_text(step, reason) if step.kind == MAIN_MODEL else ""
            outcome = await self._run_step(step, src, text, wf.session)
            results[step.name] = outcome.reply or ""
            self._emit(EVENT, {"workflow": wf.name, **labels(wf),
                               "step": step.name, "source": src, "kind": step.kind,
                               "target": outcome.target, "session": wf.session,
                               "reason": (reason or "")[:REPLY_PREVIEW], "reply": (outcome.reply or "")[:REPLY_PREVIEW]})
            outcomes.append(outcome)
            previous_fate = outcome.fate
        return outcomes

    async def _check_gate(self, gate: Gate, ctx: GateContext) -> str:
        if gate.command:
            if self._gate_slots is None:
                self._gate_slots = asyncio.Semaphore(self._gate_concurrency)
            async with self._gate_slots:   # процессов ворот одновременно не больше предела
                return await self._command_gate(gate.command)
        return (await asyncio.to_thread(self._gates[gate.fn], ctx) or "").strip()

    def status(self) -> list[dict]:
        """Для /health: когда каждый workflow запустится и запускался."""
        out = []
        for wf in self.workflows:
            st = self._state_of(wf.name)
            out.append({"name": wf.name, "next_run": _iso(st.get("next_run")), "last_run": _iso(st.get("last_run")),
                        "running": wf.name in self._running, **labels(wf),
                        **({"event": wf.event.name} if wf.event else {})})
        return out

    def _state_of(self, name: str) -> dict:
        """Состояние workflow: из памяти, пока аренда у нас; иначе из хранилища."""
        return dict(self._state.get(name, {})) if self._state is not None else self._store.get(name)

    async def run(self, interval_s: float = TICK_S) -> None:
        self._loop = asyncio.get_running_loop()   # запуск по событию из любого потока - в этот цикл
        while True:
            try:
                await self.tick()
            except Exception as e:   # Mongo пропала - следующий тик через interval_s
                self._log(f"[Workflow] тик не состоялся: {e}")
            await asyncio.sleep(interval_s)


def _iso(value) -> str | None:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else None


async def deliver_main_model(model, src: str, text: str, session: str,
                             run_turn: Callable[[str, str], Awaitable[str | None]], ids=()) -> StepOutcome:
    """Шаг основной модели (ADR-018): модель в шлюзе - её ход с источником в начале текста;
    модель вне шлюза (Claude Code) - вход через её deliver, тикер принесёт его тиком chat с полем source;
    ids сообщений очереди push едут с ним, модель подтверждает их (POST /sam/push/ack)."""
    if model.in_gateway:
        return StepOutcome(model.name, await run_turn(f"[{src}] {text}", session))
    model.deliver({"text": text, "source": src, "session": session, **({"push_ids": list(ids)} if ids else {})})
    return StepOutcome(model.name)
