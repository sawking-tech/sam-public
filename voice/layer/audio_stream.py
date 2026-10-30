"""Потоковая обработка звука: куски от синтеза проходят цепочку стадий и уходят в колонки по одному.


У каждой стадии push(кусок) -> обработанное и flush() -> хвост. Стадии держат состояние между кусками,
поэтому на стыках нет щелчков: фильтр ресемплинга и окно растяжения видят соседние сэмплы.
Звук внутри цепочки - float32 в [-1, 1]; на входе и выходе int16 переводят to_float и to_int16.
Цепочку собирает потребитель (say_out, tts_client): стадии друг о друге не знают.
"""

from __future__ import annotations

from typing import Protocol, Sequence

import numpy as np

INT16_SCALE = 32768.0
SPEED_EPS = 0.01        # темп ближе к 1 не растягиваю: разницу не слышно, а растяжение мажет звук
# Растяжение куска считается вместе с хвостом предыдущего: края Rubber Band размыты, контекст их убирает.
TEMPO_CONTEXT_SECS = 0.2
TEMPO_FADE_SECS = 0.02  # перекрёстное затухание на стыке: сглаживает расхождение в пару миллисекунд


class Stage(Protocol):
    def push(self, audio: np.ndarray) -> np.ndarray: ...
    def flush(self) -> np.ndarray: ...


def to_float(audio: np.ndarray) -> np.ndarray:
    return audio.astype(np.float32) / INT16_SCALE if audio.dtype == np.int16 else audio.astype(np.float32)


def to_int16(audio: np.ndarray) -> np.ndarray:
    if audio.dtype == np.int16:
        return audio
    return (np.clip(audio, -1.0, 1.0) * (INT16_SCALE - 1)).astype(np.int16)


class Pipeline:
    """Стадии по порядку; вход int16 или float32, выход float32."""

    def __init__(self, stages: Sequence[Stage]) -> None:
        self.stages = list(stages)

    def push(self, audio: np.ndarray) -> np.ndarray:
        x = to_float(audio)
        for stage in self.stages:
            x = stage.push(x)
        return x

    def flush(self) -> np.ndarray:
        """Хвосты стадий: хвост каждой проходит через все следующие."""
        out = np.zeros(0, np.float32)
        for stage in self.stages:
            out = np.concatenate([stage.push(out), stage.flush()]) if len(out) else stage.flush()
        return out


class Resampler:
    """Смена частоты потоком (soxr с состоянием): частота движка → частота колонок."""

    def __init__(self, rate_in: int, rate_out: int) -> None:
        import soxr
        self.passthrough = rate_in == rate_out
        self.rs = None if self.passthrough else soxr.ResampleStream(rate_in, rate_out, 1, dtype="float32")

    def push(self, audio: np.ndarray) -> np.ndarray:
        if self.passthrough or not len(audio):
            return audio
        return self.rs.resample_chunk(audio)

    def flush(self) -> np.ndarray:
        if self.passthrough:
            return np.zeros(0, np.float32)
        return self.rs.resample_chunk(np.zeros(0, np.float32), last=True)


class Tempo:
    """Темп без смены высоты (Rubber Band из pedalboard) для потока кусков.

    pedalboard растягивает только целый буфер, поэтому кусок растягивается вместе с хвостом
    предыдущего (контекст), часть, приходящаяся на контекст, отбрасывается, а стык сглаживается
    коротким перекрёстным затуханием с придержанным концом прошлого выхода."""

    def __init__(self, rate: int, speed: float, context_secs: float = TEMPO_CONTEXT_SECS,
                 fade_secs: float = TEMPO_FADE_SECS) -> None:
        self.rate, self.speed = rate, speed
        self.active = abs(speed - 1.0) > SPEED_EPS
        self.context_len = int(context_secs * rate)
        self.fade_len = int(fade_secs * rate)
        self.context = np.zeros(0, np.float32)  # конец предыдущего входа
        self.held = np.zeros(0, np.float32)     # конец предыдущего выхода, ещё не отданный

    def _stretch(self, audio: np.ndarray) -> np.ndarray:
        from pedalboard import time_stretch
        return time_stretch(audio.reshape(1, -1), samplerate=self.rate, stretch_factor=self.speed).reshape(-1)

    def push(self, audio: np.ndarray) -> np.ndarray:
        if not self.active or not len(audio):
            return audio
        src = np.concatenate([self.context, audio])
        y = self._stretch(src)
        skip = int(round(len(self.context) / self.speed))  # столько выхода приходится на контекст
        start = max(0, skip - len(self.held))
        overlap = y[start:skip]
        if len(overlap) and len(self.held):
            n = min(len(overlap), len(self.held))
            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
            joined = self.held[-n:] * (1.0 - ramp) + overlap[-n:] * ramp
            head = np.concatenate([self.held[:-n], joined])
        else:
            head = self.held
        body = y[skip:]
        keep = min(self.fade_len, len(body))
        self.held = body[len(body) - keep:].copy()
        self.context = src[-self.context_len:].copy()
        return np.concatenate([head, body[:len(body) - keep]])

    def flush(self) -> np.ndarray:
        out, self.held = self.held, np.zeros(0, np.float32)
        return out


class Gain:
    def __init__(self, gain: float) -> None:
        self.gain = gain

    def push(self, audio: np.ndarray) -> np.ndarray:
        return audio * self.gain if self.gain != 1.0 else audio

    def flush(self) -> np.ndarray:
        return np.zeros(0, np.float32)
