"""Агенты SAM для основной модели (SAM-91): создать агента, ответить в его сессию; вопрос агента - создателю.

Основная модель вне шлюза (сессия Claude Code, in_gateway=False) создаёт агента SAM, агент спрашивает её о контексте,
она отвечает в его сессию `task:<id>`, агент продолжает. Ядро - функции *_impl(params, deps) без
FastAPI и глобалов шлюза, как остальные gateway/tools/*: POST /sam/agents и /sam/inject - тонкие обёртки над ними,
MCP-обёртка инструментов шлюза ляжет рядом. Зависимости собирает корень композиции (gateway/agent.py).

Ответ - словарь со status ok или error; у ошибки reason (bad_request, busy, unavailable, not_found) и одна строка error.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..agent_levels import LEVEL_ENV_PREFIX, level_source  # noqa: F401  (имена остаются на gateway.tools.agents)
from ..env import env_int

# Кто создал задачу: основная модель снаружи шлюза; у spawn_agent - "orchestrator" (gateway/tasks.py new_task)
CREATOR_MAIN_MODEL = "main_model"
# Метки сложности: кто ставит задачу, тот выставляет; по метке - модель из конфига
# Каркас агента (ADR-022, «независимость от каркаса»): воркер шлюза или сессия Cortex (SAM-133); по умолчанию -
# SAM_AGENT_FRAMEWORK, иначе воркер шлюза, пока Cortex не прошёл живой прогон
FRAMEWORK_SAM, FRAMEWORK_CORTEX = "sam", "cortex"
FRAMEWORKS = (FRAMEWORK_SAM, FRAMEWORK_CORTEX)
FRAMEWORK_ENV = "SAM_AGENT_FRAMEWORK"
LEVELS = ("smart", "medium", "weak")
DEFAULT_LEVEL = "medium"
# Инструменты, которыми агент говорит с создателем: есть при любых правах
FROM_QUEUE = "_from_queue"   # запуск из очереди отложенных: отказ лимита не откладывает повторно
ASK_TOOL = "ask_creator"
STATUS_TOOL = "report_status"
# Шаги агента (gateway/agent_budget.py): оценка и просьба - у агента при любых правах, как связь с создателем
ESTIMATE_TOOL, REQUEST_STEPS_TOOL = "estimate_steps", "request_steps"
VOICE_TOOLS = (ASK_TOOL, STATUS_TOOL, ESTIMATE_TOOL, REQUEST_STEPS_TOOL)
TASK_SESSION_PREFIX = "task:"
SHORT_ID_LEN = 8                 # короткий id задачи, как в мониторе и task:<id>
# Источник сообщения во входящих основной модели (тик kind chat, voice/layer/ticker.py inbox_ticks)
QUESTION_SOURCE = "agent_question"
DONE_SOURCE = "agent_done"
# Дерево агентов Cortex (ADR-022, «Агент создаёт агента»): уровни от основной модели (нулевой); её агент создаёт
# не больше MAX_CHILDREN агентов, те - ни одного. Дерево растёт - растёт и расход моделей
MAX_DEPTH = env_int("SAM_AGENT_MAX_DEPTH", 1)
MAX_CHILDREN = env_int("SAM_AGENT_MAX_CHILDREN", 1)
DESCRIPTION_PREVIEW = 300        # сколько задачи показать в вопросе: основной модели хватает напомнить, о чём агент
RESULT_PREVIEW = 500             # итог агента в сообщении о завершении - столько же хранит задача (worker.py)

BAD_REQUEST, BUSY, UNAVAILABLE, NOT_FOUND = "bad_request", "busy", "unavailable", "not_found"


def _error(reason: str, text: str, **extra) -> dict:
    return {"status": "error", "reason": reason, "error": text, **extra}


def task_session(short_id: str) -> str:
    return f"{TASK_SESSION_PREFIX}{short_id}"


def live_creator(creator: str, running: Callable[[str], bool], creator_of: Callable[[str], str]) -> str:
    """Кому идут вопрос, запрос права и итог агента (ADR-022, «Создатель кончился»): создатель-агент `task:<id>`, если
    его задача идёт, иначе ближайший живой выше по цепочке, в конце - основная модель. Создатель не агент - он сам.
    running(сессия) - идёт ли задача; creator_of(короткий id) - создатель задачи, пусто - задачи нет."""
    seen: set[str] = set()
    while creator.startswith(TASK_SESSION_PREFIX):
        if running(creator):
            return creator
        if creator in seen:   # петля в цепочке - наверх, к основной модели
            return CREATOR_MAIN_MODEL
        seen.add(creator)
        creator = creator_of(creator[len(TASK_SESSION_PREFIX):]) or CREATOR_MAIN_MODEL
    return creator


def is_ancestor(agent: str, short_id: str, creator_of: Callable[[str], str]) -> bool:
    """Агент (короткий id) - создатель задачи short_id или выше по цепочке: запрос права ребёнка, поднятый к живому
    предку, тот и решает (ADR-022, «Создатель кончился»)."""
    seen: set[str] = set()
    creator = creator_of(short_id) or ""
    while creator.startswith(TASK_SESSION_PREFIX) and creator not in seen:
        if creator == task_session(agent):
            return True
        seen.add(creator)
        creator = creator_of(creator[len(TASK_SESSION_PREFIX):]) or ""
    return False


PEER_NOT_RUNNING = "Агент {id} сейчас не работает: сообщение не доставлено"
PEER_DELIVERED = "Доставлено агенту {id}"


def send_to_peer_impl(sender: str, to: str, message: str, running: Callable[[str], bool],
                      inject: Callable[[str, str], None]) -> str:
    """Сообщение агенту от агента: только в идущую задачу с точным id, без подстроки и без воскрешения - иначе
    собеседник попал бы в чужую задачу или поднял закончившуюся (ADR-022, «Кому писать»)."""
    session = task_session(to)
    if not running(session):
        return PEER_NOT_RUNNING.format(id=to)
    inject(session, PEER_MESSAGE.format(sender=sender, message=message))
    return PEER_DELIVERED.format(id=to)


def cortex_grant(requested: list | None) -> tuple[list[str] | None, str]:
    """Выдача агенту Cortex: список имён инструментов MCP как есть; не список строк - ошибка."""
    if requested is None:
        return None, ""
    if not isinstance(requested, list) or not all(isinstance(t, str) for t in requested):
        return None, "tools - список имён инструментов MCP"
    return list(requested), ""


def choose_tools(requested: list | None, allowed: list[str]) -> tuple[list[str] | None, str]:
    """Права агента: подмножество инструментов воркера. None - все. Связь с создателем есть всегда.
    Возвращает (инструменты, ошибка)."""
    if requested is None:
        return list(allowed), ""
    if not isinstance(requested, list) or not all(isinstance(t, str) for t in requested):
        return None, "tools - список имён инструментов"
    unknown = [t for t in requested if t not in allowed]
    if unknown:
        return None, f"нет инструментов воркера: {', '.join(unknown)}; есть: {', '.join(allowed)}"
    return [t for t in allowed if t in requested or t in VOICE_TOOLS], ""


# ─── Создать агента ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentSpec:
    """Что запустить: воркер SAM тем же путём, что spawn_agent (gateway/agent.py _launch_worker)."""
    description: str
    device: str
    name: str
    level: str
    tools: list[str]
    llm_config: dict
    parent_session: str
    creator: str = CREATOR_MAIN_MODEL
    framework: str = FRAMEWORK_SAM
    peers: tuple[str, ...] = ()          # кому агент может писать (ADR-022, «Кому писать»): id задач от вызывающего
    depth: int = 0                       # поколений детей под агентом Cortex, не больше MAX_DEPTH
    children: int = 0                    # сколько детей агент может создать, не больше MAX_CHILDREN
    steps: int | None = None             # прикидка создателя, сколько шагов на задачу (gateway/agent_budget.py)
    kind: str = ""                       # вид работы от создателя (gateway/task_class.KINDS): выбор модели и статистика
    env: str = ""                        # inside - внутри экосистемы SAM, outside - снаружи (gateway/task_class.ENVS)


CORTEX_NEEDS_GRANT = ("каркас cortex - только с явной выдачей инструментов: без неё сессия получила бы все инструменты "
                      "стенда (ADR-022, «Края»; пустая выдача - этап 4 SAM-133)")


def framework_error(framework: str, tools: list[str] | None) -> str:
    """Каркас без нужного условия - почему нельзя; пусто - можно."""
    return CORTEX_NEEDS_GRANT if framework == FRAMEWORK_CORTEX and not tools else ""


def _steps(raw) -> int | None:
    """Прикидка шагов создателя: целое больше 0; иначе - умолчание пула."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def pick_depth(raw) -> tuple[int, str]:
    """Глубина детей из параметра: целое от 0, больше MAX_DEPTH - срезается до него; (глубина, ошибка)."""
    if raw is None or raw == "":
        return 0, ""
    try:
        depth = int(raw)
    except (TypeError, ValueError):
        return 0, f"depth - целое число поколений детей, от 0 до {MAX_DEPTH}"
    return min(max(depth, 0), MAX_DEPTH), ""


