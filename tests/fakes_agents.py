"""Фальшивки границ для тестов группы «агенты»: модель, MCP, процесс, сеть, часы.

Свои модули в тестах настоящие; здесь только то, что стоит за внешней границей, и сборка зависимостей из настоящих
модулей так, как её делает корень композиции.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import json
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import mongomock
import yaml

from gateway import agent_loop as al
from gateway import guard, provider_transform, tasks, tool_policy, tool_text_parse, worker, worker_session
from gateway.event_bus import EventBus
from gateway.prompts import DANGEROUS_PATTERNS
from gateway.text_utils import strip_thinking as _strip_thinking
from gateway.tools import agents as agents_tool
from gateway.tools import coding_agents as ca


# ──────────────── Ожидание ────────────────

RUN_WAIT_S = 5.0   # шаги с моделями-заглушками идут миллисекунды; дольше - тест завис, срок - страховка
POLL_S = 0.001     # опрос условия, у которого нет события (тик и Mongo ходят потоком)
CLOSED = ("done", "failed", "cancelled")   # статусы закрытой задачи
WAIT_STEP_S = 60.0   # шаг поддельного времени: сроки ожидания ответа - 90 с у воркера и 600 с у Cortex


async def _polled(condition) -> None:
    """Дождаться условия опросом; вызывающий ограничивает ожидание сроком (asyncio.wait_for)."""
    while not condition():
        await asyncio.sleep(POLL_S)


# ──────────────── Модель ────────────────

def _resp(content=None, tool_calls=None, reasoning=None, finish=None):
    """Ответ модели в форме OpenAI completion (choices[0].message, finish_reason)."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    if reasoning is not None:
        msg.reasoning = reasoning
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)])


def _tc(call_id, name, args):
    """Нативный вызов инструмента; args - объект (уходит JSON) или сырая строка аргументов."""
    raw = args if isinstance(args, str) else json.dumps(args)
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=raw))


class FakeLLMClient:
    """Модель по сценарию: каждый вызов берёт следующий ответ. Исключение в очереди - сбой вызова; функция -
    ответ по аргументам вызова (может быть корутиной: так модель «думает», пока тест что-то делает)."""

    def __init__(self, responses, adapter="native", stream=False):
        self._responses = list(responses)
        self._adapter = adapter
        self._stream = stream     # текст ответа - ещё и потоком через on_delta, как у поставщика со стримом
        self.calls = []
        self.models = []
        self.parse_fails = 0

    @property
    def tools_seen(self) -> list[list[str]]:
        """Имена инструментов, которые модель видела на каждом шаге."""
        return [[t["function"]["name"] for t in c.get("tools") or []] for c in self.calls]

    async def make_llm_call(self, llm_config, **kwargs):
        # снимок сообщений: цикл дописывает в тот же список после вызова
        self.calls.append({**kwargs, **({"messages": list(kwargs["messages"])} if "messages" in kwargs else {})})
        self.models.append(llm_config.get("model"))
        if not self._responses:
            raise RuntimeError("Нет больше моков ответов")
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        if callable(nxt):
            nxt = nxt(kwargs)
            if inspect.isawaitable(nxt):
                nxt = await nxt
        content = nxt.choices[0].message.content
        if self._stream and content and kwargs.get("on_delta"):
            kwargs["on_delta"](content)
        return nxt

    def queue(self, *responses):
        """Дописать шаги сценария (шаг, которому нужна собранная обвязка теста)."""
        self._responses.extend(responses)

    def _effective_adapter(self, llm_config):
        return self._adapter

    def inc_parse_fail(self):
        self.parse_fails += 1


# ──────────────── Шина ────────────────

class RecordingBus(EventBus):
    """Настоящая свежая шина с записью событий через её слушателя."""

    def __init__(self):
        super().__init__()
        self.events: list[dict] = []
        self.add_listener(self._record)

    def _record(self, name: str, data: dict) -> None:
        # msg_id шина ставит каждому событию; в записи он мешает сравнивать события целиком
        self.events.append({"event": name, **{k: v for k, v in data.items() if k != "msg_id"}})

    def names(self) -> list[str]:
        return [e["event"] for e in self.events]

    def of(self, name: str) -> list[dict]:
        return [e for e in self.events if e["event"] == name]


# ──────────────── Цикл агента ────────────────

def _schema(names):
    return [{"type": "function", "function": {"name": n}} for n in names]


