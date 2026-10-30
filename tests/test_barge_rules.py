"""Перебивание поверх речи SAM (voice/layer/hearing.py): громкость, притихание, опознание, hold, окна стоп-слова.
Настоящий Listener без микрофона; подменены Whisper (быстрая модель окна), опознание голоса (модель ECAPA) и ядро
звука на границе устройства (притихание записывается). Звуком вживую - tests/test_voice_layer.py и ручная проверка."""
from __future__ import annotations

import json
import time

import numpy as np
import pytest

from voice.layer import hearing
from voice.layer.bus import BARGE_FLAG, HOLD_FLAG, SAY_OUT, SPEAKING_FLAG, VOICE_IN

BLOCKS_PER_S = hearing.SAMPLE_RATE // hearing.BLOCK
WAIT_S = 5               # страховка ожидания потоков слуха (окно стоп-слова, опознание)
SAID = "Семафор пропускает десять запросов, остальные ждут."


class _Core:
    """Ядро звука: AEC включён или нет, притихание записывается."""

    def __init__(self, aec: bool):
        self.apm = object() if aec else None
        self.levels: list[float] = []

    def duck(self, level: float) -> None:
        self.levels.append(level)


class _STT:
    def __init__(self, quick="", text=""):
        self.quick_text, self.text, self.windows = quick, text, []

    def transcribe(self, audio):
        return hearing.strip_hallucinations(self.text)

    def quick(self, audio):
        self.windows.append(audio)
        return self.quick_text


@pytest.fixture
def make(tmp_path, monkeypatch):
    def build(stt=None, who="off", aec=False):
        """who: "off" - опознания нет; иначе ответ опознания (имя, оценка)."""
        if who == "off":
            def no_encoder():
                raise RuntimeError("ECAPA в тестах не грузится")
            monkeypatch.setattr(hearing.speakers, "encoder", no_encoder)
        else:
            monkeypatch.setattr(hearing.speakers, "encoder", lambda: object())
            monkeypatch.setattr(hearing.speakers, "identify", lambda audio, known=None: who)
        core = _Core(aec)
        lst = hearing.Listener(tmp_path, stt or _STT(), device=None, core=core)
        (tmp_path / SPEAKING_FLAG).touch()
        return lst, core
    return build


def _feed(lst, rms: float, blocks: int):
    for _ in range(blocks):
        lst.step(np.full(hearing.BLOCK, int(rms), dtype=np.int16))


def _until(condition, lst=None, rms=0):
    """Ждать результат потока слуха; lst - кормить тишиной (опознание забирается на следующем блоке)."""
    deadline = time.monotonic() + WAIT_S
    while not condition():
        assert time.monotonic() < deadline, "поток слуха не ответил"
        if lst is not None:
            _feed(lst, rms, 1)
        time.sleep(0.01)


def _barge(d):
    path = d / BARGE_FLAG
    return path.read_text(encoding="utf-8") if path.exists() else None


def test_first_loud_block_ducks_and_a_gone_noise_gives_the_volume_back(make, tmp_path):
    """barge-in 3, 4: первый громкий блок - притихание до 0,6; два блока и тишина - не перебивание, громкость
    возвращается; три подряд выше порога (900 без AEC) - перебивание loud."""
    lst, core = make()
    loud = hearing.BARGE_RMS + 50
    _feed(lst, loud, 2)
    _feed(lst, 0, 1)
    assert core.levels == [0.6, 1.0] and _barge(tmp_path) is None
    _feed(lst, hearing.BARGE_RMS - 50, 3)
    assert _barge(tmp_path) is None                                   # ниже порога - не громко
    _feed(lst, loud, 3)
    assert _barge(tmp_path) == "loud" and (hearing.BARGE_RMS, hearing.BARGE_BLOCKS) == (900, 3)


@pytest.mark.parametrize("who, barged", [
    (("owner", 0.55), True), (("claude", 0.9), False), ((None, 0.1), False),
], ids=["known-human", "own-voice", "guest"])
def test_loudness_barges_only_for_a_recognised_human(make, tmp_path, who, barged):
    """barge-in 5, 7: при AEC громкость ниже 600 перебивает только узнанный человек; свой голос и гость без профиля -
    нет, громкость возвращается."""
    lst, core = make(who=who, aec=True)
    _feed(lst, hearing.BARGE_RMS_SURE - 100, hearing.BARGE_BLOCKS)
    _until(lambda: lst.loud_check is None, lst, 0)
    assert (_barge(tmp_path) == "loud") is barged
    if not barged:
        assert core.levels[-1] == 1.0


