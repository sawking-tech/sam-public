"""Основная мини-модель радара - маленький GRU по окну фиксированной длины со сдвигом в один отсчёт (SAM-118).

Вход - признаки одного телефона на одном диапазоне по шкале 5 Гц (scripts/radar_nn_dataset.py), окно window_s секунд, сдвиг TICK_S.
Выход - вероятности областей; вероятность комнаты - сумма её областей (иерархия в таблице классов). Ответ - самый глубокий уровень, где
уверенность не ниже порога из данных: «комната / область», иначе «комната», иначе «не знаю».
Квартира одна и неизменна: модель вправе запомнить её наизусть, регуляризации ради других домов нет. Запоминать нельзя переменное - пару
телефон+диапазон, руку, двери, вечер: это ловит проверка на отложенной проходке.
Дедупликация (только обучение): окно весит 1 / число окон с той же меткой и той же грубой формой (средний уровень с шагом DEDUP_DB_STEP,
разброс и разница концов с шагом DEDUP_SHAPE_STEP): долгие ровные стоянки и многократные одинаковые проходы не перевешивают остальное.
Метрики: доля верных окон и доля верно узнанных отрезков (стоянка или зона: большинство ответов её окон), по уровням область и комната,
доля ответов на каждом уровне иерархии и их точность; время вывода окна на numpy (без torch) на CPU.

Выгрузка для радара (--export): сеть на каждый диапазон по всем проходкам, где кто-то из телефонов был на нём (окна обоих телефонов),
веса gru-<интерфейс>.npz и описание gru-<интерфейс>.json в --out (по умолчанию папка набора, её читает radar/presence_nn.py);
--heldout w1 w2 ... дополнительно выгружает сети без каждой из этих проходок в heldout-<проходка>/ - для честного прогона потока.

Запуск: python scripts/radar_nn_gru.py --data data/walks/nn --tag 2026-10-11 [--windows 3 5] [--hidden 32] [--epochs 30]
        python scripts/radar_nn_gru.py --data data/walks/nn --tag 2026-10-11 --export [--heldout w1 w2 w3]"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

SEED = 11
BAND_COL = 7                     # столбец «на 5 ГГц» в признаках телефона (radar_nn_train.tick_features)
BATCH = 512
TIMING_CALLS = 2000
EXPERIMENTS = [                  # (обучение: телефон, проходка), (проверка: телефон, проходка) - все на 5 ГГц
    (("phone-a", "w3"), ("phone-a", "w1")), (("phone-a", "w1"), ("phone-a", "w3")),
    (("phone-b", "w3"), ("phone-b", "w2")), (("phone-b", "w2"), ("phone-b", "w3")),
    (("phone-a", "w1"), ("phone-b", "w2")), (("phone-b", "w2"), ("phone-a", "w1")),
]
PHONE_INDEX = {"phone-a": 0, "phone-b": 1}
DEDUP_DB_STEP = 0.3              # шаг среднего уровня окна в нормированных единицах: 3 дБ, около шума пары
DEDUP_SHAPE_STEP = 0.2           # шаг разброса и разницы концов окна: 2 дБ


def load_module(root: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, root / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_gru(n_features: int, hidden: int, n_areas: int):
    import torch
    from torch import nn

    class TinyGru(nn.Module):
        """Один слой GRU по окну, последнее скрытое состояние - линейная голова областей."""
        def __init__(self):
            super().__init__()
            self.gru = nn.GRU(n_features, hidden, batch_first=True)
            self.area = nn.Linear(hidden, n_areas)

        def forward(self, x):
            _, h = self.gru(x)
            return self.area(h[-1])
    return TinyGru()


def dedup_weights(xw: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Вес окна обратно числу похожих: та же метка, тот же средний уровень, разброс и разница концов в грубых шагах; среднее весов - 1."""
    db = xw[:, :, 0]
    keys = list(zip(labels.tolist(), np.round(db.mean(1) / DEDUP_DB_STEP).astype(int).tolist(), np.round(db.std(1) / DEDUP_SHAPE_STEP).astype(int).tolist(),
                    np.round((db[:, -1] - db[:, 0]) / DEDUP_SHAPE_STEP).astype(int).tolist()))
    counts = Counter(keys)
    w = np.array([1.0 / counts[k] for k in keys], dtype=np.float32)
    return w / w.mean()


