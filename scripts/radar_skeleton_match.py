"""Узнавание быстрых переходов по относительной карте (SAM-118): ряд уровня обоих телефонов против путей каркаса медленной части, DTW.

Каркас строит scripts/radar_skeleton_map.py --slow-only (graph-<диапазон>-slow-<tag>.json); телефон сравнивается с каркасом своего диапазона
в проходке 1 (bands в описании проходок), его уровень приводится к опорному сдвигом offsets_db. Путь-кандидат - простой путь каркаса из 1..MAX_EDGES рёбер;
его шаблон по каждому телефону: стоянка в начальном узле, изломы рёбер по их длительности, стоянка в конечном узле. Запрос - быстрый переход
из нарезки с запасом по краям, медианы свежих кадров по BIN_S. Цена - модуль разницы уровня в долях шума телефона, сумма по телефонам; DTW
с открытым концом по запросу. Метрики: узнан ли переход (области начала и конца), верна ли конечная область, ошибка момента прихода
(против найденного по ряду начала следующей стоянки).

Два режима сравнения: «уровень» - абсолютные дБ; «динамика» - каждый ряд минус его медиана (сдвиг руки, поворот), у запроса усиление в
пределах GAINS, DTW с местным наклоном 0,5-2 (шаги (1,1), (1,2), (2,1)). Честность: растяжение по DTW (длина запроса на ходу к длине хода
шаблона) сверяется с отношением длительностей быстрого перехода и шаблона; расхождение больше STRETCH_TOLERANCE раз - совпадение ложное.

Запуск: python scripts/radar_skeleton_match.py --data data/walks/nn --tag 2026-10-11"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import numpy as np

BIN_S = 0.4                  # шаг рядов сравнения: два тика шкалы, медиана гасит одиночные выбросы
LEAD_S = 3.0                 # запрос начинается раньше отметки «пошёл»: человек мог тронуться раньше
TAIL_S = 10.0                # и кончается позже отметки «пришёл»: отметки отстают до 12 с
START_PLATEAU_S = 2.0        # стоянка в начале шаблона
END_PLATEAU_S = 4.0          # стоянка в конце шаблона: по ней ловится приход
MAX_EDGES = 3                    # путей до 4 рёбер на каркасе из 31 ребра больше тысячи: перебор ради кандидатов, которые и так проигрывают
GAINS = (0.7, 1.0, 1.3)     # усиление ряда запроса: размах уровня на ходу у другой скорости и позы чуть другой, шире - подгонка под что угодно
STRETCH_TOLERANCE = 1.5                 # растяжение по DTW и отношение длительностей расходятся больше - совпадение ложное


def series(ticks: list[dict], phone: str, t0: float, t1: float) -> np.ndarray:
    """Медианы свежих кадров телефона по BIN_S на [t0, t1); пустой шаг берёт прошлое значение, в начале - первое известное."""
    n = int(np.ceil((t1 - t0) / BIN_S))
    bins: list[list[float]] = [[] for _ in range(n)]
    for t in ticks:
        st = t.get(phone)
        if st and st["fresh"] and t0 <= t["t"] < t1:
            bins[int((t["t"] - t0) / BIN_S)].append(st["db"])
    out = np.full(n, np.nan)
    last = next((statistics.median(b) for b in bins if b), np.nan)
    for i, b in enumerate(bins):
        last = statistics.median(b) if b else last
        out[i] = last
    return out


def template(graph: dict, path: list[str]) -> tuple[np.ndarray, int, float] | None:
    """Шаблон пути по узлам: (ряд по BIN_S, индекс начала конечной стоянки); ребра нет или нет уровня узла - None."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    pts = [(0.0, nodes[path[0]]["db_median"]), (START_PLATEAU_S, nodes[path[0]]["db_median"])]
    t = START_PLATEAU_S
    for a, b in zip(path, path[1:]):
        e = next((e for e in graph["edges"] if {e["from"], e["to"]} == {a, b}), None)
        if e is None:
            return None
        shape = e["shape"] if e["from"] == a else [{**p, "share": 1 - p["share"]} for p in reversed(e["shape"])]
        for p in shape[1:]:
            pts.append((t + p["share"] * e["seconds_median"], p["db"]))
        t += e["seconds_median"]
    arrive = t
    pts.append((t + END_PLATEAU_S, nodes[path[-1]]["db_median"]))
    if any(v is None for _, v in pts):
        return None
    xs = np.arange(0.0, pts[-1][0] + 1e-9, BIN_S)
    return np.interp(xs, [p[0] for p in pts], [p[1] for p in pts]), int(round(arrive / BIN_S)), arrive - START_PLATEAU_S


