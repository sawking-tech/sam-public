"""
SAM Skills MCP Server: скиллы стенда для моделей вне шлюза (агент по ACP, ADR-022; модель из shell, ADR-019).

Инструменты:
  load_skill(name)  - тело скилла: инструкция, по которой действовать
  list_skills()     - имена и описания скиллов

Факт скилла с командой load_skill приносит память SAM к фразе (gateway/skill_memory.py); этот сервер - то, чем модель
вне шлюза команду выполняет. Скиллы читаются с диска на каждый вызов (gateway/skills.py): правка SKILL.md видна сразу,
без перезапуска. Папка, белый список и раздаваемые личные скиллы - те же переменные, что у шлюза.
"""

import os
import sys

from mcp.server.fastmcp import FastMCP

sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))   # как mcp_aggregator.py: gateway из корня
from gateway import skills  # noqa: E402

mcp = FastMCP("skills")


def _index() -> list:
    return skills.load_all(allow=skills.allow_list())


@mcp.tool()
def load_skill(name: str) -> str:
    """Загрузить инструкцию скилла по имени и действовать по ней. Имя - из факта памяти «Скилл «имя»» или list_skills."""
    return skills.load_skill_impl({"name": name}, _index())


@mcp.tool()
def list_skills() -> str:
    """Скиллы стенда: имя и описание каждого."""
    return skills.render_index(_index()) or "Скиллов нет"


if __name__ == "__main__":
    mcp.run()
