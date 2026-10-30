"""
SAM Agent Channel MCP Server (SAM-164): каналы агента разработки (Claude Code, OpenCode) к его создателю в SAM.

Процесс поднимает claude_delegate для одного запуска агента, id задачи и адрес шлюза - в окружении
(SAM_TASK_ID, SAM_SERVER). Инструменты:
  ask_creator(question)  - вопрос создателю; ждёт ответа до SAM_AGENT_ASK_WAIT_S, без ответа - продолжать самому
  report_status(message) - короткий статус в монитор
  messages()             - реплики создателя с прошлого раза (новая вводная посреди работы)

Логика ожидания - чистая функция wait_answer, сеть и часы приходят параметрами.
"""

import os
import sys
import time

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))   # как mcp_aggregator.py: gateway из корня
from gateway.env import env_float  # noqa: E402
from sam_http import SamHttpError, request  # noqa: E402

TASK_ID = os.getenv("SAM_TASK_ID", "")
ASK_WAIT_S = env_float("SAM_AGENT_ASK_WAIT_S", 120.0)   # дольше ответа нет - агент продолжает сам (ADR-022, края)
POLL_S = env_float("SAM_AGENT_POLL_S", 2.0)            # создатель отвечает ходом модели: 4-13 с, чаще - впустую
NO_ANSWER = ("Ответа нет за {wait:.0f} с. Продолжай сам по лучшему пониманию; на чём остановился без ответа - "
             "напиши в итоге.")

mcp = FastMCP("sam-channel")


def wait_answer(inbox, wait_s: float, poll_s: float, clock=time.monotonic, sleep=time.sleep) -> list[str]:
    """Опрашивать входящие до первой реплики или срока; реплики или []."""
    deadline = clock() + wait_s
    while True:
        got = inbox()
        if got or clock() >= deadline:
            return got
        sleep(poll_s)


def _inbox() -> list[str]:
    return request("POST", f"/sam/agents/{TASK_ID}/inbox", {}).get("messages", [])


@mcp.tool()
def ask_creator(question: str) -> str:
    """Спросить создателя (того, кто поставил задачу), если без ответа не обойтись. Ждёт ответа до пары минут."""
    try:
        sent = request("POST", f"/sam/agents/{TASK_ID}/ask", {"question": question})
    except SamHttpError as e:
        return f"Вопрос не отправлен ({e}): продолжай сам"
    if sent.get("status") != "ok":
        return f"Вопрос не отправлен: {sent.get('error', '')}"
    answers = wait_answer(_inbox, ASK_WAIT_S, POLL_S)
    return "\n".join(answers) if answers else NO_ANSWER.format(wait=ASK_WAIT_S)


@mcp.tool()
def report_status(message: str) -> str:
    """Короткий статус создателю: что делаешь, где застрял."""
    try:
        request("POST", f"/sam/agents/{TASK_ID}/status", {"message": message})
        return "ok"
    except SamHttpError as e:
        return f"Статус не отправлен: {e}"


@mcp.tool()
def messages() -> str:
    """Новые реплики создателя с прошлого вызова (вводная посреди работы); пусто - ничего нового."""
    try:
        return "\n".join(_inbox()) or "Новых сообщений нет"
    except SamHttpError as e:
        return f"Входящие недоступны: {e}"


if __name__ == "__main__":
    mcp.run()