def pick_framework(raw: str | None) -> tuple[str, str]:
    """Каркас из параметра или SAM_AGENT_FRAMEWORK; (каркас, ошибка - пусто, если всё верно)."""
    value = str(raw or os.getenv(FRAMEWORK_ENV, "") or FRAMEWORK_SAM).strip().lower()
    if value not in FRAMEWORKS:
        return FRAMEWORK_SAM, f"каркас {value!r} неизвестен; есть: {', '.join(FRAMEWORKS)}"
    return value, ""


@dataclass
class CreateDeps:
    worker_tools: list[str]                                  # всё, что можно дать агенту
    running_workers: Callable[[], int]
    max_workers: int
    resolve_device: Callable[[str | None], str | None]       # None - нет online устройств
    llm_for_level: Callable[[str, int | None, str, str], Awaitable[dict | None]]   # метка, прикидка шагов, вид, среда
    launch: Callable[[AgentSpec], str]                       # -> полный id задачи
    parent_session: str                                      # куда идут события агента в мониторе
    store_ok: bool = True                                    # MongoDB на связи
    creator: str = CREATOR_MAIN_MODEL                        # кто создаёт: основная модель или агент task:<id>
    budget_gate: Callable[[str, int | None], tuple[bool, str]] = lambda description, steps: (True, "")   # лимит тяжёлых задач (SAM-260)
    defer: Callable[[dict], str] | None = None       # тяжёлая задача при лимите встаёт в очередь как есть, возвращает текст ответа


