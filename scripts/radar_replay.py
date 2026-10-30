"""Проигрыш записи треков через живой радар (SAM-118): уровни устройства идут в книгу устройств с поддельными часами, на каждой секунде берётся выбранное выборкой место
и сравнивается с правдой по таймлайну проходок: стоянки со временем и местом.

Показывает точность по секундам стоянки и задержку после смены места (сколько секунд до первого верного выбора, который держится). Случайность выборки усредняется по seed.

Запуск: python scripts/radar_replay.py [tracks.jsonl] [timeline.json] [fingerprints.json] [layout.json]"""
from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime

from dotenv import dotenv_values

from radar import presence_hub as ph
from radar import presence_rssi as rs

DEFAULT_TRACKS = "data/presence_tracks/tracks-20261010.jsonl"
DEFAULT_TIMELINE = "data/radar_walk_timeline.json"
DEFAULT_FINGERPRINTS = "data/presence_fingerprints.json"
DEFAULT_LAYOUT = "data/home_layout.json"
DEFAULT_CONTROLS = "data/radio_controls.json"
DEFAULT_PREFS = "data/device_prefs.json"          # карта предпочтений устройств; нет файла - проигрыш без неё
DEVICE_ENV = "SAM_RADAR_REPLAY_DEVICE"   # часть имени устройства, которое ходило: в окружении или .env, не в коде
IFACE = os.environ.get("SAM_RADAR_REPLAY_IFACE", "phy1-ap0")   # диапазон записи (по умолчанию 5 ГГц)
DEVICE_PART = (os.environ.get(DEVICE_ENV) or dotenv_values(".env").get(DEVICE_ENV) or "").lower()
SEEDS = 20                         # сколько раз повторить с разной случайностью выборки
STEP_S = 0.5                       # шаг проигрыша: как опрос роутера
HOLD_S = 3.0                       # верный выбор считается установленным, если держится столько секунд
EDGE_S = 3                         # секунд по краям стоянки не считаются: отметка человека запаздывает
DEFAULT_ALIASES = "data/radar_place_aliases.json"     # имя места в таймлайне -> имя в отпечатках, где они различаются; личные названия лежат в data/, не в коде
INACTIVE_MS = 100


def load(path: str):
    with open(path, encoding="utf8") as f:
        return json.load(f)


def place_alias(name: str) -> str:
    """Имя места в отпечатках по имени в таймлайне; нет в таблице - то же имя строчными."""
    low = name.lower()
    return load_optional(DEFAULT_ALIASES).get(low, low)


def load_optional(path: str) -> dict:
    """Файл настроек или пустой словарь, если его нет."""
    try:
        return load(path)
    except OSError:
        return {}


def clock_to_ts(date: str, clock: str) -> float:
    return datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M:%S").timestamp()


