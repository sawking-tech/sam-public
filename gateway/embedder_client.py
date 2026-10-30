"""Клиент эмбеддера-процесса (SAM-172) с интерфейсом модели sentence-transformers: encode и размерность.

RAGStore берёт его вместо локальной модели, если процесс отвечает на /health (SAM_EMBEDDER_URL); иначе - прежняя
локальная загрузка. Сбой процесса посреди работы - запрос идёт локальной моделью, она грузится один раз по требованию.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable

from .env import env_float

log = logging.getLogger(__name__)

HEALTH_TIMEOUT_S = env_float("SAM_EMBEDDER_HEALTH_TIMEOUT_S", 0.5)   # процесс на этой машине: дольше - его нет
EMBED_TIMEOUT_S = env_float("SAM_EMBEDDER_TIMEOUT_S", 10.0)          # пачка реиндекса; фраза - 0,06 с


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def available(base_url: str, timeout: float = HEALTH_TIMEOUT_S) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=timeout) as resp:
            return resp.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


class RemoteEmbedder:
    """encode(text | list, normalize_embeddings=True) как у SentenceTransformer; запасной путь - local_factory."""

    def __init__(self, base_url: str, local_factory: Callable[[], Any],
                 post: Callable[[str, dict, float], dict] = _post) -> None:
        self._url, self._local_factory, self._post = base_url, local_factory, post
        self._local = None
        self._dim: int | None = None

    def encode(self, texts, normalize_embeddings: bool = True):
        single = isinstance(texts, str)
        batch = [texts] if single else list(texts)
        try:
            body = self._post(f"{self._url}/embed", {"texts": batch}, EMBED_TIMEOUT_S)
            vectors = body["vectors"]
            self._dim = body.get("dim") or self._dim
        except (OSError, urllib.error.URLError, ValueError, KeyError) as e:
            log.warning("эмбеддер-процесс не ответил (%s), считаю локально", e)
            local = self._local_model().encode(texts, normalize_embeddings=normalize_embeddings)
            first = local if single else (local[0] if len(local) else [])
            self._dim = self._dim or len(first)   # размерность коллекции Qdrant не должна стать 0 (ревью 12 окт)
            return local
        return vectors[0] if single else vectors

    def get_sentence_embedding_dimension(self) -> int:
        if self._dim is None:
            self.encode("размерность")
        return self._dim or 0

    def _local_model(self):
        if self._local is None:
            self._local = self._local_factory()
        return self._local
