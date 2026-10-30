"""Лёгкий workflow стенда (SAM-96, ADR-020): файл в репозитории, его разбор и расписание.

Файл YAML, как workflow GitHub Actions: имя, триггер `on.schedule` (cron-выражение или интервал «30m») и/или
`on.event` (событие шины стенда с фильтром полей и паузой между запусками),
необязательные ворота `gate` (дешёвая проверка без LLM) и шаги - запрос основной модели (`main_model`) или
инструмент SAM (`tool` с аргументами `with`). Формат - docs/features/workflows.md.

Модуль чистый: разбор получает словарь, расписание - время параметром; файлы читает только load_dir.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

FILE_GLOBS = ("*.yml", "*.yaml")
DEFAULT_SESSION = "voice"   # сессия, от лица которой звучит SAM: основная модель ведёт голосовую беседу (SAM-91)
MAIN_MODEL, TOOL, ASK = "main_model", "tool", "ask"
_INTERVAL = re.compile(r"^\s*(\d+)\s*([smhd])\s*$")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")   # имя идёт в источник workflow:<имя>/<шаг> - без пробелов и слэшей
# Категория - область SAM, к которой относится workflow (memory, radar, stand, skills, care): множество, workflow
# входит в одно или ни в одно. Метка - сквозное свойство поведения (proactive, speaks, llm, service), их сколько
# угодно, общие для разных категорий. Имена в том же виде, что имя workflow; списки не закрыты
# Событие самого планировщика (workflows.EVENT): workflow на нём запускал бы сам себя по кругу
SCHEDULER_EVENT = "workflow"     # событие шины о каждом шаге workflow (gateway/workflows.py)
PUSH_EVENT = "workflow_push"     # событие шины о каждой отправке очереди push (gateway/push_dispatch.py)
OWN_EVENTS = (SCHEDULER_EVENT, PUSH_EVENT)   # workflow на них запускал бы сам себя
# Пауза между запусками по событию, если в файле не задана: событие вроде user_message идёт на каждой фразе
DEFAULT_EVENT_COOLDOWN_S = 60.0
# Пауза меньше хода модели (4-13 с на GLM 12 окт) дала бы workflow, запущенному собственным ответом, крутиться
# быстрее, чем модель отвечает
MIN_EVENT_COOLDOWN_S = 5.0


class WorkflowError(ValueError):
    """Файл workflow не разобран; текст - одна строка: файл, поле, что не так."""


# Следующий слот cron по (выражение, минута): внутри минуты слотов cron нет, поэтому слот после любого момента минуты
# тот же. Выражений в доме единицы, workflow - тысячи: croniter строится раз на выражение и минуту, а не на каждый
# запуск (мок-тест 12 окт: 3 итератора на запуск, тик с 600 запусками - 2,9 с)
CRON_CACHE_SIZE = 4096


@functools.lru_cache(maxsize=CRON_CACHE_SIZE)
def _cron_next(cron: str, minute: datetime) -> datetime:
    from croniter import croniter
    return croniter(cron, minute).get_next(datetime)


@dataclass(frozen=True)
class Schedule:
    """Ровно одно из двух: cron-выражение (5 полей, местное время) или интервал в секундах."""
    cron: str | None = None
    every_s: float | None = None

    def next_after(self, now: datetime) -> datetime:
        if self.every_s is not None:
            return now + timedelta(seconds=self.every_s)
        return _cron_next(self.cron, now.replace(second=0, microsecond=0))

    def period_s(self, now: datetime) -> float:
        """Период: интервал или расстояние между двумя ближайшими запусками cron."""
        if self.every_s is not None:
            return self.every_s
        first = self.next_after(now)
        return (self.next_after(first) - first).total_seconds()


@dataclass(frozen=True)
class EventTrigger:
    """Событие шины стенда: имя, равенство полей (значения строками) и пауза между запусками."""
    name: str
    where: tuple[tuple[str, str], ...] = ()
    cooldown_s: float = DEFAULT_EVENT_COOLDOWN_S
    collect: bool = False         # события во время запуска и паузы копятся и идут следующим запуском одной пачкой

    def matches(self, event_type: str, data: dict) -> bool:
        return event_type == self.name and all(str(data.get(k)) == v for k, v in self.where)


@dataclass(frozen=True)
class Gate:
    """Дешёвая проверка без LLM: зарегистрированная функция шлюза (fn) или команда shell (command)."""
    fn: str | None = None
    command: str | None = None


@dataclass(frozen=True)
class Step:
    name: str
    kind: str                     # MAIN_MODEL, ASK или TOOL
    text: str = ""                # инструкция основной модели; у ASK - вопрос, ответ идёт следующим шагам
    tool: str = ""
    args: dict | None = None
    importance: float = 1.0       # шаг основной модели: 1 - срочно, ниже - может ждать попутчиков (push_queue)
    delivery: str = "batch"       # шаг основной модели: alone, batch или merge (push_queue)
    wait_ack_s: float = 0.0       # шаг основной модели: ждать судьбу своего сообщения столько секунд (ADR-020, п. 5)
    condition: str = ""           # if: acked или not_acked (судьба предыдущего) или «шаг == текст» / «шаг != текст»; пусто - всегда


@dataclass(frozen=True)
class Workflow:
    name: str
    schedule: Schedule | None
    steps: tuple[Step, ...]
    gate: Gate | None = None
    session: str = DEFAULT_SESSION
    source_file: str = ""
    event: EventTrigger | None = None
    category: str = ""
    tags: tuple[str, ...] = ()
    consult: str = ""             # точка решения в коде (on.consult): ворота отвечают, работать ли там; шагов нет


CONSULT_KEY = "consult"


def parse_interval(raw: str) -> float | None:
    m = _INTERVAL.match(str(raw))
    return int(m.group(1)) * _UNIT_S[m.group(2)] if m else None


def _schedule(raw, where: str) -> Schedule:
    if not isinstance(raw, dict):
        raise WorkflowError(f"{where}: on.schedule - словарь с cron или every")
    cron, every = raw.get("cron"), raw.get("every")
    if bool(cron) == bool(every):
        raise WorkflowError(f"{where}: on.schedule - ровно одно из cron и every")
    if every:
        seconds = parse_interval(every)
        if not seconds:
            raise WorkflowError(f"{where}: every={every!r} - ждали число с единицей s/m/h/d, например 30m")
        return Schedule(every_s=float(seconds))
    try:
        from croniter import croniter
    except ImportError:
        raise WorkflowError(f"{where}: cron без пакета croniter - pip install croniter") from None
    if not croniter.is_valid(str(cron)):
        raise WorkflowError(f"{where}: cron={cron!r} не разобран - ждали 5 полей, например */30 * * * *")
    return Schedule(cron=str(cron))


def _event(raw, where: str) -> EventTrigger:
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict) or not raw.get("name"):
        raise WorkflowError(f"{where}: on.event - имя события шины или словарь с name, where, cooldown")
    name = str(raw["name"])
    if name in OWN_EVENTS:
        raise WorkflowError(f"{where}: on.event={name!r} - событие планировщика или очереди, workflow зациклится")
    fields = raw.get("where") or {}
    if not isinstance(fields, dict):
        raise WorkflowError(f"{where}: on.event.where - словарь поле: значение")
    cooldown = DEFAULT_EVENT_COOLDOWN_S
    if raw.get("cooldown") is not None:
        cooldown = parse_interval(raw["cooldown"])
        if cooldown is None:
            raise WorkflowError(f"{where}: cooldown={raw['cooldown']!r} - ждали число с единицей s/m/h/d, например 1m")
        if cooldown < MIN_EVENT_COOLDOWN_S:
            raise WorkflowError(f"{where}: cooldown={raw['cooldown']!r} - не меньше {MIN_EVENT_COOLDOWN_S:g} с, иначе "
                                f"workflow на событии своего же шага крутится без остановки")
    return EventTrigger(name, tuple((str(k), str(v)) for k, v in fields.items()), float(cooldown),
                        collect=raw.get("collect") is True)


def _labels(data: dict, where: str) -> tuple[str, tuple[str, ...]]:
    """Категория - ноль или одна область; метки - любые сквозные свойства."""
    category = str(data.get("category") or "")
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        raise WorkflowError(f"{where}: tags - список меток")
    for label in ([category] if category else []) + [str(t) for t in tags]:
        if not _NAME.match(label):
            raise WorkflowError(f"{where}: метка {label!r} - строчные латинские буквы, цифры, - и _")
    return category, tuple(dict.fromkeys(str(t) for t in tags))


def _gate(raw, where: str) -> Gate | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = {"fn": raw}
    if not isinstance(raw, dict) or bool(raw.get("fn")) == bool(raw.get("command")):
        raise WorkflowError(f"{where}: gate - имя функции или словарь с ровно одним из fn и command")
    return Gate(fn=raw.get("fn"), command=raw.get("command"))


CONDITIONS = ("acked", "not_acked")   # if: шага (ADR-020, п. 6), как if у шага GitHub Actions
# if по результату шага: «verdict == OK», «verdict != OK» (имя прежнего шага, сравнение без регистра и знаков в конце)
RESULT_CONDITION = re.compile(r"^([a-z0-9][a-z0-9_-]*)\s*(==|!=)\s*(.+?)\s*$")
# Подстановка результата шага в текст и with: {{имя_шага}}; {{reason}} - повод запуска
PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9][a-z0-9_-]*(?:\.[a-z0-9_]+)?)\s*\}\}")
REASON_KEY = "reason"
COUNT_KEY = "count"   # {{count}} - сколько событий в поводе: 1 - одиночное, больше - пачка collect, 0 - по расписанию
BUILTIN_KEYS = (REASON_KEY, COUNT_KEY)
EVENT_PREFIX = "event."   # {{event.поле}} - поле одиночного события-повода; у пачки и по расписанию - пусто


def _condition(raw: dict, i: int, at: str) -> str:
    text = str(raw.get("if") or "").strip()
    value = text.replace(" ", "_")
    if value in CONDITIONS:
        pass
    elif RESULT_CONDITION.match(text):
        value = " ".join(RESULT_CONDITION.match(text).groups())
    elif value:
        raise WorkflowError(f"{at}: if={value!r} - одно из {', '.join(CONDITIONS)} или «шаг == текст», «шаг != текст»")
    if value and i == 0:
        raise WorkflowError(f"{at}: if у первого шага - условию не от чего зависеть")
    return value


def _check_references(steps: list[Step], where: str) -> None:
    """Ссылки {{шаг}} и if «шаг ==» - только на прежние шаги (или reason, count): иначе подстановке нечего брать."""
    seen: set[str] = set()
    for step in steps:
        at = f"{where}: шаг {step.name}"
        if step.name in BUILTIN_KEYS:
            raise WorkflowError(f"{at}: имя занято подстановкой {{{{{step.name}}}}} - назови шаг иначе")
        texts = [step.text, repr(step.args or {})]   # вложенные словари и списки тоже: подстановка рекурсивна
        for name in (m for t in texts for m in PLACEHOLDER.findall(t)):
            if name not in BUILTIN_KEYS and not name.startswith(EVENT_PREFIX) and name not in seen:
                raise WorkflowError(f"{at}: {{{{{name}}}}} - такого прежнего шага нет")
        m = RESULT_CONDITION.match(step.condition)
        if m and m.group(1) not in seen:
            raise WorkflowError(f"{at}: if ссылается на {m.group(1)!r} - такого прежнего шага нет")
        seen.add(step.name)


def _wait_ack(raw: dict, at: str) -> float:
    value = raw.get("wait_ack", 0)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise WorkflowError(f"{at}: wait_ack={value!r} - секунды числом") from None
    if seconds < 0:
        raise WorkflowError(f"{at}: wait_ack={value} - не меньше 0")
    return seconds


def _step(raw, i: int, where: str) -> Step:
    at = f"{where}: steps[{i}]"
    if not isinstance(raw, dict):
        raise WorkflowError(f"{at} - словарь с name и main_model или tool")
    name = str(raw.get("name") or f"step{i + 1}")
    if not _NAME.match(name):
        raise WorkflowError(f"{at}: name={name!r} - строчные латинские буквы, цифры, - и _")
    if sum(bool(raw.get(k)) for k in (MAIN_MODEL, TOOL, ASK)) != 1:
        raise WorkflowError(f"{at} - ровно одно из main_model (текст), ask (вопрос основной модели) и tool (имя инструмента)")
    if raw.get(ASK):
        return Step(name, ASK, text=str(raw[ASK]).strip(), condition=_condition(raw, i, at))
    if raw.get(MAIN_MODEL):
        return Step(name, MAIN_MODEL, text=str(raw[MAIN_MODEL]).strip(), importance=_importance(raw, at),
                    delivery=_delivery(raw, at), wait_ack_s=_wait_ack(raw, at), condition=_condition(raw, i, at))
    args = raw.get("with") or {}
    if not isinstance(args, dict):
        raise WorkflowError(f"{at}: with - словарь аргументов инструмента")
    if raw.get("wait_ack"):
        raise WorkflowError(f"{at}: wait_ack - только у шага main_model: судьба есть у сообщения модели")
    return Step(name, TOOL, tool=str(raw[TOOL]), args=args, condition=_condition(raw, i, at))


def _delivery(raw: dict, at: str) -> str:
    from .push_queue import BATCH, DELIVERIES
    value = str(raw.get("delivery") or BATCH)
    if value not in DELIVERIES:
        raise WorkflowError(f"{at}: delivery={value!r} - одно из {', '.join(DELIVERIES)}")
    return value


def _importance(raw: dict, at: str) -> float:
    value = raw.get("importance", 1)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise WorkflowError(f"{at}: importance={value!r} - число от 0 до 1") from None
    if not 0 <= value <= 1:
        raise WorkflowError(f"{at}: importance={value} - число от 0 до 1")
    return value


def parse_workflow(data, where: str) -> Workflow | None:
    """Словарь файла -> Workflow; enabled: false -> None. Ошибка - WorkflowError одной строкой."""
    if not isinstance(data, dict):
        raise WorkflowError(f"{where}: ждали словарь с name, on, steps")
    if data.get("enabled") is False:
        return None
    name = str(data.get("name") or "")
    if not _NAME.match(name):
        raise WorkflowError(f"{where}: name={name!r} - строчные латинские буквы, цифры, - и _")
    # PyYAML читает по YAML 1.1: ключ on без кавычек становится True (та же ловушка у файлов GitHub Actions)
    trigger = data.get("on", data.get(True))
    if not isinstance(trigger, dict) or not ({"schedule", "event", CONSULT_KEY} & set(trigger)):
        raise WorkflowError(f"{where}: нет on.schedule, on.event и on.consult - workflow не запустится")
    if CONSULT_KEY in trigger:
        return _consulted(data, trigger, name, where)
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowError(f"{where}: steps - непустой список")
    parsed = tuple(_step(s, i, where) for i, s in enumerate(steps))
    names = [s.name for s in parsed]
    if len(set(names)) != len(names):
        raise WorkflowError(f"{where}: имена шагов повторяются: {', '.join(names)}")
    _check_references(list(parsed), where)
    schedule = _schedule(trigger["schedule"], where) if "schedule" in trigger else None
    event = _event(trigger["event"], where) if "event" in trigger else None
    category, tags = _labels(data, where)
    return Workflow(name, schedule, parsed, _gate(data.get("gate"), where),
                    str(data.get("session") or DEFAULT_SESSION), where, event, category, tags)


def _consulted(data: dict, trigger: dict, name: str, where: str) -> Workflow:
    """Workflow-решение (ADR-020, п. 11): код в точке on.consult спрашивает его ворота, работать ли; шагов нет."""
    point = str(trigger[CONSULT_KEY] or "")
    if not _NAME.match(point) or set(trigger) != {CONSULT_KEY}:
        raise WorkflowError(f"{where}: on.consult - одно имя точки решения, без schedule и event")
    gate = _gate(data.get("gate"), where)
    if gate is None or not gate.fn:
        raise WorkflowError(f"{where}: у on.consult ворота - функция (решение синхронное, без процесса)")
    if data.get("steps"):
        raise WorkflowError(f"{where}: у on.consult шагов нет - решают ворота")
    category, tags = _labels(data, where)
    return Workflow(name, None, (), gate, str(data.get("session") or DEFAULT_SESSION), where, None, category, tags,
                    consult=point)


def load_dir(path: Path, warn: Callable[[str], None] = print) -> list[Workflow]:
    """Все workflow папки; битый файл или повтор имени - строка в warn, остальные работают."""
    import yaml
    found: dict[str, Workflow] = {}
    files = sorted(f for g in FILE_GLOBS for f in path.glob(g)) if path.is_dir() else []
    for f in files:
        try:
            wf = parse_workflow(yaml.safe_load(f.read_text(encoding="utf-8")), f.name)
        except (WorkflowError, yaml.YAMLError) as e:
            warn(f"[Workflow] {f.name} пропущен: {str(e).splitlines()[0]}")
            continue
        if wf is None:
            continue
        if wf.name in found:
            warn(f"[Workflow] {f.name} пропущен: имя {wf.name} уже у {found[wf.name].source_file}")
            continue
        found[wf.name] = wf
    return list(found.values())
