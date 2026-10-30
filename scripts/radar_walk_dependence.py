"""Повторы и связи в ряду проходки (SAM-118): матричный профиль по форме ряда и корреляция приращений двух телефонов со сдвигом.

Матричный профиль (как у stumpy, но прямым счётом на numpy - ряд в тысячу секунд): для каждого окна MOTIF_S секунд расстояние до самого похожего
окна в другом месте ряда после z-нормировки (форма, не уровень). Малое расстояние - мотив (повтор прохода), большое - уникальный кусок.
Мотивы сверяются с отрезками проходки: попали ли пары в один и тот же переход (откуда-куда) или стоянку того же места.
Корреляция приращений: секундные разности уровня телефона A и телефона B со сдвигом -MAX_LAG_S..MAX_LAG_S - связь двух диапазонов в одной руке.

Запуск: python scripts/radar_walk_dependence.py --data data/walks/nn --tag 2026-10-11"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import numpy as np

MOTIF_S = 6                  # длина окна мотива, секунды: переход между соседними местами 4-12 с
TOP_MOTIFS = 10
MAX_LAG_S = 5
FLAT_SD_DB = 1.0             # окно с разбросом меньше - плоская стоянка: после z-нормировки любые две такие «похожи», в мотивы не идут


def second_series(ticks: list[dict], phone: str) -> tuple[np.ndarray, list]:
    """Медиана свежих кадров по секундам и группа (отрезок) секунды; пустая секунда - прошлое значение."""
    t0 = int(ticks[0]["t"])
    n = int(ticks[-1]["t"]) - t0 + 1
    vals: list[list[float]] = [[] for _ in range(n)]
    groups: list = [None] * n
    for t in ticks:
        s = int(t["t"]) - t0
        if t.get(phone) and t[phone]["fresh"]:
            vals[s].append(t[phone]["db"])
        groups[s] = groups[s] or t["group"]
    out, last = np.zeros(n), next(statistics.median(v) for v in vals if v)
    for i, v in enumerate(vals):
        last = statistics.median(v) if v else last
        out[i] = last
    return out, groups


def matrix_profile(x: np.ndarray, m: int) -> tuple[np.ndarray, np.ndarray]:
    """z-нормированное расстояние каждого окна до ближайшего другого окна (зона исключения m/2) и индекс этого окна."""
    w = np.lib.stride_tricks.sliding_window_view(x, m)
    z = (w - w.mean(1, keepdims=True)) / np.maximum(w.std(1, keepdims=True), 1e-6)
    d = np.sqrt(np.maximum(0.0, 2 * m * (1 - (z @ z.T) / m)))
    flat = w.std(1) < FLAT_SD_DB
    d[flat, :] = np.inf
    d[:, flat] = np.inf
    n = len(z)
    for i in range(n):
        d[i, max(0, i - m // 2):min(n, i + m // 2 + 1)] = np.inf
    return d.min(1), d.argmin(1)


def lagged_corr(a: np.ndarray, b: np.ndarray, max_lag: int) -> dict[int, float]:
    """Корреляция приращений a(t) и b(t + lag)."""
    da, db = np.diff(a), np.diff(b)
    out = {}
    for lag in range(-max_lag, max_lag + 1):
        x, y = (da[:len(da) - lag], db[lag:]) if lag >= 0 else (da[-lag:], db[:len(db) + lag])
        out[lag] = round(float(np.corrcoef(x, y)[0, 1]), 3)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="мотивы ряда проходки и связь двух телефонов")
    ap.add_argument("--data", type=Path, default=Path("data/walks/nn"))
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()
    ticks = [json.loads(l) for l in (args.data / f"ticks-{args.tag}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    summary = json.loads((args.data / f"summary-{args.tag}.json").read_text(encoding="utf-8"))
    seg = {s["group"]: s for s in summary["segments"]}
    label = lambda g: (f"стоянка {seg[g]['place']}" if seg[g]["kind"] == "stop" else f"{seg[g]['from']} -> {seg[g]['to']}") if g in seg else "-"
    a, groups = second_series(ticks, "phone-a")
    b, _ = second_series(ticks, "phone-b")
    out = {"lag_corr_increments": lagged_corr(a, b, MAX_LAG_S)}
    for name, x in (("phone-a", a), ("phone-b", b)):
        mp, nn = matrix_profile(x, MOTIF_S)
        order = [i for i in np.argsort(mp) if np.isfinite(mp[i])]
        seen, motifs = set(), []
        for i in order:
            j = int(nn[i])
            key = tuple(sorted((int(i) // MOTIF_S, j // MOTIF_S)))
            if key in seen:
                continue
            seen.add(key)
            gi, gj = groups[i + MOTIF_S // 2], groups[j + MOTIF_S // 2]
            same = gi is not None and gj is not None and label(gi).replace("стоянка ", "") == label(gj).replace("стоянка ", "")
            motifs.append({"i": int(i), "j": j, "dist": round(float(mp[i]), 2), "a": label(gi), "b": label(gj), "same_segment_kind": bool(same)})
            if len(motifs) >= TOP_MOTIFS:
                break
        finite = [i for i in np.argsort(-np.where(np.isfinite(mp), mp, -1)) if np.isfinite(mp[i])]
        unique = [{"i": int(i), "dist": round(float(mp[i]), 2), "where": label(groups[i + MOTIF_S // 2])} for i in finite[:5]]
        out[name] = {"motifs": motifs, "unique": unique, "same_share": round(sum(m["same_segment_kind"] for m in motifs) / len(motifs), 2)}
        print(f"{name}: из {len(motifs)} лучших мотивов пара в отрезках одного места или перехода: {sum(m['same_segment_kind'] for m in motifs)}")
        for m in motifs[:5]:
            print(f"   {m['dist']}: {m['a']}  ~  {m['b']}")
    print("корреляция приращений A(t) и B(t+сдвиг):", out["lag_corr_increments"])
    (args.data / f"dependence-{args.tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
