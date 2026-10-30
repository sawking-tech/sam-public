"""Выход речи (say_out): отрезки синтеза (SAM-09), судьбы реплик, поправка быстрой (SAM-167)."""

from __future__ import annotations

import json
import queue
import threading

import pytest

from voice.layer.bus import SAID
from voice.layer import say_out
from voice.layer.say_out import Speaker, segments

SHORT = "Проверяю запись звука."
LONG = "Я говорю длинную фразу из нескольких предложений, а слой пишет всё, что уходит в колонки."
MIN_CHARS = 60


@pytest.mark.parametrize("reply, parts", [
    (f"{SHORT} {LONG}", [f"{SHORT} {LONG}"]),
    (f"{LONG} {LONG}", [LONG, LONG]),
    (SHORT, [SHORT]),
], ids=["short-joins-next", "long-stay-apart", "single-short"])
def test_reply_split_into_synthesis_segments(reply, parts) -> None:
    assert segments(reply, MIN_CHARS) == parts


def _quiet_speaker(d) -> Speaker:
    """Speaker без потоков синтеза и звука: только очередь и запись судеб."""
    sp = Speaker.__new__(Speaker)
    sp.d, sp.q = d, queue.Queue()
    return sp


def test_line_fate_keeps_its_source(tmp_path) -> None:
    sp = _quiet_speaker(tmp_path)
    sp._said("Всё, умолкаю.", "played", "observer")
    sp._said("Своя реплика.", "played")
    rows = [json.loads(line) for line in (tmp_path / SAID).read_text(encoding="utf-8").splitlines()]
    assert [(r["text"], r.get("from")) for r in rows] == [("Всё, умолкаю.", "observer"), ("Своя реплика.", None)]
    # сброс очереди помнит, чья была каждая реплика
    sp.q.put(("быстрое", None, False, None, "haiku", None))
    sp.q.put(("ведущего", None, False, None, "observer", None))
    assert sp._drop_queue() == [("быстрое", "haiku"), ("ведущего", "observer")]


class _Core:
    def __init__(self):
        self.calls = []

    def wind_down(self, keep, fade, level=0.4):
        self.calls.append(("wind", keep, fade, level))

    def stop(self):
        self.calls.append(("stop",))


def _fixing_speaker(d, core):
    sp = _quiet_speaker(d)
    sp.core, sp.busy, sp.current, sp.current_who = core, True, "Длинная реплика быстрой.", "haiku"
    sp.cancel, sp.discard, sp.soften = threading.Event(), threading.Event(), threading.Event()
    return sp


def test_fix_softens_or_cuts_the_fast_reply(tmp_path) -> None:
    # SAM-167: поправка посреди длинной реплики - затихание до паузы между словами, не обрыв
    core = _Core()
    sp = _fixing_speaker(tmp_path, core)
    sp.q.put(("ещё быстрое", None, False, None, "haiku", None))
    sp.q.put(("поправка", None, False, None, "gateway", None))
    assert sp.retract_fast() == ["Длинная реплика быстрой.", "ещё быстрое"] and core.calls == []
    assert sp._soften() is True
    assert core.calls == [("wind", say_out.FIX_KEEP_SECS, say_out.FIX_FADE_SECS, say_out.FIX_LEVEL)]
    assert sp._soften() is False                   # одно затихание на отзыв
    assert sp.q.get_nowait()[0] == "поправка"      # поправка звучит следом

    # без ядра звука поправка обрывает реплику
    bare = _fixing_speaker(tmp_path, None)
    halted = []
    bare._halt = lambda: halted.append(True)
    bare.retract_fast()
    assert halted == [True] and not bare.soften.is_set()


def test_first_sound_line_splits_the_turn_from_the_phrase_record(capsys, monkeypatch):
    """15 окт: таймер до звука - от записи фразы до текста ответа (шлюз и модель) и до звука, отдельно."""
    import types
    from voice.layer import say_out as so
    monkeypatch.setattr(so.time, "time", lambda: 110.0)
    syn = types.SimpleNamespace(started_at=108.5, first_at=109.2)
    so.Speaker._log_latency(syn, reply_to=104.0)
    line = capsys.readouterr().out
    assert "до звука 1.50 с" in line and "текст ответа через 4.50 с" in line and "звук через 6.00 с" in line
    so.Speaker._log_latency(syn)
    assert "от записи фразы" not in capsys.readouterr().out


def test_bus_marks_records_to_the_millisecond():
    from datetime import datetime
    from voice.layer import bus
    ts = bus.now_iso()
    assert "." in ts and datetime.fromisoformat(ts).microsecond % 1000 == 0
