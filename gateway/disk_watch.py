"""Надзор за диском стенда (SAM-107): свободное место на дисках, падение за сутки, повод для основной модели.

Повод - 10 окт: диск C: за десять дней потерял около 100 ГБ, в какой-то момент осталось 0,3 ГБ, а система не следила.
Решение «есть ли повод» - чистая функция report по снимкам; чтение диска (usage_of) и файл снимков отделены.
Запуск: `python -m gateway.disk_watch` печатает повод одной-двумя строками или ничего (ворота workflow disk-watch:
пустой вывод - модель не зовётся). Снимки копятся в data/disk_snapshots.jsonl, не больше SNAPSHOT_KEEP строк.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .env import env_float

DRIVES_ENV = "SAM_DISK_DRIVES"
DEFAULT_DRIVES = "C:\\,E:\\"                                # диски стенда; на Linux укажи точки монтирования
MIN_FREE_GB = env_float("SAM_DISK_MIN_FREE_GB", 30.0)       # меньше - повод сказать; у Windows и Docker на 5 ГБ уже сбои
DROP_GB = env_float("SAM_DISK_DROP_GB", 15.0)               # упало на столько за DROP_WINDOW_H - повод, даже если места ещё много
DROP_WINDOW_H = env_float("SAM_DISK_DROP_WINDOW_H", 24.0)   # окно сравнения; суточный рост 5-10 ГБ виден за двое суток, быстрый - сразу
SNAPSHOT_KEEP = 2000                                         # строк: при снимке раз в 30 минут это сорок дней
SNAPSHOT_FILE = "data/disk_snapshots.jsonl"
GB = 2**30


@dataclass(frozen=True)
class Snapshot:
    ts: float
    free_gb: dict[str, float]


def report(now: Snapshot, history: list[Snapshot], min_free_gb: float | None = None, drop_gb: float | None = None,
           window_h: float | None = None) -> list[str]:
    """Строки-поводы: мало места или быстрое падение за окно; пусто - всё в порядке. Пороги по умолчанию читаются при вызове."""
    min_free_gb = MIN_FREE_GB if min_free_gb is None else min_free_gb
    drop_gb = DROP_GB if drop_gb is None else drop_gb
    window_h = DROP_WINDOW_H if window_h is None else window_h
    lines = []
    cutoff = now.ts - window_h * 3600
    for drive, free in sorted(now.free_gb.items()):
        if free < min_free_gb:
            lines.append(f"На диске {drive} свободно {free:.1f} ГБ, меньше порога {min_free_gb:.0f} ГБ.")
        past = [s.free_gb[drive] for s in history if s.ts >= cutoff and drive in s.free_gb]
        if past and max(past) - free >= drop_gb:
            lines.append(f"Диск {drive} потерял {max(past) - free:.1f} ГБ за последние {window_h:.0f} ч, сейчас свободно {free:.1f} ГБ.")
    return lines


def usage_of(drives: list[str]) -> dict[str, float]:
    """Свободное место, ГБ, по дискам; недоступный диск пропускается (флешка вынута, диска нет на этой машине)."""
    out = {}
    for d in drives:
        try:
            out[d] = shutil.disk_usage(d).free / GB
        except OSError:
            continue
    return out


def load_history(path: Path) -> list[Snapshot]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            rows.append(Snapshot(float(d["ts"]), {k: float(v) for k, v in d["free_gb"].items()}))
        except (ValueError, KeyError, TypeError):
            continue                                            # оборванная строка при остановке записи
    return rows


def append_snapshot(path: Path, snap: Snapshot, keep: int = SNAPSHOT_KEEP) -> None:
    history = load_history(path)[-(keep - 1):]
    history.append(snap)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({"ts": s.ts, "free_gb": {k: round(v, 2) for k, v in s.free_gb.items()}},
                                         ensure_ascii=False) for s in history) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    drives = [d.strip() for d in os.getenv(DRIVES_ENV, DEFAULT_DRIVES).split(",") if d.strip()]
    path = Path(os.getenv("SAM_DISK_SNAPSHOTS") or SNAPSHOT_FILE)
    now = Snapshot(time.time(), usage_of(drives))
    if not now.free_gb:
        print(f"[диск] ни один диск из {DRIVES_ENV}={','.join(drives)} не читается; проверь список", file=sys.stderr)
        return 1
    history = load_history(path)
    lines = report(now, history)
    append_snapshot(path, now)
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
