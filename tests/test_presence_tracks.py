"""Треки устройств радара (SAM-105): частота записи, ряд по имени, потолок размера. Данные выдуманные.
Файл суток берётся по местной дате; время в тестах - полдень, так что сдвиг часов на час при переводе не меняет дату."""
from datetime import datetime

import pytest

from radar import presence_rssi as rs
from radar import presence_tracks as pt

DAY = datetime(2026, 10, 10, 12, 0).timestamp()
NEXT_DAY = datetime(2026, 10, 11, 12, 0).timestamp()


def _st(sid="aaaa0001", name="Alpha-Test", level=-50.0, inactive=100) -> rs.Station:
    return rs.Station(sid, "phy0-ap0", level, inactive, name)


def test_track_log_records_by_interval_and_reads_by_name_time_range(tmp_path):
    """Строка раз в интервал, молчащая станция мимо; чтение по части имени, времени и лимиту; диапазон по всем устройствам и края записи."""
    log = pt.TrackLog(tmp_path / "interval")
    assert log.record([_st(), _st("b", "B", inactive=20000)], DAY) == 1       # молчащая станция в ряд не идёт
    assert log.record([_st()], DAY + 0.4) == 0                                  # слишком скоро
    assert log.record([_st(level=-60.0)], DAY + 1.2) == 1
    assert [(r["db"], r["name"]) for r in log.read("alpha", 0.0)] == [(-50.0, "Alpha-Test"), (-60.0, "Alpha-Test")]

    log = pt.TrackLog(tmp_path / "every", every_s=0.0)
    for i in range(10):
        log.record([_st("b", "Beta-Test", level=-70.0), _st(level=-50.0 - i)], DAY + i)
    assert len(log.read("beta", 0.0)) == 10 and log.read("nobody", 0.0) == []
    assert [r["db"] for r in log.read("Alpha", DAY + 6)] == [-56.0, -57.0, -58.0, -59.0]
    assert [r["db"] for r in log.read("Alpha", 0.0, limit=2)] == [-58.0, -59.0]
    log.record([_st()], NEXT_DAY)                                              # следующие сутки: второй файл
    rows = log.read_range(DAY + 1, DAY + 3)
    assert [r["t"] for r in rows] == [DAY + 1, DAY + 1, DAY + 2, DAY + 2, DAY + 3, DAY + 3] and {r["name"] for r in rows} == {"Alpha-Test", "Beta-Test"}
    assert log.span() == (DAY, NEXT_DAY) and pt.TrackLog(None).span() is None and pt.TrackLog(None).read_range(0, 1) == []


def test_raw_log_keeps_every_poll_in_own_files(tmp_path):
    """Сырой ряд пишет каждый круг и молчащие станции с возрастом в свои файлы, обычный - раз в секунду и только свежие."""
    raw = pt.TrackLog(tmp_path, every_s=0.0, prefix=pt.RAW_PREFIX, fresh_ms=None)
    plain = pt.TrackLog(tmp_path)
    t0 = datetime(2026, 10, 11, 12, 30).timestamp()
    for k in range(4):                                                   # четыре круга за две секунды, вторая станция молчит 9 с
        polls = [_st("a", "A", -50.0 - k, 100), _st("b", "B", -70.0, 9000)]
        raw.record(polls, t0 + 0.5 * k)
        plain.record(polls, t0 + 0.5 * k)
    rows = raw.read_range(t0, t0 + 5)
    assert len(rows) == 8 and {r["in"] for r in rows if r["id"] == "b"} == {9000}
    assert [r["db"] for r in plain.read_range(t0, t0 + 5)] == [-50.0, -52.0]
    assert sorted(p.name[:4] for p in tmp_path.iterdir()) == ["raw-", "trac"]


def _broken_last_line(tmp_path):
    log = pt.TrackLog(tmp_path)
    log.record([_st()], DAY)
    next(tmp_path.glob("tracks-*.jsonl")).open("a", encoding="utf-8").write('{"t": 1791')   # остановка посреди записи
    return len(log.read("alpha", 0.0)) == 1, ""


def _no_directory(tmp_path):
    return pt.TrackLog(None).record([_st()], DAY) == 0 and pt.TrackLog(None).read("alpha", 0.0) == [], ""


def _unwritable_directory(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    return pt.TrackLog(blocker / "sub").record([_st()], DAY) == 0, "не записаны"


@pytest.mark.parametrize("case", [_broken_last_line, _no_directory, _unwritable_directory], ids=["broken_line", "no_dir", "unwritable"])
def test_track_log_survives_broken_line_missing_or_unwritable_dir(tmp_path, capsys, case):
    """Оборванная строка не ломает чтение, без папки запись молча выключена, папка без записи - одна строка в лог и без исключения."""
    ok, said = case(tmp_path)
    out = capsys.readouterr().out
    assert ok and (said in out if said else out == "")


def test_retention_drops_oldest_days_keeps_last():
    files = [("tracks-20261008.jsonl", 30), ("tracks-20261009.jsonl", 30), ("tracks-20261010.jsonl", 30)]
    assert pt.to_delete(files, max_bytes=50) == ["tracks-20261008.jsonl", "tracks-20261009.jsonl"]
    assert pt.to_delete(files, max_bytes=1000) == []
    assert pt.to_delete([("tracks-20261010.jsonl", 10**9)], max_bytes=1) == []
