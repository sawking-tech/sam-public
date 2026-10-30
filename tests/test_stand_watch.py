"""Надзор стенда (scripts/stand_watch.py): сигнал, что сжатие истории встало (SAM-97)."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "stand_watch.py"
_spec = importlib.util.spec_from_file_location("stand_watch", SCRIPT)
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)

STALE = 600


def _health(**session):
    return {"status": "ok", "compression": {"sessions": {"voice": session}}}


@pytest.mark.parametrize("health, expected", [
    # 10 окт: 217 несжатых, последнее сжатие в 00:23, после рестарта шлюза - ни одного
    (_health(above_ceiling=True, last_ok_ago_s=3600), {"compress:voice": sw.STALLED}),
    (_health(above_ceiling=True, last_ok_ago_s=None), {"compress:voice": sw.STALLED}),
    (_health(above_ceiling=True, last_ok_ago_s=3600, running_s=900), {"compress:voice": sw.STALLED}),   # повисло
    (_health(above_ceiling=False, last_ok_ago_s=None), {"compress:voice": sw.UP}),
    (_health(above_ceiling=True, last_ok_ago_s=60), {"compress:voice": sw.UP}),
    (_health(above_ceiling=True, last_ok_ago_s=3600, running_s=30), {"compress:voice": sw.UP}),
    (_health(above_ceiling=True, last_ok_ago_s=3600, waiting_s=100), {"compress:voice": sw.UP}),    # ждёт паузы (SAM-220)
    (_health(above_ceiling=True, last_ok_ago_s=3600, waiting_s=900), {"compress:voice": sw.STALLED}),  # ждёт слишком долго
    (None, {}),                                    # шлюз лежит
    ({"status": "ok"}, {}),                        # старый шлюз без раздела compression
], ids=["no-recent", "never", "hung", "below-ceiling", "recent", "running", "waiting-pause", "waiting-too-long",
        "gateway-down", "old-gateway"])
def test_stand_watch_flags_stalled_compression(health, expected):
    assert sw.compression_states(health, STALE) == expected


def test_stall_is_a_change_line():
    before = {"gateway": sw.UP, "compress:voice": sw.UP}
    assert sw.changes(before, {**before, "compress:voice": sw.STALLED}) == ["compress:voice up -> stalled"]


def test_double_process_is_reported_as_x2(monkeypatch):
    """stand 69: два процесса одного компонента - строка «bridge up -> x2», пропавший - «down». Подменены опрос
    процессов (граница ОС), HTTP и файлы живости."""
    lines = ["python -m uvicorn gateway.main:app --port 8000", "python -u -m voice.layer.voice",
             "python -u -m voice.layer.bridge", "python -m voice.layer.tts_silero_server"]
    monkeypatch.setattr(sw.sc, "python_cmdlines", lambda: list(lines))
    monkeypatch.setattr(sw.sc, "read_file_health", lambda name, now: sw.UP)   # файлы живости слуха и моста свежие
    monkeypatch.setattr(sw.sc, "fetch_health", lambda url: {"status": "ok"})
    before = sw.snapshot()
    lines.append("python -u -m voice.layer.bridge")
    lines.remove("python -u -m voice.layer.voice")

    assert sw.changes(before, sw.snapshot()) == ["voice up -> down", "bridge up -> x2"]


# ─── Отчёты шлюзу (SAM-195) ──────────────────────────────────────────────

BEAT = 30
LONG_BEAT = 1000   # отбивка не наступит за тест: отчёт уходит только по смене
ALL_UP = {"gateway": sw.UP, "bridge": sw.UP}


@pytest.mark.parametrize("reported, now, last_sent, at, sent", [
    (None, ALL_UP, None, 0, True),                                     # старт: первая отбивка сразу
    (ALL_UP, ALL_UP, 0, BEAT - 1, False),                              # рано и без смен - ничего
    (ALL_UP, ALL_UP, 0, BEAT, True),                                   # пора отбиться
    (ALL_UP, {**ALL_UP, "bridge": sw.DOWN}, 0, 1, True),               # смена - сразу
    ({**ALL_UP, "compress:voice": sw.STALLED}, ALL_UP, 0, 1, True),    # пропал ключ - тоже смена
], ids=["first", "quiet", "beat", "change", "key-gone"])
def test_report_goes_on_change_at_once_and_otherwise_once_per_beat(reported, now, last_sent, at, sent):
    report = sw.report_due(reported, now, last_sent, at, BEAT)
    assert report == ({"states": now} if sent else None)


class _Gateway:
    """Шлюз для надзора: принимает или отказывает, пишет принятые отчёты."""

    def __init__(self):
        self.up, self.got = True, []

    def send(self, report):
        if not self.up:
            raise ConnectionRefusedError("отказ в соединении")
        self.got.append(report)


def test_change_missed_while_gateway_was_down_arrives_after_it_is_back():
    """Шлюз лежал, пока мост падал и поднимался голос: отчёт уходит первым опросом после подъёма шлюза, а не ждёт
    отбивки; строк об ошибке - одна в минуту, не на каждом опросе."""
    gw, warns, clock = _Gateway(), [], [0.0]
    rep = sw.Reporter(gw.send, beat_s=LONG_BEAT, error_every_s=60, clock=lambda: clock[0], warn=warns.append)
    start = {**ALL_UP, "voice": sw.DOWN}
    assert rep.tick(start) and gw.got == [{"states": start}]

    gw.up = False
    for t in range(15, 90, 15):                    # пять опросов без шлюза, мост упал
        clock[0] = t
        assert not rep.tick({**start, "bridge": sw.DOWN})
    assert len(warns) == 2 and all("\n" not in w and "надзор работает дальше" in w for w in warns)

    gw.up, clock[0] = True, 90
    assert rep.tick({**ALL_UP, "voice": sw.UP, "bridge": sw.DOWN})        # отбивке ещё рано: ушло как смена
    assert gw.got[-1] == {"states": {**ALL_UP, "voice": sw.UP, "bridge": sw.DOWN}}
    clock[0] = 100
    assert not rep.tick({**ALL_UP, "voice": sw.UP, "bridge": sw.DOWN})   # принято - повтора нет до отбивки


def test_monitor_lines_stay_the_same():
    """Строки для Monitor сессии не поменялись от отчётов шлюзу."""
    assert sw.changes({}, {"bridge": sw.DOWN, "voice": sw.UP}) == ["bridge down"]
    assert sw.changes({"bridge": sw.UP}, {"bridge": "x2", "compress:voice": sw.UP}) == [
        "bridge up -> x2", "compress:voice None -> up"]


def test_compression_is_carried_over_while_gateway_health_is_silent():
    before = {"gateway": sw.UP, "compress:voice": sw.STALLED, "bridge": sw.UP}
    assert sw.compression_states(None, STALE, previous=before) == {"compress:voice": sw.STALLED}


def test_gateway_health_blinking_under_load_is_not_a_compression_rise(monkeypatch, tmp_path):
    """Шлюз под нагрузкой не ответил на /health один опрос: сжатие не «поднялось», а потом не «упало» снова - шлюз
    получает только смену самого gateway."""
    from functools import partial
    from gateway import stand_report as sr
    lines = ["python -m uvicorn gateway.main:app --port 8000", "python -u -m voice.layer.voice",
             "python -u -m voice.layer.bridge", "python -m voice.layer.tts_silero_server"]
    stalled = _health(above_ceiling=True, last_ok_ago_s=3600)
    answers = iter([stalled, None, stalled])
    monkeypatch.setattr(sw.sc, "python_cmdlines", lambda: list(lines))
    monkeypatch.setattr(sw.sc, "read_file_health", lambda name, now: sw.UP)   # файлы живости слуха и моста свежие
    monkeypatch.setattr(sw.sc, "fetch_health", lambda url: next(answers) if "8000" in url else {"status": "ok"})
    events = []
    reports = sr.StandReports(lambda kind, data: events.append(data), partial(sr.save_beat, tmp_path / "b.json"),
                              clock=lambda: 1_791_000_000.0)
    rep = sw.Reporter(lambda report: reports.accept(report["states"]), clock=lambda: 0.0)
    before = {}
    for _ in range(3):
        before = sw.snapshot(before)
        rep.tick(before)
    lines_of = [e["changes"] for e in events]
    assert lines_of == ["compress:voice stalled", "gateway up -> down", "gateway down -> up"]
