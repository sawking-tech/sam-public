"""Канал агента разработки к SAM (SAM-164): Claude Code, OpenCode и любой каркас, запущенный воркером через
claude_delegate, говорят с создателем теми же каналами, что воркеры SAM (ADR-022: права и каналы одинаковы для любого
каркаса). Каналы - MCP-инструменты процесса агента (mcp/agent_channel_server.py) поверх REST шлюза:

  POST /sam/agents/{id}/ask     вопрос создателю (ask_creator воркера)
  GET  /sam/agents/{id}/inbox   реплики создателя в сессию task:<id> с прошлого раза (ответ на вопрос, новая вводная)
  POST /sam/agents/{id}/status  статус в монитор (report_status)

Здесь - логика без сети и FastAPI: что делать с запросом по контексту задачи и как собрать конфиг MCP для CLI.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

SERVER_NAME = "sam-channel"   # имя MCP-сервера у агента: инструменты mcp__sam-channel__ask_creator и т.д.
SERVER_SCRIPT = Path(__file__).resolve().parents[2] / "mcp" / "agent_channel_server.py"
TASK_ENV, GATEWAY_ENV = "SAM_TASK_ID", "SAM_SERVER"


def mcp_config(task_id: str, gateway_url: str, python: str = sys.executable) -> str:
    """Конфиг MCP одной строкой JSON для `claude --mcp-config`: stdio-сервер канала с id задачи."""
    return json.dumps({"mcpServers": {SERVER_NAME: {
        "command": python, "args": [str(SERVER_SCRIPT)],
        "env": {TASK_ENV: task_id, GATEWAY_ENV: gateway_url},
    }}}, ensure_ascii=False)


def drain(pop: Callable[[], str | None], limit: int) -> list[str]:
    """Все реплики создателя из очереди сессии задачи, не больше limit."""
    out = []
    while len(out) < limit and (msg := pop()) is not None:
        out.append(msg)
    return out


def status_event(parent_session: str, short_id: str, message: str) -> dict:
    """Статус агента разработки - как report_status воркера: событие task_message в монитор."""
    return {"content": message, "session": parent_session, "task_id": short_id, "kind": "status"}
