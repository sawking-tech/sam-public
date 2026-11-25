"""Сообщение радару «человек за компьютером» с хука терминала Claude Code (SAM-118).

Каждое сообщение человека в этом терминале это вход с устройства на рабочем месте: свидетельство о человеке у стола с весом сообщения с устройства, как чат монитора.
Без него радар теряет человека, который говорит с SAM только через терминал. Молчит всегда: радар выключен или занят - хук не должен мешать работе сессии.

Устройство входа и человек задаются настройками, имён и названий машин в коде нет: `SAM_RADAR_DEVICE`, `SAM_RADAR_WHO`, `SAM_RADAR_URL` в окружении или в `.env` проекта.
Без устройства и человека хук ничего не отправляет.

Запуск хуком UserPromptSubmit: python scripts/radar_ping.py."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from enum import StrEnum

from dotenv import dotenv_values


class Setting(StrEnum):
    URL = "SAM_RADAR_URL"
    DEVICE = "SAM_RADAR_DEVICE"
    WHO = "SAM_RADAR_WHO"


PREFIX = "SAM_RADAR_"             # настройки хука начинаются с этого: чужие переменные окружения и .env не читаются
DEFAULT_URL = "http://localhost:8010"
ENV_FILE = ".env"
PATH = "/sam/presence/message"
CHANNEL = "chat"                  # сообщение с устройства входа, как чат монитора
TIMEOUT_S = 1.0                   # радар на этом же компьютере; дольше ждать нечего, хук идёт перед каждой репликой


def settings(env=os.environ, env_file: str = ENV_FILE) -> dict[str, str]:
    """Настройки хука: окружение сильнее файла .env проекта."""
    own = lambda pairs: {k: v for k, v in pairs if v and k.startswith(PREFIX)}
    return {**own(dotenv_values(env_file).items()), **own(env.items())}


def build_request(config: dict[str, str]) -> urllib.request.Request | None:
    """Запрос радару по настройкам; нет устройства или человека - None (отправлять нечего)."""
    device, who = config.get(Setting.DEVICE), config.get(Setting.WHO)
    if not device or not who:
        return None
    body = {"channel": CHANNEL, "device": device, "who": who}
    return urllib.request.Request((config.get(Setting.URL) or DEFAULT_URL) + PATH, data=json.dumps(body).encode("utf8"), headers={"content-type": "application/json"})


def ping(config: dict[str, str]) -> bool:
    """Отправка сообщения; нет настроек или любая ошибка сети - False без исключения."""
    request = build_request(config)
    if request is None:
        return False
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    ping(settings())
    sys.exit(0)
