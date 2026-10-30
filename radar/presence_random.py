"""Источник случайности радара (SAM-118): один сид на весь радар, потоки по именам.

Каждый, кому нужна случайность (выборка путей потока, выбор места устройства, сцены быта), берёт свой поток по имени. Поток зависит только от сида и имени,
поэтому один и тот же сид даёт те же числа в том же порядке, как бы ни чередовались запросы потоков: проигрыш записи на момент T повторяется до точки.
Сид задаётся в одном месте, переменной SAM_PRESENCE_SEED; не задан - берётся из энтропии системы и печатается, чтобы прогон можно было повторить."""
from __future__ import annotations

import random
import zlib
from typing import Mapping

import numpy as np

from gateway.env import parse_int, read

SEED_ENV = "SAM_PRESENCE_SEED"
SEED_FROM_ENTROPY = -1          # значение по умолчанию: сид из энтропии системы; сид в SeedSequence отрицательным быть не может


class Randomness:
    def __init__(self, seed: int | None = None) -> None:
        self.seed = int(np.random.SeedSequence(seed).entropy)        # сид, из которого построены все потоки; при seed=None выбран системой

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "Randomness":
        """Сид из SAM_PRESENCE_SEED; не задан, пуст или отрицателен - из энтропии системы."""
        seed = read(SEED_ENV, SEED_FROM_ENTROPY, parse_int, environ=environ)
        return cls(None if seed < 0 else seed)

    def numpy(self, name: str) -> np.random.Generator:
        """Поток numpy по имени: тот же сид и то же имя дают ту же последовательность."""
        return np.random.default_rng(self._sequence(name))

    def python(self, name: str) -> random.Random:
        """Поток random по имени для кода, которому нужен random.Random."""
        return random.Random(int(self._sequence(name).generate_state(1, dtype=np.uint64)[0]))

    def _sequence(self, name: str) -> np.random.SeedSequence:
        return np.random.SeedSequence(self.seed, spawn_key=(zlib.crc32(name.encode("utf-8")),))     # crc32 стабилен между запусками, hash() от запуска к запуску другой