def loop_deps(
    *,
    llm_client=None,
    tool_impl=None,
    judge=None,
    policy=None,
    high_risk_tools=None,
    dangerous_patterns=None,
    parse_channel_format=None,
    strip_thinking=None,
    bus=None,
    answer_var=None,
):
    """Зависимости цикла как в корне композиции (gateway/agent.py): настоящие guard, provider_transform,
    tool_policy, tool_text_parse и свежая шина; подменены модель хода и модель судьи (judge).

    policy - ToolPolicy; по умолчанию правил нет (всё allow). Разбор вызовов из текста - по реестру
    имён из tool_impl. bus - шина, общая с воркером; answer_var - holder итога воркера (у шлюза - общий
    worker.worker_last_answer_var). Остальные параметры - для соседних групп, которые собирают цикл со своими частями.
    """
    tools = dict(tool_impl or {})
    judge = judge or FakeLLMClient([])
    strip = strip_thinking or _strip_thinking
    registry = {name: None for name in tools}
    return al.AgentLoopDeps(
        llm_client=llm_client or FakeLLMClient([_resp(content="default")]),
        event_bus=bus if bus is not None else RecordingBus(),
        tool_impl=tools,
        build_tools_schema=_schema,
        provider_transform=provider_transform,
        guard_syntax_check=functools.partial(guard.guard_response_syntax, llm_call_fn=judge.make_llm_call,
                                             strip_thinking_fn=strip),
        guard_tool_check=functools.partial(guard.guard_tool_call, llm_call_fn=judge.make_llm_call,
                                           strip_thinking_fn=strip),
        dangerous_patterns=list(DANGEROUS_PATTERNS if dangerous_patterns is None else dangerous_patterns),
        high_risk_tools=set(guard.HIGH_RISK_TOOLS if high_risk_tools is None else high_risk_tools),
        parse_channel_format=parse_channel_format or (lambda s: tool_text_parse.parse_channel_format(s, registry)),
        strip_thinking=strip,
        worker_last_answer_var=answer_var or contextvars.ContextVar("last_answer", default=None),
        tool_policy=(policy or tool_policy.ToolPolicy([])).decide,
    )


def policy(*rules) -> tool_policy.ToolPolicy:
    """Политика из пар (правило, действие)."""
    return tool_policy.ToolPolicy([tool_policy.parse_rule(r, a) for r, a in rules])


def llm_cfg(**kw):
    return {"model": "test", "source": "test", **kw}


# ──────────────── Сеть ────────────────

class FakeMcpHttp:
    """MCP-сервер устройства за httpx.MockTransport: initialize выдаёт сессию, остальные методы - по сценарию.

    replies: метод JSON-RPC -> поле result, {"error": ...}, httpx.Response (как есть) или строка (сырое тело)."""

    SESSION = "s-1"

    def __init__(self, replies: dict):
        self.replies = replies
        self.calls: list[dict] = []
        self.keys: list[str | None] = []   # ключ доступа SAM каждого запроса

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.keys.append(request.headers.get("x-sam-key"))
        body = json.loads(request.content)
        method = body["method"]
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 0, "result": {}},
                                  headers={"mcp-session-id": self.SESSION})
        self.calls.append({"method": method, "params": body["params"],
                           "session": request.headers.get("mcp-session-id")})
        reply = self.replies.get(method, {})
        if isinstance(reply, httpx.Response):
            return reply
        if isinstance(reply, str):
            return httpx.Response(200, text=reply)
        payload = {"error": reply["error"]} if "error" in reply else {"result": reply}
        event = json.dumps({"jsonrpc": "2.0", "id": body["id"], **payload})
        return httpx.Response(200, text=f"event: message\ndata: {event}\n\n")


async def hang(kwargs=None):
    """Ответ модели, который не приходит никогда: срок задачи выходит без настоящего ожидания."""
    await asyncio.Event().wait()


# ──────────────── Часы ────────────────

class FakeClock:
    """Часы и sleep ожидания: sleep двигает время; в момент answer_at кладёт ответ в очередь сессии."""

    def __init__(self, bus=None, session="", answer_at=None, answer=""):
        self.now, self.slept = 0.0, []
        self.bus, self.session, self.answer_at, self.answer = bus, session, answer_at, answer

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self.answer_at is not None and self.now >= self.answer_at:
            self.bus.inject(self.session, self.answer)
            self.answer_at = None


# ──────────────── Воркер шлюза ────────────────


async def _no_orchestrator(steps, kind="", env="", task_id=""):
    return None


class WorkerRig:
    """Воркер шлюза на настоящих worker, agent_loop, tasks и worker_session (mongomock), ask_creator_impl и свежей
    шине; подменены модель (replies по шагам) и часы ожидания ответа создателя."""

    SYSTEM = "Ты воркер SAM."

    def __init__(self, replies=(), *, db=None, rag=None, timeout=300, answer_wait_s=90.0, tools=None):
        self.db = db if db is not None else mongomock.MongoClient()["sam_test"]
        self.bus = RecordingBus()
        self.llm = FakeLLMClient(replies)
        self.clock = FakeClock(self.bus)
        self.delivered, self.responded, self.reanimated = [], [], []
        self.handles, self.context = {}, {}
        tool_impl = {agents_tool.ASK_TOOL: self._ask, agents_tool.STATUS_TOOL: self._report, **(tools or {})}
        self.loop = loop_deps(llm_client=self.llm, tool_impl=tool_impl, bus=self.bus,
                              answer_var=worker.worker_last_answer_var)
        self.deps = worker.WorkerDeps(
            db=self.db, event_bus=self.bus, tasks_module=tasks, worker_session_module=worker_session,
            agent_loop=functools.partial(al.run_agent_loop, self.loop), worker_tool_names=list(tool_impl),
            worker_system=self.SYSTEM, rag_fetcher=rag,
            auto_respond=lambda short_id, result, parent, cfg: self.responded.append((short_id, result, parent)),
            task_handles=self.handles, task_context_map=self.context, fallback_session_id=lambda: "main",
            timeout_seconds=timeout, max_turns=6,
            reanimate=lambda task_id, message: self.reanimated.append((task_id, message)),
            answer_wait_s=answer_wait_s, sleep=self.clock.sleep, clock=self.clock)

    def new_task(self, description="открой калькулятор", device="pc", creator="") -> str:
        task = tasks.new_task(description, device, creator=creator)
        tasks.save_task(self.db, task)
        return task["id"]

    def task(self, task_id: str) -> dict:
        return tasks.get_task(self.db, task_id)

    async def _ask(self, args):
        """ask_creator как в шлюзе: задача и создатель - по сессии воркера."""
        session = worker.worker_session_var.get(None) or ""
        short_id = session.removeprefix(agents_tool.TASK_SESSION_PREFIX) if session.startswith(
            agents_tool.TASK_SESSION_PREFIX) else ""
        parent = self.context.get(session, ("", ""))[0]
        return agents_tool.ask_creator_impl(args, parent, short_id, agents_tool.AskDeps(
            add_question=functools.partial(tasks.add_question, self.db), emit=self.bus.emit,
            deliver=self.delivered.append))

    async def _report(self, args):
        """report_status как в шлюзе: «ОТВЕТ: ...» - итог воркера и стоп цикла, остальное - статус в монитор."""
        message = str(args.get("message", ""))
        self.bus.emit("task_message", {"content": message, "session": worker.worker_session_var.get(None)})
        if message.startswith(ANSWER_MARK):
            worker.set_last_answer(message[len(ANSWER_MARK):].strip())
            return al.STOP_WORKER_MARKER
        return "ok"


