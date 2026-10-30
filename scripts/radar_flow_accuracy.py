"""Точность потока радара на размеченной проходке (SAM-123): история живого потока и прогон сырой записи через свежий поток одной метрикой.

Метрика - доля размеченных секунд, где точка субъекта лежит в верной области и в верной комнате (области и комнаты - по таблице классов
набора проходок, `scripts/radar_nn_dataset.py`). Субъект - телефон по части имени (`s24`, `redmi`) или человек (`person:<имя>`); у телефона
на двух диапазонах по секунде берётся строка его текущего диапазона - того, на котором он в эту секунду свежее слышен в сырой записи.

Запуск:
  python scripts/radar_flow_accuracy.py history [--from 21:33:00 --to 21:54:00]
  python scripts/radar_flow_accuracy.py replay  [--from ... --to ...] [--warmup 60]
Пути - аргументами, по умолчанию данные 11 окт; прогон собирает поток как живой радар (`radar.presence_hub.build`, .env)."""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import radar_nn_dataset as nd  # noqa: E402

DAY = "2026-10-11"
DEFAULT_TICKS = ROOT / "data/walks/nn/ticks-2026-10-11.jsonl"
DEFAULT_CLASSES = ROOT / "data/walks/nn/classes-2026-10-11.json"
DEFAULT_LAYOUT = ROOT / "data/home_layout.json"
DEFAULT_HISTORY = ROOT / "data/presence_flow/flow-20261011.jsonl"
DEFAULT_RAW = ROOT / "data/presence_tracks/raw-20261011.jsonl"
OWNERS_FILE = ROOT / "data" / "presence_owners.json"   # устройство -> человек; личное, вне репозитория
PERSON_PREFIX = "person:"


