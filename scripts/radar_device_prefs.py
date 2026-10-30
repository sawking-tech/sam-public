"""Карта предпочтений устройств из плана и привычек людей (SAM-118): вес места = поверхность × где люди бывают.

Телефон лежит там, где есть поверхность (стол, кровать, диван, полка, стиралка, кошачья этажерка), и там, где любят бывать люди (кладут мимо шли и забывают).
Поверхности берутся из мебели плана, время людей - из привычек (`data/home_habits.json`) за неделю, места - положения отпечатков (`data/radio_controls.json`).
Пишет `data/device_prefs.json`; личные таблицы устройств (ключ с частью имени) перекрывают общую и правятся руками, их скрипт сохраняет.

Запуск: python scripts/radar_device_prefs.py [habits.json] [controls.json] [layout.json] [out.json]"""
from __future__ import annotations

import json
import sys
from radar import presence_prefs as pp

DEFAULT_HABITS = "data/home_habits.json"
DEFAULT_CONTROLS = "data/radio_controls.json"
DEFAULT_LAYOUT = "data/home_layout.json"
DEFAULT_OUT = "data/device_prefs.json"


def load(path: str) -> dict:
    with open(path, encoding="utf8") as f:
        return json.load(f)


def build(habits: dict, controls: dict, layout: dict, previous: dict) -> dict:
    places = {pp.place_key(name): (xy["x"], xy["z"]) for name, xy in controls.items()}
    outlets = previous.get(pp.PrefsKey.OUTLETS) or {}
    weights = pp.derive(places, layout.get("objects") or [], pp.weekly_occupancy(habits), outlets)
    fixed = previous.get(pp.PrefsKey.FIXED) or {}                                                         # веса, заданные руками: скрипт их не пересчитывает
    weights.update(fixed)
    personal = {k: v for k, v in previous.items() if k not in pp.SERVICE_KEYS}  # личные таблицы устройств остаются как есть
    return {pp.PrefsKey.FIXED: fixed, pp.PrefsKey.OUTLETS: outlets, pp.PrefsKey.NOTE: "Вес места устройства = поверхность (мебель плана рядом) × время людей по привычкам за неделю; считает scripts/radar_device_prefs.py, личные таблицы устройств правятся руками",
            pp.PrefsKey.DEFAULT: weights, **personal}


def main(argv: list[str]) -> int:
    habits_path, controls_path, layout_path, out_path = (argv + [DEFAULT_HABITS, DEFAULT_CONTROLS, DEFAULT_LAYOUT, DEFAULT_OUT][len(argv):])[:4]
    try:
        previous = load(out_path)
    except OSError:
        previous = {}
    result = build(load(habits_path), load(controls_path), load(layout_path), previous)
    with open(out_path, "w", encoding="utf8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    for place, weight in sorted(result[pp.PrefsKey.DEFAULT].items(), key=lambda kv: -kv[1]):
        print(f"{weight:5.2f}  {place}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