ANSWER_MARK = "ОТВЕТ:"


def report(call_id, text):
    """Шаг модели: итог воркера через report_status."""
    return _resp(tool_calls=[_tc(call_id, agents_tool.STATUS_TOOL, {"message": f"{ANSWER_MARK} {text}"})])


def ask(call_id, question):
    """Шаг модели: вопрос создателю."""
    return _resp(tool_calls=[_tc(call_id, agents_tool.ASK_TOOL, {"question": question})])


# ──────────────── Процесс CLI агента кода ────────────────

class CliRunner:
    """Запуск CLI агента кода (граница процесса): запоминает argv, папку, срок и окружение, отвечает заготовкой;
    `--version` - кодом version_code (0 - CLI доступен)."""

    def __init__(self, result=None, version_code=0):
        self.calls: list[tuple] = []
        self.envs: list[dict | None] = []
        self.result = result or ca.CliResult(0, "", "")
        self.version_code = version_code

    def __call__(self, argv, cwd, timeout, env=None):
        if argv[-1] == "--version":
            return ca.CliResult(self.version_code, "1.0", "")
        self.calls.append((argv, cwd, timeout))
        self.envs.append(env)
        return self.result


# ──────────────── Шлюз по HTTP ────────────────

class GatewayRig:
    """Настоящее приложение шлюза (gateway.main.app) на mongomock и свежей шине, без старта стенда: TestClient без
    контекста не запускает startup (прогрев эмбеддера, workflow), Mongo и шина стенда не трогаются. Основная модель -
    Claude Code вне шлюза: её входящие - файл в tmp_path (agent._deliver_to_lead). Ключ привилегий - свой файл."""

    def __init__(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient
        from gateway import agent, main, main_model, privilege_key
        self.agent, self.main = agent, main
        self.db = mongomock.MongoClient()["sam_test"]
        self.bus = RecordingBus()
        self.inbox_file = tmp_path / "lead-in.jsonl"
        self.context: dict[str, tuple[str, str]] = {}
        self.handles: dict = {}
        lead = main_model.MainModel(
            {main_model.CLAUDE_CODE: main_model.claude_code(lambda: True, agent._deliver_to_lead)},
            [main_model.CLAUDE_CODE])
        for module in (agent, main):
            monkeypatch.setattr(module, "_db", self.db)
            monkeypatch.setattr(module, "_mongo_ok", True)
            monkeypatch.setattr(module, "event_bus", self.bus)
            monkeypatch.setattr(module, "main_models", lead)
        monkeypatch.setattr(agent, "_task_context_map", self.context)
        monkeypatch.setattr(agent, "_picked_llm", _no_orchestrator)   # без сети и настоящего .env: модель агента подменена
        monkeypatch.setattr(agent, "_task_handles", self.handles)
        monkeypatch.setattr(agent, "_LEAD_INBOX_FILE", self.inbox_file)
        monkeypatch.setenv(privilege_key.FILE_ENV, str(tmp_path / "privilege.key"))
        monkeypatch.setattr(privilege_key, "_cached", [])
        self.client = TestClient(main.app)
        self.privileged = privilege_key.headers()

    def running_task(self, description="собери новости про SAM", creator=agents_tool.CREATOR_MAIN_MODEL,
                     parent="voice", **fields) -> str:
        """Задача в Mongo и в работе (как после запуска в шлюзе); короткий id."""
        task = tasks.new_task(description, "workpc", creator=creator, **fields)
        tasks.save_task(self.db, task)
        short = task["id"][:agents_tool.SHORT_ID_LEN]
        self.context[agents_tool.task_session(short)] = (parent, short)
        return short

    def inbox(self) -> list[dict]:
        """Входящие основной модели (то, что тикер принесёт сессии Claude Code)."""
        if not self.inbox_file.exists():
            return []
        return [json.loads(line) for line in self.inbox_file.read_text(encoding="utf-8").splitlines() if line]


class UrlopenToApp:
    """urllib.request.urlopen клиентов стенда (mcp/sam_http.py) - запросом к настоящему приложению шлюза через
    TestClient: сеть подменена, слой доступа и ручки - настоящие."""

    def __init__(self, client):
        self.client = client

    def __call__(self, req, timeout=None):
        import io
        import urllib.error
        import urllib.parse
        url = urllib.parse.urlsplit(req.full_url)
        target = url.path + (f"?{url.query}" if url.query else "")
        r = self.client.request(req.get_method(), target, content=req.data, headers=dict(req.header_items()))
        if r.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, r.status_code, r.reason_phrase, None, io.BytesIO(r.content))
        return _Body(r.content)


