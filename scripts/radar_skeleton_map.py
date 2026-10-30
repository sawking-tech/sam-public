"""Относительная карта покрытия по проходкам (SAM-118): каркас из узлов (места стоянок и перекрёстки) и рёбер (участки переходов), отдельно на каждый диапазон.

Телефоны меняли диапазоны между проходками, поэтому карта строится по диапазону: в каждом тике берётся телефон, который был на этом диапазоне,
его уровень приводится к опорному телефону сдвигом из описания проходок (offsets_db). Места на одной точке (разные названия) сливаются в узел.

Узел - место стоянки (повторные стоянки на одном месте сливаются) или перекрёсток: участок коридора из таблицы классов, через который прошли
не меньше JUNCTION_MIN_LEGS переходов. У узла уровень (медиана), шум (разброс), секунды данных, координата плана - только оценка.
Ребро - участок перехода между соседними узлами: замеры хранятся относительно ребра - (доля пути 0..1 от начального узла, уровень), а не x/z.
Повторы ребра (те же концы, с учётом направления) сливаются по долям с шагом около BIN_M метров: медиана, разброс, число проходов.
Сжатие: из профиля остаются концы и точки излома (Дуглас-Пекер с допуском на шум телефона); узел-стоянка с двумя рёбрами, через который уровень
идёт без излома (разница наклонов до и после в пределах шума), убирается, рёбра склеиваются.

Запуск: python scripts/radar_skeleton_map.py --data data/walks/nn --tag 2026-10-11 [--layout data/home_layout.json]
Выход: data/walks/nn/graph-<диапазон>-<tag>.json (или -slow- без быстрой части) и строка сводки."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
from pathlib import Path

JUNCTION_MIN_LEGS = 2           # перекрёсток - участок коридора, через который прошли хотя бы столько разных переходов
BIN_M = 0.3                     # шаг слияния замеров по ребру, метры пути: как клетка мира
MIN_FRESH_PER_BIN = 1
PLACE_SNAP_M = 1.0              # точка быстрой нарезки отходит месту стоянки, если ближе этого
SAME_POINT_M = 0.3              # места с разными названиями ближе этого - один узел
NOISE_FLOOR_DB = 1.0            # допуск сжатия не меньше этого: уровень квантован по 0,5 дБ


def load_dataset_module(root: Path):
    spec = importlib.util.spec_from_file_location("radar_nn_dataset", root / "scripts" / "radar_nn_dataset.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["radar_nn_dataset"] = mod
    spec.loader.exec_module(mod)
    return mod


def douglas_peucker(points: list[tuple[float, float, float, int]], tol: float) -> list[tuple[float, float, float, int]]:
    """Точки излома профиля (доля, уровень, разброс, проходов): остаются концы и точки дальше tol дБ от хорды."""
    if len(points) <= 2:
        return list(points)
    (f0, l0, *_), (f1, l1, *_) = points[0], points[-1]
    worst, at = 0.0, None
    for i, (f, l, *_) in enumerate(points[1:-1], 1):
        chord = l0 + (l1 - l0) * ((f - f0) / (f1 - f0) if f1 > f0 else 0.0)
        if abs(l - chord) > worst:
            worst, at = abs(l - chord), i
    if worst <= tol:
        return [points[0], points[-1]]
    return douglas_peucker(points[:at + 1], tol)[:-1] + douglas_peucker(points[at:], tol)


def split_fractions(path: list[tuple[float, float]], junctions: dict[str, tuple[float, float, float]], ds) -> list[tuple[float, str]]:
    """Где путь проходит перекрёстки: (доля пути в точке наибольшего сближения, имя), по возрастанию доли; только сближения в радиусе."""
    acc = ds.path_length(path)
    total = acc[-1] or 1.0
    out = []
    for name, (x, z, r) in junctions.items():
        best = min(((math.dist((x, z), ds.point_at(path, k / 200)), k / 200) for k in range(201)), default=None)
        if best and best[0] <= r and 0.02 < best[1] < 0.98:
            out.append((best[1], name))
    return sorted(out)


def band_levels(ticks: list[dict], band: str, offsets: dict) -> list[tuple[float, float]]:
    """(время, уровень) свежих кадров телефонов на диапазоне band, приведённый к опорному телефону; два телефона в тике - среднее."""
    out = []
    for t in ticks:
        vals = [t[p]["db"] - offsets.get(p, 0.0) for p in ("phone-a", "phone-b") if t.get(p) and t[p]["fresh"] and t[p]["if"] == band]
        if vals:
            out.append((t["t"], sum(vals) / len(vals)))
    return out


def canonical_places(places) -> dict[str, str]:
    """Название места -> название первого места таблицы в той же точке (не дальше SAME_POINT_M)."""
    out = {}
    for p in places:
        out[p.name] = next(q.name for q in places if math.dist((q.x, q.z), (p.x, p.z)) <= SAME_POINT_M)
    return out


def build(ticks, summary, classes, places, layout, band: str, ds, noise_db: float, offsets: dict, slow_only: bool = False, cleanup: bool = True) -> dict:
    """Каркас диапазона; slow_only - без быстрой части (группы fast и стоянки быстрой части); cleanup False - узлы не убираются."""
    canon = canonical_places(places)
    segs = [s for s in summary["segments"] if s["kind"] in ("stop", "leg")]
    if slow_only:
        fast_stops = {(d["walk"], round(d["t_start"], 2)) for d in summary.get("stops_detail") or [] if d.get("fast")}
        segs = [s for s in segs if ":fast:" not in s["group"] and (s["walk"], round(s["t0"], 2)) not in fast_stops]
    summary = {**summary, "segments": [{**s, **({"place": canon.get(s["place"], s["place"])} if s["kind"] == "stop" else
                                                 {"from": canon.get(s["from"], s["from"]), "to": canon.get(s["to"], s["to"])})} for s in segs]}
    route = ds.world_router(layout)
    from radar import presence_topview as ptv
    from radar import presence_world as pw
    from radar.presence_flow_live import Nav
    world = pw.World(ptv.TopViewNavigation(Nav(layout), ptv.TOP_VIEW_MAX_H_M), pw.WORLD_STEP_M)     # клетка мира узла - для наложения на карту модели стен
    level_at = band_levels(ticks, band, offsets)
    iface = band
    by_place = {p.name: p for p in places}

    def snap(name: str, xy) -> str:
        if name in by_place:
            return name
        near = min(places, key=lambda p: math.dist((p.x, p.z), xy))
        return near.name if math.dist((near.x, near.z), xy) <= PLACE_SNAP_M else name

    fast = {f"w1:fast:{i}": s for i, s in enumerate(json.loads(Path(summary["_fast_path"]).read_text(encoding="utf-8"))["segments"])}
    # узлы-стоянки
    nodes: dict[str, dict] = {}
    for seg in summary["segments"]:
        if seg["kind"] != "stop":
            continue
        vals = [db for t, db in level_at if seg["t0"] <= t <= seg["t1"]]
        p = by_place[seg["place"]]
        n = nodes.setdefault(f"S:{p.name}", {"id": f"S:{p.name}", "kind": "stop", "name": p.name, "x": p.x, "z": p.z, "values": []})
        n["values"] += vals
    # переходы и их маршруты
    legs = []
    for seg in summary["segments"]:
        if seg["kind"] != "leg" or seg.get("quality") == "transition_uncertain":
            continue
        if ":fast:" in seg["group"]:
            f = fast[seg["group"]]
            names = [f["from"], *f.get("via", []), f["to"]]
            pts = [tuple(f["points"][n]) for n in names]
            a, b = canon.get(snap(f["from"], pts[0]), snap(f["from"], pts[0])), canon.get(snap(f["to"], pts[-1]), snap(f["to"], pts[-1]))
        else:
            a, b = seg["from"], seg["to"]
            pts = [(by_place[a].x, by_place[a].z), (by_place[b].x, by_place[b].z)]
        if a not in by_place or b not in by_place:
            continue
        legs.append({"from": f"S:{a}", "to": f"S:{b}", "t0": seg["t0"], "t1": seg["t1"], "path": route(pts)})
    # перекрёстки: участки коридора, через которые прошли не меньше JUNCTION_MIN_LEGS переходов
    corridor = {c["name"]: (c["x"], c["z"], c["r"]) for c in classes.get("corridor") or []}
    passes = {name: 0 for name in corridor}
    for leg in legs:
        leg["cuts"] = split_fractions(leg["path"], corridor, ds)
        for _, name in leg["cuts"]:
            passes[name] += 1
    junctions = {n for n, k in passes.items() if k >= JUNCTION_MIN_LEGS}
    for name in junctions:
        x, z, _ = corridor[name]
        nodes[f"J:{name}"] = {"id": f"J:{name}", "kind": "junction", "name": name, "x": x, "z": z, "values": []}
    # проходы рёбер: переход режется на перекрёстках; замеры - (доля ребра, уровень), длительность
    traversals: dict[tuple[str, str], list[dict]] = {}
    for leg in legs:
        cuts = [(0.0, leg["from"])] + [(f, f"J:{n}") for f, n in leg["cuts"] if n in junctions] + [(1.0, leg["to"])]
        length = ds.path_length(leg["path"])[-1]
        for (f0, a), (f1, b) in zip(cuts, cuts[1:]):
            if a == b:
                continue
            t0, t1 = leg["t0"] + (leg["t1"] - leg["t0"]) * f0, leg["t0"] + (leg["t1"] - leg["t0"]) * f1
            samples = [((t - t0) / (t1 - t0), db) for t, db in level_at if t0 <= t <= t1] if t1 > t0 else []
            key, forward = (a, b) if a < b else (b, a), a < b
            if not forward:
                samples = [(1 - f, db) for f, db in samples]
            traversals.setdefault(key, []).append({"samples": samples, "seconds": t1 - t0, "metres": length * (f1 - f0), "direction": f"{a}->{b}"})
            if a.startswith("J:"):
                nodes[a]["values"] += [db for t, db in level_at if abs(t - t0) <= 0.5]
    # слияние повторов и сжатие
    raw_points = kept_points = 0
    edges = []
    for (a, b), trs in traversals.items():
        metres = statistics.median([t["metres"] for t in trs]) or BIN_M
        bins = max(1, int(round(metres / BIN_M)))
        cols: dict[int, list[float]] = {}
        for tr in trs:
            for f, db in tr["samples"]:
                cols.setdefault(min(bins - 1, int(f * bins)), []).append(db)
        profile = [((k + 0.5) / bins, statistics.median(v), statistics.pstdev(v) if len(v) > 1 else 0.0, len(v)) for k, v in sorted(cols.items()) if len(v) >= MIN_FRESH_PER_BIN]
        ends = [(0.0, nodes[a]["values"] and statistics.median(nodes[a]["values"]) or (profile[0][1] if profile else None), 0.0, 0),
                (1.0, nodes[b]["values"] and statistics.median(nodes[b]["values"]) or (profile[-1][1] if profile else None), 0.0, 0)]
        full = [ends[0], *profile, ends[1]] if profile else ends
        full = [p for p in full if p[1] is not None]
        shape = douglas_peucker(full, max(NOISE_FLOOR_DB, noise_db)) if len(full) >= 2 else full
        raw_points += sum(len(t["samples"]) for t in trs); kept_points += max(0, len(shape) - 2)
        edges.append({"from": a, "to": b, "passes": len(trs), "directions": sorted({t["direction"] for t in trs}),
                      "seconds_median": round(statistics.median([t["seconds"] for t in trs]), 1), "metres": round(metres, 2),
                      "shape": [{"share": round(f, 3), "db": round(l, 1), "spread": round(s, 1), "n": n} for f, l, s, n in shape]})
    before = {"nodes": len(nodes), "edges": len(edges)}
    removed = remove_linear_nodes(nodes, edges, noise_db) if cleanup else []
    for n in nodes.values():
        v = n.pop("values")
        n.update(cell=int(world.cell_at(n["x"], n["z"])), db_median=round(statistics.median(v), 1) if v else None, noise_db=round(statistics.pstdev(v), 2) if len(v) > 1 else None,
                 seconds=round(len(v) * ds.TICK_S, 1))
    return {"band": band, "iface": iface, "offsets_db": offsets, "noise_db": round(noise_db, 2), "nodes": list(nodes.values()), "edges": edges,
            "before_cleanup": before, "after_cleanup": {"nodes": len(nodes), "edges": len(edges)}, "removed_nodes": removed,
            "points_raw": raw_points, "points_kept": kept_points}


def remove_linear_nodes(nodes: dict, edges: list[dict], noise_db: float) -> list[str]:
    """Убрать узлы-стоянки с двумя рёбрами, через которые уровень идёт без излома: наклон (дБ/с) последнего куска входящего ребра и первого куска
    исходящего различается меньше чем на шум за длительность короче ребра. Рёбра склеиваются, форма - по долям времени."""
    removed = []
    changed = True
    while changed:
        changed = False
        for nid, node in list(nodes.items()):
            if node["kind"] != "stop":
                continue
            touching = [e for e in edges if nid in (e["from"], e["to"])]
            if len(touching) != 2:
                continue
            e1, e2 = touching
            s1, d1 = oriented(e1, nid, into=True)
            s2, d2 = oriented(e2, nid, into=False)
            slope_in = (s1[-1]["db"] - s1[-2]["db"]) / max(1e-6, (s1[-1]["share"] - s1[-2]["share"]) * d1)
            slope_out = (s2[1]["db"] - s2[0]["db"]) / max(1e-6, (s2[1]["share"] - s2[0]["share"]) * d2)
            if abs(slope_in - slope_out) * min(d1, d2) > noise_db:
                continue
            a = e1["to"] if e1["from"] == nid else e1["from"]
            b = e2["to"] if e2["from"] == nid else e2["from"]
            total = d1 + d2
            shape = [{**p, "share": round(p["share"] * d1 / total, 3)} for p in s1[:-1]] + [{**p, "share": round((d1 + p["share"] * d2) / total, 3)} for p in s2]
            edges.remove(e1); edges.remove(e2)
            edges.append({"from": a, "to": b, "passes": min(e1["passes"], e2["passes"]), "directions": [f"{a}->{b} через {node['name']}"],
                          "seconds_median": round(total, 1), "metres": round(e1["metres"] + e2["metres"], 2), "shape": shape})
            removed.append(node["name"])
            del nodes[nid]
            changed = True
            break
    return removed


def oriented(edge: dict, node_id: str, into: bool) -> tuple[list[dict], float]:
    """Форма ребра, развёрнутая так, чтобы into=True кончалась в node_id, into=False - начиналась в нём."""
    shape = edge["shape"]
    ends_here = edge["to"] == node_id
    if into != ends_here:
        shape = [{**p, "share": round(1 - p["share"], 3)} for p in reversed(shape)]
    return shape, edge["seconds_median"]


def main() -> None:
    ap = argparse.ArgumentParser(description="относительная карта покрытия: каркас узлов и рёбер на каждый телефон")
    ap.add_argument("--data", type=Path, default=Path("data/walks/nn"))
    ap.add_argument("--tag", required=True)
    ap.add_argument("--layout", type=Path, default=Path("data/home_layout.json"))
    ap.add_argument("--fast", type=Path, default=None, help="нарезка быстрых проходок; по умолчанию data/walks/walk-<tag>-fast.json")
    ap.add_argument("--slow-only", action="store_true", help="каркас без быстрой части проходки 1 и без чистки узлов; файлы graph-<диапазон>-slow-<tag>.json")
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    ds = load_dataset_module(root)
    ticks = [json.loads(l) for l in (args.data / f"ticks-{args.tag}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    summary = json.loads((args.data / f"summary-{args.tag}.json").read_text(encoding="utf-8"))
    summary["_fast_path"] = str(args.fast or Path("data/walks") / f"walk-{args.tag}-fast.json")
    classes = json.loads((args.data / f"classes-{args.tag}.json").read_text(encoding="utf-8"))
    table = json.loads((args.data / f"places-{args.tag}.json").read_text(encoding="utf-8"))
    places = [ds.Place(p["match"], p["x"], p["z"], p["sd_m"]) for p in table["places"]]
    layout = json.loads(args.layout.read_text(encoding="utf-8"))
    offsets_all = json.loads((args.data / f"walks-{args.tag}.json").read_text(encoding="utf-8")).get("offsets_db") or {}
    for band in ("phy0-ap0", "phy1-ap0"):
        offsets = offsets_all.get(band) or {}
        levels = band_levels(ticks, band, offsets)
        stop_sd = []
        for seg in summary["segments"]:
            if seg["kind"] == "stop":
                v = [db for t, db in levels if seg["t0"] <= t <= seg["t1"]]
                if len(v) > 5:
                    stop_sd.append(statistics.pstdev(v))
        noise = statistics.median(stop_sd) if stop_sd else 3.0
        graph = build(ticks, summary, classes, places, layout, band, ds, noise, offsets, slow_only=args.slow_only, cleanup=not args.slow_only)
        (args.data / f"graph-{band}{'-slow' if args.slow_only else ''}-{args.tag}.json").write_text(json.dumps(graph, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{band}: шум {graph['noise_db']} дБ, узлов {graph['before_cleanup']['nodes']} -> {graph['after_cleanup']['nodes']}, "
              f"рёбер {graph['before_cleanup']['edges']} -> {graph['after_cleanup']['edges']}, убраны {graph['removed_nodes']}, "
              f"точек {graph['points_raw']} -> {graph['points_kept']} изломов")


if __name__ == "__main__":
    main()
