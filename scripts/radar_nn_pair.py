"""Проверка гипотезы «модель учит пару телефон+диапазон, а не дом» (SAM-118).

Одна пара (телефон на одном диапазоне) в двух проходках: учим на одной, проверяем на другой; затем перенос между разными парами.
Признаки - только этого телефона по шкале тиков (уровень, свежесть, возраст, антенны, подтверждения, скорости, диапазон), берутся тики, где
телефон на заданном диапазоне. Вариант «минус медиана» вычитает из уровня медиану пары по проходке (сдвиг пары). Метрика - область и комната
по наборам против самого частого класса обучения на той же проверке.

Запуск: python scripts/radar_nn_pair.py --data data/walks/nn --tag <дата>"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

SEED = 11
BAND_COL = 7                   # столбец признака «на 5 ГГц» в признаках телефона (radar_nn_train.tick_features)
DB_COL = 0
EXPERIMENTS = [                # (обучение: телефон, проходка), (проверка: телефон, проходка) - все на 5 ГГц
    (("phone-a", "w3"), ("phone-a", "w1")), (("phone-a", "w1"), ("phone-a", "w3")),
    (("phone-b", "w3"), ("phone-b", "w2")), (("phone-b", "w2"), ("phone-b", "w3")),
    (("phone-a", "w1"), ("phone-b", "w2")), (("phone-b", "w2"), ("phone-a", "w1")),
]


def train_mod(root: Path):
    spec = importlib.util.spec_from_file_location("radar_nn_train", root / "scripts" / "radar_nn_train.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["radar_nn_train"] = mod
    spec.loader.exec_module(mod)
    return mod


def pair_windows(track, phone_index: int, length: int, per_phone: int, centre: bool):
    """Окна признаков одного телефона, кончающиеся на тике с меткой, где телефон на 5 ГГц всё окно; centre - уровень минус медиана пары по проходке."""
    x = track.x[:, phone_index * per_phone:(phone_index + 1) * per_phone].copy()
    on = x[:, BAND_COL] > 0.5
    if centre and on.any():
        x[:, DB_COL] -= np.median(x[on, DB_COL])
    ends = [e for e in range(length - 1, len(x)) if track.area[e] >= 0 and on[e - length + 1:e + 1].all()]
    return (np.stack([x[e - length + 1:e + 1] for e in ends]) if ends else np.zeros((0, length, per_phone), dtype=np.float32)), ends


def main() -> None:
    ap = argparse.ArgumentParser(description="перенос модели внутри одной пары телефон+диапазон и между парами")
    ap.add_argument("--data", type=Path, default=Path("data/walks/nn"))
    ap.add_argument("--tag", required=True)
    ap.add_argument("--windows", type=float, nargs="+", default=[3, 5])
    ap.add_argument("--epochs", type=int, default=30)
    args = ap.parse_args()
    tr = train_mod(Path(__file__).resolve().parents[1])
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    ticks, summary, classes = tr.load(args.data, args.tag)
    areas = sorted({a for t in ticks if t["area"] is not None for a in (t.get("area_set") or [t["area"]])})
    rooms = sorted({r for t in ticks if t["room"] is not None for r in (t.get("room_set") or [t["room"]])})
    cr = classes.get("class_room") or {}
    area_room = np.array([rooms.index(cr.get(a, a)) if cr.get(a, a) in rooms else 0 for a in areas])
    tracks = {w: tr.real_track([t for t in ticks if t["walk"] == w], areas, rooms, 5.0) for w in sorted({t["walk"] for t in ticks})}
    idx = {"phone-a": 0, "phone-b": 1}
    out = []
    for w in args.windows:
        length = int(round(w / tr.TICK_S))
        for (p_tr, w_tr), (p_te, w_te) in EXPERIMENTS:
            for centre in (False, True):
                xw, e_tr = pair_windows(tracks[w_tr], idx[p_tr], length, tr.FEATURES_PER_PHONE, centre)
                xt, e_te = pair_windows(tracks[w_te], idx[p_te], length, tr.FEATURES_PER_PHONE, centre)
                if not len(xw) or not len(xt):
                    continue
                soft = tr.soft_targets([tracks[w_tr].area_sets[e] for e in e_tr], len(areas))
                m = tr.train_cnn(xw, -np.ones(len(xw), dtype=int), soft, len(tr.STATES), len(areas), args.epochs, device, SEED)
                _, pa = tr.predict_cnn(m, xt, device)
                test = tracks[w_te]
                major = int(soft.sum(0).argmax())
                r = {"window_s": w, "train": f"{p_tr}/{w_tr}", "test": f"{p_te}/{w_te}", "same_pair": p_tr == p_te, "minus_median": centre,
                     "train_windows": len(xw), "test_windows": len(xt),
                     "area_acc": round(float(np.mean([int(a) in test.area_sets[e] for a, e in zip(pa, e_te)])), 3),
                     "room_acc": round(float(np.mean([int(area_room[a]) in test.room_sets[e] for a, e in zip(pa, e_te)])), 3),
                     "majority_area_acc": round(float(np.mean([major in test.area_sets[e] for e in e_te])), 3),
                     "majority_room_acc": round(float(np.mean([int(area_room[major]) in test.room_sets[e] for e in e_te])), 3)}
                out.append(r)
                print(f"{w} с {r['train']} -> {r['test']} {'минус медиана' if centre else 'как есть'}: область {r['area_acc']} комната {r['room_acc']} "
                      f"(частый {r['majority_area_acc']}/{r['majority_room_acc']}), окон {len(xw)}/{len(xt)}", flush=True)
    (args.data / f"pair-{args.tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
