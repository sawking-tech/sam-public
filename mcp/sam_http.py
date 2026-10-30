"""REST шлюза SAM для MCP-серверов стенда (SAM-164, SAM-175): адрес из SAM_SERVER, ключ доступа - как у всех клиентов.

Сбой сети или ответ не 2xx - исключение SamHttpError одной строкой: инструмент MCP отдаёт её модели как итог.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))   # gateway из корня
from gateway.access import auth_headers  # noqa: E402

SERVER = os.getenv("SAM_SERVER", "http://localhost:8000").rstrip("/")
TIMEOUT_S = 10


class SamHttpError(Exception):
    """Шлюз не ответил или ответил ошибкой; текст - что не так."""


def request(method: str, path: str, body: dict | None = None, query: dict | None = None,
            headers: dict | None = None) -> dict:
    """headers - добавочные заголовки (ключ привилегий у MCP sam)."""
    url = f"{SERVER}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json; charset=utf-8", **auth_headers(),
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise SamHttpError(f"шлюз ответил {e.code}: {detail}") from None
    except (OSError, ValueError) as e:
        raise SamHttpError(f"шлюз недоступен ({e}); запущен ли SAM: start_sam.ps1") from None
