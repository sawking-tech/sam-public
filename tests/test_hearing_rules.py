"""Слух (voice/layer/hearing.py), правила фразы: преролл, конец длинной фразы, предел 120 с, куски по ходу речи, эхо по
голосу, отсев сегментов Whisper. Настоящий Listener без микрофона; подменены Whisper (модель) и опознание голоса
(модель ECAPA - граница: само опознание проверяет tests/test_speakers.py)."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from voice.layer import hearing
from voice.layer.bus import HEARING_FLAG, SAY_OUT, VOICE_IN

LOUD = hearing.SILENCE_RMS * 3
BARGE = hearing.BARGE_RMS + 100      # громче порога перебивания без AEC
QUIET_MARK = 7          # тихий звук до фразы: ниже порога речи, узнаётся в начале распознанного звука
SAID = "Семафор пропускает десять запросов, остальные ждут."
BLOCKS_PER_S = hearing.SAMPLE_RATE // hearing.BLOCK


class _STT:
    """Whisper: отдаёт тексты по очереди и помнит звук каждого вызова."""

    def __init__(self, *texts):
        self.texts, self.audio = list(texts), []

    def transcribe(self, audio):
        self.audio.append(audio)
        return hearing.strip_hallucinations(self.texts.pop(0) if len(self.texts) > 1 else self.texts[0])

    def quick(self, audio):
        return ""


@pytest.fixture
def make(tmp_path, monkeypatch):
    """Listener; who - ответ опознания голоса (None - библиотеки нет, слух без опознания)."""
    def build(stt, who=None):
        if who is None:
            def no_encoder():
                raise RuntimeError("ECAPA в тестах не грузится")
            monkeypatch.setattr(hearing.speakers, "encoder", no_encoder)
        else:
            monkeypatch.setattr(hearing.speakers, "encoder", lambda: object())
            monkeypatch.setattr(hearing.speakers, "identify", lambda audio, known=None: who)
        return hearing.Listener(tmp_path, stt, device=None)
    return build


def _feed(lst, rms: float, secs: float):
    for _ in range(round(secs * BLOCKS_PER_S)):
        lst.step(np.full(hearing.BLOCK, int(rms), dtype=np.int16))


def _records(d):
    path = d / VOICE_IN
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _said(d, text):
    now = hearing.datetime.now().astimezone().isoformat()
    (d / SAY_OUT).write_text(json.dumps({"ts": now, "text": text}, ensure_ascii=False) + "\n", encoding="utf-8")


def _wait_parts(lst):
    for fut in list(lst.parts):
        fut.result(timeout=5)


def test_half_a_second_before_the_phrase_is_kept(make, tmp_path):
    """hearing 2: фраза начинается с 0,5 с звука, последний блок которых - первый громкий: начало слова не
    обрезается."""
    stt = _STT("открой окно")
    lst = make(stt)
    _feed(lst, QUIET_MARK, 2.0)
    _feed(lst, LOUD, 1.0)
    _feed(lst, 0, hearing.SILENCE_SHORT + 0.1)

    audio = stt.audio[0]
    preroll = int(hearing.PREROLL_SECS * hearing.SAMPLE_RATE)
    assert np.all(audio[:preroll - hearing.BLOCK] == QUIET_MARK) and np.all(audio[preroll - hearing.BLOCK:preroll] == LOUD)
    assert len(audio) > preroll
    assert [r["text"] for r in _records(tmp_path)] == ["открой окно"]


@pytest.mark.parametrize("speech_s, pause_s, ended", [
    (5.0, 1.6, True),                 # короткая фраза - конец через 1,5 с
    (11.0, 2.0, False),               # после 10 с речи 2 с - ещё не конец
    (11.0, 2.6, True),
], ids=["short-ends-at-1.5", "long-goes-on-at-2", "long-ends-at-2.5"])
def test_long_phrase_waits_longer_for_its_end(make, tmp_path, speech_s, pause_s, ended):
    """hearing 4, addressee 15: после 10 с речи конец фразы - тишина 2,5 с, а не 1,5 с: длинную мысль не режет."""
    lst = make(_STT("длинная мысль"))
    _feed(lst, LOUD, speech_s)
    _feed(lst, 0, pause_s)
    assert bool([r for r in _records(tmp_path) if not r.get("partial")]) is ended
    assert (tmp_path / HEARING_FLAG).exists() is not ended


def test_endless_phrase_is_closed_at_120_seconds(make, tmp_path):
    """hearing 6: речь без пауз закрывается на 120 с, а не копится бесконечно."""
    lst = make(_STT("радио"))
    _feed(lst, LOUD, hearing.MAX_PHRASE_SECS - 0.1)
    assert _records(tmp_path) == []
    _feed(lst, LOUD, 0.2)
    [record] = _records(tmp_path)
    assert record["secs"] == hearing.MAX_PHRASE_SECS


def test_chunks_go_out_during_speech_and_only_the_tail_is_left(make, tmp_path):
    """hearing 7: пауза от 0,5 с после куска длиннее 2 с - кусок распознаётся сразу и пишется строкой partial;
    к концу фразы распознаётся только хвост, итог - оба куска."""
    stt = _STT("купи хлеба", "и молока")
    lst = make(stt)
    _feed(lst, LOUD, 2.5)
    _feed(lst, 0, 0.6)
    _wait_parts(lst)
    assert [(r["text"], r.get("partial")) for r in _records(tmp_path)] == [("купи хлеба", True)]
    _feed(lst, LOUD, 1.0)
    _feed(lst, 0, hearing.SILENCE_SHORT + 0.1)

    assert len(stt.audio) == 2 and len(stt.audio[1]) < len(stt.audio[0])     # второй раз - только хвост
    assert [(r["text"], r.get("partial")) for r in _records(tmp_path)][-1] == ("купи хлеба и молока", None)


def test_short_pause_inside_a_short_chunk_sends_nothing_early(make, tmp_path):
    """hearing 7: кусок короче 2 с на паузе не отправляется - распознаётся вместе с фразой."""
    stt = _STT("да")
    lst = make(stt)
    _feed(lst, LOUD, 1.0)
    _feed(lst, 0, 0.6)
    assert stt.audio == [] and _records(tmp_path) == []


@pytest.mark.parametrize("who, echo, text", [
    (("claude", 0.8), True, SAID.rstrip(".")),                     # мой голос из колонок - эхо
    (("owner", 0.7), False, SAID.rstrip(".")),                      # человек повторил мою фразу - не эхо
    ((None, 0.1), True, SAID.rstrip(".")),                         # голос не узнан - решает текст
], ids=["own-voice", "human-repeats-sam", "unknown-voice"])
def test_voice_decides_echo_before_text(make, tmp_path, who, echo, text):
    """hearing 19, 20, voice-id 5: опознанный свой голос (профиль claude) - эхо; опознанный человек эхом не бывает,
    даже повторив реплику SAM слово в слово; без опознания эхо решает текст."""
    _said(tmp_path, SAID)
    lst = make(_STT(SAID), who=who)
    _feed(lst, LOUD, 1.0)
    _feed(lst, 0, hearing.SILENCE_SHORT + 0.1)
    [record] = _records(tmp_path)
    assert (record["echo"], record["text"], record["who"]) == (echo, text, who[0])


def test_chunk_in_own_voice_is_not_written(make, tmp_path):
    """voice-id 5, 11: кусок по ходу речи опознаётся своим голосом; кусок голосом SAM в шину не пишется."""
    lst = make(_STT("я сейчас всё проверю", "проверила"), who=("claude", 0.9))
    _feed(lst, LOUD, 2.5)
    _feed(lst, 0, 0.6)
    _wait_parts(lst)
    assert [r for r in _records(tmp_path) if r.get("partial")] == []


def test_phrase_after_a_barge_starts_one_second_back(make, tmp_path):
    """hearing 11, barge-in 3, 8, 9: пока SAM говорит, 3 громких блока подряд без опознания - перебивание loud; фраза
    перебивания начинается из кольца за 1 с, а не с блока, где перебивание заметили."""
    stt = _STT("подожди, не то")
    lst = make(stt)
    (tmp_path / hearing.SPEAKING_FLAG).touch()
    _feed(lst, QUIET_MARK, 2.0)
    _feed(lst, BARGE, 0.2)
    assert not (tmp_path / hearing.BARGE_FLAG).exists()          # два громких блока - ещё не перебивание
    _feed(lst, BARGE, 0.1)
    assert (tmp_path / hearing.BARGE_FLAG).read_text(encoding="utf-8") == "loud"
    (tmp_path / hearing.SPEAKING_FLAG).unlink()
    _feed(lst, LOUD, 1.0)
    _feed(lst, 0, hearing.SILENCE_SHORT + 0.1)

    audio = stt.audio[0]          # первый кусок фразы перебивания: дальше на паузе мог уйти хвост
    back = int(hearing.BARGE_BACK_SECS * hearing.SAMPLE_RATE)
    assert np.all(audio[:back - 3 * hearing.BLOCK] == QUIET_MARK)
    assert np.all(audio[back - 3 * hearing.BLOCK:back] == BARGE)


def test_whisper_segments_failing_the_thresholds_are_dropped():
    """hearing 15: сегмент с compression_ratio > 2,4, avg_logprob < -1,0 или no_speech_prob > 0,6 - мусор, не речь.
    Подменена модель faster-whisper; разбор её сегментов - настоящий STT.transcribe."""
    def seg(text, ratio=1.0, logprob=-0.2, no_speech=0.1):
        return SimpleNamespace(text=text, compression_ratio=ratio, avg_logprob=logprob, no_speech_prob=no_speech)
    segments = [seg("открой"), seg(" ла ла ла ла", ratio=2.5), seg(" шорох", logprob=-1.1),
                seg(" тишина", no_speech=0.7), seg(" окно", ratio=2.4, logprob=-1.0, no_speech=0.6)]
    stt = hearing.STT.__new__(hearing.STT)
    stt.lock = threading.Lock()
    stt.model = SimpleNamespace(transcribe=lambda audio, **kw: (iter(segments), None))
    assert stt.transcribe(np.zeros(hearing.SAMPLE_RATE, dtype=np.int16)).split() == ["открой", "окно"]


# ─── фаза 5: P3 ───────────────────────────────────────────────────────────────

def test_whisper_gets_denoised_audio_levelled_to_3000_rms_and_the_terms(monkeypatch):
    """hearing 13, 14: перед Whisper - шумоподавление и выравнивание громкости к 3000 RMS (prepare_audio); словарь
    SAM_VOICE_TERMS идёт в initial_prompt. Подменены noisereduce и модель faster-whisper."""
    import sys
    denoised, sent = [], []
    monkeypatch.setitem(sys.modules, "noisereduce", SimpleNamespace(
        reduce_noise=lambda y, sr, **kw: denoised.append(sr) or y))
    stt = hearing.STT.__new__(hearing.STT)
    stt.lock = threading.Lock()
    stt.model = SimpleNamespace(transcribe=lambda audio, **kw: sent.append((audio, kw)) or (iter([]), None))
    t = np.arange(hearing.SAMPLE_RATE) / hearing.SAMPLE_RATE
    quiet = (np.sin(2 * np.pi * 300 * t) * 300 * np.sqrt(2)).astype(np.int16)    # RMS 300 - тихо, издалека
    stt.transcribe(quiet)
    audio, kw = sent[0]
    rms = float(np.sqrt(np.mean((audio * 32768.0) ** 2)))
    assert denoised == [hearing.SAMPLE_RATE] and abs(rms - TARGET_RMS) < TARGET_RMS * 0.02
    assert kw["initial_prompt"] == hearing.TERMS


TARGET_RMS = 3000
ENV_SCRIPT = """
import os, time
from pathlib import Path
from tests import fakes_sounddevice          # заглушка: у процесса свой импорт, conftest его не ставит
fakes_sounddevice.install()
from voice.layer import hearing, retention
d = Path(os.environ["BUS"])
for name, age_h in (("mic-1.wav", 3), ("mic-2.wav", 1), ("mic.wav", 0)):
    p = d / name
    p.write_bytes(b"x")
    t = time.time() - age_h * 3600
    os.utime(p, (t, t))
