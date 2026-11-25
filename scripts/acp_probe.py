"""Проба внешнего ACP-агента в прокси-режиме (ADR-022): тот же клиент, что в шлюзе, мок MCP стенда и живая модель.

Что делает:
  1. Изолированная домашняя папка агента: клиент копирует шаблон прокси-конфига (gateway/acp/hermes) в --home.
  2. Поднимает мок MCP: radar_where с готовым ответом и load_skill с настоящими скиллами стенда - стенд не нужен.
  3. Ход "/tools": список инструментов агента - в прокси-режиме только MCP SAM.
  4. Ход SAM с вопросом про радар: в системной части - факт скилла radar, как его приносит память (без подсказки
     про radar_where). Агент должен прочесть скилл load_skill и спросить радар. Время до текста, вызовы, ответ.

Запуск (ключ модели - ZAI_API_KEY из .env):
  python scripts/acp_probe.py [--command hermes-acp.exe] [--home data/acp/hermes-home] [--port 3999]
Свой агент Cortex (без домашней папки, рабочая папка - корень репозитория, модель - из .env):
  python scripts/acp_probe.py --command python --args "-m cortex.acp" --cwd . --template - \n      --env LLM_SOURCE,LMSTUDIO_URL,LMSTUDIO_MODEL
Код выхода 0 - агент ответил и вызвал радар (читать ли скилл, решает модель - отмечается строкой); 1 - нет.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from gateway.acp import config as acp_config  # noqa: E402
from gateway.acp.agent import AcpAgent  # noqa: E402
from gateway.acp.client import SamClient  # noqa: E402
from gateway.acp.prompt import TEXT, TOOL, prompt_text  # noqa: E402
from gateway import radar_switch, skill_memory, skills  # noqa: E402

TEMPLATE_DIR = ROOT / "gateway" / "acp" / "hermes"
DEFAULT_HOME = ROOT / "data" / "acp" / "hermes-home"
DEFAULT_COMMAND = Path(os.getenv("LOCALAPPDATA", "")) / "hermes" / "hermes-agent" / "venv" / "Scripts" / "hermes-acp.exe"
DEFAULT_PORT = 3999            # мок MCP; стендовый агрегатор на 3000 не трогается
MOCK_HOST = "127.0.0.1"
MOCK_NAME = "sam"
NO_TEMPLATE = "-"
MOCK_ANSWER = "Петя: кабинет, стол, сидит; уверенность 0,9 (мок)"
# Coding Plan z.ai, как у шлюза (gateway/llm_router.ZAI_BASE_URL); у провайдера zai в Hermes по умолчанию другой
ZAI_CODING_URL = "https://api.z.ai/api/coding/paas/v4"
AGENT_ENV = ("ZAI_API_KEY", "GLM_BASE_URL")
HOME_VAR = "HERMES_HOME"
SERVER_START_S = 1.5           # uvicorn мока поднимается за доли секунды; запас на холодный импорт
SAM_SYSTEM = "Ты SAM, домашний ассистент. Отвечай коротко, по-русски."
MEMORY_HEADER = "Из памяти (используй активно):"   # как runner.build_messages
PROBE_SKILL = "radar"
QUESTION = "Где сейчас Петя?"
TOOLS_COMMAND = "/tools"


def start_mock_mcp(port: int, calls: list[str]) -> None:
    """MCP по Streamable HTTP в фоновом потоке: radar_where с готовым ответом, load_skill - настоящие скиллы."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(MOCK_NAME, host=MOCK_HOST, port=port)

    @server.tool()
    def radar_where(name: str = "") -> str:
        """Где сейчас человек по радару квартиры: место, поза, уверенность."""
        calls.append("radar_where")
        return MOCK_ANSWER

    @server.tool()
    def load_skill(name: str) -> str:
        """Загрузить инструкцию скилла по имени и действовать по ней."""
        calls.append(f"load_skill:{name}")
        return skills.load_skill_impl({"name": name}, skills.load_all(allow=skills.allow_list()))

    threading.Thread(target=lambda: server.run(transport="streamable-http"), daemon=True).start()
    time.sleep(SERVER_START_S)


