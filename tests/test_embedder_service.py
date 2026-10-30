"""Эмбеддер отдельным процессом (SAM-172): разбор запроса сервера, клиент с запасным локальным путём."""
import urllib.error

import pytest

from gateway import embedder_client as ec
from gateway import embedder_service as es
from gateway import rag

CAP = 2   # потолок пачки в тесте


def _encode(texts):
    return [[float(len(t)), 1.0] for t in texts]


@pytest.mark.parametrize("payload,code", [
    ({"texts": ["ab", "abc"]}, 200),
    ({}, 400), ({"texts": []}, 400), ({"texts": "строка"}, 400), ({"texts": [1]}, 400), ([], 400),
    ({"texts": ["a", "b", "c"]}, 400),        # больше потолка пачки
], ids=["batch", "no-texts", "empty", "string", "not-strings", "not-object", "over-cap"])
def test_embedder_service_embeds_batch_and_rejects_bad_input(monkeypatch, payload, code):
    monkeypatch.setattr(es, "MAX_TEXTS", CAP)
    got_code, body = es.handle(payload, _encode)
    assert got_code == code
    if code == 200:
        assert body == {"vectors": [[2.0, 1.0], [3.0, 1.0]], "dim": 2}
    else:
        assert "texts" in body["error"] and "\n" not in body["error"]


class _Local:
    """Локальная модель: считает загрузки."""
    loads = 0

    def __init__(self):
        _Local.loads += 1

    def encode(self, texts, normalize_embeddings=True):
        return [9.0] if isinstance(texts, str) else [[9.0]] * len(texts)


def test_remote_embedder_falls_back_to_local_once(monkeypatch):
    seen = []

    def post(url, body, timeout):
        seen.append((url, body))
        return {"vectors": _encode(body["texts"]), "dim": 2}
    alive = ec.RemoteEmbedder("http://x", _Local, post=post)
    assert alive.encode("abc") == [3.0, 1.0] and alive.encode(["a", "bb"]) == [[1.0, 1.0], [2.0, 1.0]]
    assert alive.get_sentence_embedding_dimension() == 2 and seen[0] == ("http://x/embed", {"texts": ["abc"]})

    # Процесс лежит: локальная модель грузится один раз, размер - от её вектора
    _Local.loads = 0

    def refused(url, body, timeout):
        raise urllib.error.URLError("refused")
    dead = ec.RemoteEmbedder("http://x", _Local, post=refused)
    assert dead.encode("a") == [9.0] and dead.encode("b") == [9.0]
    assert dead.get_sentence_embedding_dimension() == 1 and _Local.loads == 1

    # Фабрика памяти выбирает путь по живости процесса
    monkeypatch.setattr(rag, "_local_embedder", lambda: "local")
    monkeypatch.setattr(ec, "available", lambda url, timeout=0.5: False)
    assert rag._default_embedder_factory() == "local"
    monkeypatch.setattr(ec, "available", lambda url, timeout=0.5: True)
    assert isinstance(rag._default_embedder_factory(), ec.RemoteEmbedder)
