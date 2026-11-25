"""Слух голосового слоя (SAM-54): фраза по паузе, эхо своей озвучки, стоп-слово поверх речи - в Listener без
микрофона; Whisper подменён, его чистка от галлюцинаций - настоящая. Договаривание WindDown -
tests/test_layer_audio_stream.py. Живая проверка (перебивание голосом, «погоди» поверх речи) - только на звуке.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from voice.layer import hearing
from voice.layer.bus import BARGE_FLAG, HEARING_FLAG, MUTE_FLAG, SAY_OUT, SPEAKING_FLAG, VOICE_IN

# Своя реплика из колонок (значения эха и стоп-слов проверены вживую)
SAID = "Семафор пропускает десять запросов, остальные ждут."
LOUD = hearing.SILENCE_RMS * 3


class _FakeSTT:
    """Whisper: сырой текст модели проходит ту же чистку, что у настоящего распознавателя."""

    def __init__(self, text="", quick=""):
        self.text, self.quick_text = text, quick

    def transcribe(self, audio):
        return hearing.strip_hallucinations(self.text)

    def quick(self, audio):
        return self.quick_text


@pytest.fixture
def listener(tmp_path, monkeypatch):
    def _no_encoder():
        raise RuntimeError("ECAPA в тестах не грузится")
    monkeypatch.setattr(hearing.speakers, "encoder", _no_encoder)
    return lambda stt: hearing.Listener(tmp_path, stt, device=None)


def _said(d, *texts):
    """Мои реплики в say-out только что: по ним слух узнаёт эхо."""
    now = hearing.datetime.now().astimezone().isoformat()
    with (d / SAY_OUT).open("a", encoding="utf-8") as f:
        for text in texts:
            f.write(json.dumps({"ts": now, "text": text}, ensure_ascii=False) + "\n")


def _records(d):
    path = d / VOICE_IN
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _feed(lst, rms: float, secs: float):
    for _ in range(int(secs * hearing.SAMPLE_RATE / hearing.BLOCK)):
        lst.step(np.full(hearing.BLOCK, int(rms), dtype=np.int16))


@pytest.mark.parametrize("raw, said, loud_secs, written", [
    ("открой браузер", [], 1.0, [("открой браузер", False)]),
    ("кашель", [], 0.2, []),                                     # короткий шум - не фраза
    ("Спасибо за просмотр", [], 1.0, []),                        # хвост-галлюцинация Whisper
    (SAID, [SAID], 1.0, [(SAID.rstrip("."), True)]),             # своя фраза из колонок - эхо
    ("а что если семафор", [SAID], 1.0, [("а что если семафор", False)]),   # своя тема - не эхо
    (SAID + " А почему не блокировка?", [SAID], 1.0, [("А почему не блокировка", False)]),   # хвост человека
], ids=["phrase", "short-noise", "hallucination", "echo", "own-topic", "human-tail"])
def test_listener_writes_phrase_on_pause_without_echo_noise_or_hallucination(listener, tmp_path, raw, said,
                                                                              loud_secs, written):
    _said(tmp_path, *said)
    lst = listener(_FakeSTT(raw))
    _feed(lst, LOUD, loud_secs)
    assert (tmp_path / HEARING_FLAG).exists()          # пока он говорит, озвучка ждёт
    _feed(lst, 0, hearing.SILENCE_SHORT + 0.2)
    assert [(r["text"], r["echo"]) for r in _records(tmp_path)] == written
    assert not (tmp_path / HEARING_FLAG).exists()


class _CountingSTT(_FakeSTT):
    def __init__(self, text=""):
        super().__init__(text)
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return super().transcribe(audio)


@pytest.mark.parametrize("started", [False, True], ids=["muted-before", "muted-mid-phrase"])
def test_mute_drops_sound_and_the_started_phrase(listener, tmp_path, started):
    """hearing 9: пока лежит mute, звук не становится фразой - ни строки в voice-in, ни записи фразы, ни
    распознавания; начатая до mute фраза бросается и после снятия флага не дописывается."""
    stt = _CountingSTT("открой браузер")
    lst = listener(stt)
    if started:
        _feed(lst, LOUD, 1.0)
        assert (tmp_path / HEARING_FLAG).exists()
    (tmp_path / MUTE_FLAG).touch()
    _feed(lst, LOUD, 1.0)
    assert not (tmp_path / HEARING_FLAG).exists()
    _feed(lst, 0, hearing.SILENCE_SHORT + 0.2)
    (tmp_path / MUTE_FLAG).unlink()
    _feed(lst, 0, hearing.SILENCE_SHORT + 0.2)
    assert _records(tmp_path) == [] and stt.calls == 0
    assert list(tmp_path.rglob("*.wav")) == []


class _StopRun(Exception):
    """Конец записи в тесте: run слуха читает микрофон бесконечно."""


class _MicCore:
    """Ядро звука на границе устройства: отдаёт заданные блоки микрофона, затем останавливает run."""
    apm = None
    input_name = "тестовый микрофон"

    def __init__(self, blocks: int):
        self.mic_q = self
        self._left = blocks

    def get(self, timeout=None):
        if self._left == 0:
            raise _StopRun
        self._left -= 1
        return np.zeros(hearing.BLOCK, dtype=np.int16)


RESTART_AT = hearing.datetime(2026, 10, 14, 12, 34, 56)


def _launch(tmp_path, monkeypatch, blocks: int, at=RESTART_AT) -> None:
    """Один запуск слуха: запись mic за запуск, уборка записей - один проход вместо потока."""
    class _Clock(hearing.datetime):
        @classmethod
        def now(cls, tz=None):
            return at
    monkeypatch.setattr(hearing, "datetime", _Clock)
    monkeypatch.setattr(hearing.retention.RetentionLoop, "start", hearing.retention.RetentionLoop.run_once)
    lst = hearing.Listener(tmp_path, _FakeSTT(), device=None, core=_MicCore(blocks))
    with pytest.raises(_StopRun):
        lst.run()
    lst.mic.close()


def _frames(path) -> int:
    import wave
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes()


def test_restart_writes_mic_to_a_new_file_and_keeps_the_previous(listener, tmp_path, monkeypatch):
    """hearing 24: первый запуск пишет mic.wav, перезапуск - mic-ЧЧММСС.wav, прошлая запись цела."""
    _launch(tmp_path, monkeypatch, blocks=3)
    _launch(tmp_path, monkeypatch, blocks=1)
    assert _frames(tmp_path / hearing.MIC_WAV) == 3 * hearing.BLOCK
    assert _frames(tmp_path / "mic-123456.wav") == hearing.BLOCK


def test_second_restart_within_a_second_keeps_the_previous(listener, tmp_path, monkeypatch):
    for blocks in (3, 2, 1):
        _launch(tmp_path, monkeypatch, blocks=blocks)
    assert sorted(_frames(p) for p in tmp_path.glob("mic*.wav")) == [b * hearing.BLOCK for b in (1, 2, 3)]


KEY_DIGITS = "904317"


def test_key_is_masked_inside_hearing_before_voice_in(listener, tmp_path, monkeypatch):
    """hearing 17: ключ, названный голосом, маскируется в самом слухе - ни готовая фраза, ни кусок по ходу речи не
    кладут его цифры в voice-in.jsonl; признак несёт назначение ключа."""
    from concurrent.futures import Future

    from gateway.secret_mask import MASK
    monkeypatch.setenv("SAM_COMMAND_SECRET", "904 317")
    lst = listener(_FakeSTT(f"ключ {KEY_DIGITS} перезапусти шлюз"))
    _feed(lst, LOUD, 1.0)
    _feed(lst, 0, hearing.SILENCE_SHORT + 0.2)
    part = Future()
    part.set_result(f"ключ {KEY_DIGITS}")
    lst._emit_partial(part, np.zeros(hearing.BLOCK, np.int16))

    final, partial = _records(tmp_path)
    assert KEY_DIGITS not in (tmp_path / VOICE_IN).read_text(encoding="utf-8")
    assert final["text"] == f"ключ {MASK} перезапусти шлюз" and final["secret"] == ["command"]
    assert partial["partial"] and partial["text"] == f"ключ {MASK}" and partial["secret"] == ["command"]


@pytest.mark.parametrize("heard, said, barged", [
    ("погоди", [], True),
    ("погодите секунду", [], True),
    ("стоп", [], True),
    ("хватит", ["хватит на сегодня"], False),        # своё «хватит» из колонок не в счёт
    ("это стопится иногда", [], False),              # корень внутри слова - не команда
    ("ну подожди я не про то говорю вообще", [], False),   # длинная фраза - рассказ
    ("", [], False),
], ids=["pogodi", "pogodite", "stop", "own-hvatit", "root-inside-word", "long-story", "empty"])
def test_stop_word_while_speaking_raises_barge(listener, tmp_path, heard, said, barged):
    _said(tmp_path, *said)
    lst = listener(_FakeSTT(quick=heard))
    (tmp_path / SPEAKING_FLAG).touch()
    lst.check_barge(np.full(int(hearing.BARGE_WINDOW * hearing.SAMPLE_RATE), 2000, np.int16))
    assert lst.barged.is_set() is barged
    assert ((tmp_path / BARGE_FLAG).read_text(encoding="utf-8") if barged else None) == ("stop" if barged else None)


def test_sure_barge_by_loudness_only_with_aec():
    assert hearing.is_sure_barge(hearing.BARGE_RMS_SURE, aec=True)
    assert not hearing.is_sure_barge(hearing.BARGE_RMS_SURE * 2, aec=False)


def test_env_file_reaches_voice_layer_process_env_wins(tmp_path, monkeypatch):
    """Настройки голоса из .env доходят до слоя (порог перебивания 9 окт); окружение процесса главнее файла."""
    from voice.layer import config
    env = tmp_path / ".env"
    env.write_text("SAM_TEST_FROM_FILE=1800\nSAM_TEST_BOTH=file\n", encoding="utf-8")
    monkeypatch.delenv("SAM_TEST_FROM_FILE", raising=False)
    monkeypatch.setenv("SAM_TEST_BOTH", "process")
    assert config.load_env(env)
    assert os.environ["SAM_TEST_FROM_FILE"] == "1800" and os.environ["SAM_TEST_BOTH"] == "process"
    monkeypatch.delenv("SAM_TEST_FROM_FILE")
