"""
SAM MCP Aggregator — единый MCP endpoint для устройства.

Читает configs/local.json, запускает все MCP серверы как subprocess,
агрегирует их инструменты на одном HTTP порту.

Добавить новый MCP: вписать в configs/local.json, перезапустить.

Запуск:
    python mcp/mcp_aggregator.py
    python mcp/mcp_aggregator.py --port 3000

Endpoint: POST http://localhost:3000/mcp  (MCP Streamable HTTP)
          GET  http://localhost:3000/sse  (MCP SSE — для обратной совместимости)
          GET  http://localhost:3000/health
"""

import asyncio
import json
import os
import shutil
import sys
import argparse
import logging
from contextlib import AsyncExitStack
from typing import NamedTuple, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# .env с токенами (PLAYWRIGHT_MCP_EXTENSION_TOKEN и т.д.) — наследуется в env
# каждого stdio-сервера через os.environ
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

class _MCPFormatter(logging.Formatter):
    _CLEAR = "\r" + " " * 100 + "\r"
    def format(self, record: logging.LogRecord) -> str:
        time = self.formatTime(record, "%H:%M:%S")
        return f"{self._CLEAR}{time} [MCP] {record.getMessage()}"


class _NoJsonrpcSpam(logging.Filter):
    """Подавляет спам 'Failed to parse JSONRPC' от banner-выводов MCP серверов."""
    def filter(self, record: logging.LogRecord) -> bool:
        return "Failed to parse JSONRPC" not in record.getMessage()


_handler = logging.StreamHandler()
_handler.setFormatter(_MCPFormatter())
_handler.addFilter(_NoJsonrpcSpam())

logging.root.setLevel(logging.INFO)
logging.root.handlers.clear()
logging.root.addHandler(_handler)
logging.root.addFilter(_NoJsonrpcSpam())

log = logging.getLogger(__name__)

# Доступ по ключу - тот же модуль, что у gateway (корень репозитория в пути: агрегатор запускают
# как mcp/mcp_aggregator.py). Пакет mcp из pip не затеняется: локальная папка mcp/ без __init__.py.
sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from gateway.access import AccessMiddleware, startup_warning  # noqa: E402
from gateway.env import env_float, env_int  # noqa: E402
from gateway import stand_watch_keeper  # noqa: E402

# Срок выполнения одного вызова инструмента (30 с) и отдельный срок ожидания очереди
# к серверу: сервер выполняет один вызов за раз, и вызов за долгим не должен отвечать
# «timed out», как будто выполнялся (SAM-76). Ожидание очереди не замерено, взято равным сроку вызова.
CALL_TIMEOUT_S = env_float("MCP_CALL_TIMEOUT_S", 30.0)
QUEUE_TIMEOUT_S = env_float("MCP_QUEUE_TIMEOUT_S", 30.0)


class ToolOutcome(NamedTuple):
    """Текст ответа инструмента и признак ошибки (isError протокола MCP)."""
    text: str
    is_error: bool = False