async def create_agent_impl(params: dict, deps: CreateDeps) -> dict:
    """Новый агент SAM: задача, метка сложности (level), права (tools), опц. device и name."""
    description = str(params.get("description") or "").strip()
    if not description:
        return _error(BAD_REQUEST, "нужна description: что сделать агенту")
    level = str(params.get("level") or DEFAULT_LEVEL).strip().lower()
    if level not in LEVELS:
        return _error(BAD_REQUEST, f"level {level!r} неизвестна; есть: {', '.join(LEVELS)}")
    framework, error = pick_framework(params.get("framework"))
    if error:
        return _error(BAD_REQUEST, error)
    depth, error = pick_depth(params.get("depth"))
    if error:
        return _error(BAD_REQUEST, error)
    # Выдача Cortex - в именах инструментов MCP стенда (radar_where), не воркера шлюза (mcp_call): невыданное и
    # неизвестное Cortex отфильтрует сам, каналы связи у сессии с создателем есть всегда (SAM-133 этап 4)
    tools, err = (cortex_grant(params.get("tools")) if framework == FRAMEWORK_CORTEX
                  else choose_tools(params.get("tools"), deps.worker_tools))
    if err:
        return _error(BAD_REQUEST, err)
    if not deps.store_ok:
        return _error(UNAVAILABLE, "MongoDB недоступна: задачу негде хранить")
    busy = deps.running_workers()
    if busy >= deps.max_workers:
        return _error(BUSY, f"все {deps.max_workers} воркера заняты ({busy} работают): дождись итога или отмени задачу")
    allowed, reason = deps.budget_gate(description, _steps(params.get("steps")))
    if not allowed:
        if deps.defer is not None and not params.get(FROM_QUEUE):   # принято как есть, запустится само (SAM-260)
            return {"status": "ok", "queued": True, "task_id": "", "note": deps.defer(params)}
        return _error(BUSY, reason)
    device = deps.resolve_device(params.get("device") or None)
    if not device:
        return _error(UNAVAILABLE, "нет online устройств для агента")
    kind, env = (str(params.get(k) or "").strip().lower() for k in ("kind", "env"))
    llm_config = await deps.llm_for_level(level, _steps(params.get("steps")), kind, env)
    if not llm_config:
        return _error(UNAVAILABLE, f"нет доступной модели для метки {level}: проверь {LEVEL_ENV_PREFIX}{level.upper()}")
    if error := framework_error(framework, tools):
        return _error(BAD_REQUEST, error)
    task_id = deps.launch(AgentSpec(description=description, device=device, name=str(params.get("name") or "").strip(),
                                    level=level, tools=tools, llm_config=llm_config,
                                    parent_session=deps.parent_session, framework=framework, creator=deps.creator,
                                    peers=tuple(p for p in (str(p).strip() for p in params.get("peers") or []) if p),
                                    depth=depth if framework == FRAMEWORK_CORTEX else 0,
                                    children=MAX_CHILDREN if framework == FRAMEWORK_CORTEX and depth else 0,
                                    steps=_steps(params.get("steps")), kind=kind, env=env))
    short_id = task_id[:SHORT_ID_LEN]
    return {"status": "ok", "task_id": short_id, "session": task_session(short_id), "level": level,
            "tools": tools, "device": device, "model": llm_config.get("model", "")}