print(hearing.TERMS)
print(sorted(retention.sweep(d)))
print(retention.RetentionLoop(d, active=lambda: None).every_s)
"""
SUBPROCESS_TIMEOUT_S = 60


def test_terms_and_recording_retention_come_from_settings(tmp_path):
    """hearing 14, 27: словарь Whisper - файл SAM_VOICE_TERMS; уборка записей - SAM_MIC_KEEP_HOURS, SAM_MIC_KEEP_MB
    и период SAM_MIC_SWEEP_EVERY_S. Свой процесс: настройки читаются при импорте."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    terms = tmp_path / "terms.txt"
    terms.write_text("Сэм, Барсик, Мурка", encoding="utf-8")
    bus = tmp_path / "bus"
    bus.mkdir()
    env = {**os.environ, "SAM_VOICE_TERMS": str(terms), "SAM_MIC_KEEP_HOURS": "2", "SAM_MIC_KEEP_MB": "100",
           "SAM_MIC_SWEEP_EVERY_S": "60", "BUS": str(bus), "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-c", ENV_SCRIPT], cwd=Path(__file__).resolve().parents[1], env=env,
                         capture_output=True, text=True, encoding="utf-8", timeout=SUBPROCESS_TIMEOUT_S)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert "Сэм, Барсик, Мурка" in lines and lines[-2:] == ["['mic-1.wav']", "60.0"]   # по умолчанию срок 24 ч


