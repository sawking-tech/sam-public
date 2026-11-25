"""Ядро звука (voice/layer/audio_core.py): AEC и его отсутствие, прореживание к 16 кГц, притихание и затухание,
калибровка и доверие к замеру, смена выхода, перечитывание не посреди речи, выбор входа, тик mic ведущему.
Настоящий AudioCore; подменены sounddevice и livekit (граница устройств), winmm (список устройств) и часы ожидания."""
from __future__ import annotations

import json
import sys
import threading
import time
import types

import numpy as np
import pytest

from voice.layer import audio_core, hearing
from voice.layer import ticker as tk
from voice.layer.audio_core import BLOCK, OUT_BLOCK, RATE, AudioCore, WindDown
from voice.layer.bus import HEARING_FLAG, MIC_EVENTS, SPEAKING_FLAG
from voice.layer.mic_watch import MicWatch
from tests.test_layer_audio_stream import bare_core, drain, speech
from tests.test_layer_mic_watch import WEBCAM_FULL, SPEAKERS, _FakeSD, _Stream
from tests.fakes_core import patch_module_sleep

OUT_RATE = audio_core.OUT_RATE
HDMI = "Монитор HDMI (NVIDIA High Definition Audio)"
HEADPHONES = "Наушники (Soundcore 3)"
WAIT_S = 5


class _Frame:
    def __init__(self, data, rate, channels, samples):
        self.data = data


class _Apm:
    """WebRTC APM на границе livekit: помнит настройки и кадры."""

    def __init__(self, **settings):
        self.settings, self.reverse, self.forward, self.delays = settings, 0, 0, []

    def process_reverse_stream(self, frame):
        self.reverse += 1

    def process_stream(self, frame):
        self.forward += 1

    def set_stream_delay_ms(self, ms):
        self.delays.append(ms)


@pytest.fixture
def sd_with_mic(monkeypatch):
    fake = _FakeSD()
    fake.mic = True
    monkeypatch.setattr(audio_core, "sd", fake)
    monkeypatch.setattr(audio_core, "input_names", lambda: [WEBCAM_FULL])
    return fake


def _livekit(monkeypatch, available: bool):
    if available:
        rtc = types.SimpleNamespace(AudioProcessingModule=_Apm, AudioFrame=_Frame)
        monkeypatch.setitem(sys.modules, "livekit", types.SimpleNamespace(rtc=rtc))
    else:
        monkeypatch.setitem(sys.modules, "livekit", None)       # import падает, как без пакета


def _mic_block(core, samples):
    core._on_in(samples.reshape(-1, 1), len(samples), None, types.SimpleNamespace(input_overflow=False))


def _out_block(core):
    out = np.zeros((BLOCK, 1), np.int16)
    core._on_out(out, BLOCK, None, types.SimpleNamespace(output_underflow=False))
    return out[:, 0]


# ─── AEC ──────────────────────────────────────────────────────────────────────

def test_aec_takes_speaker_frames_as_reference_and_keeps_noise_suppression_off(sd_with_mic, monkeypatch):
    """audio-core 4: APM с эхоподавлением, без шумоподавления (оно срезало голос при двойной речи); кадр колонок -
    эталон process_reverse_stream, кадр микрофона - process_stream, кадрами по 10 мс."""
    _livekit(monkeypatch, True)
    core = AudioCore(aec=True)
    assert core.apm.settings == {"echo_cancellation": True, "noise_suppression": False, "high_pass_filter": True}
    core.play(np.full(BLOCK, 1000, np.int16), RATE)
    _out_block(core)
    _mic_block(core, np.zeros(BLOCK, np.int16))
    frames = BLOCK // audio_core.FRAME
    assert (core.apm.reverse, core.apm.forward) == (frames, frames)


def test_without_livekit_the_core_works_and_says_so_once(sd_with_mic, monkeypatch, capsys):
    """audio-core 5: нет livekit - одна строка, ядро работает без AEC: звук играет, микрофон доходит до слуха."""
    _livekit(monkeypatch, False)
    core = AudioCore(aec=True)
    lines = [line for line in capsys.readouterr().out.splitlines() if "AEC" in line]
    assert core.apm is None and len(lines) == 1 and "без него" in lines[0]
    core.play(np.full(BLOCK, 1000, np.int16), RATE)
    assert np.all(_out_block(core) == 1000)
    for _ in range(3):
        _mic_block(core, np.full(BLOCK, 500, np.int16))
    assert core.mic_q.qsize() == 3 * BLOCK // 3 // OUT_BLOCK


