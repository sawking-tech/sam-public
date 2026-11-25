"""Запуск радара отдельным веб-сервисом (SAM-105): `python -m radar.presence_service`.

Запускается, только если включён флагом SAM_RADAR (gateway/radar_switch.py). Хост и порт - из SAM_RADAR_HOST и
SAM_RADAR_PORT. Радар не зависит от Mongo, Qdrant, MCP и голоса: ему нужны только
файл планировки, роутер по SSH (режим rssi) и каталог `data/`.
"""
from __future__ import annotations

import os
import sys

import uvicorn
from dotenv import load_dotenv

from gateway import radar_switch, stand_watch_keeper
from gateway.env import env_int

from . import presence_log
from .presence_api import create_app
from .presence_hub import build

HOST_ENV = "SAM_RADAR_HOST"
DEFAULT_HOST = "0.0.0.0"
PORT_ENV = "SAM_RADAR_PORT"
DEFAULT_PORT = 8010


def main() -> None:
    load_dotenv()
    if not radar_switch.enabled():       # выключен флагом: сервис не поднимается и надзор не будит
        sys.exit(radar_switch.OFF_LINE)
    presence_log.setup().info("радар запускается")
    stand_watch_keeper.start("radar")   # надзор лежит - поднять (SAM-195, холодный старт)
    uvicorn.run(create_app(build()), host=os.getenv(HOST_ENV) or DEFAULT_HOST, port=env_int(PORT_ENV, DEFAULT_PORT))


if __name__ == "__main__":
    main()
