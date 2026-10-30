"""Временные слоты проходок набора (SAM-118): для каждой стоянки окна недели, где место правдоподобно по привычкам, и лучшие старты цепочек.

Привычки лежат в data/ вне git, поэтому результат тоже в data/. Логика - radar/presence_walk_slots.py, здесь только файлы.

Размечается тот, кто нёс устройство прогона (`--carrier`): другие люди дома в проходке не участвуют, их привычки к ней не приложимы. Без `--carrier` размечаются все из привычек.

Запуск: python scripts/radar_walk_slots.py [--carrier ИМЯ] [walks.json] [home_habits.json] [out.json]"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from radar import presence_walk_slots as ws

DEFAULT_WALKS = "tests/data/radar_walks/walks-5ghz-slow.json"
DEFAULT_HABITS = "data/home_habits.json"
DEFAULT_OUT = "data/radar_walk_slots.json"


def load(path: str) -> dict:
    with open(path, encoding="utf8") as f:
        return json.load(f)


def label_file(walks_path: str, habits_path: str, carrier: str | None = None) -> dict:
    """Слоты проходки для носителя устройства; носителя нет - для всех людей из привычек."""
    walks, habits = load(walks_path), load(habits_path)
    matcher = ws.HabitPlaceMatcher({k: tuple(v) for k, v in habits["place_xy"].items()}, [ws.Room(**r) for r in walks["rooms"].values()])
    places_xy = {k: (v["x"], v["z"]) for k, v in walks["places"].items()}
    persons = [carrier] if carrier else list(habits["people"])
    return ws.label_walk(habits, walks["stands"], matcher, places_xy, persons, walks["legs"])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--carrier", help="кто нёс устройство прогона; по умолчанию все из привычек")
    parser.add_argument("files", nargs="*", help="walks.json, home_habits.json, out.json")
    args = parser.parse_args(argv)
    walks_path, habits_path, out_path = (args.files + [DEFAULT_WALKS, DEFAULT_HABITS, DEFAULT_OUT][len(args.files):])[:3]
    labelled = label_file(walks_path, habits_path, args.carrier)
    with open(out_path, "w", encoding="utf8") as f:
        json.dump(labelled, f, ensure_ascii=False, indent=1, default=dataclasses.asdict)
    print(f"{out_path}: {len(labelled['matched'])} мест, людей {len(labelled['persons'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
