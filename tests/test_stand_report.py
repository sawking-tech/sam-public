"""Отчёты надзора в шлюзе (SAM-195): ручка POST /sam/stand/watch, отбивка в памяти и файле, смена - событие шины,
на него срабатывает workflow stand-health.yml. Подменены только файл отбивки (tmp_path) и шаг основной модели."""
import asyncio
from functools import partial
from pathlib import Path

import pytest

from gateway import main, stand_report as sr
from gateway import workflows as wf
from gateway.event_bus import EventBus, event_bus
from gateway.workflow_spec import load_dir
from tests.fakes_agents import _polled, _sched
from tests.fakes_core import app_client  # noqa: F401  (фикстура)

REPO_WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
FELL = sr.Change("bridge", sr.UP, "down")
ROSE = sr.Change("voice", "down", sr.UP)
SOON_S = 2.0
AT = 1_791_000_000.0   # 14 окт 2026: Windows не переводит в местное время слишком ранние метки


def _reports(tmp_path, emit, clock=lambda: AT, log=print, last=None):
    return sr.StandReports(emit, partial(sr.save_beat, tmp_path / "beat.json"), clock=clock, log=log, last=last)


@pytest.mark.parametrize("before, now, expected", [
    ({}, {"bridge": sr.UP, "voice": "down"}, [("voice", None, "down")]),            # первая отбивка - только не up
    ({"bridge": sr.UP}, {"bridge": sr.UP}, []),                                     # повтор того же - не смена
    ({"bridge": sr.UP}, {"bridge": "x2"}, [("bridge", sr.UP, "x2")]),
    ({"bridge": sr.UP}, {"bridge": sr.UP, "compress:voice": sr.UP}, []),            # новая сессия сжатия, живая
    ({sr.GATEWAY: sr.UP, "compress:voice": "stalled"}, {sr.GATEWAY: sr.UP},
     [("compress:voice", "stalled", sr.UP)]),                                       # ушла из /health - подъём
    ({sr.GATEWAY: sr.UP, "compress:voice": sr.UP}, {sr.GATEWAY: sr.UP}, []),        # ушла живая - ничего
], ids=["first", "same", "changed", "new-up", "gone-stalled", "gone-up"])
def test_gateway_counts_changes_from_the_last_beat(before, now, expected):
    assert [(c.component, c.before, c.now) for c in sr.transitions(before, now)] == expected


@pytest.mark.parametrize("changes, expected", [
    ([], None),
    ([FELL], {"changes": "bridge up -> down", "down": "bridge", "restart": "bridge"}),
    ([FELL, ROSE], {"changes": "bridge up -> down; voice down -> up", "down": "bridge", "up": "voice", "restart": "bridge"}),
    ([sr.Change("gateway", None, "x2")], {"changes": "gateway x2", "down": "gateway"}),   # впервые
], ids=["none", "fell", "fell-and-rose", "first-seen"])
def test_change_event_names_what_fell_and_what_rose(changes, expected):
    assert sr.change_event(changes) == expected


def test_beat_is_remembered_and_saved_and_only_a_change_is_an_event(tmp_path):
    events = []
    reports = _reports(tmp_path, lambda kind, data: events.append((kind, data)))
    assert reports.accept({"bridge": sr.UP}) is None and events == []              # отбивка без смен - не событие
    assert reports.last == sr.Beat(AT, {"bridge": sr.UP})
    assert sr.load_beat(tmp_path / "beat.json") == reports.last

    reports.accept({"bridge": "down"})
    reports.accept({"bridge": "down"})                       # повтор отчёта, на который шлюз не успел ответить
    assert events == [(sr.EVENT, {"changes": "bridge up -> down", "down": "bridge", "restart": "bridge"})]


