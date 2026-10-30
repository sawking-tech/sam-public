"""Запуск Cortex по stdio: python -m cortex.acp. Шлюз поднимает его подпроцессом (SAM_ACP_COMMAND, SAM_ACP_ARGS)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
# Буфер строки протокола: подсказка хода с историей и памятью - сотни КБ; как у SDK по умолчанию с запасом
STDIO_LIMIT_BYTES = 16 * 1024 * 1024


async def _main() -> None:
    from .stdio import protocol_streams, take_stdio
    source, sink = take_stdio()   # до импортов цикла: их print уже идёт в stderr

    import acp
    from gateway.llm_router import get_best_llm, source_config
    from gateway.agent_levels import level_source
    from gateway.prompts import DANGEROUS_PATTERNS
    from .agent import CortexAgent
    from .config import from_env

    reader, writer = await protocol_streams(source, sink, STDIO_LIMIT_BYTES)
    agent = CortexAgent(from_env(), get_best_llm, pick_source=source_config, level_source=level_source,
                         dangerous_patterns=tuple(DANGEROUS_PATTERNS))
    # SDK: input_stream - куда пишет агент, output_stream - откуда читает
    await acp.run_agent(agent, input_stream=writer, output_stream=reader)


def main() -> None:
    load_dotenv(ROOT / ".env")   # шлюз передаёт подпроцессу только SAM_ACP_ENV, настройки моделей - из .env
    asyncio.run(_main())


if __name__ == "__main__":
    main()