async def run_turn(agent: AcpAgent, text: str) -> tuple[str, list[str], float | None, float]:
    """Ход: ответ, ходы инструментов, секунды до первого текста и до конца."""
    started, first = time.monotonic(), None
    answer, tools = [], []
    async for piece in agent.turn(text):
        if piece.kind == TEXT and piece.text:
            first = first if first is not None else time.monotonic() - started
            answer.append(piece.text)
        elif piece.kind == TOOL:
            tools.append(piece.text)
    return "".join(answer), tools, first, time.monotonic() - started


def configure_env() -> None:
    os.environ.setdefault("GLM_BASE_URL", os.getenv("ZAI_BASE_URL", ZAI_CODING_URL))


async def probe(args: argparse.Namespace) -> int:
    calls: list[str] = []
    configure_env()
    mcp_url = args.mcp_url or f"http://{MOCK_HOST}:{args.port}/mcp"
    if not args.mcp_url:
        start_mock_mcp(args.port, calls)
    template = None if str(args.template) == NO_TEMPLATE else args.template
    cfg = acp_config.AcpConfig(command=str(args.command), args=tuple(shlex.split(args.args, posix=False)),
                               cwd=args.cwd or args.home.parent / "workdir",
                               mcp_url=mcp_url, mcp_name=MOCK_NAME,
                               env_names=AGENT_ENV + tuple(n.strip() for n in args.env.split(",") if n.strip()),
                               home_template=template, home=args.home, home_var=HOME_VAR if template else "")
    agent = AcpAgent(cfg, SamClient())
    try:
        tools_text, _, _, tools_s = await run_turn(agent, TOOLS_COMMAND)
        print(f"[tools] {tools_s:.1f} с\n{tools_text.strip()}\n")
        fact = next(skill_memory.skill_fact(s) for s in skills.load_all() if s.name == PROBE_SKILL)
        messages = [{"role": "system", "content": f"{SAM_SYSTEM}\n\n{MEMORY_HEADER}\n{fact.text}"},
                    {"role": "user", "content": QUESTION}]
        answer, tools, first, total = await run_turn(agent, prompt_text(messages))
    finally:
        await agent.close()
    first_s = f"{first:.1f} с" if first is not None else "нет текста"
    print(f"[ход] до первого текста {first_s}, всего {total:.1f} с; инструменты: {tools or 'нет'}; "
          f"вызовы мока MCP: {calls}")
    print(f"[ответ] {answer.strip()}")
    if not answer.strip():
        print("агент не ответил текстом: смотреть логи в --home/logs")
        return 1
    called = set(calls) | {name for t in tools for name in (f"load_skill:{PROBE_SKILL}", "radar_where")
                           if name.split(":")[0] in t}   # настоящий MCP: вызовы видны ходами инструментов агента
    print(f"[скилл] {'прочитан' if f'load_skill:{PROBE_SKILL}' in called else 'не читался'} - решает модель")
    if "radar_where" not in called:
        print("агент не спросил радар: radar_where не вызван")
        return 1
    return 0


def main() -> int:
    load_dotenv(ROOT / ".env")
    os.environ[radar_switch.ENV] = "1"     # проба идёт на настоящем скилле радара и мок-инструменте radar_where
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--command", type=Path, default=DEFAULT_COMMAND)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--mcp-url", default="", help="настоящий MCP стенда вместо мока, например http://localhost:3000/mcp")
    parser.add_argument("--template", type=Path, default=TEMPLATE_DIR,
                        help=f"шаблон домашней папки агента; {NO_TEMPLATE} - без домашней папки")
    parser.add_argument("--env", default="", help="ещё переменные окружения агенту, через запятую (LLM_SOURCE,LMSTUDIO_URL)")
    parser.add_argument("--args", default="", help="аргументы команды агента строкой, как в shell")
    parser.add_argument("--cwd", type=Path, default=None, help="рабочая папка агента; по умолчанию рядом с --home")
    args = parser.parse_args()
    if not args.command.exists() and not shutil.which(str(args.command)):
        print(f"нет агента {args.command}: укажите --command")
        return 1
    return asyncio.run(probe(args))


if __name__ == "__main__":
    sys.exit(main())
