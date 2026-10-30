"""SAM-76: границы эндпоинтов gateway - инъекция в задачу и картинки фраз из очереди."""
import pytest

from gateway import agent, main
from tests.fakes_core import app_client  # noqa: F401 - фикстура


@pytest.mark.parametrize("sid, delivered", [
    ("task:abcd", "стоп"),
    ("task:", None),        # пустой префикс входит в имя любой задачи: инъекция ушла бы первой попавшейся
    ("task:   ", None),
], ids=["by-prefix", "empty-prefix", "blank-prefix"])
def test_inject_reaches_task_by_prefix_only(app_client, monkeypatch, sid, delivered):
    client, _ = app_client
    monkeypatch.setattr(agent, "_task_context_map", {"task:abcd1234": ("main", "abcd1234")})   # работающий воркер
    r = client.post("/sam/inject", json={"session_id": sid, "message": "стоп"})
    got = main.event_bus.pop_inject(sid)
    if delivered is None:
        assert r.json()["status"] == "error" and got is None
    else:
        assert r.json()["kind"] == "running_inject" and got == delivered


async def test_first_phrase_images_go_only_with_first_turn():
    """Склеенный ход из очереди не получает картинки первой фразы второй раз."""
    calls = []

    async def _run(msg, device_id, user_id, images):
        calls.append((msg, images))
        return "ok"
    handler = main._turn_handler(_run, "s", None, ["data:image/png;base64,AAA"])
    await handler("смотри")
    await handler("и ещё вот это")
    assert calls == [("смотри", ["data:image/png;base64,AAA"]), ("и ещё вот это", [])]
