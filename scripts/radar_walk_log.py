"""Отметка проходки радара (SAM-118): строка «время и слова человека» в файл проходки за день, например «пришёл в ванную», «пошёл».

Запуск: python scripts/radar_walk_log.py "пришёл в ванную" [--at <секунды эпохи>] [--no-radar]
В строку идёт и то, что радар говорит в этот момент: лучшие места каждого телефона и человека из потока (адрес - SAM_RADAR_URL).
Папка - SAM_RADAR_WALK_DIR (по умолчанию data/walks), файл walk-ГГГГ-ММ-ДД.jsonl; разметку стоянок по этим строкам собирает разбор записи."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path

WALK_DIR_ENV = "SAM_RADAR_WALK_DIR"
DEFAULT_WALK_DIR = "data/walks"
FILE_FORMAT = "walk-%Y-%m-%d.jsonl"
RADAR_URL_ENV = "SAM_RADAR_URL"
DEFAULT_RADAR_URL = "http://localhost:8010"
FLOW_PATH = "/sam/presence/flow"
TOP_PLACES = 3                 # мест на субъекта: больше в строке не читается глазами
TIMEOUT_S = 5.0


def radar_view(flow: dict, top: int = TOP_PLACES) -> dict:
    """Сводка потока: имя субъекта -> лучшие места с вероятностью."""
    return {s.get("name") or s.get("id"): [[p["place"], round(p["p"], 2)] for p in (s.get("places") or [])[:top]]
            for s in (flow.get("devices") or []) + (flow.get("people") or [])}


def fetch_flow(url: str) -> dict:
    with urllib.request.urlopen(url + FLOW_PATH, timeout=TIMEOUT_S) as r:
        return json.loads(r.read())


def line(text: str, at: float, radar: dict | None = None) -> str:
    row = {"t": round(at, 2), "time": datetime.fromtimestamp(at).strftime("%H:%M:%S"), "text": text.strip()}
    return json.dumps({**row, "radar": radar} if radar is not None else row, ensure_ascii=False)


def append(folder: Path, text: str, at: float, radar: dict | None = None) -> Path:
    """Дописать отметку в файл дня; папка создаётся при первой записи."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / datetime.fromtimestamp(at).strftime(FILE_FORMAT)
    with path.open("a", encoding="utf-8") as f:
        f.write(line(text, at, radar) + "\n")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="отметка проходки радара")
    parser.add_argument("text", help="слова человека: «пришёл в ванную», «пошёл»")
    parser.add_argument("--at", type=float, default=None, help="время отметки в секундах эпохи; по умолчанию сейчас")
    parser.add_argument("--no-radar", action="store_true", help="не спрашивать радар")
    args = parser.parse_args()
    at = args.at if args.at is not None else time.time()
    radar = None
    if not args.no_radar:
        try:
            radar = radar_view(fetch_flow(os.environ.get(RADAR_URL_ENV) or DEFAULT_RADAR_URL))
        except OSError as e:
            print(f"радар не ответил: {e}; отметка без него")
    path = append(Path(os.environ.get(WALK_DIR_ENV) or DEFAULT_WALK_DIR), args.text, at, radar)
    print(f"{path}: {line(args.text, at, radar)}")


if __name__ == "__main__":
    main()