class _Body:
    def __init__(self, content: bytes):
        self._content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._content


# ──────────────── Планировщик workflow ────────────────

TZ = timezone(timedelta(hours=6))   # Омск, как стенд
T0 = datetime(2026, 10, 10, 2, 0, tzinfo=TZ)
WORKFLOW_FILE = """
name: demo
on:
  schedule:
    every: 30m
gate: some_gate
steps:
  - name: ask
    main_model: |
      Сделай дело.
  - name: save
    tool: remember
    with: {fact: x}
"""


def _flow(**over):
    """Workflow demo из файла с заменой полей."""
    from gateway.workflow_spec import parse_workflow
    data = yaml.safe_load(WORKFLOW_FILE)
    data.update(over)
    return parse_workflow(data, "demo.yml")


class WallClock:
    """Часы планировщика (datetime с поясом): тест двигает их сам."""

    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _sched(flows, run_step=None, gates=None, store=None, clock=None, **kw):
    """Планировщик с шагом-заглушкой для соседних групп: (планировщик, вызовы шагов, события)."""
    from gateway import workflows as wf
    calls, events = [], []

    async def _step(step, src, text, session):
        calls.append((src, text, session))
        return wf.StepOutcome("fake", "ok")
    s = wf.Scheduler(flows, store or wf.MemoryStore(), gates or {}, run_step or _step,
                     lambda kind, ev: events.append((kind, ev)), now=clock or WallClock(), log=lambda m: None, **kw)
    return s, calls, events


class GatewayTurn:
    """Ход основной модели в шлюзе (sam_agent.run): запоминает вход, отвечает заготовкой."""

    def __init__(self, reply="Ложись."):
        self.reply, self.turns = reply, []

    async def run(self, text, device_id=None, _system_call=False, persist_reply=True):
        self.turns.append((text, device_id))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def _bulk_update_one(collection, requests, ordered=True, **kw):
    """bulk_write из UpdateOne для mongomock 4.3: его разбор не знает поля sort у UpdateOne pymongo 4.16."""
    for op in requests:
        collection.update_one(op._filter, op._doc, upsert=op._upsert)


def mongomock_bulk_compatible(monkeypatch) -> None:
    """MongoStore пишет сдвиги bulk_write: в mongomock - построчно, как update_one."""
    monkeypatch.setattr(mongomock.collection.Collection, "bulk_write", _bulk_update_one)


class WorkflowRig:
    """Планировщик на настоящем шаге шлюза (agent.build_workflow_step), настоящем PushDispatcher и MongoStore на
    mongomock; подменены часы, основная модель (вне шлюза - её входящие, в шлюзе - ход), модель шага ask и
    инструменты шага (кроме переданных tool)."""

    def __init__(self, monkeypatch, flows, *, in_gateway=False, hold_s=0.0, ask_replies=(), ask_model=True,
                 gates=None, tool=None, command_gate=None, store=None, **scheduler):
        from gateway import agent, main_model, push_dispatch, push_queue, quiet_mode, workflows as wf
        self.wf = wf
        mongomock_bulk_compatible(monkeypatch)
        self.inbox: list[dict] = []
        self.turn = GatewayTurn()
        model = main_model.llm() if in_gateway else main_model.claude_code(lambda: True, self.inbox.append)
        monkeypatch.setattr(agent, "main_models", main_model.MainModel({model.name: model}, [model.name]))
        monkeypatch.setattr(agent, "sam_agent", self.turn)
        self.bus = RecordingBus()
        monkeypatch.setattr(agent, "event_bus", self.bus)
        self.quiet = quiet_mode.QuietBook(None)
        monkeypatch.setattr(agent, "_quiet_book", self.quiet)
        self.wall, self.mono = WallClock(), [0.0]
        self.push = push_dispatch.PushDispatcher(push_queue.PushQueue(max_hold_s=hold_s), agent._deliver_push,
                                                 self.bus.emit, clock=lambda: self.mono[0], hold=agent._push_hold)
        self.ask_llm = FakeLLMClient(ask_replies)
        self._ask_model = ask_model
        self.tools: list[tuple] = []
        step = agent.build_workflow_step(
            self.push, is_quiet=self.quiet.is_quiet, emit=self.bus.emit, ask_llm=self._ask_cfg,
            ask_call=self.ask_llm.make_llm_call, normalize=provider_transform.normalize_messages_for_provider,
            tool=tool or self._tool)
        self.db = mongomock.MongoClient()["sam_test"]
        self.store = store or wf.MongoStore(self.db)
        self.logs: list[str] = []
        self.scheduler = wf.Scheduler(flows, self.store, gates if gates is not None else {}, step, self.bus.emit,
                                      now=self.wall, clock=lambda: self.mono[0], log=self.logs.append,
                                      **({"command_gate": command_gate} if command_gate else {}), **scheduler)

    async def _ask_cfg(self):
        return llm_cfg() if self._ask_model else None

    async def _tool(self, name, args, session):
        self.tools.append((name, args, session))
        return f"{name}: ok"

    def events(self) -> list[dict]:
        return self.bus.of(self.wf.EVENT)

    async def idle(self) -> None:
        """Дождаться конца запусков: тик заводит их задачами сразу, до возврата; идёт ли - видно в status()."""
        await asyncio.wait_for(_polled(lambda: not any(st["running"] for st in self.scheduler.status())), RUN_WAIT_S)

    def steps(self) -> list[dict]:
        """События выполненных шагов (у пропущенного шага поля target нет)."""
        return [e for e in self.events() if "target" in e]

    async def ran(self, count: int) -> list[dict]:
        """Дождаться count выполненных шагов: запуск по тику и событию идёт своей задачей, ждём его событие шины,
        а не число оборотов цикла. Срок - страховка от зависшего теста."""
        await asyncio.wait_for(self._until(lambda: len(self.steps()) >= count), RUN_WAIT_S)
        await self.idle()
        return self.steps()

    async def _until(self, condition) -> None:
        arrived = asyncio.Event()
        self.bus.add_listener(lambda name, data: condition() and arrived.set())
        if not condition():
            await arrived.wait()

    async def started(self):
        """Планировщик в своём цикле (как на стенде): события шины доходят до него; первый тик - только сроки."""
        self.bus.add_listener(self.scheduler.on_event)
        self._task = asyncio.ensure_future(self.scheduler.run(interval_s=RUN_WAIT_S * 1000))
        await asyncio.wait_for(_polled(lambda: self.scheduler.tick_status() != {}), RUN_WAIT_S)
        return self

    def stop(self):
        task = getattr(self, "_task", None)
        if task is not None:
            task.cancel()


