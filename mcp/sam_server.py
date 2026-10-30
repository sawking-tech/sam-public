"""
SAM MCP Server (SAM-175): инструменты SAM для основной модели вне шлюза (ведущий Claude Code, агент по ACP) вместо
curl к REST - память, агенты, подтверждение очереди push, переключение модели.

Инструменты:
  memory_search(query, limit)          - факты долгой памяти по запросу
  remember(fact, summary, tags)        - записать факт в долгую память
  create_agent(description, ...)       - агент SAM под задачу (как spawn_agent)
  answer_agent(task_id, message)       - ответ или вводная агенту в сессию task:<id>
  grant_agent_tool(task_id, tool)      - инструмент агенту Cortex по его запросу права
  grant_agent_steps(task_id, steps)    - шаги агенту по его request_steps (SAM-212)
  push_ack(ids)                        - подтвердить сообщения очереди push из тика (push_ids)
  switch_main_model(source, model, main) - сменить основную модель или поставщика модели шлюза без рестарта
  sandbox_exec(code, lang, timeout)    - кусок кода в тёплом контейнере песочницы беседы (ADR-016)
  restart_component(name, force)       - перезапустить компонент стенда без вопроса (SAM-195)

Каждый инструмент - вызов REST шлюза (mcp/sam_http.py); сбой - строкой модели, без стека.
"""

import json
import os
import sys

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))   # gateway из корня
from sam_http import SamHttpError, request  # noqa: E402
from gateway import privilege_key  # noqa: E402
UNAUTHORIZED = "шлюз ответил 401"   # начало ошибки sam_http на отказ в доступе

mcp = FastMCP("sam")
TASK_PREFIX = "task:"


def _call(method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict | str:
    """Вызов REST; привилегированной ручке - с ключом привилегий (ведущему он положен, агентам - нет)."""
    privileged = privilege_key.needs(method, path)
    try:
        return request(method, path, body, query, headers=privilege_key.headers() if privileged else {})
    except SamHttpError as e:
        if not (privileged and str(e).startswith(UNAUTHORIZED)):
            return str(e)
    privilege_key.forget()   # файл ключа пересоздан, пока сервер жил: прочитать заново и повторить один раз
    try:
        return request(method, path, body, query, headers=privilege_key.headers())
    except SamHttpError as e:
        return str(e)


@mcp.tool()
def memory_search(query: str, limit: int = 5) -> str:
    """Факты долгой памяти SAM по запросу: о людях, доме, договорённостях."""
    res = _call("GET", "/sam/memory/search", query={"q": query, "limit": limit})
    return res if isinstance(res, str) else ("\n".join(res.get("facts") or []) or "Ничего не нашлось")


@mcp.tool()
def remember(fact: str, summary: str = "", tags: str = "") -> str:
    """Записать факт в долгую память SAM: один факт одной записью, с тем, кто сказал и когда."""
    res = _call("POST", "/sam/memory", {"fact": fact, "summary": summary, "tags": tags})
    return res if isinstance(res, str) else str(res.get("result", ""))


@mcp.tool()
def create_agent(description: str, level: str = "", tools: list[str] | None = None, name: str = "",
                 device: str = "", framework: str = "", peers: list[str] | None = None, depth: int = 0,
                 steps: int = 0, kind: str = "", env: str = "") -> str:
    """Агент SAM под задачу: действие на устройстве, поиск, код. level - smart, medium или weak. framework - sam
    или cortex; у cortex tools - имена инструментов MCP стенда, peers - id задач, которым агент может писать,
    depth - сколько поколений своих агентов он может создать (0 - ни одного); steps - твоя прикидка шагов на задачу
    (этапы), агент оценит сам, пул - меньшая из оценок; kind - вид работы (проектировать, исследовать, исправить,
    создать, решить, проверить, узнать, сделать), env - inside (внутри экосистемы SAM) или outside (снаружи)."""
    body = {"description": description, **({"level": level} if level else {}), **({"tools": tools} if tools else {}),
            **({"name": name} if name else {}), **({"device": device} if device else {}),
            **({"framework": framework} if framework else {}), **({"peers": peers} if peers else {}),
            **({"depth": depth} if depth else {}), **({"steps": steps} if steps else {}),
            **({"kind": kind} if kind else {}), **({"env": env} if env else {})}
    res = _call("POST", "/sam/agents", body)
    return res if isinstance(res, str) else f"агент {res.get('task_id', '')}: {res.get('status', '')}"


@mcp.tool()
def answer_agent(task_id: str, message: str) -> str:
    """Ответ на вопрос агента или новая вводная - в его сессию; закончившего агента это воскрешает."""
    session = task_id if task_id.startswith(TASK_PREFIX) else f"{TASK_PREFIX}{task_id}"
    res = _call("POST", "/sam/inject", {"session_id": session, "message": message})
    return res if isinstance(res, str) else str(res.get("status") or res)


@mcp.tool()
def agent_effort(task_id: str, direction: str = "more", reason: str = "") -> str:
    """Дать своему агенту больше (more) или меньше (less) сил: не тянет - more, задача проще - less. Модель за этим
    выбирает подборщик; агент продолжает с тем же контекстом."""
    res = _call("POST", f"/sam/agents/{task_id.removeprefix(TASK_PREFIX)}/effort", {"direction": direction, "reason": reason})
    return res if isinstance(res, str) else str(res.get("result") or res)


@mcp.tool()
def agent_info(task_id: str) -> str:
    """Что за агент и как работает: модель, шаги (пул, потрачено, осталось, потолок), время, смены сил."""
    res = _call("GET", f"/sam/agents/{task_id.removeprefix(TASK_PREFIX)}/info")
    return res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)


