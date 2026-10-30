"""Фейковый ACP-агент для тестов клиента шлюза (gateway/acp): говорит по stdio тем же SDK, что Hermes.

Поведение задаётся меткой в тексте хода:
  MODE:echo        - ответ кусками "Привет, " и "Петя", между ними ход инструмента
  MODE:permission  - просит разрешение на delete_file и отвечает выбранным вариантом
  MODE:always      - то же, но предлагает только «разрешить навсегда»
  MODE:die         - процесс выходит посреди хода
  MODE:env         - отвечает значениями ACP_FAKE_PASSED и ACP_FAKE_HIDDEN из своего окружения
  MODE:home        - отвечает текстом файла config.yaml из папки в ACP_FAKE_HOME
  MODE:think       - мысль агента (agent_thought_chunk), потом ответ "Ирина в зале."
  MODE:mcp         - отвечает MCP-серверами своей сессии: «имя url» через «;», без серверов - none
Без метки - echo.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import acp
from acp import InitializeResponse, NewSessionResponse, PromptResponse
from acp.schema import PermissionOption, ToolCallUpdate

ECHO, PERMISSION, DIE, ENV, HOME = "MODE:echo", "MODE:permission", "MODE:die", "MODE:env", "MODE:home"
ALWAYS = "MODE:always"
THINK = "MODE:think"
MCP = "MODE:mcp"
DIE_EXIT_CODE = 3


class FakeAgent:
    def __init__(self) -> None:
        self._conn = None
        self._mcp: dict[str, list] = {}

    def on_connect(self, conn) -> None:
        self._conn = conn

    async def initialize(self, protocol_version: int, **kwargs) -> InitializeResponse:
        return InitializeResponse(protocol_version=protocol_version)

    async def new_session(self, cwd: str, mcp_servers=None, **kwargs) -> NewSessionResponse:
        sid = uuid.uuid4().hex
        self._mcp[sid] = list(mcp_servers or [])
        return NewSessionResponse(session_id=sid)

    async def prompt(self, prompt, session_id: str, **kwargs) -> PromptResponse:
        text = "".join(getattr(block, "text", "") for block in prompt)
        if DIE in text:
            os._exit(DIE_EXIT_CODE)
        if HOME in text:
            home = Path(os.environ["ACP_FAKE_HOME"])
            await self._say(session_id, (home / "config.yaml").read_text(encoding="utf-8").strip())
            return PromptResponse(stop_reason="end_turn")
        if ENV in text:
            await self._say(session_id, f"{os.getenv('ACP_FAKE_PASSED')}|{os.getenv('ACP_FAKE_HIDDEN')}")
            return PromptResponse(stop_reason="end_turn")
        if MCP in text:
            servers = "; ".join(f"{m.name} {m.url}" for m in self._mcp.get(session_id, []))
            await self._say(session_id, servers or "none")
            return PromptResponse(stop_reason="end_turn")
        if THINK in text:
            await self._conn.session_update(session_id, acp.update_agent_thought_text("Ирина вчера была в зале"))
            await self._say(session_id, "Ирина в зале.")
            return PromptResponse(stop_reason="end_turn")
        if PERMISSION in text or ALWAYS in text:
            options = [PermissionOption(option_id="allow_always", name="Always", kind="allow_always")]
            if PERMISSION in text:
                options = [PermissionOption(option_id="allow_once", name="Allow", kind="allow_once"), *options,
                           PermissionOption(option_id="reject_once", name="Reject", kind="reject_once")]
            reply = await self._conn.request_permission(
                options=options,
                session_id=session_id,
                tool_call=ToolCallUpdate(tool_call_id="t1", title="delete_file", raw_input={"path": "x.txt"}))
            chosen = getattr(reply.outcome, "option_id", None) or reply.outcome.outcome
            await self._say(session_id, f"permission:{chosen}")
            return PromptResponse(stop_reason="end_turn")
        await self._say(session_id, "Привет, ")
        await self._conn.session_update(session_id, acp.start_tool_call("t1", "radar_where", status="in_progress"))
        await self._say(session_id, "Петя")
        return PromptResponse(stop_reason="end_turn")

    async def _say(self, session_id: str, text: str) -> None:
        await self._conn.session_update(session_id, acp.update_agent_message_text(text))

    async def cancel(self, session_id: str, **kwargs) -> None:
        return None


if __name__ == "__main__":
    asyncio.run(acp.run_agent(FakeAgent()))