# ──────────────── Cortex: MCP сессии и клиент ACP ────────────────

class FakeMcp:
    """Серверы MCP сессии Cortex (граница - SDK mcp): инструменты по имени, вызов - заготовкой; соединение настоящее
    McpTools. tools: имя -> итог или {"result", "description", "destructive", "params"}."""

    def __init__(self, tools: dict | None = None):
        self.tools = {name: spec if isinstance(spec, dict) else {"result": spec} for name, spec in (tools or {}).items()}
        self.calls: list[tuple[str, dict]] = []

    async def connect(self, servers, stack):
        from cortex.acp.mcp_tools import McpTools
        tools = McpTools()
        tools.add(self, [SimpleNamespace(
            name=name, description=spec.get("description", name),
            inputSchema={"type": "object", "properties": {p: {"type": "string"} for p in spec.get("params", ())}},
            annotations=SimpleNamespace(destructiveHint=spec.get("destructive", False)))
            for name, spec in self.tools.items()])
        return tools

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        result = self.tools[name]["result"]
        return SimpleNamespace(content=[SimpleNamespace(text=result(args) if callable(result) else result)],
                               isError=False)


class FakeAcpClient:
    """Клиент ACP Cortex (граница протокола): обновления сессии, разрешения (allow) и расширения (ext)."""

    def __init__(self, allow=True, ext=None):
        self.updates, self.permissions, self.events = [], [], []
        self._allow = allow
        self._ext = ext or (lambda method, params: {"result": "Вопрос отправлен создателю"})

    async def session_update(self, session_id, update):
        self.updates.append(update)

    async def request_permission(self, options, session_id, tool_call):
        self.permissions.append(tool_call.title)
        return SimpleNamespace(outcome=SimpleNamespace(outcome="selected", option_id="allow" if self._allow else "reject"))

    async def ext_method(self, method, params):
        self.events.append((method, params))
        reply = self._ext(method, params)
        return await reply if inspect.isawaitable(reply) else reply

    def texts(self) -> str:
        return "".join(u.content.text for u in self.updates if getattr(u, "session_update", "") == "agent_message_chunk")

    def tool_updates(self) -> list[tuple[str, str]]:
        return [(u.session_update, u.status) for u in self.updates
                if getattr(u, "session_update", "") in ("tool_call", "tool_call_update")]


CORTEX_LLM = {"source": "lmstudio", "model": "test-model", "model_server": "http://lm/v1"}


def cortex_agent(llm, mcp=None, client=None, cfg=None, model=CORTEX_LLM, sources=None, level_map=None):
    """Настоящий CortexAgent: модель по сценарию, MCP и клиент - фальшивки границ, политика - поставляемая.
    Возвращает (агент, кто выбирал модель: источник метки или router)."""
    from cortex.acp import config as cortex_config
    from cortex.acp.agent import CortexAgent
    picked = []

    async def pick(task, skip_sources):
        assert cortex_config.SELF_SOURCE in skip_sources   # Cortex не зовёт сам себя
        picked.append("router")
        return model

    async def pick_source(source, model=""):
        picked.append(f"{source}:{model}" if model else source)
        return (sources or {}).get(source)
    agent = CortexAgent(cfg or cortex_config.CortexConfig(), pick, connect_tools=(mcp or FakeMcp()).connect, llm=llm,
                        pick_source=pick_source, level_source=lambda level: (level_map or {}).get(level, ""),
                        policy=tool_policy.load_policy(tool_policy.DEFAULT_POLICY_FILE),
                        dangerous_patterns=tuple(DANGEROUS_PATTERNS))
    agent.on_connect(client or FakeAcpClient())
    return agent, picked


