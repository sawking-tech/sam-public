"""Состояние компонентов в /health (SAM-80)."""
import threading

import pytest

from gateway import health
from tests.fakes_core import app_client  # noqa: F401  (фикстура)


async def test_component_state_is_one_line():
    released = threading.Event()

    def _ok():
        return None

    def _down():
        raise ConnectionError("localhost:27017: refused\nTopology Description: <...>")

    def _silent():
        raise RuntimeError()

    def _hang():
        released.wait()                     # висит, пока тест не отпустит: срок кончается раньше
    try:
        out = await health.check({"mongo": _ok, "qdrant": _down, "silent": _silent, "mcp": _hang}, timeout_s=0.05)
    finally:
        released.set()
    assert out == {"mongo": "ok", "qdrant": "down: localhost:27017: refused", "silent": "down: RuntimeError",
                   "mcp": "timeout"}


@pytest.mark.parametrize("llm", [{"source": "test", "model": "m"}, None], ids=["llm_available", "no_llm"])
def test_health_endpoint_reports_llm_and_components(app_client, monkeypatch, llm):  # noqa: F811
    from gateway import main
    client, _ = app_client

    async def _llm(*a, **kw):               # поставщики LLM - внешняя граница
        return llm
    monkeypatch.setattr(main, "get_best_llm", _llm)
    monkeypatch.setattr(main, "_health_probes", lambda: {"mongo": lambda: None})
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["components"] == {"mongo": "ok"}
    if llm is None:
        assert body["llm"] is None and body["llm_error"]
    else:
        assert not body.get("llm_error")



SEARCH_HOLD_S = 10    # поиск держит поток не дольше: страховка, если тест сломан
HEALTH_WAIT_S = 3     # /health при свободном цикле отвечает за доли секунды


def test_health_answers_while_a_memory_search_is_running(app_client, monkeypatch):  # noqa: F811
    """stand 53: поиск по памяти идёт в потоке - пока эмбеддер считает, /health отвечает."""
    import concurrent.futures

    from gateway import main
    client, _ = app_client
    searching, released = threading.Event(), threading.Event()

    def slow_search(q, limit):              # эмбеддер синхронный: держит поток, пока тест не отпустит
        searching.set()
        released.wait(SEARCH_HOLD_S)
        return "- кот Барсик"

    async def _llm(*a, **kw):
        return {"source": "test", "model": "m"}
    monkeypatch.setattr(main, "get_rag_context", slow_search)
    monkeypatch.setattr(main, "get_best_llm", _llm)
    monkeypatch.setattr(main, "_health_probes", lambda: {})
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        search = pool.submit(client.get, "/sam/memory/search", params={"q": "кот"})
        try:
            assert searching.wait(SEARCH_HOLD_S)
            health = pool.submit(client.get, "/health").result(timeout=HEALTH_WAIT_S)
            assert health.status_code == 200 and not search.done()
        finally:
            released.set()
        assert search.result().json()["facts"] == ["кот Барсик"]
