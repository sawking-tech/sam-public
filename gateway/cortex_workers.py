"""Агенты SAM в сессиях Cortex (ADR-022 «Агенты в Cortex», SAM-133 этап 3).

spawn_agent с каркасом `cortex` создаёт задачу как обычно (Mongo, TTL, монитор), а работу ведёт сессия Cortex за ACP:
права создателя - в `_meta` сессии (cortex/acp/rights.py), вопрос и статус агента приходят расширением
`_sam/agent_event` и идут теми же функциями, что у воркеров шлюза; ответ создателя на вопрос - инъекцией в идущий ход
(`_sam/inject`); итог - тем же путём, что итог воркера. Зависимости приходят параметрами: логика проверяется без
процесса Cortex и Mongo.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .acp.prompt import TEXT
from .env import env_float, env_int
from .tools.agents import AGENT_MARKS, TASK_SESSION_PREFIX, is_ancestor, peer_mark

INBOX_POLL_S = env_float("SAM_CORTEX_INBOX_POLL_S", 1.0)   # создатель отвечает ходом модели (4-13 с): раз в секунду хватает
ASK, STATUS, PERMISSION, SEND = "ask", "status", "permission", "send"   # виды события канала (cortex/acp/channels.KIND)
SPAWN, GRANT_CHILD = "spawn", "grant_child"
# Сколько агент ждёт сообщения между ходами (итог ребёнка, ответ на вопрос): ребёнок работает минутами, создатель
# отвечает ходом модели (4-13 с); дольше - задача закрывается с итогом последнего хода (ADR-022, «Создатель кончился»)
WAIT_S = env_float("SAM_CORTEX_WAIT_S", 600.0)
CONTINUE = "Твоя задача: {description}\nСделано прошлым ходом: {result}\nНовое сообщение: {message}\nПродолжай задачу."
CANCELLED = "агент отменён; последний ход: {last}"
LOG_PREVIEW = 120   # сколько реплики показать в логе
# Ходов на задачу: родителю с ребёнком хватает трёх (создать, ответить, принять итог); запас на вопросы создателю,
# а петля агентов, будящих друг друга, кончается здесь
MAX_TURNS = env_int("SAM_CORTEX_TASK_TURNS", 10)   # ходы задачи; шаги одного хода - SAM_CORTEX_MAX_TURNS (cortex/acp/config.py)
PEER_PREFIX = "agent:"   # право писать агенту в выдаче (cortex/acp/rights.PEER_PREFIX)
GRANT_METHOD = "_sam/grant"   # расширение ACP Cortex: выдать инструмент сессии (cortex/acp/agent.py)
PERMISSION_QUESTION = ("Агент просит инструмент {tool}: {why}. Выдать - grant_agent_tool(task_id={id}, tool={tool}); "
                       "без ответа он продолжит без него")
CHILD_PERMISSION_QUESTION = ("Твой ребёнок {id} просит {tool}: {why}. Выдать - grant_child(task_id={id}, tool={tool}); "
                             "чего у тебя нет - сначала request_permission у своего создателя")
SPAWNED = "Агент {id} создан; его итог придёт тебе [TASK_DONE:{id}], писать ему - send_to_agent(to={id})"
NO_MORE_CHILDREN = "Создавать агентов тебе больше нельзя"
WIDER_CHILD = "Ребёнку нельзя шире твоих прав: {names} нет в твоей выдаче"
# Ключи `_meta` и ответа на spawn - как cortex/acp/rights и channels
CREATOR_KEY, TOOLS_KEY, LEVEL_KEY, PEERS_KEY, PEER_NOTES_KEY = "creator", "tools", "level", "peers", "peer_notes"
DEPTH_KEY, CHILDREN_KEY, SPAWNED_KEY = "depth", "children", "spawned"
BAD_SPAWN = "spawn: tools - список имён, depth - целое"
NOT_A_CHILD = "Задача {id} - не твой потомок: выдавать можно только своим"
GRANT_DEFERRED = "задача между ходами: право будет в её следующем ходе"


async def _no_spawn(parent: str, params: dict) -> dict:
    return {"status": "error", "error": "создание агентов недоступно"}


@dataclass
class WorkerDeps:
    agent: Any                                               # gateway/acp/agent.AcpAgent процесса Cortex
    ask: Callable[[str, str, str], str]                      # (родительская сессия, id, вопрос) -> ответ агенту
    status: Callable[[str, str, str], str]                   # (родительская сессия, id, текст) -> ответ агенту
    done: Callable[[str, str, str, bool], None]              # (id, итог, родительская сессия, удалось ли)
    pop_inbox: Callable[[str], str | None]                   # реплика создателя в сессию задачи или None
    requeue: Callable[[str, str], None]                      # вернуть реплику в очередь задачи: инъекция не дошла
    send: Callable[[str, str, str], str] = lambda sender, to, message: "отправка агенту недоступна"   # (от, кому, текст)
    answerer: Callable[[str], str] = lambda short_id: ""     # id -> сессия task:<id> живого создателя-агента или иное
    spawn: Callable[[str, dict], Awaitable[dict]] = _no_spawn   # (id родителя, параметры create_agent) -> его ответ
    creator_of: Callable[[str], str] = lambda short_id: ""   # создатель задачи (task:<id> у ребёнка агента)
    children_running: Callable[[str], bool] = lambda short_id: False   # идут ли дети задачи
    awaits_answer: Callable[[str], bool] = lambda short_id: True       # прочтёт ли вопрос задачи тот, кому он ушёл
    forward_up: Callable[[str, str], bool] = lambda short_id, msg: False   # реплика закрытой задачи - дальше по цепочке
    peer_note: Callable[[str], str] = lambda short_id: ""   # кто агент и чем занят: строка для его собеседников
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep   # пауза опроса входящих
    clock: Callable[[], float] = time.monotonic               # срок ожидания сообщения между ходами


def rights_meta(creator: str, tools: list[str] | None, level: str, peers: list[str] | None = None,
                depth: int = 0, children: int = 0, peer_notes: dict[str, str] | None = None) -> dict:
    """`_meta` сессии Cortex: создатель, выдача (None - не передаётся: все инструменты), метка, собеседники, глубина
    и число детей; без права на детей их полей нет вовсе."""
    return {CREATOR_KEY: creator, **({TOOLS_KEY: list(tools)} if tools is not None else {}),
            **({LEVEL_KEY: level} if level else {}), **({PEERS_KEY: list(peers)} if peers else {}),
            **({PEER_NOTES_KEY: dict(peer_notes)} if peer_notes else {}),
            **({DEPTH_KEY: depth, CHILDREN_KEY: children} if depth and children else {})}


class CortexWorkers:
    def __init__(self, deps: WorkerDeps) -> None:
        self._deps = deps
        self._by_session: dict[str, tuple[str, str]] = {}   # id сессии ACP -> (id задачи, родительская сессия)
        self._alive: set[str] = set()                         # задачи, которые идут (между ходами тоже)
        self._asked: dict[str, str] = {}                      # задача с вопросом без ответа -> кто отвечает (id или пусто)
        self._granted: dict[str, list[str]] = {}              # права, выданные по ходу задачи
        self._meta: dict[str, dict] = {}                      # `_meta` задачи от создателя: права, глубина, число детей
        self._spawned: dict[str, int] = {}                    # сколько детей задача уже создала

    async def on_agent_event(self, params: dict) -> dict:
        """Вызов канала агента из сессии Cortex (`_sam/agent_event`); права по выдаче Cortex уже проверил."""
        task = self._by_session.get(str(params.get("session_id") or ""))
        if task is None:
            return {"result": "Задача сессии не найдена: канал к создателю закрыт"}
        short_id, parent = task
        handler = {ASK: self._on_ask, PERMISSION: self._on_permission, SEND: self._on_send, SPAWN: self._on_spawn,
                   GRANT_CHILD: self._on_grant_child}.get(str(params.get("kind") or ""), self._on_status)
        out = await handler(short_id, parent, params)
        return out if isinstance(out, dict) else {"result": out}

    async def _on_ask(self, short_id: str, parent: str, params: dict) -> str:
        return await self._ask(parent, short_id, str(params.get("text") or ""), self._answerer(short_id))

    async def _on_permission(self, short_id: str, parent: str, params: dict) -> str:
        answerer = self._answerer(short_id)   # решает родитель-агент своим grant_child, иначе - grant_agent_tool
        template = CHILD_PERMISSION_QUESTION if answerer else PERMISSION_QUESTION
        question = template.format(tool=params.get("tool") or params.get("text"), why=params.get("why") or "-",
                                   id=short_id)
        return await self._ask(parent, short_id, question, answerer)

    async def _on_send(self, short_id: str, parent: str, params: dict) -> str:
        return self._deps.send(short_id, str(params.get("to") or ""), str(params.get("message") or ""))

    async def _on_status(self, short_id: str, parent: str, params: dict) -> str:
        return self._deps.status(parent, short_id, str(params.get("text") or ""))

    async def _on_spawn(self, short_id: str, parent: str, params: dict) -> str | dict:
        """Ребёнок агента (ADR-022, «Агент создаёт агента»): Cortex проверил права по сессии, шлюз перепроверяет по
        своему учёту задачи - число детей, выдачу не шире родителя, глубину на поколение меньше."""
        parent_depth = int(self._meta.get(short_id, {}).get(DEPTH_KEY) or 0)
        if parent_depth <= 0 or self._children_left(short_id) <= 0:
            return NO_MORE_CHILDREN
        tools, depth = params.get("tools"), params.get("depth") or 0
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools) or not isinstance(depth, int):
            return BAD_SPAWN
        if wider := [t for t in tools if t not in self._own_tools(short_id)]:
            return WIDER_CHILD.format(names=", ".join(wider))
        # место занято до await: два вызова в одном шаге модели идут параллельно и иначе оба прошли бы проверку
        self._spawned[short_id] = self._spawned.get(short_id, 0) + 1
        try:
            out = await self._deps.spawn(short_id, {"description": params.get("description"), "tools": tools,
                                                    "level": params.get("level") or None,
                                                    "depth": min(depth, parent_depth - 1)})
        except BaseException:
            self._spawned[short_id] -= 1
            raise
        if out.get("status") != "ok":
            self._spawned[short_id] -= 1
            return f"Агент не создан: {out.get('error')}"
        await self.grant(short_id, f"{PEER_PREFIX}{out['task_id']}")   # родитель пишет ребёнку без запроса права
        return {"result": SPAWNED.format(id=out["task_id"]), SPAWNED_KEY: True}

    def _children_left(self, short_id: str) -> int:
        return int(self._meta.get(short_id, {}).get(CHILDREN_KEY) or 0) - self._spawned.get(short_id, 0)

    def _own_tools(self, short_id: str) -> list[str]:
        """Выдача задачи: исходная и выданная по ходу (без прав писать агентам)."""
        granted = [t for t in self._granted.get(short_id, []) if not t.startswith(PEER_PREFIX)]
        return [*self._meta.get(short_id, {}).get(TOOLS_KEY, []), *granted]

    async def _on_grant_child(self, short_id: str, parent: str, params: dict) -> str:
        """Предок выдаёт право своему потомку (запрос мог подняться мимо кончившегося родителя); что право есть у
        самого выдающего, проверил Cortex."""
        child, tool = str(params.get("task_id") or ""), str(params.get("tool") or "")
        if not is_ancestor(short_id, child, self._deps.creator_of):
            return NOT_A_CHILD.format(id=child)
        result = await self.grant(child, tool)
        return "Выдано" if result.get("granted") else f"Не выдано: {result.get('reason')}"

    def _answerer(self, short_id: str) -> str:
        """Кто отвечает агенту: id живого создателя-агента или пусто, если отвечает не агент."""
        answerer = self._deps.answerer(short_id)
        return answerer[len(TASK_SESSION_PREFIX):] if answerer.startswith(TASK_SESSION_PREFIX) else ""

    async def _ask(self, parent: str, short_id: str, question: str, answerer: str) -> str:
        """Вопрос создателю; если отвечает агент, он получает право писать спросившему (ADR-022, «Кому писать»)."""
        reply = self._deps.ask(parent, short_id, question)
        if self._deps.awaits_answer(short_id):   # вопрос, который никто не читает, агента не держит
            self._asked[short_id] = answerer
        if answerer:
            await self.grant(answerer, f"{PEER_PREFIX}{short_id}")
        return reply

    async def run(self, short_id: str, description: str, meta: dict, parent_session: str, inbox_key: str) -> str:
        """Задача агента до конца (ADR-022, «Живёт задачу, а не ход»): ход, а пока идут его дети или ждёт ответ на
        вопрос - следующий ход по новому сообщению; итог - создателю. Реплики по ходу - инъекцией."""
        self._alive.add(short_id)
        self._meta[short_id] = meta
        text, result, ok = description, "", False
        try:
            for turn in range(MAX_TURNS):
                result, ok = await self._turn(short_id, text, self._meta_for(short_id, meta), parent_session, inbox_key)
                if not ok or turn == MAX_TURNS - 1:   # последний ход: забранное сообщение уже некуда было бы деть
                    break
                message = await self._next_message(short_id, inbox_key)
                if message is None:
                    break
                text = CONTINUE.format(description=description, result=result, message=message)
        except asyncio.CancelledError:   # отмена создателем: задача закрывается, иначе осталась бы «живой» в маршрутах
            result, ok = CANCELLED.format(last=result), False
            raise
        finally:
            self._alive.discard(short_id)
            self._asked.pop(short_id, None)
            self._granted.pop(short_id, None)
            self._meta.pop(short_id, None)
            self._spawned.pop(short_id, None)
            self._drop_inbox(short_id, inbox_key)
            self._deps.done(short_id, result, parent_session, ok)
        return result

    def _drop_inbox(self, short_id: str, inbox_key: str) -> None:
        """Реплики, пришедшие закрытой задаче: итог и вопрос агента - дальше по цепочке, прочее - строкой в лог."""
        while (msg := self._deps.pop_inbox(inbox_key)) is not None:
            if not self._deps.forward_up(short_id, msg):
                print(f"[Cortex] задача {short_id} закрыта, реплика не доставлена: {msg[:LOG_PREVIEW]}")

    async def _turn(self, short_id: str, text: str, meta: dict, parent_session: str, inbox_key: str) -> tuple[str, bool]:
        """Один ход в новой сессии Cortex: (текст ответа, удался ли)."""
        parts: list[str] = []
        sid: list[str] = []

        def bind(session_id: str) -> None:
            sid.append(session_id)
            self._by_session[session_id] = (short_id, parent_session)
            late.append(asyncio.create_task(self._regrant(session_id, short_id, meta)))
        late: list[asyncio.Task] = []
        forward = asyncio.create_task(self._forward_inbox(short_id, sid, inbox_key))
        try:
            async for piece in self._deps.agent.turn(text, meta=meta, on_session=bind):
                if piece.kind == TEXT:
                    parts.append(piece.text)
            return "".join(parts).strip(), True
        except Exception as e:
            return f"агент Cortex не закончил: {e}", False
        finally:
            forward.cancel()
            for t in late:
                t.cancel()
            for s in sid:
                self._by_session.pop(s, None)

    async def _next_message(self, short_id: str, inbox_key: str) -> str | None:
        """Сообщение для следующего хода, пока агенту есть чего ждать: идут его дети или висит его вопрос. Ждать
        нечего или срок вышел - None: задача закрывается с итогом последнего хода."""
        deadline = self._deps.clock() + WAIT_S
        while self._deps.clock() < deadline:
            if (msg := self._deps.pop_inbox(inbox_key)) is not None:
                self._note_answer(short_id, msg)
                return msg
            self._reask_if_answerer_gone(short_id)
            if short_id not in self._asked and not self._deps.children_running(short_id):
                return None
            await self._deps.sleep(INBOX_POLL_S)
        if (msg := self._deps.pop_inbox(inbox_key)) is not None:   # пришла за последний интервал до срока
            self._note_answer(short_id, msg)
        return msg

    def _reask_if_answerer_gone(self, short_id: str) -> None:
        """Спрошенный агент закрылся: вопрос поднялся выше по цепочке (forward_up), ответит ближайший живой."""
        answerer = self._asked.get(short_id)
        if answerer and answerer not in self._alive:
            self._asked[short_id] = self._answerer(short_id)

    def _note_answer(self, short_id: str, msg: str) -> None:
        """Вопрос закрыт, только если ответил спрошенный: агент - своим письмом, основная модель - сообщением без
        агентских меток (итог ребёнка, вопрос ребёнка и письмо другого агента ответом не считаются)."""
        if short_id not in self._asked:
            return
        answerer = self._asked[short_id]
        answered = msg.startswith(peer_mark(answerer)) if answerer else not msg.startswith(AGENT_MARKS)
        if answered:
            del self._asked[short_id]

    async def _regrant(self, session_id: str, short_id: str, meta: dict) -> None:
        """Права, выданные, пока сессия открывалась (после расчёта `_meta`), - в открытую сессию."""
        def in_meta(right: str) -> bool:
            if right.startswith(PEER_PREFIX):
                return right[len(PEER_PREFIX):] in meta.get(PEERS_KEY, [])
            return right in meta.get(TOOLS_KEY, [])
        for tool in [r for r in self._granted.get(short_id, []) if not in_meta(r)]:
            await self._deps.agent.ext(GRANT_METHOD, {"session_id": session_id, "tool": tool})

    def _meta_for(self, short_id: str, meta: dict) -> dict:
        """`_meta` хода с правами, выданными по ходу задачи: новая сессия их не помнит."""
        granted = self._granted.get(short_id) or []
        tools = [t for t in granted if not t.startswith(PEER_PREFIX)]
        peers = [t[len(PEER_PREFIX):] for t in granted if t.startswith(PEER_PREFIX)]
        out = dict(meta)
        if tools and TOOLS_KEY in out:
            out[TOOLS_KEY] = list(dict.fromkeys([*out[TOOLS_KEY], *tools]))
        if peers:
            out[PEERS_KEY] = list(dict.fromkeys([*out.get(PEERS_KEY, []), *peers]))
            out[PEER_NOTES_KEY] = {**out.get(PEER_NOTES_KEY, {}),
                                   **{p: n for p in peers if (n := self._deps.peer_note(p))}}
        if CHILDREN_KEY in out:   # новая сессия узнаёт, сколько детей осталось
            out[CHILDREN_KEY] = max(int(out[CHILDREN_KEY]) - self._spawned.get(short_id, 0), 0)
        return out

    async def grant(self, short_id: str, tool: str) -> dict:
        """Создатель выдал агенту право: в идущую сессию (видно со следующего шага) и в следующие ходы задачи."""
        if short_id not in self._alive:
            return {"granted": False, "reason": f"задача {short_id} не идёт"}
        if tool == f"{PEER_PREFIX}{short_id}":   # писать самому себе - петля без собеседника
            return {"granted": False, "reason": "агенту нельзя писать самому себе"}
        sid = next((s for s, (task, _) in self._by_session.items() if task == short_id), None)
        note = self._deps.peer_note(tool[len(PEER_PREFIX):]) if tool.startswith(PEER_PREFIX) else ""
        result = (await self._deps.agent.ext(GRANT_METHOD, {"session_id": sid, "tool": tool, **({"note": note} if note else {})}) if sid
                  else {"granted": True, "reason": GRANT_DEFERRED})   # имя проверит Cortex при открытии сессии
        if result.get("granted"):
            self._granted.setdefault(short_id, []).append(tool)
        return result

    async def _forward_inbox(self, short_id: str, sid: list[str], inbox_key: str) -> None:
        while True:
            await self._deps.sleep(INBOX_POLL_S)
            if sid and (msg := self._deps.pop_inbox(inbox_key)) is not None:
                sending = asyncio.ensure_future(self._deps.agent.inject(sid[0], msg))
                try:
                    accepted = await asyncio.shield(sending)
                except asyncio.CancelledError:   # ход кончился посреди передачи: дождаться ответа Cortex, без дубля
                    self._settle(short_id, inbox_key, msg, await _outcome(sending))
                    raise
                self._settle(short_id, inbox_key, msg, accepted)

    def _settle(self, short_id: str, inbox_key: str, msg: str, accepted: bool) -> None:
        if accepted:
            self._note_answer(short_id, msg)
        else:   # не принята - не теряется, ждёт следующей попытки
            self._deps.requeue(inbox_key, msg)


async def _outcome(sending: asyncio.Future) -> bool:
    """Исход инъекции, начатой до отмены хода: Cortex принял её или нет."""
    try:
        return bool(await sending)
    except Exception:
        return False