async def cortex_turn(agent, text="Где Ирина?", session=None, **meta):
    """Ход Cortex в новой сессии (или в session - открытой заранее); (ответ prompt, id сессии)."""
    import acp
    sid = session or (await agent.new_session(cwd=".", mcp_servers=[], **meta)).session_id
    return await agent.prompt([acp.text_block(text)], sid), sid


class DuringStep:
    """Шаг модели Cortex, во время которого клиент шлёт агенту расширение ACP (_sam/inject, _sam/grant) в сессию
    хода; ответ модели - reply. Ответы агента на расширения - в results."""

    def __init__(self, method, params_list, reply):
        self.method, self.params_list, self.reply, self.results = method, params_list, reply, []
        self.agent = self.session = None

    def into(self, agent, session):
        self.agent, self.session = agent, session
        return self

    async def __call__(self, kwargs):
        for params in self.params_list:
            self.results.append(await self.agent.ext_method(self.method, {"session_id": self.session, **params}))
        return self.reply


# ──────────────── Процесс Cortex за ACP в этом же процессе ────────────────

class _PipeTransport(asyncio.Transport):
    """Транспорт пайпа в процессе: записанные байты - сразу в читатель другой стороны; провод виден тесту (wire)."""

    def __init__(self, target: asyncio.StreamReader, wire: list):
        super().__init__()
        self._target, self._wire, self._closing = target, wire, False

    def write(self, data: bytes) -> None:
        for line in bytes(data).splitlines():
            if line.strip():
                self._wire.append(json.loads(line))
        self._target.feed_data(data)

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True
        if not self._target.at_eof():
            self._target.feed_eof()

    def get_extra_info(self, name, default=None):
        return default


def _pipe_writer(target: asyncio.StreamReader, wire: list) -> asyncio.StreamWriter:
    """Настоящий asyncio.StreamWriter поверх пайпа: SDK ACP принимает только его."""
    loop = asyncio.get_running_loop()
    return asyncio.StreamWriter(_PipeTransport(target, wire), asyncio.StreamReaderProtocol(asyncio.StreamReader()),
                                None, loop)


class FakeProcess:
    """Процесс агента для AcpAgent: жив, пока пара открыта; выход - кодом."""

    pid = 0

    def __init__(self):
        self.returncode = None
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode

    def exit(self, code: int = 0) -> None:
        if self.returncode is None:
            self.returncode = code
        self._exited.set()


class AcpPair:
    """Процесс Cortex за ACP без подпроцесса: пара asyncio-пайпов, с одной стороны acp.connect_to_agent (шлюз), с
    другой acp.run_agent с настоящим агентом make_agent(). Подставляется в AcpAgent как spawn; протокол - настоящий,
    все сообщения провода - в wire."""

    def __init__(self, make_agent):
        self.make_agent = make_agent
        self.agents: list = []
        self.wire: list[dict] = []
        self._alive: tuple = ()
        self._serving: list[asyncio.Future] = []

    @contextlib.asynccontextmanager
    async def spawn(self, client, command, *args, env=None, cwd=None):
        import acp
        to_agent, to_client = asyncio.StreamReader(), asyncio.StreamReader()
        agent = self.make_agent()
        self.agents.append(agent)
        serving = asyncio.ensure_future(acp.run_agent(agent, _pipe_writer(to_client, self.wire), to_agent))
        conn = acp.connect_to_agent(client, _pipe_writer(to_agent, self.wire), to_client)
        process = FakeProcess()
        self._alive = (to_agent, to_client, process)
        self._serving.append(serving)
        try:
            yield conn, process
        finally:
            await conn.close()
            to_agent.feed_eof()
            serving.cancel()
            process.exit(0)

    async def close(self) -> None:
        """Конец теста: процесс агента выходит, его сторона протокола останавливается."""
        if self._alive:
            self.kill(0)
        for serving in self._serving:
            serving.cancel()
        await asyncio.gather(*self._serving, return_exceptions=True)

    def methods(self) -> list[str]:
        return [m["method"] for m in self.wire if "method" in m]

    def metas(self) -> list[dict]:
        """_meta каждой открытой сессии - что агент получил от создателя."""
        return [m["params"].get("_meta") or {} for m in self.wire if m.get("method") == "session/new"]

    def kill(self, code: int = 3) -> None:
        """Процесс агента умер посреди хода: stdio оборвался, код выхода есть."""
        to_agent, to_client, process = self._alive
        to_agent.feed_eof()
        to_client.feed_eof()
        process.exit(code)


class FakeTime:
    """Часы и пауза ожидания входящих задачи: пауза кончается, когда тест сдвинул время (advance) или в очередь
    сессии легла реплика (nudge от события injected шины), - без настоящего ожидания и без гонки на числе тиков."""

    def __init__(self):
        self.now = 0.0
        self._waiters: list[asyncio.Future] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self._waiters.append(future)
        await future

    def nudge(self) -> None:
        waiters, self._waiters = self._waiters, []
        for future in waiters:
            if not future.done():
                future.set_result(None)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.nudge()