def simple_paths(graph: dict, max_edges: int) -> list[list[str]]:
    adj: dict[str, set] = {}
    for e in graph["edges"]:
        adj.setdefault(e["from"], set()).add(e["to"]); adj.setdefault(e["to"], set()).add(e["from"])
    out = []

    def walk(path):
        if len(path) > 1:
            out.append(list(path))
        if len(path) - 1 >= max_edges:
            return
        for nxt in sorted(adj.get(path[-1], ())):
            if nxt not in path:
                walk(path + [nxt])
    for start in sorted(adj):
        walk([start])
    return out


def dtw_slope(query: np.ndarray, temp: np.ndarray) -> tuple[float, list[tuple[int, int]]]:
    """DTW с местным наклоном 0,5-2: шаги (1,1), (1,2), (2,1); весь шаблон, конец запроса свободный; цена средняя по клеткам пути.
    Строка зависит только от двух прошлых строк, поэтому считается целиком на numpy."""
    q, t = len(query), len(temp)
    cost = np.nansum(np.abs(query[:, None, :] - temp[None, :, :]), axis=2)
    acc = np.full((q + 1, t + 1), np.inf)
    cells = np.zeros((q + 1, t + 1))
    move = np.zeros((q + 1, t + 1), dtype=np.int8)       # 0 - (1,1), 1 - (1,2), 2 - (2,1)
    acc[0, 0] = 0.0
    c_pad = np.vstack([np.full((1, t), np.inf), cost])  # c_pad[i] = cost[i-1]
    for i in range(1, q + 1):
        j = np.arange(1, t + 1)
        cand = np.full((3, t), np.inf)
        cnt = np.zeros((3, t))
        cand[0] = acc[i - 1, j - 1] + c_pad[i, j - 1]; cnt[0] = cells[i - 1, j - 1] + 1
        ok = j >= 2
        cand[1, ok] = acc[i - 1, j[ok] - 2] + c_pad[i, j[ok] - 1] + c_pad[i, j[ok] - 2]; cnt[1, ok] = cells[i - 1, j[ok] - 2] + 2
        if i >= 2:
            cand[2] = acc[i - 2, j - 1] + c_pad[i, j - 1] + c_pad[i - 1, j - 1]; cnt[2] = cells[i - 2, j - 1] + 2
        k = np.argmin(cand, axis=0)
        acc[i, 1:] = cand[k, np.arange(t)]; cells[i, 1:] = cnt[k, np.arange(t)]; move[i, 1:] = k
    with np.errstate(invalid="ignore", divide="ignore"):
        norm = np.where(cells[1:, t] > 0, acc[1:, t] / np.maximum(cells[1:, t], 1), np.inf)
    if not np.isfinite(norm).any():
        return np.inf, []
    end = int(np.argmin(norm)) + 1
    path, i, j = [], end, t
    while i > 0 and j > 0:
        path.append((i - 1, j - 1))
        k = move[i, j]
        i, j = (i - 1, j - 1) if k == 0 else ((i - 1, j - 2) if k == 1 else (i - 2, j - 1))
    return float(norm[end - 1]), path[::-1]