def read_levels(path: str, device_part: str, iface: str, t0: float, t1: float) -> dict[int, tuple[str, float, str]]:
    """Уровень устройства по секундам интервала: секунда -> (sid, медиана уровня, имя)."""
    per_second: dict[int, list[float]] = {}
    ident: dict[str, str] = {}
    with open(path, encoding="utf8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            name = row.get("name") or ""
            if device_part in name.lower() and row.get("if") == iface and t0 <= row["t"] <= t1:
                per_second.setdefault(int(row["t"]), []).append(row["db"])
                ident[row["id"]] = name
    sid, name = next(iter(ident.items()), (None, None))
    return {sec: (sid, sorted(v)[len(v) // 2], name) for sec, v in per_second.items()}


def truth_by_second(timeline: dict) -> dict[int, str]:
    """Место стоянки по секундам (края отрезаны): секунда -> имя места в отпечатках."""
    out = {}
    for stand in timeline["stands"]:
        place = place_alias(stand["place"])
        a, b = clock_to_ts(timeline["date"], stand["from"]), clock_to_ts(timeline["date"], stand["to"])
        for sec in range(int(a) + EDGE_S, int(b) - EDGE_S + 1):
            out[sec] = place
    return out


def replay(levels, layout: dict, fingerprints_file: str, seed: int, t0: float, t1: float, track_window_s: float | None = None) -> dict[int, tuple[str | None, list[tuple[str, float]]]]:
    """Секунда -> (выбранное место, лучшие места по оценке): устройство проигрывается через книгу устройств и хаб с поддельными часами."""
    now = [t0]
    hub = ph.PresenceHub(ph.MODE_RSSI, layout, clock=lambda: now[0], fingerprints_file=__import__("pathlib").Path(fingerprints_file), rng=random.Random(seed), track_window_s=track_window_s, device_prefs=load_optional(DEFAULT_PREFS), controls=load_optional(DEFAULT_CONTROLS))
    out: dict[int, tuple[str | None, list[str]]] = {}
    t = t0
    while t <= t1:
        now[0] = t
        sec = int(t)
        if sec in levels:
            sid, level, name = levels[sec]
            hub.devices.update([rs.Station(sid, IFACE, level, INACTIVE_MS, name)], t)
        rows = [r for r in hub._devices_view(t) if r["present"] and DEVICE_PART in (r["name"] or "").lower() and r["iface"] == IFACE]
        if rows:
            at = rows[0].get(ph.DEVICE_AT)
            out[sec] = (at["place"] if at else None, [(w["place"], w["p"]) for w in rows[0][ph.DEVICE_WHERE]])
        t += STEP_S
    return out


def score(series, truth: dict[int, str]) -> dict[str, float]:
    """Метрики по секундам стоянок: попадание выбора выборкой, вершины, тройки и средняя вероятность на верном месте (честность оценки).

    Средняя вероятность на верном месте равна ожидаемой доле верных выборок: если она сильно ниже доли верных вершин, оценка размазана и выборка метку часто ставит мимо."""
    secs = [s for s in sorted(truth) if s in series]
    def p_true(s):
        return next((p for place, p in series[s][1] if place == truth[s]), 0.0)
    return {"sample": sum(series[s][0] == truth[s] for s in secs) / len(secs),
            "top1": sum(bool(series[s][1]) and series[s][1][0][0] == truth[s] for s in secs) / len(secs),
            "top3": sum(any(place == truth[s] for place, _ in series[s][1][:3]) for s in secs) / len(secs),
            "p_true": sum(p_true(s) for s in secs) / len(secs)}


def settle_lags(timeline: dict, series) -> list[float | None]:
    """Задержка после смены места: секунд от начала стоянки до первого верного выбора вершины, который держится HOLD_S; None - не установился."""
    lags = []
    for stand in timeline["stands"]:
        place = place_alias(stand["place"])
        a, b = int(clock_to_ts(timeline["date"], stand["from"])), int(clock_to_ts(timeline["date"], stand["to"]))
        found = None
        for sec in range(a, b - int(HOLD_S)):
            window = [series.get(s) for s in range(sec, sec + int(HOLD_S) + 1)]
            if all(w and w[1] and w[1][0][0] == place for w in window):
                found = float(sec - a)
                break
        lags.append(found)
    return lags


def calibration(series, truth: dict[int, str], edges=(0.0, 0.4, 0.6, 0.8, 1.01)) -> list[tuple[str, int, float]]:
    """По верхней вероятности оценки: сколько секунд и как часто вершина верна; у честной оценки доля близка к самой вероятности."""
    out = []
    for lo, hi in zip(edges, edges[1:]):
        picks = [(series[s][1][0][0] == truth[s]) for s in sorted(truth) if s in series and series[s][1] and lo <= series[s][1][0][1] < hi]
        out.append((f"{lo:.1f}-{min(hi, 1.0):.1f}", len(picks), sum(picks) / len(picks) if picks else float("nan")))
    return out


def main(argv: list[str]) -> int:
    if not DEVICE_PART:
        print(f"Не задано устройство прогона: добавь {DEVICE_ENV}=часть имени в .env или окружение")
        return 2
    tracks, timeline_path, fingerprints, layout_path = (argv + [DEFAULT_TRACKS, DEFAULT_TIMELINE, DEFAULT_FINGERPRINTS, DEFAULT_LAYOUT][len(argv):])[:4]
    timeline, layout = load(timeline_path), load(layout_path)
    t0, t1 = clock_to_ts(timeline["date"], timeline["from"]), clock_to_ts(timeline["date"], timeline["to"])
    levels = read_levels(tracks, DEVICE_PART, IFACE, t0 - 120, t1)
    truth = truth_by_second(timeline)
    print(f"{len(levels)} секунд уровня, {len(truth)} секунд стоянок, {SEEDS} повторов")
    totals: dict[str, list[float]] = {}
    last = None
    for seed in range(SEEDS):
        last = replay(levels, layout, fingerprints, seed, t0 - 120, t1)
        for key, value in score(last, truth).items():
            totals.setdefault(key, []).append(value)
    for key, label in (("sample", "выбор выборкой"), ("top1", "вершина оценки"), ("top3", "истина в тройке"), ("p_true", "средняя вероятность на верном месте")):
        values = totals[key]
        mean = sum(values) / len(values)
        spread = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5
        print(f"{label:38s} {100 * mean:5.1f}% (разброс между повторами {100 * spread:.1f})")
    print("калибровка вершины (верхняя вероятность: секунд, доля верных):")
    for name, n, share in calibration(last, truth):
        print(f"  {name}: {n:4d} с, {100 * share:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