# ─── Ответить в сессию агента ─────────────────────────────────────────────────

@dataclass
class AnswerDeps:
    running_sessions: Callable[[], list[str]]                # сессии работающих воркеров: task:<short>
    inject: Callable[[str, str], None]                       # в работающего: прочтёт следующим шагом как [INJECT]
    find_idle: Callable[[str], dict | None]                  # сохранённая сессия завершившегося (worker_session)
    resume: Callable[[str, str], None]                       # (полный id, сообщение): воскресить с историей (SAM-38)


def answer_agent_impl(params: dict, deps: AnswerDeps) -> dict:
    """Сообщение в сессию агента task:<id>: работающему - в ход, завершившемуся - воскрешение с ответом."""
    sid = str(params.get("session_id") or "").strip()
    msg = str(params.get("message") or "")
    prefix = sid.split(":", 1)[1].strip() if sid.startswith(TASK_SESSION_PREFIX) else ""
    if not prefix:
        # Пустой префикс входит в имя любой задачи (SAM-76)
        return {"message": msg, "status": "error", "reason": BAD_REQUEST,
                "error": "Empty task id: use task:<id prefix>"}
    for running in deps.running_sessions():
        if running == sid or prefix in running:
            deps.inject(sid, msg)
            return {"message": msg, "status": "ok", "kind": "running_inject"}
    session = deps.find_idle(prefix)
    if not session:
        return {"message": msg, "status": "error", "reason": NOT_FOUND,
                "error": "Task not found (not running, no archived session)"}
    if session.get("status") == "dead":
        return {"message": msg, "status": "error", "reason": NOT_FOUND, "error": "Task is dead, cannot reanimate"}
    deps.resume(session["_id"], msg)
    return {"message": msg, "status": "ok", "kind": "reanimated", "task_id": session["_id"][:SHORT_ID_LEN]}


# ─── Вопрос агента создателю ──────────────────────────────────────────────────

@dataclass
class AskDeps:
    add_question: Callable[[str, str], dict | None]          # (короткий id, вопрос) -> задача после записи
    emit: Callable[[str, dict], None]                        # шина событий: монитор
    deliver: Callable[[dict], None]                          # во входящие основной модели (ConcreteModel.deliver)
    route: Callable[[str], str] = lambda creator: creator    # создатель задачи -> кому отвечать (live_creator)
    to_agent: Callable[[str, str], None] = lambda session, text: None   # во входящие агента task:<id>
    # Создатель - модель в шлюзе (orchestrator, spawn_agent из хода): (короткий id, сессия хода, вопрос) -> её ход,
    # ответ которого уходит агенту (SAM-212: без него вопрос видел только монитор)
    to_gateway_model: Callable[[str, str, str], None] = lambda short_id, session, text: None


QUESTION_MARK, DONE_MARK, PEER_MARK = "[вопрос агента ", "[TASK_DONE:", "[от агента "   # начала сообщений агентам
AGENT_QUESTION = QUESTION_MARK + "{id}{name}] {question}\nОтвет - send_to_agent(to={id}, message=...)"
GATEWAY_QUESTION = (QUESTION_MARK + "{id}{name}] {question}\nТвой ответ этим ходом уйдёт агенту, не человеку: "
                    "ответь ему по делу; шаги - grant_steps или отказ с причиной, молчать нельзя.")
CREATOR_REPLY = "Ответ создателя: {reply}"
CREATOR_SILENT = "Создатель ответил без текста: решай сам по тому, что знаешь"


def creator_reply(reply: str | None) -> str:
    """Ответ модели-создателя в шлюзе для агента: пустой ответ - тоже ответ, агент не ждёт до срока."""
    text = (reply or "").strip()
    return CREATOR_REPLY.format(reply=text) if text else CREATOR_SILENT
AGENT_DONE = DONE_MARK + "{id}] Агент{name} закончил: {result}"
PEER_MESSAGE = PEER_MARK + "{sender}] {message}"
AGENT_MARKS = (QUESTION_MARK, DONE_MARK, PEER_MARK)


def peer_mark(sender: str) -> str:
    """Начало письма агента sender: по нему видно, ответил ли спрошенный."""
    return f"{PEER_MARK}{sender}]"