def train_gru(xw, soft, n_areas, hidden, epochs, device, class_w, sample_w=None):
    import torch
    torch.manual_seed(SEED)
    m = make_gru(xw.shape[2], hidden, n_areas).to(device)
    opt = torch.optim.Adam(m.parameters(), lr=3e-3)
    x, a, w = torch.tensor(xw, device=device), torch.tensor(soft, device=device), torch.tensor(class_w, device=device)
    sw = torch.tensor(sample_w if sample_w is not None else np.ones(len(xw), dtype=np.float32), device=device)
    gen = torch.Generator(device="cpu").manual_seed(SEED)
    for _ in range(epochs):
        order = torch.randperm(len(x), generator=gen).to(device)
        for b in range(0, len(x), BATCH):
            i = order[b:b + BATCH]
            loss = (-(a[i] * torch.log_softmax(m(x[i]), 1) * w).sum(1) * sw[i]).mean()
            opt.zero_grad(); loss.backward(); opt.step()
    return m.eval()


def predict_proba(m, xw, device) -> np.ndarray:
    import torch
    with torch.no_grad():
        return torch.softmax(m(torch.tensor(xw, device=device)), 1).cpu().numpy()


def gru_numpy_weights(m) -> dict:
    sd = {k: v.detach().cpu().numpy() for k, v in m.state_dict().items()}
    return {"w_ih": sd["gru.weight_ih_l0"], "w_hh": sd["gru.weight_hh_l0"], "b_ih": sd["gru.bias_ih_l0"], "b_hh": sd["gru.bias_hh_l0"],
            "a_w": sd["area.weight"], "a_b": sd["area.bias"]}


def gru_numpy(w: dict, x: np.ndarray) -> np.ndarray:
    """Вывод окна x [T, F] на numpy: логиты областей. Формулы torch.nn.GRU (порядок ворот r, z, n)."""
    h = np.zeros(w["w_hh"].shape[1], dtype=np.float32)
    sig = lambda v: 1.0 / (1.0 + np.exp(-v))
    for xt in x:
        gi, gh = w["w_ih"] @ xt + w["b_ih"], w["w_hh"] @ h + w["b_hh"]
        ir, iz, inn = np.split(gi, 3); hr, hz, hn = np.split(gh, 3)
        r, z = sig(ir + hr), sig(iz + hz)
        n = np.tanh(inn + r * hn)
        h = (1 - z) * n + z * h
    return w["a_w"] @ h + w["a_b"]


def band_windows(tracks: dict, walks, band_5ghz: bool, length: int, per_phone: int):
    """Окна обоих телефонов из проходок walks, где телефон всё окно на заданном диапазоне; цели - мягкие по наборам областей."""
    xs, sets = [], []
    for w in walks:
        tk = tracks[w]
        for phone, k in PHONE_INDEX.items():
            x = tk.x[:, k * per_phone:(k + 1) * per_phone]
            on = (x[:, BAND_COL] > 0.5) == band_5ghz
            for e in range(length - 1, len(x)):
                if tk.area[e] >= 0 and on[e - length + 1:e + 1].all():
                    xs.append(x[e - length + 1:e + 1]); sets.append(tk.area_sets[e])
    return (np.stack(xs) if xs else np.zeros((0, length, per_phone), dtype=np.float32)), sets


