"""Тревога по тишине надзора (SAM-195): ворота gateway/stand_silence.py, workflow stand-silence.yml на настоящей
команде ворот, возраст отбивки в /health. Подменены файл отбивки (tmp_path), окружение и шаг основной модели."""
import os
from functools import partial
from pathlib import Path

import pytest

from gateway import main, stand_report as sr, stand_silence as ss
from gateway import workflows as wf
from gateway.workflow_spec import load_dir
from tests.fakes_agents import _sched
from tests.fakes_core import app_client  # noqa: F401  (фикстура)

REPO_WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
AT = 1_791_000_000.0   # 14 окт 2026: Windows не переводит в местное время слишком ранние метки
SILENT = 90


def _beat(at):
    return sr.Beat(at, {"gateway": sr.UP})


@pytest.mark.parametrize("beat, started, now, alarm", [
    (_beat(AT), AT - 600, AT + SILENT, False),            # ровно порог - ещё не тревога
    (_beat(AT), AT - 600, AT + SILENT + 1, True),         # надзор замолчал
    (None, AT, AT + SILENT, False),                        # файла нет, шлюз только поднялся
    (None, AT, AT + SILENT + 1, True),                     # файла нет дольше порога после старта
    (_beat(AT - 3600), AT, AT + 30, False),                # старая отбивка, шлюз только поднялся: надзор не успел
    (_beat(AT - 3600), AT, AT + SILENT + 1, True),         # после подъёма шлюза надзор так и молчит
    (_beat(AT), None, AT + SILENT + 1, True),              # ворота вне шлюза: только отбивка
    (None, None, AT, True),                                # отбивок не было, старт неизвестен
], ids=["at-threshold", "silent", "fresh-start", "no-file", "restart-grace", "silent-after-restart",
        "no-start", "nothing"])
def test_alarm_when_the_supervisor_is_silent_longer_than_the_threshold(beat, started, now, alarm):
    assert bool(ss.silence(beat, started, now, SILENT)) is alarm


def test_one_silence_is_one_reason_so_the_model_wakes_once():
    """Вывод ворот не меняется, пока молчание то же: ворота с тем же выводом модель не будят."""
    first = ss.silence(_beat(AT), AT - 600, AT + SILENT + 1, SILENT)
    assert first == ss.silence(_beat(AT), AT - 600, AT + 3600, SILENT)
    assert "надзор стенда молчит" in first and "\n" not in first
    assert first != ss.silence(_beat(AT + 600), AT - 600, AT + 3600, SILENT)   # новое молчание - новый повод


@pytest.mark.parametrize("raw, expected", [("", None), ("12.5", 12.5), ("вчера", None)])
def test_gateway_start_is_read_from_the_environment(raw, expected):
    assert ss.started_at({ss.STARTED_ENV: raw}) == expected


@pytest.fixture
def silent_supervisor(tmp_path, monkeypatch):
    """Отбивка час назад, шлюз запущен два часа назад: надзор молчит."""
    import time
    path = tmp_path / "beat.json"
    sr.save_beat(path, _beat(time.time() - 3600))
    monkeypatch.setenv(sr.BEAT_FILE_ENV, str(path))
    monkeypatch.setenv(ss.STARTED_ENV, str(time.time() - 7200))
    return path


def test_gate_command_prints_the_reason_or_nothing(silent_supervisor, capsys):
    import time
    assert ss.main() == 0 and "надзор стенда молчит" in capsys.readouterr().out
    sr.save_beat(silent_supervisor, _beat(time.time()))
    assert ss.main() == 0 and capsys.readouterr().out == ""


async def test_stand_silence_workflow_alarms_once_through_the_real_gate_command(silent_supervisor):
    """Настоящий stand-silence.yml и его команда ворот процессом: молчание - шаг основной модели с поводом; тот же
    вывод ворот в следующую минуту модель не зовёт."""
    flow = next(f for f in load_dir(REPO_WORKFLOWS, warn=pytest.fail) if f.name == "stand-silence")
    assert flow.schedule.every_s == 60 and flow.category == "stand"
    s, calls, _ = _sched([flow])
    store = s._store
    ctx = wf.GateContext(None, None)
    await s.run_once(flow, ctx, store.get(flow.name).get("gate"))
    assert len(calls) == 1
    src, text, session = calls[0]
    assert src == wf.source("stand-silence", "alarm") and "надзор стенда молчит" in text
    assert "8000/health" in text and "8010/health" not in text        # радар по умолчанию выключен, его в списке нет
    assert "mcp_call" in text                        # проверка с ПК стенда, не из песочницы
    await s.run_once(flow, ctx, store.get(flow.name).get("gate"))
    assert len(calls) == 1


def test_health_shows_the_last_beat_and_its_age(app_client, tmp_path, monkeypatch):
    client, _ = app_client
    monkeypatch.setattr(main, "_health_probes", lambda: {})
    reports = sr.StandReports(lambda kind, data: None, partial(sr.save_beat, tmp_path / "beat.json"),
                              clock=lambda: AT + 42, last=_beat(AT))
    monkeypatch.setattr(main, "_stand_reports", reports)
    assert client.get("/health").json()["stand_watch"]["age_s"] == 42
    monkeypatch.setattr(main, "_stand_reports", sr.StandReports(lambda kind, data: None, lambda beat: None))
    assert client.get("/health").json()["stand_watch"] == {"last_beat": None, "age_s": None}


def test_gateway_stamps_its_start_for_the_gate_process():
    assert ss.started_at(os.environ) is not None
