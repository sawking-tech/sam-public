"""Клиент радара для SAM (SAM-105, SAM-198): шлюз, сканирование и голос ходят в радар только его функциями.

Радар - отдельное приложение (`radar/`), его модулей SAM не импортирует. Функции - маршруты `/sam/tools/<имя>` сервиса
радара (`radar/presence_sam_api.py`): `note` - свидетельство канала (чат, голос: имя и устройство), `where` и
`layout` - чтение для сканирования. Ключ доступа - как у MCP-переходника (`SAM_API_KEY`, заголовок `x-sam-key`).

Свидетельство уходит в фоне, сбой не мешает ходу чата: после сбоя клиент молчит `RETRY_AFTER_S` секунд, чтобы
выключенный радар не тормозил каждый ход. Чтение отдаёт JSON ответа или None, если радар молчит или отказал.
Пустой `SAM_RADAR_URL` выключает и то и другое; выключенный радар (`gateway/radar_switch.py`) - тоже: `build` отдаёт клиент
без адреса.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable

from . import access, radar_switch

URL_ENV = "SAM_RADAR_URL"
DEFAULT_URL = "http://127.0.0.1:8010"
# Функции радара: префикс и имена - контракт с radar/presence_sam_api.py (PREFIX, NOTE, WHERE, LAYOUT), сверяет тест
TOOLS_PREFIX = "/sam/tools"
NOTE, WHERE, LAYOUT = "note", "where", "layout"
TIMEOUT_S = 2.0                # радар на этой же машине: дольше ждать нечего
RETRY_AFTER_S = 30.0           # пауза свидетельств после сбоя связи


def _headers(body: bytes | None) -> dict:
    key = access.configured_key()
    return {**({access.HEADER: key} if key else {}), **({"Content-Type": "application/json"} if body else {})}


def _post(url: str, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    urllib.request.urlopen(urllib.request.Request(url, data, _headers(data)), timeout=TIMEOUT_S).close()


def _get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, None, _headers(None)), timeout=TIMEOUT_S) as resp:
        return json.load(resp)


class RadarClient:
    def __init__(self, base_url: str, post: Callable[[str, dict], None] = _post, get: Callable[[str], object] = _get,
                 clock: Callable[[], float] = time.monotonic, background: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self._post = post
        self._get = get
        self._clock = clock
        self._background = background
        self._silent_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def url(self, name: str, query: dict | None = None) -> str:
        return f"{self.base_url}{TOOLS_PREFIX}/{name}" + (f"?{urllib.parse.urlencode(query)}" if query else "")

    def note_message(self, channel: str, device: str | None = None, who: str | None = None,
                     level: float | None = None, place: str | None = None) -> None:
        """Свидетельство канала функцией note: в фоне, без ответа."""
        if not self.enabled or self._clock() < self._silent_until:
            return
        payload = {"channel": channel, "device": device, "who": who, "level": level, "place": place}
        if self._background:
            threading.Thread(target=self._send, args=(payload,), daemon=True).start()
        else:
            self._send(payload)

    def _send(self, payload: dict) -> None:
        try:
            self._post(self.url(NOTE), payload)
        except Exception:        # радар выключен, лежит или отказал: чат важнее, повтор после паузы
            self._silent_until = self._clock() + RETRY_AFTER_S

    def read(self, name: str, query: dict | None = None):
        """Ответ функции чтения (where, layout) разобранным JSON; радар выключен, молчит или отказал - None."""
        if not self.enabled:
            return None
        try:
            return self._get(self.url(name, query))
        except (OSError, ValueError):     # HTTPError (409 - радар выключен) - тоже OSError
            return None


def build(env=os.environ) -> RadarClient:
    return RadarClient(env.get(URL_ENV, DEFAULT_URL) if radar_switch.enabled(env) else "")