def dtw_open_end(query: np.ndarray, temp: np.ndarray) -> tuple[float, list[tuple[int, int]]]:
    """DTW: query [Q, P] против temp [T, P] (P телефонов, nan - телефон не участвует); весь шаблон, конец запроса свободный.
    Цена - средняя по пути; возвращает цену и путь сопоставления (i запроса, j шаблона)."""
    q, t = len(query), len(temp)
    cost = np.nansum(np.abs(query[:, None, :] - temp[None, :, :]), axis=2)
    acc = np.full((q + 1, t + 1), np.inf)
    steps = np.zeros((q + 1, t + 1), dtype=int)
    acc[0, 0] = 0.0
    for i in range(1, q + 1):
        for j in range(1, t + 1):
            choices = (acc[i - 1, j - 1], acc[i - 1, j], acc[i, j - 1])
            k = int(np.argmin(choices))
            acc[i, j] = cost[i - 1, j - 1] + choices[k]
            steps[i, j] = k
    norm = acc[1:, t] / (np.arange(1, q + 1) + t)
    end = int(np.argmin(norm)) + 1
    path, i, j = [], end, t
    while i > 0 and j > 0:
        path.append((i - 1, j - 1))
        k = steps[i, j]
        i, j = (i - 1, j - 1) if k == 0 else ((i - 1, j) if k == 1 else (i, j - 1))
    return float(norm[end - 1]), path[::-1]


def area_of(node_id: str, classes: dict) -> str:
    name = node_id.split(":", 1)[1]
    return (classes.get("place_class") or {}).get(name, name)


