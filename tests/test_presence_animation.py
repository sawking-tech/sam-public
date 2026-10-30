"""Параметры анимации роя на сервере (SAM-113): строки журнала, сохранение сразу и маршруты."""
import json
import logging

import pytest
from fastapi.testclient import TestClient

from radar import presence as pr
from radar import presence_api
from radar import presence_hub as ph


def _hub(file):
    return ph.PresenceHub(ph.MODE_RSSI, pr.EMPTY_LAYOUT, clock=lambda: 10.0, animation_file=file)


def test_animation_change_is_saved_logged_and_restored_on_start(tmp_path, caplog):
    file = tmp_path / "anim.json"
    hub = _hub(file)
    with caplog.at_level(logging.INFO, logger="sam.radar"):
        hub.set_animation({"durMax": 0.4})
        hub.set_animation({"durMax": 0.5, "arc": 0.1})
        hub.set_animation({"durMax": 0.5, "arc": 0.1})                 # без изменений журнал молчит
    lines = [r.getMessage() for r in caplog.records if "анимация" in r.getMessage()]
    assert lines == ["анимация: durMax (нет) -> 0.4", "анимация: durMax 0.4 -> 0.5", "анимация: arc (нет) -> 0.1"]
    assert json.loads(file.read_text(encoding="utf-8")) == {"durMax": 0.5, "arc": 0.1}
    assert _hub(file).animation == {"durMax": 0.5, "arc": 0.1}


@pytest.mark.parametrize("bad", [
    {}, [], {"1x": 1}, {"arc": "x"}, {"arc": -0.1}, {"arc": 11}, {f"k{i}": 1 for i in range(17)},
])
def test_animation_route_accepts_named_numbers_rejects_garbage_and_resets(tmp_path, bad):
    client = TestClient(presence_api.create_app(_hub(tmp_path / "a.json")))
    assert client.get("/sam/presence/animation").json() == {"values": {}}
    assert client.post("/sam/presence/animation", json={"delayEdge": 0.1, "arc": 0}).json() == {"values": {"delayEdge": 0.1, "arc": 0.0}}
    assert client.post("/sam/presence/animation", json=bad).status_code == 422
    assert client.get("/sam/presence/animation").json() == {"values": {"delayEdge": 0.1, "arc": 0.0}}   # мусор ничего не тронул
    assert client.delete("/sam/presence/animation").json() == {"values": {}}