def subjects(owners_file: Path = OWNERS_FILE) -> tuple[str, ...]:
    """Субъекты разметки: устройства и их хозяева (person:<ключ>) из data/presence_owners.json; нет файла - пусто."""
    try:
        owners = json.loads(owners_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    return (*owners, *(PERSON_PREFIX + p for p in dict.fromkeys(owners.values())))


SUBJECTS = subjects()
WARMUP_S = 60.0          # прогон начинается раньше первой оценённой секунды: окну 14 с и началу окна нужно сложиться (как у перемотки)
FRESH_MS = 1000          # кадр моложе - телефон в эту секунду слышен на этом диапазоне


@dataclass(frozen=True)
class Label:
    walk: str
    areas: frozenset
    rooms: frozenset


@dataclass(frozen=True)
class Point:
    second: int
    subject: str
    iface: str          # интерфейс строки; у человека - "person"
    x: float
    z: float


def clock(text: str) -> float:
    return datetime.strptime(f"{DAY} {text}", "%Y-%m-%d %H:%M:%S").timestamp()


def load_labels(path: Path) -> dict[int, Label]:
    out = {}
    for line in path.open(encoding="utf-8"):
        r = json.loads(line)
        if r.get("area_set"):
            out[int(r["t"])] = Label(r["walk"], frozenset(r["area_set"]), frozenset(r["room_set"]))
    return out


def subject_of(text: str) -> str | None:
    low = text.lower()
    return next((s for s in SUBJECTS if s in low), None)


def current_band(raw_path: Path, lo: float, hi: float) -> dict[tuple[str, int], str]:
    """(телефон, секунда) -> интерфейс, на котором телефон в эту секунду слышен свежим кадром (если на обоих - тот, где кадр моложе)."""
    best: dict[tuple[str, int], tuple[int, str]] = {}
    for line in raw_path.open(encoding="utf-8"):
        r = json.loads(line)
        if not lo <= r["t"] <= hi or r.get("in", 0) > FRESH_MS:
            continue
        sub = subject_of(r.get("name") or "")
        if sub is None:
            continue
        key = (sub, int(r["t"]))
        if key not in best or r["in"] < best[key][0]:
            best[key] = (r["in"], r["if"])
    return {k: v[1] for k, v in best.items()}


def history_points(path: Path, lo: float, hi: float) -> Iterator[Point]:
    for line in path.open(encoding="utf-8"):
        r = json.loads(line)
        if not lo <= r["t"] <= hi or r.get("x") is None:
            continue
        sub = subject_of(f"{r['id']} {r.get('name') or ''}")
        if sub:
            yield Point(int(r["t"]), sub, r["id"].rpartition("|")[2], float(r["x"]), float(r["z"]))


def replay_points(raw_path: Path, lo: float, hi: float, warmup_s: float = WARMUP_S) -> Iterator[Point]:
    """Свежий поток как у живого радара, опрос за опросом из сырой записи; точки - раз в секунду."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    os.environ["SAM_PRESENCE_RAW_TRACKS"] = "0"
    from radar import presence_hub as ph, presence_rssi as rs
    flow = ph.build().flow.fresh()
    polls: dict[float, list] = collections.OrderedDict()
    for line in raw_path.open(encoding="utf-8"):
        r = json.loads(line)
        if lo - warmup_s <= r["t"] <= hi:
            polls.setdefault(r["t"], []).append(r)
    last = None
    for t, rows in polls.items():
        flow.observe([rs.Station(r["id"], r["if"], float(r["db"]), int(r["in"]), r.get("name")) for r in rows], t)
        s = int(t)
        if s == last or t < lo:
            continue
        last = s
        for row in flow.rows():
            sub = subject_of(f"{row['id']} {row.get('name') or ''}")
            if sub and row.get("point"):
                yield Point(s, sub, row.get("iface") or "", float(row["point"]["x"]), float(row["point"]["z"]))


def score(points: Iterable[Point], labels: dict[int, Label], band: dict[tuple[str, int], str], classes: dict, rooms: list) -> dict:
    """(проходка, субъект) -> секунд, верная область, верная комната; телефон - только строка текущего диапазона."""
    out: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    for p in points:
        lab = labels.get(p.second)
        if lab is None:
            continue
        if not p.subject.startswith("person:") and band.get((p.subject, p.second)) not in (None, p.iface):
            continue
        area = nd.area_at(classes, rooms, p.x, p.z)
        c = out[(lab.walk, p.subject)]
        c["n"] += 1
        c["area"] += area in lab.areas
        c["room"] += nd.area_room(classes, rooms, area) in lab.rooms
    return out


def report(name: str, result: dict) -> None:
    for (walk, sub), c in sorted(result.items()):
        print(f"{name:8s} {walk} {sub:12s} секунд {c['n']:5d} область {100 * c['area'] / c['n']:5.1f}% комната {100 * c['room'] / c['n']:5.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description="точность потока радара на размеченной проходке")
    ap.add_argument("mode", choices=("history", "replay", "both"))
    ap.add_argument("--from", dest="start", default="21:33:00")
    ap.add_argument("--to", dest="end", default="23:12:00")
    ap.add_argument("--warmup", type=float, default=WARMUP_S)
    ap.add_argument("--ticks", type=Path, default=DEFAULT_TICKS)
    ap.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    ap.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    args = ap.parse_args()
    lo, hi = clock(args.start), clock(args.end)
    labels = load_labels(args.ticks)
    classes = json.loads(DEFAULT_CLASSES.read_text(encoding="utf-8"))
    rooms = json.loads(DEFAULT_LAYOUT.read_text(encoding="utf-8"))["rooms"]
    band = current_band(args.raw, lo, hi)
    if args.mode in ("history", "both"):
        report("история", score(history_points(args.history, lo, hi), labels, band, classes, rooms))
    if args.mode in ("replay", "both"):
        report("прогон", score(replay_points(args.raw, lo, hi, args.warmup), labels, band, classes, rooms))


if __name__ == "__main__":
    main()