def main() -> None:
    ap = argparse.ArgumentParser(description="узнавание быстрых переходов по каркасу медленной части")
    ap.add_argument("--data", type=Path, default=Path("data/walks/nn"))
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()
    ticks = [json.loads(l) for l in (args.data / f"ticks-{args.tag}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    summary = json.loads((args.data / f"summary-{args.tag}.json").read_text(encoding="utf-8"))
    classes = json.loads((args.data / f"classes-{args.tag}.json").read_text(encoding="utf-8"))
    table = json.loads((args.data / f"places-{args.tag}.json").read_text(encoding="utf-8"))
    phones = list(table["phones"].values())
    walks = json.loads((args.data / f"walks-{args.tag}.json").read_text(encoding="utf-8"))
    bands = next(w for w in walks["walks"] if w["id"] == "w1")["bands"]
    graphs = {p: json.loads((args.data / f"graph-{bands[p]}-slow-{args.tag}.json").read_text(encoding="utf-8")) for p in phones}
    offset = {p: (walks.get("offsets_db") or {}).get(bands[p], {}).get(p, 0.0) for p in phones}
    noise = {p: max(1.0, graphs[p]["noise_db"]) for p in phones}
    paths = [p for p in simple_paths(graphs[phones[0]], MAX_EDGES)]
    templates = {}
    for path in paths:
        per = [template(graphs[p], path) for p in phones]
        if all(per):
            n = max(len(x[0]) for x in per)
            temp = np.stack([np.pad(x[0], (0, n - len(x[0])), mode="edge") / noise[p] for x, p in zip(per, phones)], axis=1)
            templates[tuple(path)] = (temp, per[0][1], per[0][2])
    fast = json.loads((Path("data/walks") / f"walk-{args.tag}-fast.json").read_text(encoding="utf-8"))["segments"]
    stops = [s for s in summary["stops_detail"] if s.get("walk", "w1") == "w1"]
    place_area = classes.get("place_class") or {}
    results = {}
    for mode in ("level", "dynamics"):
        rows = []
        for i, seg in enumerate(fast):
            t0, t1 = seg["t_start"] - LEAD_S, seg["t_end"] + TAIL_S
            query = np.stack([(series(ticks, p, t0, t1) - offset[p]) / noise[p] for p in phones], axis=1)
            scored = []
            for path, (temp, arrive_j, walk_s) in templates.items():
                if mode == "level":
                    score, align = dtw_slope(query, temp)
                    gain = 1.0
                else:
                    qc, tc = query - np.nanmedian(query, axis=0), temp - np.nanmedian(temp, axis=0)
                    score, align, gain = min(((*dtw_slope(qc * g, tc), g) for g in GAINS), key=lambda r: r[0])
                if not align:
                    continue
                walk = [i_ for i_, j_ in align if int(round(START_PLATEAU_S / BIN_S)) <= j_ < arrive_j]
                stretch = (max(walk) - min(walk) + 1) * BIN_S / walk_s if walk and walk_s > 0 else None
                expected = seg["seconds"] / walk_s if walk_s > 0 else None
                honest = stretch is not None and expected is not None and 1 / STRETCH_TOLERANCE <= stretch / expected <= STRETCH_TOLERANCE
                scored.append((score, path, arrive_j, align, gain, stretch, expected, honest))
            scored.sort(key=lambda r: r[0])
            score, best, arrive_j, align, gain, stretch, expected, honest = scored[0]
            hits = [i_ for i_, j_ in align if j_ >= arrive_j]
            predicted_arrive = t0 + (min(hits) if hits else len(query) - 1) * BIN_S
            nxt = next((s for s in stops if s["t_start"] >= seg["t_start"] - 1), None)
            true_from = next((place_area[k] for k in place_area if seg["from"] in k or k in seg["from"]), seg["from"])
            true_to = next((place_area[k] for k in place_area if seg["to"] in k or k in seg["to"]), seg["to"])
            got_from, got_to = area_of(best[0], classes), area_of(best[-1], classes)
            rows.append({"leg": i, "true": f"{true_from} -> {true_to}", "found": f"{got_from} -> {got_to}", "path": list(best), "score": round(score, 3),
                         "leg_ok": got_from == true_from and got_to == true_to, "end_ok": got_to == true_to, "honest": bool(honest),
                         "gain": gain, "stretch": round(stretch, 2) if stretch else None, "expected_stretch": round(expected, 2) if expected else None,
                         "arrive_error_s": round(predicted_arrive - nxt["t_start"], 1) if nxt else None})
        results[mode] = rows
    out = {"templates": len(templates), "modes": {}}
    for mode, rows in results.items():
        out["modes"][mode] = {"legs": rows, "leg_ok": sum(r["leg_ok"] for r in rows), "end_ok": sum(r["end_ok"] for r in rows), "n": len(rows),
                              "false_by_stretch": sum(not r["honest"] for r in rows),
                              "arrive_error_abs_median_s": statistics.median([abs(r["arrive_error_s"]) for r in rows if r["arrive_error_s"] is not None]),
                              "gains": [r["gain"] for r in rows], "stretches": [r["stretch"] for r in rows]}
        for r in rows:
            print(f"{mode:8s} {r['leg']}: правда {r['true']:38s} найдено {r['found']:38s} переход {'да' if r['leg_ok'] else 'нет'} конец {'да' if r['end_ok'] else 'нет'} "
                  f"растяжение {r['stretch']} (ждали {r['expected_stretch']}) {'честно' if r['honest'] else 'ЛОЖНО'} усиление {r['gain']} приход {r['arrive_error_s']} с")
        m = out["modes"][mode]
        print(f"{mode}: переход узнан {m['leg_ok']}/{m['n']}, конечная область {m['end_ok']}/{m['n']}, ложных по растяжению {m['false_by_stretch']}, ошибка прихода {m['arrive_error_abs_median_s']} с")
    (args.data / f"match-fast-{args.tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def _unused():
    out = {"templates": len(templates), "legs": rows, "leg_ok": sum(r["leg_ok"] for r in rows), "end_ok": sum(r["end_ok"] for r in rows), "n": len(rows),
           "arrive_error_abs_median_s": statistics.median([abs(r["arrive_error_s"]) for r in rows if r["arrive_error_s"] is not None])}
    (args.data / f"match-fast-{args.tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in rows:
        print(f"{r['leg']}: правда {r['true']:40s} найдено {r['found']:40s} переход {'да' if r['leg_ok'] else 'нет'} конец {'да' if r['end_ok'] else 'нет'} приход {r['arrive_error_s']} с")
    print(f"шаблонов {out['templates']}, переход узнан {out['leg_ok']}/{out['n']}, конечная область {out['end_ok']}/{out['n']}, ошибка прихода по модулю {out['arrive_error_abs_median_s']} с")


if __name__ == "__main__":
    main()
