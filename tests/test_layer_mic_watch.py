"""Сторож микрофона (mic_watch), ядро звука без входа и слух - без устройств.

Старт AudioCore без микрофона и переоткрытие вернувшегося входа - на подменённом sounddevice (вживую - выдёргиванием микрофона).
"""

from __future__ import annotations

import json
import types

import numpy as np
import pytest

from voice.layer import audio_core, hearing
from voice.layer.bus import HEARING_FLAG, MIC_EVENTS
from voice.layer.mic_watch import LIVE, WAITING, MicWatch, find_input

STALL, POLL = 2.0, 2.0
BLOCK_SECS = 0.04                      # колбэк входа ядра: 40 мс
WEBCAM_FULL = "Микрофон (USB Pro Stream Webcam)"
WEBCAM_MME = WEBCAM_FULL[:31]              # winmm и MME режут имя до 31 знака
OTHER = "Микрофон гарнитуры (Soundcore 3)"
SPEAKERS = "Динамики (Realtek High Definition Audio)"
NO_DEVICE = "Error querying device -1"  # текст PortAudioError, с которым 9 окт падал голосовой слой


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def tick(self, secs: float) -> None:
        self.t += secs


def watch(live: bool = True) -> tuple[MicWatch, Clock]:
    clock = Clock()
    return MicWatch(live=live, stall_secs=STALL, poll_secs=POLL, clock=clock), clock


# ─── MicWatch: решения по часам ───────────────────────────────────────────────

def _blocks_keep_it_live(w, clock):
    for _ in range(200):               # 8 с блоков каждые 40 мс
        clock.tick(BLOCK_SECS)
        w.block()
        assert not w.stalled()
    assert w.state == LIVE


def _silence_stalls_once(w, clock):
    clock.tick(STALL - 0.1)
    assert not w.stalled()
    clock.tick(0.2)
    assert w.stalled() and w.state == WAITING
    clock.tick(10)
    assert not w.stalled()             # одна строка в лог на потерю, не на каждый шаг сторожа


def _reopen_resets_the_clock(w, clock):
    clock.tick(STALL - 0.1)
    w.restart()                        # смена выхода переоткрыла потоки: это не потеря
    clock.tick(STALL - 0.1)
    assert not w.stalled()


def _first_block_after_loss_is_back(w, clock):
    clock.tick(STALL + 0.1)
    assert w.stalled()
    assert not w.resumed()             # блоков ещё нет
    clock.tick(1.0)
    w.restart()                        # переоткрыли, но поток ещё молчит
    assert not w.resumed()
    clock.tick(BLOCK_SECS)
    w.block()
    assert w.resumed()
    assert w.state == LIVE and not w.resumed()


def _started_without_mic_back_on_first_block(w, clock):
    assert w.waiting and not w.stalled()
    clock.tick(5)
    w.block()
    assert w.resumed()


@pytest.mark.parametrize("live, scenario", [
    (True, _blocks_keep_it_live),
    (True, _silence_stalls_once),
    (True, _reopen_resets_the_clock),
    (True, _first_block_after_loss_is_back),
    (False, _started_without_mic_back_on_first_block),
], ids=["blocks-keep-live", "silence-stalls-once", "reopen-resets", "first-block-after-loss", "started-without-mic"])
def test_mic_stall_reported_once_and_back_on_block(live, scenario) -> None:
    w, clock = watch(live=live)
    scenario(w, clock)


def test_mic_retry_polling() -> None:
    assert not watch()[0].retry_due()  # микрофон работает: опрашивать нечего
    w, clock = watch(live=False)
    assert w.retry_due()               # старт без микрофона: первый опрос сразу
    clock.tick(POLL - 0.1)
    assert not w.retry_due()
    clock.tick(0.2)
    assert w.retry_due()


# ─── find_input: какое устройство ждать ───────────────────────────────────────

@pytest.mark.parametrize("names, wanted, found", [
    ([], WEBCAM_FULL, None),                       # камеры нет в системе
    ([OTHER], WEBCAM_FULL, None),                  # другой вход - не тот
    ([OTHER, WEBCAM_MME], WEBCAM_FULL, WEBCAM_MME),    # вернулась, имя обрезано winmm
    ([], None, None),
    ([OTHER], None, OTHER),                      # старт без микрофона: годится любой вход
    (None, WEBCAM_FULL, WEBCAM_FULL),                # список неизвестен: всё равно пробуем
    (None, None, ""),
], ids=["absent", "other-device", "same-device-cut-name", "any-none", "any-first", "unknown-list", "unknown-any"])
def test_find_input_waits_for_the_same_device(names, wanted, found) -> None:
    assert find_input(names, wanted) == found