# ─── микрофон к слуху ─────────────────────────────────────────────────────────

def _tone(freq, secs):
    t = np.arange(int(secs * RATE)) / RATE
    return (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16)


def test_mic_goes_to_hearing_at_16_khz_in_100_ms_blocks_without_aliasing(sd_with_mic, monkeypatch):
    """audio-core 2, 3: микрофон 48 кГц - ФНЧ 7,6 кГц и каждый третий сэмпл: слух получает блоки 100 мс на 16 кГц,
    речь (1 кГц) проходит, тон выше половины 16 кГц (10 кГц) не заворачивается в слышимое; копия блока - узлу звука."""
    _livekit(monkeypatch, False)
    core = AudioCore(aec=False)
    tapped = []
    core.mic_tap = tapped.append
    for tone_hz, keep in ((1_000, True), (10_000, False)):
        core.mic_q.queue.clear()
        audio = _tone(tone_hz, 1.0)
        for i in range(0, len(audio), BLOCK):
            _mic_block(core, audio[i:i + BLOCK])
        blocks = list(core.mic_q.queue)
        assert all(len(b) == OUT_BLOCK for b in blocks) and len(blocks) == 10           # 1 с = 10 блоков
        rms = float(np.sqrt(np.mean(np.concatenate(blocks[2:]).astype(np.float32) ** 2)))
        assert (rms > 4000) is keep and (rms < 200) is not keep
    assert len(tapped) == 20


# ─── громкость колонок ────────────────────────────────────────────────────────

def test_duck_moves_volume_by_small_steps_without_clicks():
    """audio-core 24: duck(0.6) - громкость идёт шагом 0,15 за блок 40 мс, внутри блока плавно: щелчка нет."""
    core = bare_core()
    core.play(np.full(BLOCK * 6, 10_000, np.int16), RATE)
    core.duck(0.6)
    out = drain(core, 6).astype(np.float32) / 10_000
    assert np.max(np.abs(np.diff(out))) < 0.001                      # без скачка между соседними сэмплами
    per_block = [out[(i + 1) * BLOCK - 1] for i in range(6)]
    assert np.allclose(per_block[:3], [0.85, 0.7, 0.6], atol=0.001) and np.allclose(per_block[3:], 0.6, atol=0.001)


def test_fade_out_reaches_silence_in_its_time_and_drops_the_rest():
    """audio-core 25: fade_out(0,4) - до нуля за 0,4 с плавно, остаток очереди не звучит."""
    core = bare_core()
    core.play(np.full(RATE * 3, 10_000, np.int16), RATE)
    core.fade_out(0.4)
    blocks = int(0.4 * RATE / BLOCK)
    out = drain(core, blocks + 2)
    assert np.max(np.abs(np.diff(out.astype(np.float32)))) < 100
    assert not np.any(out[(blocks + 1) * BLOCK:]) and not core.busy()


# ─── калибровка колонок ───────────────────────────────────────────────────────

