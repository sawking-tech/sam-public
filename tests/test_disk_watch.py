"""Надзор за диском (SAM-107): когда есть повод, снимки, ворота-команда. Числа выдуманные."""
import pytest

from gateway import disk_watch as dw

HOUR = 3600.0


def _s(ts, **free) -> dw.Snapshot:
    return dw.Snapshot(ts, {k.replace("_", ":"): v for k, v in free.items()})


@pytest.mark.parametrize("now, history, expected", [
    (_s(100 * HOUR, C_=90.0), [_s(90 * HOUR, C_=92.0)], []),
    (_s(100 * HOUR, C_=4.6), [], [("C:", "4.6")]),
    (_s(100 * HOUR, C_=80.0), [_s(90 * HOUR, C_=100.0), _s(95 * HOUR, C_=90.0)], [("C:", "потерял 20.0")]),
    (_s(100 * HOUR, C_=80.0), [_s(10 * HOUR, C_=200.0)], []),
    (_s(100 * HOUR, C_=3.0, E_=500.0), [], [("C:", "3.0")]),
], ids=["enough_space", "low_free_space", "fast_drop_in_window", "old_history_outside_window",
        "drives_judged_separately"])
def test_disk_report_names_reason(now, history, expected):
    lines = dw.report(now, history)
    assert len(lines) == len(expected)
    for line, words in zip(lines, expected):
        assert all(w in line for w in words), line


def test_snapshots_keep_last_rows_and_survive_broken_line(tmp_path):
    path = tmp_path / "snap.jsonl"
    for i in range(5):
        dw.append_snapshot(path, _s(float(i), C_=10.0 + i), keep=3)
    with path.open("a", encoding="utf-8") as f:
        f.write('{"ts": 9')                                       # остановка посреди записи
    assert [r.ts for r in dw.load_history(path)] == [2.0, 3.0, 4.0]


def test_disk_gate_command_output(tmp_path, monkeypatch, capsys):
    snapshots = tmp_path / "s.jsonl"
    monkeypatch.setenv("SAM_DISK_SNAPSHOTS", str(snapshots))
    monkeypatch.setenv(dw.DRIVES_ENV, f"{tmp_path},{tmp_path / 'no-such-disk'}")   # нечитаемый диск пропущен
    monkeypatch.setattr(dw, "MIN_FREE_GB", 10**9)                 # на подставном пороге место «кончилось»
    assert dw.main() == 0
    out = capsys.readouterr().out
    assert "свободно" in out and "no-such-disk" not in out
    monkeypatch.setattr(dw, "MIN_FREE_GB", 0.0)
    assert dw.main() == 0 and capsys.readouterr().out == ""
    assert len(snapshots.read_text(encoding="utf-8").splitlines()) == 2

    monkeypatch.setenv(dw.DRIVES_ENV, str(tmp_path / "nope"))
    assert dw.main() == 1 and dw.DRIVES_ENV in capsys.readouterr().err