# ─── AudioCore без входного устройства: подменённый sounddevice ───────────────

class _Stream:
    """Поток sounddevice без звука: устройство, латентность, start/stop/close."""

    def __init__(self, device, callback=None, **_) -> None:
        self.device, self.callback, self.latency = device, callback, 0.05
        self.active = False

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        pass


class _FakeSD:
    """Система, где микрофона нет (InputStream падает, как PortAudio на device -1), пока mic=True."""

    PortAudioError = audio_core.sd.PortAudioError

    def __init__(self) -> None:
        self.mic = False
        self.inputs: list[_Stream] = []

    def _devices(self) -> list[dict]:
        devs = [{"index": 0, "name": SPEAKERS, "hostapi": 0,
                 "max_input_channels": 0, "max_output_channels": 2}]
        if self.mic:
            devs.append({"index": 1, "name": WEBCAM_FULL, "hostapi": 0,
                         "max_input_channels": 1, "max_output_channels": 0})
        return devs

    def query_devices(self, device=None, kind=None):
        devs = self._devices()
        if device is None and kind is None:
            return devs
        if device is None:
            device = 1 if kind == "input" else 0
        return next(d for d in devs if d["index"] == device)

    def InputStream(self, device=None, **kw) -> _Stream:
        if not self.mic:
            raise self.PortAudioError(NO_DEVICE)
        st = _Stream(1 if device is None else device, **kw)
        self.inputs.append(st)
        return st

    def OutputStream(self, device=None, **kw) -> _Stream:
        return _Stream(0 if device is None else device, **kw)

    def _terminate(self) -> None:
        pass

    def _initialize(self) -> None:
        pass


@pytest.fixture
def no_mic(monkeypatch) -> _FakeSD:
    fake = _FakeSD()
    monkeypatch.setattr(audio_core, "sd", fake)
    monkeypatch.setattr(audio_core, "input_names", lambda: [WEBCAM_MME] if fake.mic else [])
    return fake


def _mic_events(d) -> list[dict]:
    path = d / MIC_EVENTS
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _block(core) -> None:
    core._on_in(np.zeros((audio_core.BLOCK, 1), np.int16), audio_core.BLOCK, None,
                types.SimpleNamespace(input_overflow=False))


def test_audio_core_survives_mic_loss_and_return(no_mic, tmp_path, capsys) -> None:
    """Старт без микрофона - одна строка; вернувшийся вход переоткрывается; потеря сообщается один раз."""
    core = audio_core.AudioCore(aec=False, pick_in=lambda: None)
    core.start()
    assert core.input_name == "" and core.mic_watch.waiting
    assert core._reported_delay() == 50          # только колонки: входа нет
    assert capsys.readouterr().out.strip().splitlines() == [
        "[слух] нет микрофона (вход по умолчанию); жду устройство"]

    clock = Clock()
    core.mic_watch = MicWatch(live=False, stall_secs=STALL, poll_secs=POLL, clock=clock)
    core._mic_step(tmp_path)                     # устройства нет: переоткрывать нечего
    assert core._in is None and not no_mic.inputs
    no_mic.mic = True                            # камеру воткнули обратно
    clock.tick(POLL)
    core._mic_step(tmp_path)
    assert core._in is not None and core._in.active and core.input_name == WEBCAM_FULL
    clock.tick(BLOCK_SECS)
    _block(core)
    core._mic_step(tmp_path)
    assert not core.mic_watch.waiting

    clock.tick(STALL + 0.1)                      # колбэк перестал приходить
    no_mic.mic = False
    core._mic_step(tmp_path)
    core._mic_step(tmp_path)
    events = _mic_events(tmp_path)
    assert [e["status"] for e in events] == ["back", "lost"]
    assert events[1]["device"] == WEBCAM_FULL
    core.close()


# ─── слух: тишина из ядра снимает hearing ─────────────────────────────────────

def test_mic_silence_drops_phrase_and_hearing_flag(tmp_path, monkeypatch) -> None:
    def _no_encoder():
        raise RuntimeError("ECAPA в тестах не грузится")
    monkeypatch.setattr(hearing.speakers, "encoder", _no_encoder)
    stt = types.SimpleNamespace(transcribe=lambda audio: "", quick=lambda audio: "")
    lst = hearing.Listener(tmp_path, stt, device=None)
    loud = np.full(hearing.BLOCK, int(hearing.SILENCE_RMS * 3), dtype=np.int16)
    for _ in range(10):                          # 1 с его речи, фраза не кончилась
        lst.step(loud)
    assert (tmp_path / HEARING_FLAG).exists()
    lst._mic_silent()
    assert not (tmp_path / HEARING_FLAG).exists()
    assert lst.phrase == []
