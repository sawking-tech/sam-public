"""ACP-агент подпроцессом (ADR-022): запуск, инициализация, сессия на ход, перезапуск после смерти.

Каждый ход - новая сессия с MCP SAM: контекст хода собирает SAM, своя история агента не копится. Агент умер (закрыт
stdio, процесс вышел) - ход кончается ошибкой AcpError, следующий ход поднимает агента заново. Запуск процесса
передаётся параметром (spawn), чтобы проверять без настоящего агента.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncIterator, Callable

import acp
from acp.schema import ClientCapabilities, FileSystemCapabilities, HttpMcpServer, Implementation

from .client import SamClient
from .config import AcpConfig, agent_env, prepare_home
from .prompt import Piece

CLIENT_NAME = "sam-gateway"
CLIENT_VERSION = "0.1"
MCP_TYPE_HTTP = "http"
# Сколько ждать ответа агента на отмену и закрытие сессии: зависший агент не держит ход дольше
CANCEL_TIMEOUT_S = 5.0
INJECT_METHOD = "_sam/inject"   # расширение ACP Cortex: реплика в идущий ход (cortex/acp/agent.py)
# Сколько ждать кода выхода после ошибки хода, чтобы отличить мёртвый процесс от ошибки живого агента
EXIT_WAIT_S = 0.5

Spawn = Callable[..., Any]   # асинхронный контекст -> (соединение, процесс), как acp.spawn_agent_process


class AcpError(Exception):
    """Агент не поднялся, умер или ответил ошибкой: ход не состоялся."""


class AcpAgent:
    def __init__(self, cfg: AcpConfig, client: SamClient, spawn: Spawn = acp.spawn_agent_process) -> None:
        self._cfg = cfg
        self._client = client
        self._spawn = spawn
        self._stack: contextlib.AsyncExitStack | None = None
        self._conn: Any = None
        self._process: Any = None
        self._start_lock = asyncio.Lock()
        self.turns = 0   # идущие ходы: перезапуск Cortex ждёт их конца (gateway/restart.py)

    @property
    def alive(self) -> bool:
        return self._conn is not None and self._process is not None and self._process.returncode is None

    @property
    def exited(self) -> bool:
        """Процесс поднимался и вышел, а ход его ещё не сбросил: health check Cortex (SAM-195). Не поднятый - не
        поломка: процесс встаёт на первом ходе."""
        return self._conn is not None and self._process is not None and self._process.returncode is not None

    async def start(self) -> None:
        """Поднять процесс сейчас, не дожидаясь хода: перезапуск Cortex (SAM-195)."""
        await self._ensure()

    async def _ensure(self) -> Any:
        async with self._start_lock:
            if self.alive:
                return self._conn
            await self._drop()
            self._cfg.cwd.mkdir(parents=True, exist_ok=True)
            stack = contextlib.AsyncExitStack()
            try:
                prepare_home(self._cfg)
                conn, process = await stack.enter_async_context(
                    self._spawn(self._client, self._cfg.command, *self._cfg.args, env=agent_env(self._cfg),
                                cwd=str(self._cfg.cwd)))
                await asyncio.wait_for(conn.initialize(
                    protocol_version=acp.PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(
                        fs=FileSystemCapabilities(read_text_file=False, write_text_file=False), terminal=False),
                    client_info=Implementation(name=CLIENT_NAME, version=CLIENT_VERSION),
                ), timeout=self._cfg.start_timeout_s)
            except Exception as e:
                await stack.aclose()
                raise AcpError(f"агент {self._cfg.command} не поднялся: {e or type(e).__name__}") from e
            self._stack, self._conn, self._process = stack, conn, process
            print(f"[ACP] агент поднят: {self._cfg.command} (pid {getattr(process, 'pid', '?')})")
            return conn

    async def _drop(self) -> None:
        stack, self._stack, self._conn, self._process = self._stack, None, None, None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    def _mcp_servers(self) -> list:
        if not self._cfg.mcp_url:
            return []
        return [HttpMcpServer(name=self._cfg.mcp_name, url=self._cfg.mcp_url, headers=[], type=MCP_TYPE_HTTP)]

    async def turn(self, text: str, meta: dict | None = None,
                   on_session: Callable[[str], None] | None = None) -> AsyncIterator[Piece]:
        """Один ход: новая сессия, подсказка текстом, куски ответа по мере прихода. meta - `_meta` сессии (права
        агента от создателя, SAM-133); on_session(id) - id сессии, пока ход идёт (каналы и инъекция агенту)."""
        self.turns += 1
        try:
            async for piece in self._turn(text, meta, on_session):
                yield piece
        finally:
            self.turns -= 1

    async def _turn(self, text: str, meta: dict | None,
                    on_session: Callable[[str], None] | None) -> AsyncIterator[Piece]:
        conn = await self._ensure()
        try:
            # срок как у старта: агент подключает MCP сессии, на холодном MCP это секунды
            session = await asyncio.wait_for(conn.new_session(cwd=str(self._cfg.cwd), mcp_servers=self._mcp_servers(),
                                                              **(meta or {})),
                                             self._cfg.start_timeout_s)
        except Exception as e:
            await self._fail()
            raise AcpError(f"агент не открыл сессию: {e or type(e).__name__}") from e
        sid = session.session_id
        if on_session is not None:
            on_session(sid)
        queue = self._client.open(sid)
        prompt = asyncio.ensure_future(conn.prompt(prompt=[acp.text_block(text)], session_id=sid))
        # SDK не закрывает висящий запрос, когда агент вышел: выход процесса ждётся рядом с ответом
        exited = asyncio.ensure_future(self._process.wait())
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, prompt, exited}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                    continue
                getter.cancel()
                if prompt not in done:
                    await self._drop()
                    raise AcpError(f"агент вышел посреди хода, код {exited.result()}")
                break
            while not queue.empty():    # куски, пришедшие вместе с концом хода
                yield queue.get_nowait()
            try:
                prompt.result()
            except Exception as e:
                await self._fail()
                raise AcpError(f"ход агента упал: {e or type(e).__name__}") from e
        finally:
            exited.cancel()
            self._client.close(sid)
            await self._end_session(conn, sid, prompt)

    async def ext(self, method: str, params: dict) -> dict:
        """Расширение ACP агенту (выдача инструмента `_sam/grant`); агента нет или сбой - отказ строкой."""
        if self._conn is None:
            return {"granted": False, "reason": "агент не поднят"}
        try:
            return await asyncio.wait_for(self._conn.ext_method(method, params), CANCEL_TIMEOUT_S)
        except Exception as e:
            return {"granted": False, "reason": str(e) or type(e).__name__}

    async def inject(self, session_id: str, text: str) -> bool:
        """Реплика в идущий ход сессии агента (`_sam/inject`, ответ создателя на вопрос); False - не принята."""
        if self._conn is None:
            return False
        try:
            reply = await asyncio.wait_for(self._conn.ext_method(INJECT_METHOD, {"session_id": session_id, "text": text}),
                                           CANCEL_TIMEOUT_S)
        except Exception as e:
            print(f"[ACP] инъекция агенту не дошла: {e}")
            return False
        return bool((reply or {}).get("accepted"))

    async def _end_session(self, conn: Any, sid: str, prompt: asyncio.Future) -> None:
        """Конец хода: прерванный снаружи (отмена, срок) - отмена агенту; сессия закрывается, если агент жив."""
        interrupted = not prompt.done()
        prompt.cancel()
        if self._conn is not conn:       # агент сброшен или уже поднят заново: отменять и закрывать некому
            return
        if interrupted:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(conn.cancel(session_id=sid), CANCEL_TIMEOUT_S)
        with contextlib.suppress(Exception):   # закрытие сессии - не у всех агентов, и не обязательно
            await asyncio.wait_for(conn.close_session(session_id=sid), CANCEL_TIMEOUT_S)

    async def _fail(self) -> None:
        """Ошибка хода: если процесс умер - сбросить, следующий ход поднимет заново."""
        if self._process is not None:   # обрыв stdio приходит раньше кода выхода процесса
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._process.wait(), EXIT_WAIT_S)
        if not self.alive:
            await self._drop()

    async def close(self) -> None:
        await self._drop()
