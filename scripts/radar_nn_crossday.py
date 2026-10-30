"""Проверка мини-модели на другом дне (SAM-118): учим на проходках одного дня, проверяем на размеченной прогулке другого дня.

Ряды другого дня записаны раз в секунду и только уровнем (без возраста кадра и признаков линка), а носился один телефон. Поэтому модель здесь
узкая: один носимый телефон, по секунде - уровень, был ли свежий отсчёт, диапазон; окно window_s секунд. Те же признаки строятся и по шкале
проходок (каждый пятый тик). Метки другого дня - стоянки таймлайна (scripts/radar_walk_dataset.py), место -> область по таблице классов.

Предобучение без разметки (--pretrain): тот же кодировщик учится предсказывать уровень следующей секунды по окну на всех неразмеченных рядах
носимого телефона (файлы и исключаемые интервалы - в описании проходок, unlabeled), затем дообучается на проходках.

Запуск: python scripts/radar_nn_crossday.py --data data/walks/nn --tag 2026-10-11 --timeline data/radar_walk_timeline.json --phone-a s24 [--pretrain]"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

TICKS_PER_S = 5
FEATURES = 3                     # уровень, свежий отсчёт за секунду, 5 ГГц
SEED = 11
GAP_S = 60                       # разрыв записи длиннее - новый непрерывный кусок
PRETRAIN_EPOCHS = 10


def train_mod(root: Path):
    spec = importlib.util.spec_from_file_location("radar_nn_train", root / "scripts" / "radar_nn_train.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["radar_nn_train"] = mod
    spec.loader.exec_module(mod)
    return mod


def walk_seconds(ticks: list[dict], phone: str, tr) -> tuple[np.ndarray, list, list]:
    """По каждой секунде проходок: признаки телефона, набор верных областей (номера) и группа."""
    feats, sets, groups = [], [], []
    last_db, last_band = tr.DB_MISSING, 0.0
    for i in range(0, len(ticks) - TICKS_PER_S + 1, TICKS_PER_S):
        chunk = ticks[i:i + TICKS_PER_S]
        fresh = [t[phone] for t in chunk if t.get(phone) and t[phone]["fresh"]]
        if fresh:
            last_db, last_band = float(np.median([f["db"] for f in fresh])), 1.0 if fresh[-1]["if"] == tr.BAND_5GHZ else 0.0
        feats.append([(last_db - tr.DB_ZERO) / tr.DB_SCALE, 1.0 if fresh else 0.0, last_band])
        end = chunk[-1]
        sets.append(end.get("area_set") or ([end["area"]] if end["area"] else []))
        groups.append(end["group"])
    return np.array(feats, dtype=np.float32), sets, groups


def day_seconds(rows: list[dict], name_part: str, t0: float, t1: float, tr) -> np.ndarray:
    n = int(t1 - t0)
    out = np.zeros((n, FEATURES), dtype=np.float32)
    by_s: dict[int, list] = {}
    for r in rows:
        if name_part in (r.get("name") or "").lower() and t0 <= r["t"] < t1:
            by_s.setdefault(int(r["t"] - t0), []).append(r)
    last_db, last_band = tr.DB_MISSING, 1.0
    for s in range(n):
        if s in by_s:
            last_db = float(np.median([r["db"] for r in by_s[s]]))
            last_band = 1.0 if by_s[s][-1]["if"] == tr.BAND_5GHZ else 0.0
        out[s] = [(last_db - tr.DB_ZERO) / tr.DB_SCALE, 1.0 if s in by_s else 0.0, last_band]
    return out


def unlabeled_pieces(spec: dict, name_part: str, tr, day_of) -> list[np.ndarray]:
    """Непрерывные куски неразмеченных рядов телефона по секундам; интервалы exclude вырезаются."""
    pieces = []
    excl = [(day_of(e["day"], e["from"]), day_of(e["day"], e["to"])) for e in spec.get("exclude") or []]
    for f in spec["files"]:
        rows = [json.loads(l) for l in Path(f).read_text(encoding="utf-8").splitlines() if l.strip()]
        rows = sorted((r for r in rows if name_part in (r.get("name") or "").lower() and not any(a <= r["t"] < b for a, b in excl)), key=lambda r: r["t"])
        if not rows:
            continue
        start, chunk = rows[0]["t"], []
        for a, b in zip(rows, rows[1:] + [None]):
            chunk.append(a)
            if b is None or b["t"] - a["t"] > GAP_S:
                piece = day_seconds(chunk, name_part, start, a["t"] + 1, tr)
                if len(piece) > 30:
                    pieces.append(piece)
                if b is not None:
                    start, chunk = b["t"], []
    return pieces


def pretrain(pieces: list[np.ndarray], w: int, tr, device: str):
    """Кодировщик свёртки учится предсказывать уровень следующей секунды по окну w секунд."""
    import torch
    xs, ys = [], []
    for p in pieces:
        for e in range(w - 1, len(p) - 1):
            xs.append(p[e - w + 1:e + 1]); ys.append(p[e + 1, 0])
    x = torch.tensor(np.stack(xs), device=device); y = torch.tensor(np.array(ys, dtype=np.float32), device=device)
    torch.manual_seed(SEED)
    m = tr.make_cnn(FEATURES, len(tr.STATES), 1).to(device)
    opt = torch.optim.Adam(m.parameters(), lr=tr.LR)
    for _ in range(PRETRAIN_EPOCHS):
        order = torch.randperm(len(x), device=device)
        for b in range(0, len(x), 1024):
            i = order[b:b + 1024]
            _, pred = m(x[i])
            loss = torch.nn.functional.mse_loss(pred[:, 0], y[i])
            opt.zero_grad(); loss.backward(); opt.step()
    return {k: v for k, v in m.state_dict().items() if k.startswith("body.")}, len(xs)


def main() -> None:
    ap = argparse.ArgumentParser(description="мини-модель: учим на одном дне, проверяем на размеченной прогулке другого дня")
    ap.add_argument("--data", type=Path, default=Path("data/walks/nn"))
    ap.add_argument("--tag", required=True)
    ap.add_argument("--timeline", type=Path, required=True)
    ap.add_argument("--phone-a", default="s24", help="часть имени носимого телефона в треках другого дня (он же phone-a в наборе)")
    ap.add_argument("--windows", type=int, nargs="+", default=[3, 5, 8])
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--pretrain", action="store_true", help="сравнить с предобучением кодировщика на неразмеченных рядах")
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    tr = train_mod(root)
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    ticks, summary, classes = tr.load(args.data, args.tag)
    areas = sorted({a for t in ticks if t["area"] is not None for a in (t.get("area_set") or [t["area"]])})
    a_id = {a: i for i, a in enumerate(areas)}
    cr = classes.get("class_room") or {}
    room_of = lambda a: cr.get(a, a)
    timeline = json.loads(args.timeline.read_text(encoding="utf-8"))
    day = timeline["date"]
    ts = lambda c: datetime.strptime(f"{day} {c}", "%Y-%m-%d %H:%M:%S").timestamp()
    rows = [json.loads(l) for l in Path(timeline["tracks_file"]).read_text(encoding="utf-8").splitlines() if l.strip()]
    t0, t1 = ts(timeline["from"]), ts(timeline["to"])
    test_x = day_seconds(rows, args.phone_a.lower(), t0, t1, tr)
    # места разметки -> области: таблица классов из data/ (личная разметка квартиры), место без строки - само себе область
    place_area = classes.get("place_class") or {}
    test_lab = np.full(len(test_x), -1)
    for st in timeline["stands"]:
        a = place_area.get(st["place"], st["place"])
        if a in a_id:
            test_lab[int(ts(st["from"]) - t0):int(ts(st["to"]) - t0)] = a_id[a]
    walks = sorted({t["walk"] for t in ticks})
    tracks = [walk_seconds([t for t in ticks if t["walk"] == w], "phone-a", tr) for w in walks]
    out = {"day": day, "test_seconds": int((test_lab >= 0).sum()), "runs": []}
    pieces = []
    if args.pretrain:
        spec = json.loads((args.data / f"walks-{args.tag}.json").read_text(encoding="utf-8"))["unlabeled"]
        day_of = lambda d, c: datetime.strptime(f"{d} {c}", "%Y-%m-%d %H:%M:%S").timestamp()
        pieces = unlabeled_pieces(spec, args.phone_a.lower(), tr, day_of)
        out["unlabeled_seconds"] = int(sum(len(p) for p in pieces))
        print(f"неразмеченных секунд: {out['unlabeled_seconds']} в {len(pieces)} кусках", flush=True)
    for w in args.windows:
        xs, ys = [], []
        for feats, sets, _ in tracks:
            for e in range(w - 1, len(feats)):
                if sets[e]:
                    xs.append(feats[e - w + 1:e + 1]); ys.append([a_id[a] for a in sets[e]])
        xw = np.stack(xs)
        soft = tr.soft_targets(ys, len(areas))
        ends = [e for e in range(w - 1, len(test_x)) if test_lab[e] >= 0]
        truth = test_lab[ends]
        major = int(soft.sum(0).argmax())
        variants = [("с нуля", None)]
        if args.pretrain:
            body, n_pre = pretrain(pieces, w, tr, device)
            variants.append((f"предобучение ({n_pre} окон)", body))
        for name, body in variants:
            m = tr.train_cnn(xw, -np.ones(len(xw), dtype=int), soft, len(tr.STATES), len(areas), args.epochs, device, SEED, init=body)
            _, pa = tr.predict_cnn(m, np.stack([test_x[e - w + 1:e + 1] for e in ends]), device)
            r = {"window_s": w, "variant": name, "area_acc": round(float((pa == truth).mean()), 3),
                 "room_acc": round(float(np.mean([room_of(areas[p]) == room_of(areas[t]) for p, t in zip(pa, truth)])), 3),
                 "majority_area": areas[major], "majority_area_acc": round(float((truth == major).mean()), 3),
                 "majority_room_acc": round(float(np.mean([room_of(areas[major]) == room_of(areas[t]) for t in truth])), 3)}
            out["runs"].append(r)
            print(f"окно {w} с, {name}: область {r['area_acc']} комната {r['room_acc']} (частый класс {r['majority_area']}: {r['majority_area_acc']}/{r['majority_room_acc']})", flush=True)
    (args.data / f"crossday-{day}-{args.tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
