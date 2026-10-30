"""Срок хранения записей слуха (SAM-106): что удаляется по сроку и потолку, что не трогается никогда."""
import os
import time

import pytest

from voice.layer import retention as rt

HOUR = 3600.0
MB = 2**20
NOW = 1_000_000.0
DAY = 24 * HOUR


def _f(name, mb, age_h) -> rt.RecFile:
    return rt.RecFile(name, int(mb * MB), NOW - age_h * HOUR)


THREE_300 = [_f("mic-a.wav", 300, 5), _f("mic-b.wav", 300, 3), _f("mic-c.wav", 300, 1)]


@pytest.mark.parametrize("files, ceiling_mb, protect, gone", [
    ([], 600, frozenset(), []),
    ([_f("mic-1.wav", 100, 1), _f("mic-2.wav", 100, 5)], 600, frozenset(), []),
    # старше срока уходят, самый новый остаётся, даже если старый
    ([_f("mic-a.wav", 50, 40), _f("mic-b.wav", 50, 30), _f("mic-c.wav", 50, 26)], 10_000, frozenset(),
     ["mic-a.wav", "mic-b.wav"]),
    (THREE_300, 650, frozenset(), ["mic-a.wav"]),
    (THREE_300, 350, frozenset(), ["mic-a.wav", "mic-b.wav"]),
    ([_f("mic.wav", 900, 50), _f("mic-new.wav", 10, 1)], 100, frozenset({"mic.wav"}), []),
    ([_f("mic-big.wav", 900, 1)], 100, frozenset(), []),
], ids=["empty", "young", "older-than-term-but-newest-stays", "ceiling-oldest-first", "ceiling-until-fits",
        "active-never", "one-huge-newest-kept"])
def test_expired_recordings(files, ceiling_mb, protect, gone):
    assert sorted(rt.expired(files, NOW, DAY, ceiling_mb * MB, protect=protect)) == gone


def _age(path, hours):
    past = time.time() - hours * HOUR
    os.utime(path, (past, past))


def test_sweep_removes_old_and_keeps_active(tmp_path, capsys):
    (tmp_path / "phrases").mkdir()
    for name in ("mic.wav", "mic-old.wav", "mic-fresh.wav", "phrases/101010.wav", "last-phrase.wav", "ticks.jsonl"):
        (tmp_path / name).write_bytes(b"x" * 100)
    # скан видит только записи микрофона и фраз
    assert sorted(f.name for f in rt.scan(tmp_path)) == ["mic-fresh.wav", "mic-old.wav", "mic.wav",
                                                        "phrases/101010.wav"]
    _age(tmp_path / "mic-old.wav", 48)
    _age(tmp_path / "mic.wav", 48)
    removed = rt.sweep(tmp_path, protect=frozenset({"mic.wav"}), keep_hours=24, keep_mb=600)
    assert removed == ["mic-old.wav"]
    assert (tmp_path / "mic.wav").exists() and (tmp_path / "mic-fresh.wav").exists()
    assert "убрано старых записей: 1" in capsys.readouterr().out

    # цикл берёт активную запись у слуха: старый активный файл не трогается
    _age(tmp_path / "mic.wav", 100)
    loop = rt.RetentionLoop(tmp_path, active=lambda: "mic.wav")
    assert "mic.wav" not in loop.run_once() and (tmp_path / "mic.wav").exists()
