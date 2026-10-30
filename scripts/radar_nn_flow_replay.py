"""Прогон потока положения по сырой записи проходки: без сети и с сетью разного веса (SAM-118).

Опросы роутера из сырой записи (raw-ГГГГММДД.jsonl, около 5 Гц, с признаками линка) идут в поток так же, как в живом радаре. Сеть берётся из выгрузки
без этой проходки (heldout-<проходка>/ от scripts/radar_nn_gru.py --export --heldout), чтобы проверка была честной. Раз в секунду точка потока каждого
телефона переводится в область (как в разметке) и сравнивается с метками шкалы набора: доля верной области и комнаты по наборам.

Запуск: python scripts/radar_nn_flow_replay.py --data data/walks/nn --tag 2026-10-11 --raw data/presence_tracks/raw-20261011.jsonl [--walks w1 w2 w3] [--weights 0.1 0.2 0.4]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from radar import presence_hub as ph  # noqa: E402
from radar import presence_link as plk  # noqa: E402
from radar import presence_nn as pnn  # noqa: E402
from radar import presence_rssi as rs  # noqa: E402
from radar.presence_random import Randomness  # noqa: E402

WARMUP_S = 30.0                 # поток разгоняется на записи перед проходкой: окно в 14 с и старт
SEED = 7
PHONES = {"S24": "phone-a", "Redmi": "phone-b"}


def station(row: dict) -> rs.Station:
    """Строка сырой записи -> опрос станции с признаками линка, как из разбора роутера."""
    ln = row.get("ln") or {}
    rate = lambda v: plk.LinkRate(*(v + [None, None, None])[:3]) if v else plk.LinkRate()
    ack = ln.get("ack") or [None, None]
    link = plk.LinkFeatures(avg_db=ln.get("avg"), avg_chains=tuple(ln.get("avgch") or ()), ack_last_db=ack[0], ack_avg_db=ack[1],
                            tx=rate(ln.get("tx")), rx=rate(ln.get("rx"))) if ln else None
    return rs.Station(row["id"], row["if"], row["db"], int(row.get("in", 0)), row.get("name"), link)


def build_flow(env_extra: dict, layout: dict):
    import os
    env = {**os.environ, "SAM_PRESENCE_FLOW": "1", **env_extra}
    prefs = ph._read_json(Path(env.get(ph.PREFS_FILE_ENV) or ph.DEFAULT_PREFS_FILE), {}, "предпочтения устройств")
    owners = ph._read_json(Path(env.get(ph.OWNERS_FILE_ENV) or ph.DEFAULT_OWNERS_FILE), {}, "хозяева устройств")
    habits = ph._read_json(Path(env.get(ph.HABITS_FILE_ENV) or ph.DEFAULT_HABITS_FILE), {}, "привычки людей")
    return ph._build_flow(env, layout, prefs, habits, Randomness(SEED), owners)


def run(flow, polls: list[list[dict]], labels: dict, classes: dict, rooms: list, start: float) -> dict:
    hits = {p: [0, 0, 0] for p in PHONES.values()}             # окна, верная область, верная комната
    last_second = None
    room_of = lambda a: (classes.get("class_room") or {}).get(a, a)
    for rows in polls:
        now = rows[0]["t"]
        flow.observe([station(r) for r in rows], now)
        second = int(now)
        if now < start or second == last_second:
            continue
        last_second = second
        lab = labels.get(second)
        if not lab:
            continue
        for part, phone in PHONES.items():
            name = next((r["name"] for r in rows if part in (r.get("name") or "")), None)
            point = flow.point_of(name) if name else None
            truth = lab.get(phone)
            if point is None or truth is None:
                continue
            area = pnn.area_of_point(classes, rooms, point["x"], point["z"])
            h = hits[phone]
            h[0] += 1; h[1] += area in truth[0]; h[2] += room_of(area) in truth[1]
    return {p: {"seconds": h[0], "area_acc": round(h[1] / h[0], 3) if h[0] else None, "room_acc": round(h[2] / h[0], 3) if h[0] else None} for p, h in hits.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description="поток без сети и с сетью на сырой записи проходки")
    ap.add_argument("--data", type=Path, default=Path("data/walks/nn"))
    ap.add_argument("--tag", required=True)
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--layout", type=Path, default=Path("data/home_layout.json"))
    ap.add_argument("--walks", nargs="+", default=["w1", "w2", "w3"])
    ap.add_argument("--weights", type=float, nargs="+", default=[0.1, 0.2, 0.4])
    args = ap.parse_args()
    layout = json.loads(args.layout.read_text(encoding="utf-8"))
    rooms = layout.get("rooms") or []
    classes = json.loads((args.data / f"classes-{args.tag}.json").read_text(encoding="utf-8"))
    ticks = [json.loads(l) for l in (args.data / f"ticks-{args.tag}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    raw = [json.loads(l) for l in args.raw.read_text(encoding="utf-8").splitlines() if l.strip()]
    out = []
    for walk in args.walks:
        wt = [t for t in ticks if t["walk"] == walk]
        start, end = wt[0]["t"], wt[-1]["t"]
        labels: dict[int, dict] = {}
        for t in wt:
            if t["area"] is None:
                continue
            labels.setdefault(int(t["t"]), {})
            for phone in PHONES.values():
                if t.get(phone) and t[phone]["fresh"]:
                    labels[int(t["t"])][phone] = (set(t["area_set"]), set(t["room_set"]))
        rows = [r for r in raw if start - WARMUP_S <= r["t"] <= end]
        polls: dict[float, list] = {}
        for r in rows:
            polls.setdefault(r["t"], []).append(r)
        ordered = [polls[k] for k in sorted(polls)]
        variants = [("поток", {})] + [(f"поток+сеть {w}", {pnn.NN_ENV: "1", pnn.NN_DIR_ENV: str(args.data / f"heldout-{walk}"), pnn.NN_WEIGHT_ENV: str(w)}) for w in args.weights]
        for name, env in variants:
            res = run(build_flow(env, layout), ordered, labels, classes, rooms, start)
            out.append({"walk": walk, "variant": name, **res})
            print(f"{walk} {name}: " + "; ".join(f"{p} область {v['area_acc']} комната {v['room_acc']} ({v['seconds']} с)" for p, v in res.items()), flush=True)
    (args.data / f"flow-replay-{args.tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
