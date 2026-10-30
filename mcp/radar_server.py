"""
SAM Radar MCP Server (SAM-105, radar-in-sam): переходник к ручкам радара для моделей вне шлюза.

SAM в шлюзе зовёт радар как приложение: `app_call(radar, where, {person})`. Модели, которые видят только MCP стенда
(агенты Cortex, ADR-022; ведущая сессия Claude Code), получают те же ручки инструментами radar_*:
  radar_where(person)             - where
  radar_history(person, at)       - history
  radar_layout()                  - layout
  radar_tell(person, place, pose) - tell
  radar_devices()                 - devices
  radar_furniture(room)           - furniture

Ответы и описания собирает сервис радара (`radar/presence_sam_api.py`, маршруты /sam/tools/*); переходник только
пересылает аргументы и возвращает текст. Адрес - SAM_RADAR_URL, ключ - SAM_API_KEY, как у прокси шлюза.
Радар не отвечает - одна строка, что делать.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP

sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))   # как mcp_aggregator.py: gateway из корня
from gateway import access  # noqa: E402
from radar import presence_sam_api as api  # noqa: E402
from gateway.app_tools import answer  # noqa: E402

URL_ENV = "SAM_RADAR_URL"
DEFAULT_URL = "http://127.0.0.1:8010"
TIMEOUT_S = 5.0
UNREACHABLE = "радар не отвечает на {base}: запусти powershell -File start_radar.ps1"

mcp = FastMCP("radar")


def _base() -> str:
    return (os.getenv(URL_ENV) or DEFAULT_URL).rstrip("/")


def _headers(body: bytes | None) -> dict:
    key = access.configured_key()
    return {**({access.HEADER: key} if key else {}), **({"Content-Type": "application/json"} if body else {})}


def call(name: str, query: dict | None = None, body: dict | None = None) -> str:
    """Ручка радара: GET со строкой запроса или POST с JSON; ответ ручки - как есть, отказ - с кодом."""
    base = _base()
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    url = f"{base}{api.PREFIX}/{name}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, _headers(data)), timeout=TIMEOUT_S) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return answer(e.code, e.read().decode("utf-8", "replace"))
    except OSError:
        return UNREACHABLE.format(base=base)


@mcp.tool(description=f"{api.DESCRIPTIONS[api.WHERE]}. person - {api.PERSON_ANY}")
def radar_where(person: str = "") -> str:
    return call(api.WHERE, {"person": person})


@mcp.tool(description=f"{api.DESCRIPTIONS[api.HISTORY]}. person - {api.PERSON}; at - {api.AT}")
def radar_history(person: str, at: str) -> str:
    return call(api.HISTORY, {"person": person, "at": at})


@mcp.tool(description=api.DESCRIPTIONS[api.LAYOUT])
def radar_layout() -> str:
    return call(api.LAYOUT)


@mcp.tool(description=f"{api.DESCRIPTIONS[api.TELL]}. person - {api.PERSON}; place - {api.PLACE}; pose - {api.POSE}")
def radar_tell(person: str, place: str, pose: str = "") -> str:
    return call(api.TELL, body={"person": person, "place": place, "pose": pose})


@mcp.tool(description=api.DESCRIPTIONS[api.DEVICES])
def radar_devices() -> str:
    return call(api.DEVICES)


@mcp.tool(description=f"{api.DESCRIPTIONS[api.FURNITURE]}. room - {api.ROOM}")
def radar_furniture(room: str = "") -> str:
    return call(api.FURNITURE, {"room": room})


if __name__ == "__main__":
    mcp.run()