# Сервер, который объявляет поверхность встроенного приложения (радар), не поднимается, когда приложение снято:
# метка `removed` в его папке приложений (gateway/builtin_apps.py, REMOVED; модуль шлюза отсюда не импортируется)
APPS_DIR = os.getenv("SAM_APPS_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                     "data", "apps")
REMOVED_MARKER = "removed"


def removed_app(name: str, apps_dir: str | None = None) -> bool:
    return bool(name) and os.path.isfile(os.path.join(apps_dir or APPS_DIR, name, REMOVED_MARKER))


def _error(message: str) -> ToolOutcome:
    return ToolOutcome(json.dumps({"error": message}), True)


class SubServer:
    """Один подключённый MCP сервер."""

    def __init__(self, name: str):
        self.name = name
        self.session: Optional[ClientSession] = None
        self.tools: dict = {}  # tool_name → tool schema
        self._lock = asyncio.Lock()

    async def list_tools(self) -> list:
        result = await self.session.list_tools()
        self.tools = {t.name: t for t in result.tools}
        return result.tools

    async def call_tool(self, tool_name: str, arguments: dict,
                        timeout: float | None = None) -> ToolOutcome:
        timeout = CALL_TIMEOUT_S if timeout is None else timeout
        try:
            await asyncio.wait_for(self._lock.acquire(), QUEUE_TIMEOUT_S)
        except asyncio.TimeoutError:
            return _error(f"Tool '{tool_name}' not started: server '{self.name}' busy "
                          f"with another call for {QUEUE_TIMEOUT_S:g}s")
        try:
            result = await asyncio.wait_for(self.session.call_tool(tool_name, arguments), timeout)
        except asyncio.TimeoutError:
            return _error(f"Tool '{tool_name}' timed out after {timeout:g}s")
        finally:
            self._lock.release()
        parts = [c.text if hasattr(c, "text") else str(c) for c in (result.content or [])]
        return ToolOutcome("\n".join(parts), bool(getattr(result, "isError", False)))




class MCPAggregator:
    """Управляет пулом SubServer, агрегирует tools."""

    def __init__(self):
        self.servers: list[SubServer] = []
        self._tool_index: dict[str, SubServer] = {}  # tool_name → server
        self._exit_stack = AsyncExitStack()
        self._ready = False   # все серверы опрошены; до этого /health и спиннер ждут

    async def start(self, config_path: str):
        config = json.loads(open(config_path, encoding="utf-8").read())
        servers_cfg = [cfg for cfg in config.get("servers", []) if not removed_app(cfg.get("name", ""))]

        # Запускаем серверы параллельно
        await asyncio.gather(*[
            self._start_server(cfg) for cfg in servers_cfg
        ], return_exceptions=True)
        self._ready = True

    async def _start_server(self, cfg: dict):
        name = cfg["name"]
        cmd = cfg["command"]
        args = cfg.get("args", [])
        cwd = cfg.get("cwd", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        # Resolve python to current interpreter
        if cmd == "python":
            cmd = sys.executable
        # On Windows, npm/npx scripts are .cmd files — must use the .cmd extension
        # (node itself is node.exe, not a .cmd)
        elif sys.platform == "win32" and cmd in ("npx", "npm") and not cmd.endswith(".cmd"):
            cmd = cmd + ".cmd"

        # Resolve relative .py script paths to absolute so that the server
        # can be launched from any working directory
        if cmd == sys.executable and args and args[0].endswith(".py") and not os.path.isabs(args[0]):
            resolved = os.path.join(cwd, args[0])
            if os.path.exists(resolved):
                args = [resolved] + list(args[1:])
            else:
                log.warning(f"[{name}] Script not found: {resolved} (tried relative: {args[0]})")

        params = StdioServerParameters(command=cmd, args=args, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        srv = SubServer(name)

        try:
            read, write = await self._exit_stack.enter_async_context(
                stdio_client(params)
            )
            session = await self._exit_stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()
            srv.session = session
            self.servers.append(srv)   # без этого tools/list пуст (снято в 6128ad4), tools/call шёл мимо списка

            tools = await srv.list_tools()
            collected = []
            for t in tools:
                self._tool_index[t.name] = srv
                collected.append(t.name)
                preview = "  ".join(collected[-6:])
                print(f"\r  [{name}] {len(collected):>3}  {preview:<70}  ", end="", flush=True)
            print()  # newline after server done
            log.info("[%s] %d tools", name, len(tools))

        except Exception as e:
            log.error(f"[{name}] FAILED to start: {e}")

    async def stop(self):
        await self._exit_stack.aclose()

    def all_tools(self) -> list[dict]:
        result = []
        for srv in self.servers:
            for t in srv.tools.values():
                result.append({
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {},
                })
        return result

    async def call(self, tool_name: str, arguments: dict) -> ToolOutcome:
        srv = self._tool_index.get(tool_name)
        if not srv:
            available = list(self._tool_index.keys())
            return ToolOutcome(json.dumps({
                "error": f"Tool '{tool_name}' not found",
                "available": available,
            }), True)
        try:
            return await srv.call_tool(tool_name, arguments)
        except Exception as e:
            return _error(str(e))


# ─── FastAPI app ──────────────────────────────────────────────────────────────

_aggregator: MCPAggregator = None
READY_POLL_S = 0.2       # как часто ждать готовности агрегатора для строки «Ready»
SPINNER_FRAMES = ["·", "−", "+", "✦", "+", "−"]
SPINNER_STEP_S = 0.18    # шаг бегущей строки: читается глазом, не грузит консоль
SEPARATOR = "  |  "
MIN_COL_W = 8            # уже - имя инструмента не прочесть
LINE_MARGIN = 1          # последний столбец не занимать: иначе часть терминалов переносит строку
FALLBACK_TERMINAL = (120, 30)   # вывод не в терминал (лог) - ширина по умолчанию


async def _mcp_spinner() -> None:
    """✦  Ready: 62 tools [ srv1: <бегущий список> | srv2: ... ] - строка не шире терминала.

    Строка шире вкладки переносилась, 
 возвращал курсор только в начало последней перенесённой строки, и консоль
    заполнялась обрывками (9 окт). Поэтому вся строка обрезается по ширине терминала, окно сервера делится поровну."""
    while _aggregator is None or not _aggregator._ready:
        await asyncio.sleep(READY_POLL_S)
    servers = [s for s in _aggregator.servers if s.tools]
    total = sum(len(s.tools) for s in servers)
    contents = {s.name: "  ".join(s.tools.keys()) + "   " for s in servers}
    offsets = {s.name: 0 for s in servers}
    i = 0
    while servers:
        width = shutil.get_terminal_size(FALLBACK_TERMINAL).columns - LINE_MARGIN
        head = f"  {SPINNER_FRAMES[i % len(SPINNER_FRAMES)]}  Ready: {total} tools [ "
        names = sum(len(s.name) + len(": ") + len(SEPARATOR) for s in servers)
        col_w = max(MIN_COL_W, (width - len(head) - names) // len(servers))
        parts = []
        for srv in servers:
            line, off = contents[srv.name], offsets[srv.name]
            parts.append(f"{srv.name}: {(line * 3)[off: off + col_w].ljust(col_w)}")
            offsets[srv.name] = (off + 1) % len(line)
        print(f"\r{(head + SEPARATOR.join(parts) + ' ]')[:width]}", end="", flush=True)
        i += 1
        await asyncio.sleep(SPINNER_STEP_S)


def create_app(config_path: str) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _aggregator
        _aggregator = MCPAggregator()
        # Старт в фоне — HTTP сервер поднимается сразу, health отвечает 503 до готовности
        asyncio.create_task(_aggregator.start(config_path))
        spinner = asyncio.create_task(_mcp_spinner())
        yield
        spinner.cancel()
        await _aggregator.stop()

    app = FastAPI(title="SAM MCP Aggregator", lifespan=lifespan)
    # Тот же ключ, что у gateway: без него любое устройство в сети запускало powershell (SAM-79)
    app.add_middleware(AccessMiddleware)
    if warning := startup_warning():
        log.warning(warning)

    @app.get("/health")
    async def health():
        if _aggregator is None or not _aggregator._tool_index:
            return JSONResponse({"status": "starting", "tools": 0}, status_code=503)
        return JSONResponse({
            "status": "ok",
            "tools": len(_aggregator._tool_index),
            "servers": [s.name for s in _aggregator.servers],
        })

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        """MCP Streamable HTTP transport."""
        body = await request.json()
        method = body.get("method", "")
        req_id = body.get("id", 1)
        params = body.get("params", {})

        if method == "initialize":
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "sam-mcp-aggregator", "version": "1.0"},
                },
            })

        if method == "tools/list":
            tools = _aggregator.all_tools()
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"tools": tools},
            })

        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            outcome = await _aggregator.call(tool_name, arguments)
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": outcome.text}],
                    "isError": outcome.is_error,
                },
            })

        if method == "notifications/initialized":
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {}})

        return JSONResponse({
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })

    @app.get("/sse")
    async def sse_info():
        """SSE endpoint hint — агрегатор использует Streamable HTTP (/mcp)."""
        return JSONResponse({
            "info": "Use POST /mcp for MCP Streamable HTTP transport",
            "tools": len(_aggregator._tool_index),
        })

    return app


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SAM MCP Aggregator")
    parser.add_argument("--port", type=int, default=env_int("MCP_PORT", 3000))
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    config_dir = os.path.join(os.path.dirname(__file__), "configs")
    config_path = os.path.join(config_dir, "local.json")

    if not os.path.exists(config_path):
        print(f"Config not found: {config_path}")
        print(f"Create configs/local.json with your MCP servers list.")
        print(f"Available: {os.listdir(config_dir)}")
        sys.exit(1)

    log.info(f"Starting aggregator on port {args.port}")
    log.info(f"Config: {config_path}")

    app = create_app(config_path)
    stand_watch_keeper.start("mcp")   # надзор лежит - поднять (SAM-195, холодный старт)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
