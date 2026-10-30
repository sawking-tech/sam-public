"""Цепочка шагов обработки людей радара (SAM-105): порядок, трасса изменений и отладочный лог хаба."""
import logging

import pytest

from radar import presence_hub as ph
from radar import presence_pipeline as pl
from radar import presence_rssi as rs

OWNER, PHONE_OWNER = "owner", "phone-owner"          # нейтральные идентификаторы людей


def _row(person=OWNER, x=1.0, z=1.0, pose="stand", face=0.0, confidence=0.5):
    return {"person": person, "x": x, "z": z, "pose": pose, "face": face, "confidence": confidence}


STEPS = [("сдвиг", lambda rows, now: [{**r, "x": r["x"] + 1.0} for r in rows]),
         ("поза", lambda rows, now: [{**r, "pose": "sit"} for r in rows]),
         ("без перемен", lambda rows, now: rows),
         ("добавить", lambda rows, now: rows + [_row("b")]),
         ("убрать", lambda rows, now: [r for r in rows if r["person"] != "c"])]


@pytest.mark.parametrize("with_trace", [True, False])
def test_pipeline_runs_steps_in_order_and_traces_each_change(with_trace):
    """Шаги идут по порядку; трасса называет шаг, изменивший место или позу, отмечает появление и удаление; без трассы результат тот же."""
    trace = {} if with_trace else None
    out = pl.run(STEPS, [_row(), _row("c")], 1.0, trace)
    assert [(r["person"], r["x"], r["pose"]) for r in out] == [(OWNER, 2.0, "sit"), ("b", 1.0, "stand")]
    if with_trace:
        assert trace[OWNER][0].startswith("сдвиг: место (1.00; 1.00) -> (2.00; 1.00)")
        assert trace[OWNER][1] == "поза: поза stand -> sit" and len(trace[OWNER]) == 2          # шаг без перемен в трассе не пишется
        assert "добавить: появился" in trace["b"][0] and trace["c"][-1] == "убрать: убран"


def test_hub_debug_log_writes_one_line_per_change_and_survives_a_person_without_place(caplog):
    """С отладкой хаб пишет одну строку на изменение (комната, шаги, источник), повтор не пишет; человек без места не роняет кадр."""
    layout = {"name": "t", "rooms": [{"id": "r", "title": "спальня", "x": 0, "z": 0, "w": 5, "d": 3}],
              "objects": [{"title": "Кровать", "kind": "furniture", "x": 1, "z": 0.5, "w": 1.6, "d": 2.0}], "people": {OWNER: {"gender": "female"}}}
    hub = ph.PresenceHub(ph.MODE_RSSI, layout, clock=lambda: 10.0, owners={"phone": PHONE_OWNER})
    hub.set_person(OWNER, 0.9, "кровать")
    hub.feed_stations([rs.Station("p1", "phy0-ap0", -46.0, 100, "My-Phone")], now=10.0)     # устройство хозяина без места
    with caplog.at_level(logging.INFO, logger="sam.radar"):
        hub.identities(10.0)
        assert "состояние" not in caplog.text                                                   # отладка выключена: подробных строк нет
        hub.set_debug(True)
        rows = hub.identities(10.0)
        hub.identities(10.1)                                                                     # то же состояние: второй строки нет
    assert {r["person"] for r in rows} == {OWNER, PHONE_OWNER}
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith(f"состояние {OWNER}:")]
    assert len(lines) == 1
    assert "комната спальня" in lines[0] and "шаги:" in lines[0] and "ручная вероятность" in lines[0]
    assert f"состояние {PHONE_OWNER}: места нет" in caplog.text