def test_cli_lists_devices_or_recognises_a_file_and_exits(tmp_path, monkeypatch, capsys):
    """hearing 28: --list-devices печатает устройства без загрузки моделей; --file распознаёт файл и выходит, без
    микрофона и шины. Подменены sounddevice и модель."""
    import sys
    import wave
    loaded = []
    monkeypatch.setattr(hearing.sd, "query_devices", lambda *a: "0 Микрофон (Webcam)")
    monkeypatch.setattr(hearing, "STT", lambda: loaded.append("stt") or SimpleNamespace(
        transcribe=lambda audio: f"услышала {len(audio)}"))
    monkeypatch.setattr(hearing, "Listener", lambda *a, **kw: pytest.fail("CLI не слушает микрофон"))
    monkeypatch.setattr(sys, "argv", ["hearing", "--list-devices"])
    hearing.main()
    assert capsys.readouterr().out.strip() == "0 Микрофон (Webcam)" and loaded == []

    path = tmp_path / "phrase.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(hearing.SAMPLE_RATE)
        w.writeframes(np.zeros(hearing.SAMPLE_RATE, np.int16).tobytes())
    monkeypatch.setattr(sys, "argv", ["hearing", "--file", str(path)])
    hearing.main()
    assert capsys.readouterr().out.strip() == f"услышала {hearing.SAMPLE_RATE}" and loaded == ["stt"]


THREAD_WAIT_S = 5


def test_voice_is_identified_in_its_own_thread_while_whisper_works(make, tmp_path, monkeypatch):
    """voice-id 8: опознание голоса идёт своим потоком параллельно распознаванию: Whisper ещё пишет текст, а голос
    уже опознаётся (последовательно оба ждали бы друг друга до срока)."""
    identifying = threading.Event()
    threads = {}

    class _SlowSTT(_STT):
        def transcribe(self, audio):
            threads["stt"] = threading.current_thread().name
            threads["parallel"] = identifying.wait(THREAD_WAIT_S)     # текст готов только когда опознание идёт
            return super().transcribe(audio)

    lst = make(_SlowSTT("открой окно"), who=("owner", 0.8))

    def identify(audio, known=None):
        threads["who"] = threading.current_thread().name
        identifying.set()
        return "owner", 0.8
    monkeypatch.setattr(hearing.speakers, "identify", identify)
    _feed(lst, LOUD, 1.0)
    _feed(lst, 0, hearing.SILENCE_SHORT + 0.1)
    assert threads["parallel"] and threads["who"] != threads["stt"]
    assert [(r["text"], r["who"]) for r in _records(tmp_path)] == [("открой окно", "owner")]