@mcp.tool()
def grant_agent_tool(task_id: str, tool: str) -> str:
    """Выдать агенту Cortex инструмент по его запросу права (request_permission) или agent:<id> - право писать
    этому агенту; видно со следующего шага."""
    res = _call("POST", f"/sam/agents/{task_id}/grant", {"tool": tool})
    return res if isinstance(res, str) else "выдано"


@mcp.tool()
def grant_agent_steps(task_id: str, steps: int, reason: str = "") -> str:
    """Выдать агенту шаги по его просьбе (request_steps): из своей оценки задачи, не больше нужного. Отказ - через
    answer_agent с причиной: молча агента не оставлять."""
    res = _call("POST", f"/sam/agents/{task_id.removeprefix(TASK_PREFIX)}/steps", {"steps": steps, "reason": reason})
    return res if isinstance(res, str) else str(res.get("result") or "выдано")


@mcp.tool()
def push_ack(ids: list[str]) -> str:
    """Подтвердить сообщения очереди push из тика (поле push_ids): ход по ним закончен."""
    res = _call("POST", "/sam/push/ack", {"ids": ids})
    return res if isinstance(res, str) else f"подтверждено: {res.get('acked', 0)}"


@mcp.tool()
def switch_main_model(source: str = "", model: str = "", main: str = "") -> str:
    """Сменить основную модель без рестарта: main - claude-code (ведущий) или llm (модель шлюза); source - поставщик
    модели шлюза: lmstudio, zai, gemini, dashscope, anthropic, claude_code, acp, auto."""
    res = _call("POST", "/sam/main-model", {"source": source, "model": model, "main": main})
    return res if isinstance(res, str) else str(res.get("result", ""))


@mcp.tool()
def sandbox_exec(code: str, lang: str = "", timeout: int = 0) -> str:
    """Быстрая песочница SAM: кусок кода в тёплом изолированном контейнере беседы за миллисекунды. lang - python
    (по умолчанию; переменные живут между вызовами), shell, node или sql (DuckDB); без сети и файлов ПК; timeout -
    секунд на запуск, зависание гасит контейнер."""
    res = _call("POST", "/sam/sandbox/exec", {"code": code, "lang": lang, "timeout": timeout})
    return res if isinstance(res, str) else str(res.get("result", ""))


@mcp.tool()
def restart_component(name: str, force: bool = False) -> str:
    """Перезапустить компонент стенда SAM, без вопроса: упавший, зависший или после правки его кода. name: gateway
    (после ответа, вкладка поднимет), mcp, voice, bridge, tts, embedder, client, cosy, radar, cortex. Сам проверяет
    health компонента: уже поднялся - не перезапускает; force - без проверки, после правки кода или по прямой просьбе.
    После - проверь /health компонента."""
    res = _call("POST", "/sam/stand/restart", {"name": name, "force": force})
    return res if isinstance(res, str) else str(res.get("result", ""))


if __name__ == "__main__":
    mcp.run()