def test_echo_delay_is_found_by_correlation_of_the_sweep():
    """audio-core 6: свип 300-5000 Гц, эхо в микрофоне на 150 мс тише и с шумом - сдвиг по корреляции 150 мс."""
    sweep = audio_core.cal_signal()
    out = np.concatenate([sweep, np.zeros(RATE // 2, np.int16)])
    lag = int(0.150 * RATE)
    rng = np.random.default_rng(1)
    mic = np.zeros(len(out), np.float32)
    mic[lag:lag + len(sweep)] = 0.3 * sweep
    mic += rng.normal(0, 200, len(mic))
    delay_ms, corr = audio_core.find_lag(out, mic.astype(np.int16), 0.0)
    assert abs(delay_ms - 150) <= 1 and corr >= audio_core.CAL_MIN_CORR


@pytest.mark.parametrize("lag_ms, corr, delay, cached", [
    (180.0, 0.6, 180, True),
    (180.0, 0.1, 50, False),            # слабое совпадение - заявленная задержка
    (900.0, 0.6, 50, False),            # сдвиг вне 20-600 мс
    (10.0, 0.6, 50, False),
], ids=["trusted", "weak", "too-late", "too-early"])
def test_measured_delay_is_trusted_only_when_strong_and_plausible(tmp_path, monkeypatch, lag_ms, corr, delay, cached):
    """audio-core 7, 8: замеру верят при корреляции от 0,2 и сдвиге 20-600 мс - он идёт в кэш по имени выхода;
    иначе заявленная задержка устройств, кэш не трогается. Подменены замер (запись свипа) и сон."""
    core = bare_core()
    core.out_name, core._in, core._out = SPEAKERS, None, _Stream(0)
    monkeypatch.setattr(audio_core, "CAL_FILE", tmp_path / "calibration.json")
    monkeypatch.setattr(audio_core, "find_lag", lambda out, mic, offset: (lag_ms, corr))
    patch_module_sleep(monkeypatch, audio_core, lambda s: None)
    monkeypatch.setattr(core, "busy", lambda: False)

    assert core.calibrate() == delay
    cache = json.loads((tmp_path / "calibration.json").read_text(encoding="utf-8")) if cached else {}
    assert (cache.get(SPEAKERS, {}).get("delay_ms") == delay) is cached
    assert (tmp_path / "calibration.json").exists() is cached


# ─── смена выхода ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("flag", [None, SPEAKING_FLAG, HEARING_FLAG, "queue"], ids=["idle", "speaking", "hearing",
                                                                                     "queue"])
def test_output_is_polled_only_when_nobody_speaks(tmp_path, flag):
    """audio-core 10: системный выход опрашивается только в тишине: очередь колонок пуста, нет speaking и hearing."""
    core = bare_core()
    if flag == "queue":
        core.play(np.full(BLOCK, 1, np.int16), RATE)
    elif flag:
        (tmp_path / flag).touch()
    assert core._idle(tmp_path) is (flag is None)


class _Outputs(_FakeSD):
    """Выходы системы: колонки и, если plugged, монитор; open_fails - монитор в списке, но не открывается."""

    def __init__(self, plugged=True, open_fails=False):
        super().__init__()
        self.mic, self.plugged, self.open_fails, self.reinits = True, plugged, open_fails, 0

    def _devices(self):
        devs = super()._devices()
        if self.plugged:
            devs.append({"index": 2, "name": HDMI, "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2})
        return devs

    def OutputStream(self, device=None, **kw):
        if device == 2 and self.open_fails:
            raise self.PortAudioError("Invalid device")
        return super().OutputStream(device, **kw)

    def _terminate(self):
        self.reinits += 1


@pytest.mark.parametrize("plugged, open_fails, ends_on, missing, reinit", [
    (True, False, HDMI, "", 0),                  # в списке - переключиться, только колонки
    (False, False, SPEAKERS, HDMI, 1),           # нет и после перечитывания - остаться, имя больше не пробовать
    (True, True, SPEAKERS, HDMI, 0),             # в списке, но не открылся - вернуться на прежний
], ids=["switch", "not-found", "open-fails"])
def test_output_switch_keeps_sam_audible(monkeypatch, plugged, open_fails, ends_on, missing, reinit):
    """audio-core 11, 12, 13: смена системного выхода переоткрывает колонки и выставляет задержку; нового устройства
    нет в PortAudio - список перечитывается (микрофон переоткрывается); не нашлось или не открылось - прежний выход."""
    fake = _Outputs(plugged, open_fails)
    monkeypatch.setattr(audio_core, "sd", fake)
    _livekit(monkeypatch, False)
    core = AudioCore(aec=False)
    delays = []
    monkeypatch.setattr(core, "_set_delay", lambda force=False: delays.append(core.out_name))
    core._switch_output(HDMI)
    assert core.out_name == ends_on and core._missing == missing and fake.reinits == reinit
    assert core._out.active
    assert delays == ([ends_on] if not open_fails else [])


def test_missing_output_is_not_retried_and_switching_waits_for_silence(tmp_path, monkeypatch):
    """audio-core 10, 13: опрос раз в DEVICE_POLL (2 с); имя, которое не нашлось, больше не пробуется; пока SAM
    говорит, выход не меняется."""
    fake = _Outputs(plugged=False)
    monkeypatch.setattr(audio_core, "sd", fake)
    _livekit(monkeypatch, False)
    core = AudioCore(aec=False)
    tries, polls = [], []
    monkeypatch.setattr(core, "_switch_output", lambda name: (tries.append(name), setattr(core, "_missing", name)))
    defaults = iter([HDMI, HDMI, HEADPHONES])
    monkeypatch.setattr(audio_core, "default_output_name", lambda: next(defaults))

    def sleep(seconds):
        polls.append(seconds)
        if len(polls) == 3:
            (tmp_path / SPEAKING_FLAG).touch()          # третий опрос - SAM говорит
        if len(polls) == 4:
            core._closed = True
    patch_module_sleep(monkeypatch, audio_core, sleep)
    core._watch(tmp_path)

    assert audio_core.DEVICE_POLL == 2.0 and set(polls) == {2.0}
    assert tries == [HDMI]


# ─── микрофон: сторож ─────────────────────────────────────────────────────────

def test_lost_mic_reaches_the_lead_as_a_mic_tick(sd_with_mic, tmp_path, monkeypatch):
    """audio-core 16: потеря микрофона - событие lost в mic.jsonl, тикер ведущего приносит его тиком mic."""
    _livekit(monkeypatch, False)
    core = AudioCore(aec=False)
    clock = types.SimpleNamespace(t=100.0)
    core.mic_watch = MicWatch(live=True, stall_secs=2.0, poll_secs=2.0, clock=lambda: clock.t)
    out = []
    ticker = tk.Ticker(tmp_path, out=out.append)
    clock.t += 2.1
    core._mic_step(tmp_path)
    ticker.poll()
    [tick] = [json.loads(line) for line in out]
    assert (tick["kind"], tick["status"]) == ("mic", "lost") and WEBCAM_FULL in tick["text"]


@pytest.mark.parametrize("speaking", [True, False], ids=["sam-speaks", "silence"])
def test_lost_mic_is_reopened_only_between_lines(sd_with_mic, tmp_path, monkeypatch, speaking):
    """audio-core 19: перечитывание PortAudio закрывает колонки - посреди речи SAM вход не переоткрывается."""
    _livekit(monkeypatch, False)
    core = AudioCore(aec=False)
    clock = types.SimpleNamespace(t=100.0)
    core.mic_watch = MicWatch(live=False, stall_secs=2.0, poll_secs=2.0, clock=lambda: clock.t)
    reopened = []
    monkeypatch.setattr(core, "_reopen_mic", lambda: reopened.append(True))
    if speaking:
        core.play(np.full(BLOCK, 1, np.int16), RATE)
    clock.t += 2.1
    core._mic_step(tmp_path)
    assert bool(reopened) is not speaking


# ─── выбор входа ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("arg, env, device", [
    (5, "Webcam", 5),                      # --device главнее
    (None, "1", 1),                      # MIC_DEVICE индексом
    (None, "webcam", 1),                 # MIC_DEVICE частью имени, без регистра
    (None, "Динамики", None),            # выход с таким именем - не вход: системный
    (None, "", None),
], ids=["arg", "env-index", "env-name", "env-not-an-input", "default"])
def test_input_is_picked_by_flag_then_mic_device_then_system(sd_with_mic, monkeypatch, arg, env, device):
    """audio-core 23: вход - --device, иначе MIC_DEVICE (индекс или часть имени), иначе системный."""
    monkeypatch.setattr(hearing, "sd", audio_core.sd)
    monkeypatch.setenv("MIC_DEVICE", env)
    assert hearing.pick_device(arg) == device


# ─── договаривание ────────────────────────────────────────────────────────────

def test_wind_down_finishes_a_phrase_with_less_than_0_6_s_left():
    """barge-in 30: перебили, а до конца фразы меньше 0,6 с - фраза договаривается до конца, хотя пауза между словами
    раньше: замолчать в ней значило бы оборвать фразу на последнем слове."""
    def wound(left_secs):
        core = bare_core()
        phrase = speech("wg" * 10)
        core.play(phrase, RATE)
        core.end_phrase()
        lead = (len(phrase) - int(left_secs * RATE)) // BLOCK * BLOCK
        drain(core, lead // BLOCK)
        core.wind_down(0.03, 0.4)
        rest = drain(core, (len(phrase) - lead) // BLOCK + 1)
        return np.count_nonzero(rest), np.count_nonzero(phrase[lead:])
    played, whole = wound(0.55)
    assert played >= 0.95 * whole
    played, whole = wound(1.5)                                       # до конца далеко - замолкает в паузе
    assert played < 0.5 * whole
    assert audio_core.WIND_FINISH_SECS == 0.6


def test_wind_down_without_a_pause_fades_out_after_three_more_seconds():
    """barge-in 32: речь без пауз - после запаса ещё 3 с поиска паузы, затем затухание за 0,4 с до тишины."""
    keep, fade, extra = int(1.1 * RATE), int(0.4 * RATE), int(audio_core.WIND_EXTRA_SECS * RATE)
    w = WindDown(1.0, 0.4, keep, fade, extra)
    audio = np.full(keep + extra + fade + RATE, 10_000, np.int16)
    out = np.concatenate([w.apply(audio[i:i + BLOCK], audio[i + BLOCK:]) for i in range(0, len(audio), BLOCK)])
    assert audio_core.WIND_EXTRA_SECS == 3.0 and w.stop_at is None
    before_fade = out[keep + extra - BLOCK:keep + extra]
    assert np.all(before_fade > 0) and np.all(out[keep + extra + fade:] == 0)
    fading = out[keep + extra:keep + extra + fade].astype(np.float32)
    assert np.all(np.diff(fading) <= 0)                              # гаснет плавно, без возврата


# ─── фаза 5: P3 ───────────────────────────────────────────────────────────────

def _cal_core(tmp_path, monkeypatch, cache: dict):
    core = bare_core()
    core.out_name, core.apm, core.delay_ms = SPEAKERS, _Apm(), 0
    cal = tmp_path / "calibration.json"
    if cache:
        cal.write_text(json.dumps(cache), encoding="utf-8")
    monkeypatch.setattr(audio_core, "CAL_FILE", cal)
    measured = []
    monkeypatch.setattr(core, "calibrate", lambda: measured.append(core.out_name) or 240)
    return core, measured


@pytest.mark.parametrize("cache, force, delay, measured", [
    ({SPEAKERS: {"delay_ms": 180}}, False, 180, []),               # выход из кэша не калибруется заново
    ({HDMI: {"delay_ms": 300}}, False, 240, [SPEAKERS]),           # кэш по имени выхода: чужой не подходит
    ({SPEAKERS: {"delay_ms": 180}}, True, 240, [SPEAKERS]),        # перекалибровка по флагу - замер
], ids=["cached", "other-output", "forced"])
def test_aec_delay_comes_from_the_cache_by_output_name(tmp_path, monkeypatch, cache, force, delay, measured):
    """audio-core 8: задержка AEC кэшируется по имени выхода в SAM_SPEAKER_CALIBRATION; выход из кэша не
    калибруется заново."""
    core, got = _cal_core(tmp_path, monkeypatch, cache)
    core._set_delay(force=force)
    assert core.apm.delays == [delay] and core.delay_ms == delay and got == measured


@pytest.mark.parametrize("aec", [True, False], ids=["aec", "no-aec"])
def test_calibrate_file_recalibrates_in_the_next_silence_and_is_removed(tmp_path, monkeypatch, aec):
    """audio-core 9: файл calibrate в шине - калибровка в ближайшую тишину, кэш перезаписывается замером, файл
    удаляется; пока говорят, файл ждёт. Без AEC калибровать нечего - файл просто снимается."""
    core, measured = _cal_core(tmp_path, monkeypatch, {SPEAKERS: {"delay_ms": 180}})
    core.apm = core.apm if aec else None
    core._closed, core._follow = False, False
    bus = tmp_path / "bus"
    bus.mkdir()
    (bus / audio_core.CAL_FLAG).touch()
    (bus / SPEAKING_FLAG).touch()
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        if len(polls) == 2:
            assert (bus / audio_core.CAL_FLAG).exists() and measured == []    # SAM говорила - файл ждёт
            (bus / SPEAKING_FLAG).unlink()
        if len(polls) == 3:
            core._closed = True
    patch_module_sleep(monkeypatch, audio_core, sleep)
    core._watch(bus)
    assert not (bus / audio_core.CAL_FLAG).exists()
    assert measured == ([SPEAKERS] if aec else [])


def test_outside_windows_the_system_output_is_not_followed(monkeypatch):
    """audio-core 14: не на Windows системный выход и список входов не узнать (winmm нет) - ядро за выходом не
    следит, сторож микрофона не перечитывает список."""
    monkeypatch.setattr(audio_core.sys, "platform", "linux")
    assert audio_core.default_output_name() is None and audio_core.input_names() is None


class _Started:
    """Компонент процесса голоса на границе (модели, устройства, сокет): пишет порядок старта."""

    def __init__(self, order, name):
        self.order, self.name = order, name
        self.made = types.SimpleNamespace(start=lambda: None, watch=lambda d: None, publish=None, close=lambda: None,
                                          raw_peak=0.0, clean_peak=0.0, underflows=0, overflows=0, mic_tap=None,
                                          tap=None, run=lambda: None)

    def __call__(self, *args, **kwargs):
        self.order.append(self.name)
        return self.made


REPORT_TICKS = 5


def test_voice_process_loads_whisper_before_sound_and_reports_only_changes(tmp_path, monkeypatch, capsys):
    """audio-core 1, 26, 27, 28: модели Whisper грузятся до открытия звука; раз в секунду при SAM_VOICE_DEBUG_AEC
    и флаге speaking - пик до и после AEC; обрывы колонок и переполнения микрофона - строка только при смене счётчика;
    файл record-out в шине включает запись колонок в speaker.wav."""
    from voice.layer import voice
    order, threads = [], {}
    parts = {name: _Started(order, name) for name in ("STT", "AudioCore", "FramePublisher", "Listener")}
    for name, part in parts.items():
        monkeypatch.setattr(voice, name, part)
    monkeypatch.setattr(voice, "pick_device", lambda device: None)
    monkeypatch.setattr(voice.threading, "Thread",
                        lambda target, args=(), daemon=None, name="": types.SimpleNamespace(
                            start=lambda: threads.update({name: (target, args)})))
    monkeypatch.setattr(voice, "DEBUG_AEC", True)
    monkeypatch.setattr(sys, "argv", ["voice", str(tmp_path)])
    (tmp_path / "record-out").touch()
    voice.main()
    assert order[:2] == ["STT", "AudioCore"] and order[-1] == "Listener"
    assert threads["say"][1][2] is not None                         # запись колонок включена файлом
    assert (tmp_path / "speaker.wav").exists()

    core = parts["AudioCore"].made
    counts = iter([(0, 0), (2, 0), (2, 0), (2, 1), (2, 1)])
    ticks = []

    def sleep(seconds):                                             # секунда отчёта: звук жил, счётчики сменились
        ticks.append(seconds)
        if len(ticks) > REPORT_TICKS:
            raise StopIteration
        core.underflows, core.overflows = next(counts)
        core.raw_peak, core.clean_peak = 900.0, 40.0
    patch_module_sleep(monkeypatch, voice, sleep)
    (tmp_path / SPEAKING_FLAG).touch()
    capsys.readouterr()
    with pytest.raises(StopIteration):
        threads["report"][0]()
    out = capsys.readouterr().out.splitlines()
    assert set(ticks) == {1}
    assert sum("микрофон до AEC 900, после 40" in line for line in out) == REPORT_TICKS
    assert [line for line in out if "обрывы" in line] == ["[звук] обрывы колонок 2, переполнения микрофона 0",
                                                          "[звук] обрывы колонок 2, переполнения микрофона 1"]


def test_speaker_recording_keeps_only_the_time_of_lines(tmp_path, monkeypatch):
    """audio-core 28: запись колонок включает SAM_VOICE_RECORD_OUT=1 или файл record-out; в speaker.wav идёт только
    звук реплик (active), тишина между ними не пишется."""
    from voice.layer import recorder
    assert not recorder.enabled(tmp_path)
    monkeypatch.setenv("SAM_VOICE_RECORD_OUT", "1")
    assert recorder.enabled(tmp_path)
    rec = recorder.OutputRecorder(tmp_path / recorder.SPEAKER_WAV, RATE)
    rec.feed(np.full(BLOCK, 7, np.int16))                       # между репликами
    rec.active = True
    rec.feed(np.full(BLOCK, 9, np.int16))
    rec.active = False
    rec.feed(np.full(BLOCK, 7, np.int16))
    deadline = time.monotonic() + WAIT_S
    while not rec._q.empty() and time.monotonic() < deadline:   # писатель - свой поток
        time.sleep(0.01)
    time.sleep(0.1)
    rec._wav.close()                                          # закрытие сбрасывает буфер файла
    import wave
    with wave.open(str(tmp_path / recorder.SPEAKER_WAV), "rb") as wav:
        data = np.frombuffer(wav.readframes(wav.getnframes()), np.int16)
    assert len(data) == BLOCK and np.all(data == 9)
