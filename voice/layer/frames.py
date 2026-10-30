"""Кадры узла звука (ADR-017): формат кадра и раздача подписчикам, без сети и звука.

Кадр - очищенный эхоподавлением звук микрофона, int16 моно 16 кГц, как у AudioCore.mic_q (100 мс):
  заголовок HEADER: метка времени отправки (float64, time.time) и число отсчётов (uint32), затем PCM.

Раздача: у каждого подписчика своя очередь ограниченной длины. Отстающий теряет самые старые кадры и не
тормозит узел и других; потерю он видит по счётчику dropped.
"""
from __future__ import annotations

import struct
from collections import deque
from dataclasses import dataclass, field
from threading import Condition

import numpy as np

HEADER = struct.Struct("<dI")
SAMPLE_BYTES = 2                 # int16
# Очередь подписчика в кадрах: 50 кадров по 100 мс - 5 с звука; дольше отстающий уже не слушатель
QUEUE_FRAMES = 50


def encode(block: np.ndarray, ts: float) -> bytes:
    pcm = np.asarray(block, dtype=np.int16).tobytes()
    return HEADER.pack(ts, len(pcm) // SAMPLE_BYTES) + pcm


class Decoder:
    """Поток байтов из сокета -> кадры (ts, block); недополученный кадр ждёт следующих байтов."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[tuple[float, np.ndarray]]:
        self._buf += data
        out = []
        while len(self._buf) >= HEADER.size:
            ts, n = HEADER.unpack_from(self._buf)
            end = HEADER.size + n * SAMPLE_BYTES
            if len(self._buf) < end:
                break
            out.append((ts, np.frombuffer(self._buf[HEADER.size:end], dtype=np.int16).copy()))
            self._buf = self._buf[end:]
        return out


@dataclass
class Subscriber:
    """Очередь кадров одного подписчика; полная - теряет самый старый."""
    limit: int = QUEUE_FRAMES
    frames: deque = field(default_factory=deque)
    dropped: int = 0
    closed: bool = False
    _cv: Condition = field(default_factory=Condition)

    def put(self, frame: bytes) -> None:
        with self._cv:
            if len(self.frames) >= self.limit:
                self.frames.popleft()
                self.dropped += 1
            self.frames.append(frame)
            self._cv.notify()

    def take(self, timeout: float | None = None) -> bytes | None:
        """Следующий кадр; None - закрыт или за timeout ничего не пришло."""
        with self._cv:
            if not self.frames and not self.closed:
                self._cv.wait(timeout)
            return self.frames.popleft() if self.frames else None

    def close(self) -> None:
        with self._cv:
            self.closed = True
            self._cv.notify_all()


class Fanout:
    """Один кадр - всем подписчикам."""

    def __init__(self) -> None:
        self._subs: list[Subscriber] = []

    @property
    def count(self) -> int:
        return len(self._subs)

    def add(self, limit: int = QUEUE_FRAMES) -> Subscriber:
        sub = Subscriber(limit)
        self._subs.append(sub)
        return sub

    def remove(self, sub: Subscriber) -> None:
        sub.close()
        if sub in self._subs:
            self._subs.remove(sub)

    def publish(self, frame: bytes) -> None:
        for sub in list(self._subs):
            sub.put(frame)
