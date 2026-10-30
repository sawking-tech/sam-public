"""Запись того, что уходит в колонки, пока я говорю: заикания и паузы видны прямо на звуке,
а не угадываются по логу.

Включается переменной SAM_VOICE_RECORD_OUT=1 или файлом record-out в папке шины (его видно при запуске
voice.py, если переменную не передать во вкладку), пишет в speaker.wav папки шины. Ядро звука отдаёт
каждый блок колонок в feed() из звукового колбэка; на диск пишет отдельный поток, чтобы колбэк
не ждал файловой системы. Пишется только время реплик (active), паузы внутри реплики - нулями,
тишина между репликами - нет: за час разговора это десятки мегабайт, а не сотни.
"""

from __future__ import annotations

import queue
import threading
import wave
from pathlib import Path

import numpy as np
from gateway.env import env_bool

SPEAKER_WAV = "speaker.wav"
RECORD_FLAG = "record-out"


def enabled(d: Path) -> bool:
    return env_bool("SAM_VOICE_RECORD_OUT", False) or (d / RECORD_FLAG).exists()


class OutputRecorder:
    def __init__(self, path: Path, rate: int) -> None:
        self.active = False
        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(rate)
        threading.Thread(target=self._write, daemon=True, name="recorder").start()

    def feed(self, block: np.ndarray) -> None:
        """Из звукового колбэка: только положить в очередь."""
        if self.active:
            self._q.put(block.copy())

    def _write(self) -> None:
        while True:
            self._wav.writeframes(self._q.get().astype(np.int16).tobytes())
