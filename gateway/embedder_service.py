"""Эмбеддер отдельным долгоживущим процессом (SAM-172): модель грузится раз, рестарт шлюза её не трогает.

  python -m gateway.embedder_service [--port 10204]

Холодная загрузка paraphrase-multilingual-MiniLM-L12-v2 вместе с torch - 28,8 с (замер 12 окт), тёплый поиск -
0,06-0,28 с; шлюз до SAM-172 грузил модель на каждом старте, и первые полминуты память искалась одними словами.
HTTP на стандартной библиотеке: POST /embed {"texts": [...]} -> {"vectors": [[...]], "dim": N}, GET /health.
Разбор запроса - чистая функция handle, модель приходит параметром.
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .env import env_int

PORT = env_int("SAM_EMBEDDER_PORT", 10204)   # рядом с синтезом Silero (10202) и CosyVoice (10203)
HOST = "127.0.0.1"                            # только эта машина: тексты памяти наружу не ходят
MAX_TEXTS = env_int("SAM_EMBEDDER_MAX_TEXTS", 256)   # пачка реиндекса памяти - сотни фактов; больше - частями
LABEL = "[эмбеддер]"

Encode = Callable[[list[str]], list[list[float]]]


def handle(payload: dict, encode: Encode) -> tuple[int, dict]:
    """Тело запроса /embed -> (код, ответ). Ошибка ввода - 400 одной строкой."""
    texts = payload.get("texts") if isinstance(payload, dict) else None
    if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
        return 400, {"error": "texts - непустой список строк"}
    if len(texts) > MAX_TEXTS:
        return 400, {"error": f"texts - не больше {MAX_TEXTS} за запрос"}
    vectors = encode(texts)
    return 200, {"vectors": vectors, "dim": len(vectors[0]) if vectors else 0}


def _handler(encode: Encode, model_name: str):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, {"status": "ok", "model": model_name})
            else:
                self._send(404, {"error": "есть GET /health и POST /embed"})

        def do_POST(self) -> None:
            if self.path != "/embed":
                self._send(404, {"error": "есть GET /health и POST /embed"})
                return
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                self._send(400, {"error": "тело - JSON"})
                return
            self._send(*handle(payload, encode))

        def log_message(self, fmt: str, *args) -> None:   # запрос на каждую фразу - без строки в консоль
            pass
    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Эмбеддер SAM отдельным процессом")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    from .embed_model import load_sentence_model
    from .rag import EMBED_MODEL_NAME
    started = time.monotonic()
    model = load_sentence_model(EMBED_MODEL_NAME)
    model.encode("прогрев", normalize_embeddings=True)
    print(f"{LABEL} {EMBED_MODEL_NAME} загружен за {time.monotonic() - started:.1f} с, порт {args.port}", flush=True)

    def encode(texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in model.encode(texts, normalize_embeddings=True)]
    ThreadingHTTPServer((HOST, args.port), _handler(encode, EMBED_MODEL_NAME)).serve_forever()


if __name__ == "__main__":
    main()
