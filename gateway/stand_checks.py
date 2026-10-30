"""Health check компонентов стенда (SAM-195): одни и те же проверки у надзора (scripts/stand_watch.py) и у перезапуска
(gateway/restart.py, «уже поднялся - перезапуск не нужен»).

Имена - как у restart_component. Компонент жив, когда его процесс запущен ровно один раз (два шлюза на одном порту -
тоже поломка, 9 окт старый шлюз пережил перезапуск и держал порт) и его health check отвечает: /health шлюза, MCP,
эмбеддера и радара; у слуха и моста HTTP нет - их файл живости в шине (voice/layer/health.py); у синтеза - только
процесс. Cortex живёт в шлюзе - строка cortex в components его /health. Выключить компонент на этой машине (радар не
запущен) - SAM_STAND_WATCH_OFF через запятую.

Решение (worst, process_state, health_state, cortex_state, combine) отделено от сети, процессов и файлов.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request

from voice.layer import health as voice_health
from voice.layer.config import BUS_DIR

from . import radar_client
from .stand_report import GATEWAY, UP

DOWN = "down"
HTTP_TIMEOUT_S = 3
HEALTH_PATH = "/health"
GATEWAY_URL = os.getenv("SAM_SERVER", "http://localhost:8000")
# Адреса по умолчанию - как у их клиентов: gateway/rag.py (SAM_EMBEDDER_URL), gateway/radar_client.py
# (SAM_RADAR_URL)
HEALTH = {
    GATEWAY: GATEWAY_URL + HEALTH_PATH,
    "mcp": f"http://localhost:{os.getenv('MCP_PORT', '3000')}{HEALTH_PATH}",
    "embedder": (os.getenv("SAM_EMBEDDER_URL") or "http://127.0.0.1:10204").rstrip("/") + HEALTH_PATH,
    "radar": (os.getenv(radar_client.URL_ENV) or radar_client.DEFAULT_URL).rstrip("/") + HEALTH_PATH,
}
# Процесс -> подстрока командной строки; каждый должен быть запущен ровно один раз
PROCESSES = {
    GATEWAY: "uvicorn gateway.main:app",
    "voice": "voice.layer.voice",
    "bridge": "voice.layer.bridge",
    "tts": "voice.layer.tts_silero_server",
}
FILES = {"voice": voice_health.VOICE_HEALTH, "bridge": voice_health.BRIDGE_HEALTH}   # файл живости в шине
CORTEX = "cortex"            # строка components в /health шлюза
OFF = {n.strip() for n in os.getenv("SAM_STAND_WATCH_OFF", "").split(",") if n.strip()}
# Таблица процессов через powershell - около секунды; дольше 10 с - WMI повис, а проверка стоит на пути хода
PROCESS_QUERY_TIMEOUT_S = 10.0
SUPERVISED = tuple(dict.fromkeys([*HEALTH, *PROCESSES, *FILES]))
PROCESS_QUERY = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                 "ForEach-Object { $_.CommandLine }")


def fetch_health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as r:
            return json.load(r)
    except Exception:
        return None


def health_state(body: dict | None) -> str:
    return UP if body and body.get("status") == "ok" else DOWN


def python_cmdlines() -> list[str] | None:
    """Командные строки процессов python; таблица не прочиталась (не Windows, нет powershell, WMI повис) - None."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", PROCESS_QUERY], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=PROCESS_QUERY_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def process_state(count: int) -> str:
    return UP if count == 1 else (DOWN if count == 0 else f"x{count}")


def worst(*states: str | None) -> str:
    """Состояние компонента из нескольких проверок: первая не up (x2, down), иначе up; проверок нет - down."""
    known = [st for st in states if st is not None]
    return next((st for st in known if st != UP), UP if known else DOWN)


def read_file_health(name: str, now: float) -> str | None:
    """Файл живости в шине; нет файла - None: процесс только поднялся, судит счёт процессов."""
    return voice_health.state(voice_health.read(BUS_DIR / name), now)


def cortex_state(gateway_health: dict | None, previous: dict[str, str] | None = None) -> dict[str, str]:
    """Cortex по строке components в /health шлюза; шлюз не ответил - из прошлого снимка; старый шлюз без строки -
    ничего."""
    if gateway_health is None:
        return {k: v for k, v in (previous or {}).items() if k == CORTEX}
    line = (gateway_health.get("components") or {}).get(CORTEX)
    return {} if line is None else {CORTEX: UP if line == "ok" else DOWN}


def combine(name: str, lines: list[str] | None, bodies: dict[str, dict | None], now: float) -> str:
    """Состояние одного компонента из процесса, HTTP и файла живости."""
    proc = (process_state(sum(PROCESSES[name] in ln for ln in lines))
            if name in PROCESSES and lines is not None else None)
    http = health_state(bodies[name]) if name in bodies else None
    file = read_file_health(FILES[name], now) if name in FILES else None
    return worst(proc, http, file)


def component_states(names: list[str] | None = None, now: float | None = None) -> tuple[dict[str, str], dict]:
    """Состояния компонентов (по умолчанию - всех, кроме OFF) и ответы /health по имени."""
    now = time.time() if now is None else now
    names = [n for n in (names or SUPERVISED) if n in SUPERVISED and n not in OFF]
    lines = python_cmdlines() if any(n in PROCESSES for n in names) else None
    bodies = {n: fetch_health(HEALTH[n]) for n in names if n in HEALTH}
    return {n: combine(n, lines, bodies, now) for n in names}, bodies


def check(name: str) -> str | None:
    """Состояние одного компонента для перезапуска; проверки у него нет (client, cosy) - None."""
    return component_states([name])[0].get(name)