# ──────────────── Память: Qdrant и эмбеддер (копия из tests/test_rag.py) ────────────────

class FakeEmbedder:
    """Детерминированный embedder. Одинаковые тексты → одинаковые векторы."""

    def __init__(self, dim: int = 8):
        self.dim = dim

    def get_sentence_embedding_dimension(self):
        return self.dim

    def encode(self, text: str, normalize_embeddings: bool = False):
        words = (text or "").lower().split()
        vec = [0.0] * self.dim
        for w in words:
            h = hash(w)
            for i in range(self.dim):
                vec[i] += ((h >> (i * 4)) & 0xFF) / 255.0
        if normalize_embeddings:
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            vec = [x / norm for x in vec]
        return vec


class FakePoint:
    def __init__(self, point_id, vector, payload, score=0.0):
        self.id = point_id
        self.vector = vector
        self.payload = payload
        self.score = score


class FakeCollections:
    def __init__(self, names):
        self.collections = [type("C", (), {"name": n})() for n in names]


class FakeCollectionInfo:
    def __init__(self, dim):
        params = type("P", (), {"size": dim})()
        all_params = type("AP", (), {"vectors": params})()
        self.config = type("CF", (), {"params": all_params})()


class FakeQdrant:
    """In-memory Qdrant emulator."""

    def __init__(self, existing_collection_dim=None):
        self.collections = {}
        if existing_collection_dim is not None:
            self.collections["context"] = {"dim": existing_collection_dim, "points": {}}

    def get_collections(self):
        return FakeCollections(list(self.collections.keys()))

    def get_collection(self, name):
        col = self.collections.get(name)
        if col is None:
            raise RuntimeError("collection not found")
        return FakeCollectionInfo(col["dim"])

    def create_collection(self, collection_name, vectors_config):
        self.collections[collection_name] = {
            "dim": getattr(vectors_config, "size", 0), "points": {},
        }

    def delete_collection(self, collection_name):
        self.collections.pop(collection_name, None)

    def upsert(self, collection_name, points):
        col = self.collections.setdefault(collection_name, {"dim": 0, "points": {}})
        for p in points:
            col["points"][p.id] = {"id": p.id, "vector": p.vector, "payload": p.payload}

    @staticmethod
    def _cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(x * x for x in b)) or 1.0
        return dot / (na * nb)

    def _match_filter(self, payload, query_filter):
        if query_filter is None:
            return True
        for cond in query_filter.must:
            key = cond.key
            val = payload.get(key) if payload else None
            if hasattr(cond.match, "any"):
                req = cond.match.any
                if isinstance(val, list):
                    if not any(t in val for t in req):
                        return False
                elif val not in req:
                    return False
            elif hasattr(cond.match, "value"):
                if val != cond.match.value:
                    return False
        return True

    def query_points(self, collection_name, query, limit=10,
                     score_threshold=None, query_filter=None):
        col = self.collections.get(collection_name, {"points": {}})
        scored = []
        for p in col["points"].values():
            if not self._match_filter(p["payload"], query_filter):
                continue
            score = self._cosine(query, p["vector"])
            if score_threshold is not None and score < score_threshold:
                continue
            scored.append(FakePoint(p["id"], p["vector"], p["payload"], score))
        scored.sort(key=lambda x: x.score, reverse=True)
        return type("R", (), {"points": scored[:limit]})()

    def delete(self, collection_name, points_selector):
        col = self.collections.get(collection_name, {"points": {}})
        to_del = []
        # Может быть PointIdsList (для prune) или Filter
        if hasattr(points_selector, "points"):
            to_del = list(points_selector.points)
        else:
            for pid, p in col["points"].items():
                if self._match_filter(p["payload"], points_selector):
                    to_del.append(pid)
        for pid in to_del:
            col["points"].pop(pid, None)

    def scroll(self, collection_name, limit=100, offset=None,
               with_payload=True, with_vectors=False):
        col = self.collections.get(collection_name, {"points": {}})
        all_pts = list(col["points"].values())
        start = offset or 0
        chunk = all_pts[start:start + limit]
        next_offset = start + limit if start + limit < len(all_pts) else None
        points = [
            FakePoint(p["id"], p["vector"] if with_vectors else None,
                       p["payload"] if with_payload else None)
            for p in chunk
        ]
        return (points, next_offset)

    def set_payload(self, collection_name, payload, points):
        """Эмуляция qdrant_client.set_payload — обновляет payload без перезаписи всего vector."""
        col = self.collections.get(collection_name, {"points": {}})
        for pid in points:
            if pid in col["points"]:
                col["points"][pid]["payload"].update(payload)


def offline_rag(monkeypatch, memory_repo):
    """Хранилище памяти шлюза (gateway.rag) на FakeQdrant и FakeEmbedder: поиск фактов без Qdrant и модели стенда."""
    from gateway import rag
    store = rag.RAGStore(memory_repo=memory_repo, qdrant_client_factory=FakeQdrant,
                         embedder_factory=lambda: FakeEmbedder(dim=8))
    monkeypatch.setattr(rag, "_default_store", store)
    return store


# ──────────────── Агенты основной модели: HTTP, воркер шлюза и Cortex за ACP ────────────────

