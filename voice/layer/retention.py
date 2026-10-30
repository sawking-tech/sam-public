"""Срок хранения записей слуха (SAM-106): mic-*.wav и phrases/*.wav копились без ротации, около гигабайта за полтора суток.

Решение «что удалить» - чистая функция expired по именам, размерам и времени; обход диска (sweep) и поток (RetentionLoop)
только исполняют его. Свежайший файл и файл, в который слух пишет сейчас, не удаляются никогда. Повод - 10 окт диск C терял
по 5-10 ГБ в сутки, подозреваемые - записи SAM.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from gateway.env import env_float

KEEP_HOURS = env_float("SAM_MIC_KEEP_HOURS", 24.0)        # дольше сутки записи нужны разве что для разбора ошибок слуха
KEEP_MB = env_float("SAM_MIC_KEEP_MB", 600.0)              # потолок всех записей слуха вместе: мик-файл за вечер 270 МБ
SWEEP_EVERY_S = env_float("SAM_MIC_SWEEP_EVERY_S", 3600.0)  # как часто проходить по папке при работающем слухе
PATTERNS = ("mic*.wav", "phrases/*.wav")                   # записи слуха; last-phrase.wav и шина (jsonl) не трогаем


@dataclass(frozen=True)
class RecFile:
    name: str        # относительный путь внутри папки шины
    size: int
    mtime: float


def expired(files: list[RecFile], now: float, keep_s: float, max_bytes: int,
            protect: frozenset[str] = frozenset()) -> list[str]:
    """Имена файлов к удалению: старше срока, затем самые старые, пока всё не уложится в потолок.

    protect - имена, которые не трогаются (активная запись); самый новый файл тоже остаётся всегда."""
    if not files:
        return []
    newest = max(files, key=lambda f: f.mtime).name
    keep = set(protect) | {newest}
    out = [f.name for f in files if f.name not in keep and now - f.mtime > keep_s]
    left = sorted((f for f in files if f.name not in out), key=lambda f: f.mtime)
    total = sum(f.size for f in left)
    for f in left:
        if total <= max_bytes:
            break
        if f.name not in keep:
            out.append(f.name)
            total -= f.size
    return out


def scan(directory: Path, patterns: tuple[str, ...] = PATTERNS) -> list[RecFile]:
    found = {}
    for pattern in patterns:
        for p in directory.glob(pattern):
            try:
                st = p.stat()
            except OSError:
                continue                                 # файл исчез между перечислением и stat
            found[p.relative_to(directory).as_posix()] = RecFile(p.relative_to(directory).as_posix(), st.st_size, st.st_mtime)
    return list(found.values())


def sweep(directory: Path, protect: frozenset[str] = frozenset(), now: float | None = None,
          keep_hours: float = KEEP_HOURS, keep_mb: float = KEEP_MB) -> list[str]:
    """Один проход: удалить лишнее, вернуть имена удалённого. Сбой удаления - одна строка в лог, остальное продолжается."""
    now = time.time() if now is None else now
    removed = []
    for name in expired(scan(directory), now, keep_hours * 3600, int(keep_mb * 2**20), protect):
        try:
            (directory / name).unlink()
            removed.append(name)
        except OSError as e:
            print(f"[слух] запись {name} не удалена: {e}; закрой программу, которая её держит, или убери вручную")
    if removed:
        print(f"[слух] убрано старых записей: {len(removed)}", flush=True)
    return removed


class RetentionLoop:
    """Поток, который раз в SWEEP_EVERY_S убирает старые записи; активный файл берётся у слуха при каждом проходе."""

    def __init__(self, directory: Path, active: Callable[[], str | None], every_s: float = SWEEP_EVERY_S) -> None:
        self.directory, self.active, self.every_s = directory, active, every_s
        self._stop = threading.Event()

    def _protect(self) -> frozenset[str]:
        name = self.active()
        return frozenset({name}) if name else frozenset()

    def run_once(self) -> list[str]:
        return sweep(self.directory, self._protect())

    def start(self) -> threading.Thread:
        def loop() -> None:
            while True:
                try:
                    self.run_once()
                except OSError as e:
                    print(f"[слух] уборка записей не удалась: {e}")
                if self._stop.wait(self.every_s):
                    return
        thread = threading.Thread(target=loop, name="mic-retention", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
