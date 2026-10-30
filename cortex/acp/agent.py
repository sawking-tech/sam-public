"""Сторона агента ACP в Cortex (ADR-022): сессия с инструментами MCP, ход через agent_loop, отмена, разрешение.

Сессия держит соединения с MCP из session/new. Ход: текст подсказки от шлюза -> agent_loop с моделью из роутера и
инструментами сессии -> поток текста и вызовов клиенту (updates). Инструмент из списка разрешений идёт через
session/request_permission: решает судья SAM на стороне клиента. Модель недоступна или цикл упал - ошибка запроса:
шлюз отдаёт ход следующему поставщику.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

import acp
from acp import InitializeResponse, NewSessionResponse, PromptResponse, RequestError
from acp.schema import AgentCapabilities, Implementation, McpCapabilities, PermissionOption, ToolCallUpdate

from gateway import llm_client, provider_transform
from gateway.agent_loop import AgentLoopDeps, run_agent_loop
from gateway.readonly_command import is_read_only
from gateway.tool_policy import ALLOW, ASK, DENY, SHELL_TOOLS, ToolPolicy, load_policy
from gateway.text_utils import strip_thinking
from gateway.tool_text_parse import parse_channel_format

from .config import SELF_SOURCE, CortexConfig
from .mcp_tools import McpTools, connect
from .rights import PEER_PREFIX, Rights, allowed, from_meta, with_peer
from . import channels
from .prompt import prompt_text, turn_messages
from .tool_search import SEARCH_NAME, ToolCatalog, ToolSearch
from .updates import UpdateSink

AGENT_NAME = "sam-cortex"
AGENT_VERSION = "0.1"
END_TURN, CANCELLED = "end_turn", "cancelled"
ALLOW_ID, REJECT_ID = "allow", "reject"
# Расширение ACP: реплика в идущий ход сессии (монитор, голос во время ответа), цикл видит её на следующем шаге
INJECT_METHOD = "_sam/inject"
LOG_PREVIEW = 120   # сколько реплики показать в логе
LATE_INJECT = "{text}\nТвой ответ: {answer}\n[INJECT] {inject}"   # префикс - как у инъекции в шаг (gateway/worker.py)
# Расширение ACP: создатель выдал агенту инструмент из подключённых к сессии (request_permission, SAM-133 этап 4)
GRANT_METHOD = "_sam/grant"
NOT_GRANTED = "Инструмента {name} нет в твоей выдаче: попроси право у создателя (request_permission)"
SELECTED = "selected"
PERMISSION_OPTIONS = [
    PermissionOption(option_id=ALLOW_ID, name="Разрешить", kind="allow_once"),
    PermissionOption(option_id=REJECT_ID, name="Отказать", kind="reject_once"),
]

PickLlm = Callable[..., Awaitable[dict | None]]   # как llm_router.get_best_llm(task=..., skip_sources=...)
PickSource = Callable[[str], Awaitable[dict | None]]   # как llm_router.source_config(source): модель метки сложности
LevelSource = Callable[[str], str]                 # метка -> источник из конфига (gateway/tools/agents.level_source)


@dataclass
class _Session:
    tools: McpTools
    stack: contextlib.AsyncExitStack
    turn: asyncio.Task | None = None
    sink: UpdateSink | None = None
    search: ToolSearch | None = None
    rights: Rights = field(default_factory=Rights)
    full: McpTools | None = None    # все подключённые инструменты: из них создатель выдаёт по запросу права
    last_answer: contextvars.ContextVar = field(
        default_factory=lambda: contextvars.ContextVar("cortex_last_answer", default=None))


async def _syntax_ok(text: str, llm_config: dict) -> bool:
    """Проверка синтаксиса вызова из текста - режим custom шлюза; в Cortex разбор текста работает без неё."""
    return True


def granted(response: Any) -> bool:
    outcome = getattr(response, "outcome", None)
    return getattr(outcome, "outcome", None) == SELECTED and getattr(outcome, "option_id", None) == ALLOW_ID


class CortexAgent:
    def __init__(self, cfg: CortexConfig, pick_llm: PickLlm, connect_tools=connect, llm: Any = llm_client,
                 read_only_commands: tuple[str, ...] | None = None, pick_source: PickSource | None = None,
                 level_source: LevelSource = lambda level: "", policy: ToolPolicy | None = None,
                 dangerous_patterns: tuple[str, ...] | None = None) -> None:
        self._cfg = cfg
        self._dangerous = tuple(dangerous_patterns or ())   # паттерны опасных команд шлюза: приходят от корня композиции
        self._pick_source, self._level_source = pick_source, level_source
        # белый список команд только на чтение - из политики шлюза: одна правда для обоих (SAM-127); None - файл
        # SAM_TOOL_POLICY или поставляемый
        self._rules = policy if policy is not None else load_policy()   # deny шлюза действует и здесь (ключ, set_env)
        self._read_only = self._rules.read_only if read_only_commands is None else read_only_commands
        self._llm = llm
        self._pick_llm = pick_llm
        self._connect_tools = connect_tools
        self._conn: Any = None
        self._sessions: dict[str, _Session] = {}

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def initialize(self, protocol_version: int, client_capabilities: Any = None, client_info: Any = None,
                         **kwargs: Any) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_info=Implementation(name=AGENT_NAME, version=AGENT_VERSION),
            agent_capabilities=AgentCapabilities(mcp_capabilities=McpCapabilities(http=True)),
        )

    async def new_session(self, cwd: str, mcp_servers: list | None = None, **kwargs: Any) -> NewSessionResponse:
        stack = contextlib.AsyncExitStack()
        tools = await self._connect_tools(mcp_servers or [], stack)
        rights = from_meta(kwargs)   # _meta сессии: создатель, выдача, метка (SAM-133)
        full = tools
        if rights.tools is not None:
            tools = tools.only(allowed(tools.names, rights))
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = _Session(tools, stack, search=self._search_for(tools), rights=rights,
                                              full=full if rights.tools is not None else None)
        print(f"[Cortex] сессия {session_id[:8]}: инструментов {len(tools.names)}"
              f"{f', создатель {rights.creator}' if rights.creator else ''}{f', метка {rights.level}' if rights.level else ''}")
        return NewSessionResponse(session_id=session_id)

    async def prompt(self, prompt: list, session_id: str, **kwargs: Any) -> PromptResponse:
        session = self._session(session_id)
        session.turn = asyncio.ensure_future(self._turn(session, session_id, prompt_text(prompt)))
        try:
            await session.turn
        except asyncio.CancelledError:
            return PromptResponse(stop_reason=CANCELLED)
        finally:
            session.turn = None
        return PromptResponse(stop_reason=END_TURN)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        session = self._sessions.get(session_id)
        if session is not None and session.turn is not None:
            session.turn.cancel()

    async def close_session(self, session_id: str, **kwargs: Any) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return None
        if session.turn is not None:
            session.turn.cancel()
        with contextlib.suppress(Exception):
            await session.stack.aclose()
        return None

    async def ext_method(self, method: str, params: dict) -> dict:
        if method == GRANT_METHOD:
            return self._grant(self._session(params.get("session_id", "")), str(params.get("tool") or ""),
                               str(params.get("note") or ""))
        if method != INJECT_METHOD:
            raise RequestError.method_not_found(method)
        session = self._session(params.get("session_id", ""))
        if session.sink is None:
            return {"accepted": False, "reason": "хода нет"}
        session.sink.inject(str(params.get("text", "")))
        return {"accepted": True}

    async def ext_notification(self, method: str, params: dict) -> None:
        return None

    def _grant(self, session: _Session, name: str, note: str = "") -> dict:
        """Выдать инструмент из подключённых к сессии; виден со следующего шага хода."""
        if name.startswith(PEER_PREFIX):   # право писать агенту (ADR-022): в список собеседников
            peer = name[len(PEER_PREFIX):]
            session.rights = with_peer(session.rights, peer, note)
            return {"granted": True}
        if session.full is None:
            return {"granted": False, "reason": "у сессии нет выдачи: доступны все инструменты"}
        if name not in session.full.names:
            return {"granted": False, "reason": f"инструмента {name} нет у сессии"}
        granted = tuple(dict.fromkeys([*(session.rights.tools or ()), name]))
        session.rights = replace(session.rights, tools=granted)
        session.tools = session.full.only(granted)
        return {"granted": True}

    def _search_for(self, tools: Any) -> ToolSearch | None:
        """Поиск инструментов, если их больше порога; иначе модель видит весь список."""
        if len(tools.names) <= self._cfg.search_min_tools:
            return None
        return ToolSearch(ToolCatalog(tools.descriptions()), tools.names, self._cfg.core_tools, self._cfg.search_limit)

    def _policy(self, destructive: set[str]):
        """ask - разрешение клиента (судья SAM): инструменты из списка и с destructiveHint, кроме команды оболочки
        только на чтение."""
        ask = set(self._cfg.permission_tools) | destructive

        def decide(name: str, args: dict) -> str:
            if self._rules.decide(name, args or {}) == DENY:
                return DENY
            if name not in ask:
                return ALLOW
            if name in SHELL_TOOLS and is_read_only(str((args or {}).get("command") or ""), self._read_only):
                return ALLOW
            return ASK
        return decide

    async def _llm_for(self, rights: Rights) -> dict | None:
        """Модель метки сложности из конфига; метки или источника нет, или он недоступен - выбор роутера."""
        value = self._level_source(rights.level) if rights.level else ""
        # значение метки - "источник" или "источник:модель" (gateway/agent_levels.py); Cortex шлюз не импортирует
        source, _, model = value.partition(":")
        source = source.strip().lower()
        if source and source != SELF_SOURCE and self._pick_source is not None:
            if cfg := await self._pick_source(source, model.strip()):
                return cfg
        return await self._pick_llm(task=self._cfg.llm_task, skip_sources={SELF_SOURCE})

    def _session(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise RequestError.invalid_params({"session_id": session_id, "reason": "нет такой сессии"})
        return session

    async def _turn(self, session: _Session, session_id: str, text: str) -> None:
        llm_config = await self._llm_for(session.rights)
        if not llm_config:
            raise RequestError.internal_error({"reason": "нет доступной модели"})
        sink = UpdateSink(lambda update: self._conn.session_update(session_id=session_id, update=update))
        session.sink = sink
        try:
            deps = self._deps(session, session_id, sink)
            names = (session.tools.names + ([SEARCH_NAME] if session.search else [])
                     + (list(channels.names(session.rights)) if session.rights.creator else []))
            final, _ = await run_agent_loop(deps, turn_messages(text), llm_config, names,
                                            max_turns=self._cfg.max_turns, session_id=session_id)
            # Реплика, принятая, пока модель писала финальный ответ, цикл уже не заберёт: продолжаем ход с ней
            for _ in range(self._cfg.late_injects):
                if (late := sink.pop_inject(session_id)) is None:
                    break
                final, _ = await run_agent_loop(deps, turn_messages(LATE_INJECT.format(text=text, answer=final or "",
                                                                                       inject=late)),
                                                llm_config, names, max_turns=self._cfg.max_turns, session_id=session_id)
            while (left := sink.pop_inject(session_id)) is not None:   # сверх предела - петля: не молча
                print(f"[Cortex] реплика сверх {self._cfg.late_injects} продолжений хода не принята: {left[:LOG_PREVIEW]}")
            answer = strip_thinking(final or "").strip()
            if answer and answer != strip_thinking(sink.last_segment).strip():
                sink.say(answer)
        finally:
            session.sink = None
            await sink.close()

    def _deps(self, session: _Session, session_id: str, sink: UpdateSink) -> AgentLoopDeps:
        search = session.search
        known = session.full or session.tools   # вызов по имени - среди подключённых, право - по текущей выдаче
        own = session.rights.creator != ""   # агент с создателем: каналы связи есть при любых правах (ADR-022)
        registry = {**known.registry(), **(channels.registry(session.rights) if own else {})}

        async def ask(name: str, args: dict, llm_config: dict) -> bool:
            call = ToolCallUpdate(tool_call_id=uuid.uuid4().hex, title=name, raw_input=args)
            response = await self._conn.request_permission(options=PERMISSION_OPTIONS, session_id=session_id,
                                                           tool_call=call)
            return granted(response)

        async def call(name: str, args: dict) -> str:
            if name not in session.tools.names:   # выдача могла вырасти посреди хода (_sam/grant) - смотрим сейчас
                return NOT_GRANTED.format(name=name)
            result = await session.tools.call(name, args)
            if search is not None:
                search.activate([name])          # вызван по имени без поиска - дальше виден полной схемой
                search.activate_mentioned(result)  # скилл назвал инструмент - он приходит со следующего шага
            return result

        def schemas(names: list[str]) -> list[dict]:
            own_schemas = channels.schemas(session.rights) if own else []
            if search is None:
                return session.tools.schemas(session.tools.names) + own_schemas
            return session.tools.schemas(search.active) + [search.bridge()] + own_schemas

        impl = {name: (lambda args, _n=name: call(_n, args)) for name in known.names}
        if search is not None:
            impl[SEARCH_NAME] = search.call
        if own:
            def children(delta: int) -> None:
                session.rights = replace(session.rights, children=max(session.rights.children + delta, 0))
            impl.update(channels.impl(lambda method, params: self._conn.ext_method(method, params), session_id,
                                      rights=lambda: session.rights, on_children=children))

        # выдача посреди хода (_sam/grant) видна со следующего шага: показанное - текущий набор, право решает call()
        offered = lambda names: frozenset(impl)
        return AgentLoopDeps(
            llm_client=self._llm,
            event_bus=sink,
            tool_impl=impl,
            offered=offered,
            build_tools_schema=schemas,
            provider_transform=provider_transform,
            guard_syntax_check=_syntax_ok,
            guard_tool_check=ask,
            dangerous_patterns=list(self._dangerous),
            high_risk_tools=set(),
            tool_policy=self._policy(known.destructive()),
            parse_channel_format=lambda content: parse_channel_format(content, registry),
            strip_thinking=strip_thinking,
            worker_last_answer_var=session.last_answer,
        )