def export_band(tracks, walks, band, length, areas, classes, tr, hidden, epochs, device, folder: Path, dedup: bool) -> dict | None:
    """Обучить сеть диапазона по проходкам walks и записать веса и описание для radar/presence_nn.py."""
    xw, sets = band_windows(tracks, walks, band == "phy1-ap0", length, tr.FEATURES_PER_PHONE)
    if not len(xw):
        return None
    soft = tr.soft_targets(sets, len(areas))
    sw = dedup_weights(xw, soft.argmax(1)) if dedup else None
    m = train_gru(xw, soft, len(areas), hidden, epochs, device, tr.class_weights(soft.argmax(1), len(areas)), sw)
    folder.mkdir(parents=True, exist_ok=True)
    np.savez(folder / f"gru-{band}.npz", **gru_numpy_weights(m))
    priors = soft.mean(0)
    meta = {"iface": band, "areas": areas, "priors": [round(float(v), 6) for v in priors], "window_ticks": length, "tick_s": tr.TICK_S,
            "norm": {"db_zero": tr.DB_ZERO, "db_scale": tr.DB_SCALE, "db_missing": tr.DB_MISSING, "age_cap_s": tr.AGE_CAP_S, "rate_log_scale": tr.RATE_LOG_SCALE},
            "classes": {"spots": classes.get("spots") or [], "corridor": classes.get("corridor") or []},
            "trained_on": list(walks), "windows": int(len(xw)), "dedup": dedup, "hidden": hidden, "epochs": epochs}
    (folder / f"gru-{band}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def answer(proba: np.ndarray, area_room: np.ndarray, n_rooms: int, thr: dict) -> tuple[str, int]:
    """Ответ по иерархии: («area», область) если её уверенность не ниже порога, иначе («room», комната), иначе («none», -1)."""
    a = int(proba.argmax())
    if proba[a] >= thr["area"]:
        return "area", a
    room_p = np.bincount(area_room, weights=proba, minlength=n_rooms)
    r = int(room_p.argmax())
    return ("room", r) if room_p[r] >= thr["room"] else ("none", -1)


def pair_windows(track, phone: str, length: int, per_phone: int):
    """Окна одного телефона со сдвигом в один отсчёт, кончающиеся на тике с меткой, телефон на 5 ГГц всё окно."""
    k = PHONE_INDEX[phone]
    x = track.x[:, k * per_phone:(k + 1) * per_phone]
    on = x[:, BAND_COL] > 0.5
    ends = [e for e in range(length - 1, len(x)) if track.area[e] >= 0 and on[e - length + 1:e + 1].all()]
    return (np.stack([x[e - length + 1:e + 1] for e in ends]) if ends else np.zeros((0, length, per_phone), dtype=np.float32)), ends


def evaluate(proba, ends, test, area_room, n_rooms, thr, kinds) -> dict:
    pa = proba.argmax(1)
    ok_area = np.array([int(a) in test.area_sets[e] for a, e in zip(pa, ends)])
    ok_room = np.array([int(area_room[a]) in test.room_sets[e] for a, e in zip(pa, ends)])
    # отрезки: большинство ответов окон отрезка
    by_seg: dict[str, list[int]] = {}
    for a, e in zip(pa, ends):
        by_seg.setdefault(test.group[e], []).append(int(a))
    seg_area = seg_room = 0
    for g, votes in by_seg.items():
        e0 = next(e for e in ends if test.group[e] == g)
        top = Counter(votes).most_common(1)[0][0]
        seg_area += top in test.area_sets[e0]; seg_room += int(area_room[top]) in test.room_sets[e0]
    stop_like = {g for g in by_seg if kinds.get(g) in ("stop", "zone")}
    seg_stop_area = sum(Counter(by_seg[g]).most_common(1)[0][0] in test.area_sets[next(e for e in ends if test.group[e] == g)] for g in stop_like)
    seg_stop_room = sum(int(area_room[Counter(by_seg[g]).most_common(1)[0][0]]) in test.room_sets[next(e for e in ends if test.group[e] == g)] for g in stop_like)
    # иерархический ответ
    levels, correct = Counter(), Counter()
    for p, e in zip(proba, ends):
        lvl, v = answer(p, area_room, n_rooms, thr)
        levels[lvl] += 1
        if lvl == "area":
            correct[lvl] += v in test.area_sets[e]
        elif lvl == "room":
            correct[lvl] += v in test.room_sets[e]
    n = len(ends)
    return {"windows": n, "area_acc": round(float(ok_area.mean()), 3), "room_acc": round(float(ok_room.mean()), 3),
            "segments": len(by_seg), "segment_area_acc": round(seg_area / len(by_seg), 3), "segment_room_acc": round(seg_room / len(by_seg), 3),
            "stops_zones": len(stop_like), "stop_zone_area_acc": round(seg_stop_area / max(1, len(stop_like)), 3),
            "stop_zone_room_acc": round(seg_stop_room / max(1, len(stop_like)), 3),
            "answer_share": {k: round(levels[k] / n, 3) for k in ("area", "room", "none")},
            "answer_acc": {k: round(correct[k] / levels[k], 3) if levels[k] else None for k in ("area", "room")}}


def main() -> None:
    ap = argparse.ArgumentParser(description="маленький GRU по окну со сдвигом в отсчёт: перенос внутри пары и между парами, иерархический ответ")
    ap.add_argument("--data", type=Path, default=Path("data/walks/nn"))
    ap.add_argument("--tag", required=True)
    ap.add_argument("--windows", type=float, nargs="+", default=[3, 5])
    ap.add_argument("--hidden", type=int, default=32)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--dedup", action="store_true", help="сравнить с дедупликацией обучающих окон по весам")
    ap.add_argument("--export", action="store_true", help="выгрузить сети по диапазонам для радара вместо опытов")
    ap.add_argument("--heldout", nargs="*", default=[], help="с --export: ещё выгрузки без каждой из этих проходок")
    ap.add_argument("--out", type=Path, default=None, help="с --export: папка выгрузки, по умолчанию папка набора")
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    tr = load_module(root, "radar_nn_train")
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    ticks, summary, classes = tr.load(args.data, args.tag)
    areas = sorted({a for t in ticks if t["area"] is not None for a in (t.get("area_set") or [t["area"]])})
    rooms = sorted({r for t in ticks if t["room"] is not None for r in (t.get("room_set") or [t["room"]])})
    cr = classes.get("class_room") or {}
    area_room = np.array([rooms.index(cr.get(a, a)) if cr.get(a, a) in rooms else 0 for a in areas])
    thr = (classes.get("hierarchy") or {}).get("thresholds") or {"area": 0.5, "room": 0.6}
    tracks = {w: tr.real_track([t for t in ticks if t["walk"] == w], areas, rooms, 5.0) for w in sorted({t["walk"] for t in ticks})}
    kinds = {s["group"]: s["kind"] for s in summary["segments"]}
    if args.export:
        length = int(round(args.windows[0] / tr.TICK_S))
        walks = sorted(tracks)
        plans = [(args.out or args.data, walks)] + [((args.out or args.data) / f"heldout-{h}", [w for w in walks if w != h]) for h in args.heldout]
        for folder, use in plans:
            for band in ("phy0-ap0", "phy1-ap0"):
                meta = export_band(tracks, use, band, length, areas, classes, tr, args.hidden, args.epochs, device, folder, args.dedup)
                print(f"{folder}: {band} по {use}: " + (f"{meta['windows']} окон" if meta else "окон нет"), flush=True)
        return
    out = {"areas": areas, "rooms": rooms, "thresholds": thr, "hidden": args.hidden, "epochs": args.epochs, "runs": []}
    started = time.time()
    for w in args.windows:
        length = int(round(w / tr.TICK_S))
        for (p_tr, w_tr), (p_te, w_te) in EXPERIMENTS:
            xw, e_tr = pair_windows(tracks[w_tr], p_tr, length, tr.FEATURES_PER_PHONE)
            xt, e_te = pair_windows(tracks[w_te], p_te, length, tr.FEATURES_PER_PHONE)
            soft = tr.soft_targets([tracks[w_tr].area_sets[e] for e in e_tr], len(areas))
            for dedup in ((False, True) if args.dedup else (False,)):
                sw = dedup_weights(xw, soft.argmax(1)) if dedup else None
                m = train_gru(xw, soft, len(areas), args.hidden, args.epochs, device, tr.class_weights(soft.argmax(1), len(areas)), sw)
                r = evaluate(predict_proba(m, xt, device), e_te, tracks[w_te], area_room, len(rooms), thr, kinds)
                major = int(soft.sum(0).argmax())
                r.update(dedup=dedup, effective_windows=round(float(len(sw) / (sw ** 2).mean()), 0) if dedup else len(xw))
                r.update(window_s=w, train=f"{p_tr}/{w_tr}", test=f"{p_te}/{w_te}", same_pair=p_tr == p_te,
                       majority_area_acc=round(float(np.mean([major in tracks[w_te].area_sets[e] for e in e_te])), 3),
                       majority_room_acc=round(float(np.mean([int(area_room[major]) in tracks[w_te].room_sets[e] for e in e_te])), 3))
                out["runs"].append(r)
                print(f"{w} с {r['train']} -> {r['test']} {'дедуп' if dedup else 'все'}: окна область {r['area_acc']} комната {r['room_acc']} (частый {r['majority_area_acc']}/{r['majority_room_acc']}); "
                    f"отрезки {r['segment_area_acc']}/{r['segment_room_acc']} из {r['segments']}, стоянки и зоны {r['stop_zone_area_acc']}/{r['stop_zone_room_acc']} из {r['stops_zones']}; "
                    f"ответы {r['answer_share']} верно {r['answer_acc']}, {time.time() - started:.0f} с", flush=True)
    length = int(round(args.windows[0] / tr.TICK_S))
    xw, e_tr = pair_windows(tracks["w1"], "phone-a", length, tr.FEATURES_PER_PHONE)
    m = train_gru(xw, tr.soft_targets([tracks["w1"].area_sets[e] for e in e_tr], len(areas)), len(areas), args.hidden, 3, device, np.ones(len(areas), dtype=np.float32))
    w_np = gru_numpy_weights(m)
    x = xw[0]
    with torch.no_grad():
        ref = m(torch.tensor(x[None], device=device)).cpu().numpy()[0]
    diff = float(np.abs(gru_numpy(w_np, x) - ref).max())
    t0 = time.perf_counter()
    for _ in range(TIMING_CALLS):
        gru_numpy(w_np, x)
    out["cpu"] = {"window_s": args.windows[0], "params": int(sum(p.numel() for p in m.parameters())), "cpu_ms_per_window": round((time.perf_counter() - t0) / TIMING_CALLS * 1000, 3),
                  "numpy_vs_torch_max_diff": round(diff, 6)}
    print("CPU:", out["cpu"])
    (args.data / f"gru-{args.tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