def forward_orphan(msg: str, short_id: str, target: str, inject: Callable[[str, str], None],
                   deliver: Callable[[dict], None]) -> bool:
    """Итог или вопрос агента, пришедший закрытой задаче, - дальше по цепочке (ADR-022, «Создатель кончился»):
    живому агенту-предку в сессию, основной модели во входящие. False - не агентское или доставить некуда."""
    if not msg.startswith((DONE_MARK, QUESTION_MARK)):
        return False
    if target.startswith(TASK_SESSION_PREFIX):
        inject(target, msg)
        return True
    if target == CREATOR_MAIN_MODEL:
        source = DONE_SOURCE if msg.startswith(DONE_MARK) else QUESTION_SOURCE
        deliver({"text": msg, "source": source, "session": task_session(short_id), "task_id": short_id})
        return True
    return False


def _name(task: dict) -> str:
    return f" ({task['name']})" if task.get("name") else ""


def question_message(task: dict, short_id: str, question: str) -> dict:
    """Вопрос агента для основной модели: что за агент, о чём задача, куда отвечать."""
    name = _name(task)
    session = task_session(short_id)
    text = (f"Агент {short_id}{name} спрашивает: {question}\n"
            f"Задача: {str(task.get('description', ''))[:DESCRIPTION_PREVIEW]}\n"
            f"Ответ - POST /sam/inject session_id={session}")
    return {"text": text, "source": QUESTION_SOURCE, "session": session, "task_id": short_id}


def done_message(task: dict, short_id: str, result: str) -> dict:
    """Итог агента для основной модели; заданные им вопросы видны при завершении."""
    name = _name(task)
    lines = [f"[TASK_DONE:{short_id}] Агент{name} закончил: {result[:RESULT_PREVIEW]}"]
    asked = [q.get("text", "") for q in task.get("questions") or []]
    if asked:
        lines.append("Он спрашивал: " + " | ".join(asked))
        lines.append(f"Ответ с контекстом воскресит его: POST /sam/inject session_id={task_session(short_id)}")
    return {"text": "\n".join(lines), "source": DONE_SOURCE, "session": task_session(short_id), "task_id": short_id}


def ask_creator_impl(params: dict, parent_session: str, short_id: str, deps: AskDeps) -> str:
    """Вопрос воркера создателю. Воркер не ждёт: ответ придёт [INJECT]-ом или воскресит его."""
    question = str(params.get("question") or "").strip()
    if not question:
        return "Нужен question: что уточнить у создателя"
    if not short_id:
        return "ask_creator работает только внутри агента"
    task = deps.add_question(short_id, question)
    if task is None:
        return f"Задача {short_id} не найдена: вопрос не записан"
    deps.emit("task_message", {"content": f"Вопрос создателю: {question}", "session": parent_session,
                               "task_id": short_id, "kind": "question"})
    target = deps.route(str(task.get("creator") or ""))
    if target == CREATOR_MAIN_MODEL:
        deps.deliver(question_message(task, short_id, question))
    elif target.startswith(TASK_SESSION_PREFIX):
        deps.to_agent(target, AGENT_QUESTION.format(id=short_id, name=_name(task), question=question))
    else:
        deps.to_gateway_model(short_id, parent_session,
                              GATEWAY_QUESTION.format(id=short_id, name=_name(task), question=question))
    return ("Вопрос отправлен создателю. Ответ придёт сообщением [INJECT]; делай пока то, что можно без него. "
            "Без ответа никак - заверши report_status(\"ОТВЕТ: ...\") с тем, что успел, и вопросом: "
            "ответ воскресит тебя с твоей историей.")


def done_router(deliver: Callable[[dict], None], get_task: Callable[[str], dict | None],
                fallback: Callable[[str, str, str, dict], None], route: Callable[[str], str] = lambda creator: creator,
                to_agent: Callable[[str, str], None] = lambda session, text: None,
                ) -> Callable[[str, str, str, dict], None]:
    """auto_respond воркера (gateway/worker.py): итог - основной модели во входящие, агенту-создателю (или живому
    выше по цепочке) - в его сессию; создатель orchestrator - как было."""
    def _respond(short_id: str, result: str, parent_session: str, llm_config: dict) -> None:
        task = get_task(short_id)
        target = route(str(task.get("creator") or "")) if task else ""
        if target == CREATOR_MAIN_MODEL:
            deliver(done_message(task, short_id, result))
            return
        if target.startswith(TASK_SESSION_PREFIX):
            to_agent(target, AGENT_DONE.format(id=short_id, name=_name(task), result=result[:RESULT_PREVIEW]))
            return
        fallback(short_id, result, parent_session, llm_config)
    return _respond