def test_very_loud_sound_with_aec_barges_without_asking_whose_voice(make, tmp_path):
    """barge-in 6: при AEC громче 600 - не эхо SAM: перебивание сразу, опознание не спрашивается."""
    lst, _ = make(who=("claude", 0.9), aec=True)
    _feed(lst, hearing.BARGE_RMS_SURE + 50, hearing.BARGE_BLOCKS)
    assert _barge(tmp_path) == "loud" and lst.loud_check is None


def test_hold_line_is_not_cut_by_loudness_but_is_by_a_stop_word(make, tmp_path):
    """barge-in 18: реплику с hold громкость не перебивает и не приглушает; стоп-слово перебивает."""
    lst, core = make(stt=_STT(quick="стоп"))
    (tmp_path / HOLD_FLAG).touch()
    _feed(lst, hearing.BARGE_RMS + 500, 2 * BLOCKS_PER_S)
    assert _barge(tmp_path) != "loud" and core.levels == []
    _until(lambda: _barge(tmp_path) == "stop")


def test_quiet_windows_are_not_sent_to_the_stop_word_model(make, tmp_path):
    """barge-in 13, 14: окна стоп-слова 1,5 с с перекрытием; окно тише 150 RMS модель не видит."""
    stt = _STT(quick="")
    lst, _ = make(stt=stt)
    _feed(lst, hearing.BARGE_MIN_RMS - 50, 4 * BLOCKS_PER_S)
    time.sleep(0.2)
    assert stt.windows == []
    _feed(lst, hearing.BARGE_MIN_RMS + 50, 2 * BLOCKS_PER_S)
    _until(lambda: stt.windows)
    assert len(stt.windows[0]) == int(hearing.BARGE_WINDOW * hearing.SAMPLE_RATE)


def test_window_repeating_sam_does_not_barge(make, tmp_path):
    """barge-in 15: окно, которое повторяет реплику SAM (эхо по тексту), не перебивает, даже со стоп-словом."""
    now = hearing.datetime.now().astimezone().isoformat()
    (tmp_path / SAY_OUT).write_text(json.dumps({"ts": now, "text": SAID}, ensure_ascii=False) + "\n", encoding="utf-8")
    stt = _STT(quick="стоп семафор пропускает")
    lst, _ = make(stt=stt)
    _feed(lst, hearing.BARGE_MIN_RMS + 50, 2 * BLOCKS_PER_S)
    _until(lambda: stt.windows)
    time.sleep(0.1)
    assert _barge(tmp_path) is None


def test_stop_windows_go_on_during_a_human_phrase_over_sam(make, tmp_path):
    """barge-in 16: фраза человека, начатая мягким перебиванием поверх SAM, не прекращает окна стоп-слова:
    «стой» внутри неё перебивает."""
    stt = _STT(quick="стой")
    lst, _ = make(stt=stt)
    (tmp_path / SPEAKING_FLAG).unlink()
    _feed(lst, hearing.SILENCE_RMS * 3, 3)             # фраза человека началась до речи SAM
    (tmp_path / SPEAKING_FLAG).touch()
    _feed(lst, hearing.SILENCE_RMS * 3, 2 * BLOCKS_PER_S)
    _until(lambda: _barge(tmp_path) == "stop")
    assert lst.phrase                                   # фраза человека продолжается


def test_stop_word_in_a_chunk_during_speech_barges(make, tmp_path):
    """barge-in 17: стоп-слово в куске фразы по ходу речи обрывает SAM сразу, кусок пишется в шину."""
    stt = _STT(text="стой, погоди")
    lst, _ = make(stt=stt)
    (tmp_path / SPEAKING_FLAG).unlink()
    _feed(lst, hearing.SILENCE_RMS * 3, 2 * BLOCKS_PER_S + 5)
    (tmp_path / SPEAKING_FLAG).touch()
    _feed(lst, 0, 6)                                    # пауза 0,6 с: кусок длиннее 2 с уходит в распознавание
    _until(lambda: _barge(tmp_path) == "stop")
    _until(lambda: (tmp_path / VOICE_IN).exists())
    assert json.loads((tmp_path / VOICE_IN).read_text(encoding="utf-8").splitlines()[0])["partial"] is True