def test_gateway_restart_takes_the_last_beat_from_the_file(tmp_path):
    """Шлюз лежал: прошлая отбивка поднимается из файла, его собственный простой и пропавшее на это время сжатие
    событием не становятся, а мост, упавший за это время, - становится."""
    events = []
    _reports(tmp_path, lambda kind, data: None).accept({"gateway": sr.UP, "bridge": sr.UP, "compress:voice": sr.UP})
    after = _reports(tmp_path, lambda kind, data: events.append(data), last=sr.load_beat(tmp_path / "beat.json"))
    after.accept({"gateway": sr.UP, "bridge": "down"})
    assert events == [{"changes": "bridge up -> down", "down": "bridge", "restart": "bridge"}]


def test_unwritable_beat_file_logs_one_line_and_the_event_still_goes(tmp_path):
    events, logs = [], []

    def broken(beat):
        raise PermissionError("нет доступа")
    reports = sr.StandReports(lambda kind, data: events.append(kind), broken, log=logs.append)
    reports.accept({"bridge": "down"})
    assert events == [sr.EVENT] and len(logs) == 1 and "\n" not in logs[0] and reports.last is not None


@pytest.mark.parametrize("content", [None, "", "{not json", '{"states": {}}'], ids=["missing", "empty", "broken", "no-at"])
def test_missing_or_broken_beat_file_reads_as_no_beat(tmp_path, content):
    path = tmp_path / "beat.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert sr.load_beat(path) is None


def test_report_endpoint_puts_the_change_on_the_bus(app_client, tmp_path, monkeypatch):
    client, _ = app_client
    seen = []
    monkeypatch.setattr(event_bus, "_listeners", [lambda kind, data: seen.append((kind, data))])
    monkeypatch.setattr(main, "_stand_reports", _reports(tmp_path, event_bus.emit))

    beat = client.post(sr.REPORT_PATH, json={"states": {"bridge": sr.UP}})
    assert beat.status_code == 200 and beat.json() == {"event": None} and seen == []

    r = client.post(sr.REPORT_PATH, json={"states": {"bridge": "down"}})
    assert r.status_code == 200 and r.json()["event"]["down"] == "bridge"
    assert [kind for kind, _ in seen] == [sr.EVENT] and seen[0][1]["changes"] == "bridge up -> down"
    assert sr.load_beat(tmp_path / "beat.json").states == {"bridge": "down"}


def test_report_endpoint_takes_only_json(app_client):
    client, _ = app_client
    r = client.post(sr.REPORT_PATH, content=b"{}", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


async def test_stand_health_workflow_fires_on_a_change_from_the_bus(tmp_path):
    """Настоящий stand-health.yml на настоящей шине: смена из отчёта надзора - шаг основной модели с поводом; отбивка
    без смен модель не зовёт."""
    flow = next(f for f in load_dir(REPO_WORKFLOWS, warn=pytest.fail) if f.name == "stand-health")
    assert flow.event.name == sr.EVENT and flow.event.collect and flow.category == "stand"
    s, calls, _ = _sched([flow])
    s._loop = asyncio.get_running_loop()
    bus = EventBus()
    bus.add_listener(s.on_event)
    reports = _reports(tmp_path, bus.emit)

    reports.accept({"bridge": sr.UP})
    reports.accept({"bridge": "down"})
    await asyncio.wait_for(_polled(lambda: calls), SOON_S)
    src, text, session = calls[0]
    assert src == wf.source("stand-health", "tell") and session == "voice"
    assert f"Событие стенда: {sr.EVENT}" in text and "bridge up -> down" in text and "<silent></silent>" in text
    assert "restart_component" in text and "без вопроса" in text   # упавшее SAM перезапускает сама
    assert "сам проверит" in text and "не лечится" in text and "x2 у gateway" in text and "mcp_call" not in text
    assert len(calls) == 1


def test_gone_compression_is_not_a_rise_when_gateway_health_did_not_answer():
    before = {"gateway": sr.UP, "compress:voice": "stalled"}
    assert [(c.component, c.now) for c in sr.transitions(before, {"gateway": "down"})] == [("gateway", "down")]
