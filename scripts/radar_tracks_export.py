"""Обезличенная выгрузка сырых треков радара (SAM-118): строки уровня устройств за интервал без имён, адресов и времени суток.

Имена устройств заменяются псевдонимами (--name начало_имени=псевдоним), остальные становятся other-1, other-2 по порядку появления; идентификатор станции
(хэш MAC) и дата не выгружаются, время идёт в секундах от начала интервала. Запуск:
python scripts/radar_tracks_export.py data/presence_tracks/tracks-ГГГГММДД.jsonl ГГГГ-ММ-ДД ЧЧ:ММ:СС ЧЧ:ММ:СС out.jsonl --name <начало_имени>=phone-a --name <начало_имени>=phone-b"""
from __future__ import annotations

import argparse
import json
from datetime import datetime

OTHER_PREFIX = "other-"
CLOCK_FORMAT = "%Y-%m-%d %H:%M:%S"


def pseudonym_of(name: str | None, aliases: dict[str, str], others: dict[str, str]) -> str:
    """Псевдоним по началу имени; неизвестное устройство получает номер по порядку появления."""
    low = (name or "").lower()
    for prefix, alias in aliases.items():
        if low.startswith(prefix):
            return alias
    return others.setdefault(low or "?", f"{OTHER_PREFIX}{len(others) + 1}")


def export_rows(lines: list[str], t0: float, t1: float, aliases: dict[str, str]) -> list[dict]:
    others: dict[str, str] = {}
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not t0 <= row.get("t", -1) <= t1:
            continue
        item = {"t": round(row["t"] - t0, 2), "d": pseudonym_of(row.get("name"), aliases, others), "db": row["db"], "if": row.get("if")}
        if row.get("ln"):
            item["ln"] = row["ln"]
        out.append(item)
    return out


def parse_aliases(pairs: list[str]) -> dict[str, str]:
    return {key.lower(): alias for key, alias in (pair.split("=", 1) for pair in pairs)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tracks")
    ap.add_argument("day")
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("out")
    ap.add_argument("--name", action="append", default=[])
    args = ap.parse_args()
    t0 = datetime.strptime(f"{args.day} {args.start}", CLOCK_FORMAT).timestamp()
    t1 = datetime.strptime(f"{args.day} {args.end}", CLOCK_FORMAT).timestamp()
    with open(args.tracks, encoding="utf8") as f:
        rows = export_rows(f.read().splitlines(), t0, t1, parse_aliases(args.name))
    with open(args.out, "w", encoding="utf8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows) + "\n")
    print(f"строк {len(rows)}, устройств {len({r['d'] for r in rows})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
