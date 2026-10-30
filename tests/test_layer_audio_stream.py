"""Проверка потокового звука (audio_stream) и договаривания после перебивания (audio_core.WindDown)

SAM-09.
"""

from __future__ import annotations

import threading
from collections import deque

import numpy as np

from voice.layer.audio_core import BLOCK, FRAME, RATE, AudioCore, WindDown
from voice.layer.audio_stream import Pipeline, Resampler, Tempo, to_int16

ENGINE_RATE = 24_000     # частота CosyVoice
CHUNKS_SECS = (0.84, 2.0, 3.0)  # куски, как их отдаёт CosyVoice (cosy.log 8 окт)
SPEED = 0.94             # темп из session.json

WORD_SECS = 0.30         # «слово» в синтетической речи
GAP_SECS = 0.10          # пауза между словами (короче 80 мс - смычка, не пауза)
MARK_SECS = 0.30         # пауза на запятой (у Кэма 300-570 мс)
LEVEL = 0.4              # громкость к концу трёх слов договаривания
KEEP_SECS, FADE_SECS, EXTRA_SECS = 1.1, 0.4, 3.0


def voice_like(seconds: float, rate: int) -> np.ndarray:
    """Гладкий сигнал с переменным тоном: у речи нет скачков между соседними сэмплами."""
    t = np.arange(int(seconds * rate)) / rate
    return (0.3 * np.sin(2 * np.pi * (180 * t + 40 * np.sin(2 * np.pi * 3 * t)))).astype(np.float32)


def split(audio: np.ndarray, rate: int) -> list[np.ndarray]:
    out, pos = [], 0
    for secs in CHUNKS_SECS:
        out.append(audio[pos:pos + int(secs * rate)])
        pos += len(out[-1])
    return out


def run_chunked(stage_factory, audio: np.ndarray, rate: int) -> np.ndarray:
    pipe = Pipeline([stage_factory()])
    return np.concatenate([pipe.push(c) for c in split(audio, rate)] + [pipe.flush()])


HF_WINDOW_SECS = 0.01
# Щелчок на стыке - всплеск высоких частот: максимум энергии второй разности по окнам 10 мс.
# Цельное растяжение 0,0333, нарезка без контекста 0,0355 (+6,5%), с контекстом 0,0333 (замер 8 окт).
HF_TOLERANCE = 1.02


def hf_peak(audio: np.ndarray, rate: int) -> float:
    win = int(HF_WINDOW_SECS * rate)
    d = np.diff(audio, 2)
    n = len(d) // win
    return float(np.sqrt((d[:n * win].reshape(n, win) ** 2).mean(1)).max())


def test_resampler_stream_matches_whole() -> None:
    audio = voice_like(sum(CHUNKS_SECS), ENGINE_RATE)
    whole = run_chunked(lambda: Resampler(ENGINE_RATE, RATE), audio, ENGINE_RATE * 10)  # один кусок
    chunked = run_chunked(lambda: Resampler(ENGINE_RATE, RATE), audio, ENGINE_RATE)
    n = min(len(whole), len(chunked))
    assert abs(len(chunked) - len(audio) * RATE / ENGINE_RATE) < RATE * 0.01
    assert np.max(np.abs(whole[:n] - chunked[:n])) < 1e-3


def test_tempo_stream_has_no_jumps_at_joins() -> None:
    audio = voice_like(sum(CHUNKS_SECS), ENGINE_RATE)
    whole = Tempo(ENGINE_RATE, SPEED)
    ref = np.concatenate([whole.push(audio), whole.flush()])
    chunked = run_chunked(lambda: Tempo(ENGINE_RATE, SPEED), audio, ENGINE_RATE)
    assert abs(len(chunked) - len(ref)) < ENGINE_RATE * 0.02          # длина та же, ±20 мс
    assert hf_peak(chunked, ENGINE_RATE) <= HF_TOLERANCE * hf_peak(ref, ENGINE_RATE)  # стыки не щёлкают


def test_tempo_off_at_normal_speed() -> None:
    audio = voice_like(1.0, ENGINE_RATE)
    assert run_chunked(lambda: Tempo(ENGINE_RATE, 1.0), audio, ENGINE_RATE).shape == audio.shape


def speech(pattern: str) -> np.ndarray:
    """w - слово, g - пауза между словами, m - пауза на запятой; int16 на частоте колонок."""
    secs = {"w": WORD_SECS, "g": GAP_SECS, "m": MARK_SECS}
    parts = [to_int16(voice_like(secs[c], RATE)) if c == "w" else np.zeros(int(secs[c] * RATE), np.int16)
             for c in pattern]
    return np.concatenate(parts)