class RoutedLLM(FakeLLMClient):
    """Модель агентов по сценарию на задачу: шаг берётся из очереди той задачи, чьё описание есть в сообщениях
    вызова (родитель и ребёнок идут параллельно, порядок их вызовов не задан)."""

    def __init__(self, routes: dict[str, list]):
        super().__init__([])
        self.routes = {key: list(steps) for key, steps in routes.items()}
        self.by_task: dict[str, list[dict]] = {key: [] for key in routes}

    async def make_llm_call(self, llm_config, **kwargs):
        text = " ".join(str(m.get("content")) for m in kwargs.get("messages") or [] if m.get("role") == "user")
        key = next((k for k in self.routes if k in text), None)
        if key is None:
            raise RuntimeError(f"сценария для вызова нет: {text[:120]!r}")
        self._responses = self.routes[key]
        reply = await super().make_llm_call(llm_config, **kwargs)
        self.by_task[key].append(self.calls[-1])
        return reply


async def _allow_all(title, args):
    return True


class AgentsRig(GatewayRig):
    """Шлюз по HTTP в том же цикле (httpx.ASGITransport): POST /sam/agents запускает настоящий воркер шлюза или
    настоящую сессию Cortex за ACP-парой (agent.build_cortex_workers). Подменены модель агентов (RoutedLLM - и
    воркер, и Cortex), MCP сессии Cortex, судья разрешений, часы ожидания входящих (FakeTime) и память (FakeQdrant)."""

    def __init__(self, monkeypatch, tmp_path, routes: dict[str, list], mcp_tools: dict | None = None,
                 judge=_allow_all):
        super().__init__(monkeypatch, tmp_path)
        from gateway import catalog, llm_client
        from gateway.repos.memory_repo import MemoryRepo
        for name, value in (("LLM_SOURCE", "gemini"), ("GEMINI_API_KEY", "test-do-not-use"),
                            ("LLM_FAST_SOURCE", "off")):   # роутер без опроса поставщиков
            monkeypatch.setenv(name, value)
        for name in ("SAM_AGENT_FRAMEWORK", "SAM_AGENT_LLM_SMART", "SAM_AGENT_LLM_MEDIUM", "SAM_AGENT_LLM_WEAK"):
            monkeypatch.delenv(name, raising=False)
        catalog.register_device(self.db, "workpc", "127.0.0.1", {"mcp_url": "http://127.0.0.1:3000"})
        self.time = FakeTime()
        self.bus.add_listener(lambda name, data: name == "injected" and self.time.nudge())
        self.llm = RoutedLLM(routes)
        monkeypatch.setattr(llm_client, "make_llm_call", self.llm.make_llm_call)
        monkeypatch.setattr(llm_client, "_adapter", "openai")
        offline_rag(monkeypatch, MemoryRepo(self.db))
        self.mcp = FakeMcp(mcp_tools or {"radar_where": "Ирина в зале", "get_current_time": "20:15"})
        self.pair = AcpPair(lambda: cortex_agent(self.llm, self.mcp)[0])
        self.workers = self.agent.build_cortex_workers(spawn_process=self.pair.spawn, permission=judge,
                                                       sleep=self.time.sleep, clock=self.time)
        monkeypatch.setattr(self.agent, "_cortex_workers", lambda: self.workers)
        monkeypatch.setattr(self.agent, "_worker_sleep", self.time.sleep)
        monkeypatch.setattr(self.agent, "_worker_clock", self.time)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.main.app), base_url="http://testserver")

    async def post(self, path: str, body: dict, privileged: bool = False) -> httpx.Response:
        return await self.http.post(path, json=body, headers=self.privileged if privileged else {})

    async def create(self, **body) -> str:
        r = await self.post("/sam/agents", body, privileged=True)
        assert r.status_code == 200, r.text
        return r.json()["task_id"]

    async def until(self, condition) -> None:
        await asyncio.wait_for(_polled(condition), RUN_WAIT_S)

    def task(self, short: str) -> dict:
        return tasks.find_task(self.db, short)

    async def finished(self, short: str) -> dict:
        """Задача закрылась (done, failed, cancelled), её фоновая работа отработала до конца."""
        await self.until(lambda: self.task(short)["status"] in CLOSED
                         and agents_tool.task_session(short) not in self.context)
        await asyncio.gather(*[h for tid, h in self.handles.items() if tid.startswith(short)], return_exceptions=True)
        return self.task(short)

    async def out_of_time(self, short: str) -> dict:
        """Никто не отвечает: время идёт шагами по WAIT_STEP_S, пока задача не закроется по сроку."""
        await self.until(lambda: self.time.advance(WAIT_STEP_S) or self.task(short)["status"] in CLOSED)
        return await self.finished(short)

    def inbox_of(self, source: str) -> list[dict]:
        return [m for m in self.inbox() if m.get("source") == source]

    async def close(self):
        for handle in list(self.handles.values()):
            handle.cancel()
        await asyncio.gather(*self.handles.values(), return_exceptions=True)
        await self.pair.close()
        await self.http.aclose()


class Gate:
    """Шаг модели агента, который ждёт сигнала теста (ответа создателя, итога ребёнка) и отвечает reply."""

    def __init__(self, reply):
        self.reply, self.open, self.entered = reply, asyncio.Event(), asyncio.Event()

    async def __call__(self, kwargs):
        self.entered.set()   # модель начала шаг: реплика, пришедшая теперь, - уже после разбора входящих шага
        await self.open.wait()
        return self.reply