def wind(audio: np.ndarray, block: int = 1_920) -> tuple[WindDown, np.ndarray]:
    w = WindDown(1.0, LEVEL, int(KEEP_SECS * RATE), int(FADE_SECS * RATE), int(EXTRA_SECS * RATE))
    out = []
    for i in range(0, len(audio), block):
        out.append(w.apply(audio[i:i + block], audio[i + block:]))
        if w.finished:
            break
    return w, np.concatenate(out)


def silent_at(audio: np.ndarray, pos: int) -> bool:
    return not np.any(audio[pos:pos + FRAME])


def test_wind_down_stops_between_words() -> None:
    audio = speech("wg" * 20)
    w, _ = wind(audio)
    assert w.stop_at is not None and w.stop_at >= w.keep
    assert silent_at(audio, w.stop_at)                                 # не посреди слова


def test_wind_down_goes_on_to_a_near_comma() -> None:
    # запас кончается в слове, за ним пауза между словами, ещё слово и запятая: договорить до запятой
    # (три слова, со знаком четыре)
    words_before = int(KEEP_SECS / (WORD_SECS + GAP_SECS))
    audio = speech("wg" * words_before + "w" + "g" + "w" + "m" + "wg" * 10)
    w, _ = wind(audio)
    comma_at = len(speech("wg" * words_before + "wgw"))
    assert comma_at - FRAME <= w.stop_at <= comma_at + 2 * FRAME


def test_wind_down_does_not_stop_inside_a_word() -> None:
    # «слово» со смычкой 50 мс внутри: тишина короче паузы между словами, резать нельзя (23:13)
    half = to_int16(voice_like(WORD_SECS / 2, RATE))
    word = np.concatenate([half, np.zeros(int(0.05 * RATE), np.int16), half])
    gap = np.zeros(int(GAP_SECS * RATE), np.int16)
    audio = np.concatenate([word, gap] * 20)
    w, _ = wind(audio)
    assert w.stop_at % (len(word) + len(gap)) >= len(word) - FRAME   # в паузе после слова, не в смычке


def test_wind_down_each_word_quieter() -> None:
    # плавно: каждое слово тише предыдущего, к концу трёх слов около LEVEL
    audio = speech("wg" * 20)
    _, out = wind(audio)
    step = len(speech("wg"))
    word = len(speech("w"))
    peaks = [np.max(np.abs(out[i * step:i * step + word])) / np.max(np.abs(audio[i * step:i * step + word]))
             for i in range(3)]
    assert peaks[0] > peaks[1] > peaks[2]
    assert abs(peaks[2] - LEVEL) < 0.25



class _OutStatus:
    output_underflow = False


def bare_core() -> AudioCore:
    """Ядро без устройств: колбэк колонок крутит тест."""
    c = AudioCore.__new__(AudioCore)
    c._play, c._lock = deque(), threading.Lock()
    c.gain, c.target, c.ramp, c.fade_stop = 1.0, 1.0, 0.15, False
    c._wind, c._phrase_end = None, False
    c.tap, c._cal, c.apm, c.underflows = None, None, None, 0
    return c


def drain(core: AudioCore, blocks: int) -> np.ndarray:
    out = np.zeros((BLOCK, 1), np.int16)
    got = []
    for _ in range(blocks):
        core._on_out(out, BLOCK, None, _OutStatus)
        got.append(out[:, 0].copy())
    return np.concatenate(got)


def test_wind_down_ends_with_the_phrase_and_spares_the_next() -> None:
    # SAM-180: перебили на последнем слове короткой реплики - договаривание кончается вместе с ней, а не живёт
    # ещё 4,5 с и не глушит и не режет посреди слова следующую реплику (прозрачная речь шлёт реплики короткими строками)
    core = bare_core()
    core.play(speech("wg"), RATE)
    core.end_phrase()
    core.wind_down(KEEP_SECS, FADE_SECS)
    drain(core, 20)
    assert not core.busy() and not core.winding
    nxt = speech("wg" * 15)
    core.play(nxt, RATE)
    out = drain(core, len(nxt) // BLOCK + 1)
    assert np.array_equal(out[:len(nxt)], nxt)    # целиком и в полную громкость


def test_next_line_between_take_and_wind_check_is_not_trimmed() -> None:
    # SAM-180, ревью: следующая строка пришла, когда колбэк уже забрал конец фразы, но ещё не снял договаривание -
    # она звучит целиком, а не режется по остатку запаса
    core = bare_core()
    first = speech("wg")
    core.play(first, RATE)
    core.end_phrase()
    core.wind_down(KEEP_SECS, FADE_SECS)
    core._take(len(first))                 # первая половина колбэка: очередь пуста, договаривание ещё живо
    nxt = speech("wg" * 40)
    core.play(nxt, RATE)
    assert sum(len(a) for a in core._play) == len(nxt)
    assert not core.winding
