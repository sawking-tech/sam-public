"""Обучение мини-модели «что происходит и где» по короткой серии уровня Wi-Fi (SAM-118).

Вход - окно W секунд шкалы ticks (около 5 Гц) по двум телефонам: уровень, свежесть кадра, разница антенн, подтверждения, скорости. Выхода два:
состояние (идёт / только что остановился / стоит давно, порог «только что» - just_stopped_s) и область (место, участок коридора или комната).
Модели: крошечная 1D-свёртка (torch на обучении, на выводе чистый numpy по весам .npz, без torch) и для сравнения градиентный бустинг по сводке окна.
Проверка только на отложенном: целые отрезки (стоянки и переходы) уходят в проверку по группам, обучающие окна, задевшие отложенный отрезок,
выбрасываются. Синтетика - склейки обучающих отрезков по совпадению названия места на стыке, длительность стоянок 0,5-2x, границы стоянок сдвинуты
случайно на ±boundary_jitter_s; в проверку не идёт. Баланс - прореживание «стоит давно» до long_keep_s секунд на стоянку (начало стоянки целиком).

Запуск: python scripts/radar_nn_train.py --data data/walks/nn --tag 2026-10-11 [--windows 2 3 5 8] [--folds 5] [--epochs 40] [--device auto]
Результат: data/walks/nn/results-<tag>.json, веса моделей data/walks/nn/model-<W>s-<tag>.npz."""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

TICK_S = 0.2                     # шаг шкалы набора (scripts/radar_nn_dataset.py)
PHONES = ("phone-a", "phone-b")
DB_ZERO, DB_SCALE = -60.0, 10.0  # уровень -60 дБм -> 0, 10 дБ -> 1
DB_MISSING = -75.0               # уровень до первого кадра телефона: слабый, но в диапазоне
AGE_CAP_S = 5.0                  # возраст кадра обрезается: старше пяти секунд - всё равно «молчит»
RATE_LOG_SCALE = 7.0             # log1p(скорость, Мбит/с) около 7 у 1000 Мбит/с
FEATURES_PER_PHONE = 8
BAND_5GHZ = "phy1-ap0"           # интерфейс 5 ГГц роутера: признак диапазона телефона (телефоны меняли диапазоны между проходками)
STATES = ("идёт", "только что остановился", "стоит давно")
STABLE_TICKS = 5                 # ответ считается установившимся, если верен столько тиков подряд (1 с)
SEED = 11
HIDDEN = 24                      # каналов свёртки: больше на одной проходке переобучается
BATCH = 256
LR = 3e-3
WEIGHT_DECAY = 1e-4
TIMING_CALLS = 2000              # вызовов вывода для замера времени на окно
SYNTH_ROUTES_PER_TICK = 1.0      # синтетики примерно столько же тиков, сколько настоящих в обучении
SYNTH_MAX_PIECES = 9             # стоянок и переходов в одной склейке


# ── данные ───────────────────────────────────────────────────────────────

def load(data: Path, tag: str) -> tuple[list[dict], dict, dict]:
    ticks = [json.loads(line) for line in (data / f"ticks-{tag}.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    summary = json.loads((data / f"summary-{tag}.json").read_text(encoding="utf-8"))
    classes = json.loads((data / f"classes-{tag}.json").read_text(encoding="utf-8"))
    return ticks, summary, classes


def tick_features(ticks: list[dict]) -> np.ndarray:
    """Признаки тиков [N, 2 * FEATURES_PER_PHONE]; уровень и линк без кадра в тике берутся с прошлого тика (роутер держит последний)."""
    out = np.zeros((len(ticks), FEATURES_PER_PHONE * len(PHONES)), dtype=np.float32)
    for k, phone in enumerate(PHONES):
        last = {"db": DB_MISSING, "ant_diff": 0.0, "ack": DB_MISSING, "tx": 0.0, "rx": 0.0, "age": AGE_CAP_S, "band5": 0.0}
        for i, t in enumerate(ticks):
            st = t.get(phone)
            fresh = 0.0
            if st:
                ack = [v for v in (st.get("ack0"), st.get("ack1")) if v is not None]
                last.update(db=st["db"], age=min(st["age_ms"] / 1000.0, AGE_CAP_S), band5=1.0 if st.get("if") == BAND_5GHZ else 0.0, ant_diff=st.get("ant_diff") if st.get("ant_diff") is not None else last["ant_diff"],
                            ack=sum(ack) / len(ack) if ack else last["ack"], tx=st.get("tx_rate") or last["tx"], rx=st.get("rx_rate") or last["rx"])
                fresh = 1.0 if st["fresh"] else 0.0
            base = k * FEATURES_PER_PHONE
            out[i, base:base + FEATURES_PER_PHONE] = [(last["db"] - DB_ZERO) / DB_SCALE, fresh, last["age"] / AGE_CAP_S, last["ant_diff"] / DB_SCALE,
                                                      (last["ack"] - DB_ZERO) / DB_SCALE, math.log1p(last["tx"]) / RATE_LOG_SCALE, math.log1p(last["rx"]) / RATE_LOG_SCALE,
                                                      last["band5"]]
    return out


def state_of(standing_s: float | None, just_s: float) -> int:
    return 0 if standing_s is None else (1 if standing_s < just_s else 2)


@dataclass
class Track:
    """Непрерывная шкала: признаки, метки по тикам (-1 - метки нет), группа тика и флаг стыка синтетики."""
    x: np.ndarray
    state: np.ndarray
    area: np.ndarray
    room: np.ndarray
    group: list
    junction: np.ndarray
    area_sets: list | None = None          # верные области тика (у зоны - все её области)
    room_sets: list | None = None


def real_track(ticks: list[dict], areas: list[str], rooms: list[str], just_s: float) -> Track:
    a_id, r_id = {a: i for i, a in enumerate(areas)}, {r: i for i, r in enumerate(rooms)}
    labelled = [t["area"] is not None for t in ticks]
    return Track(tick_features(ticks),
                 np.array([state_of(t["standing_s"], just_s) if ok and t.get("state_known", True) else -1 for t, ok in zip(ticks, labelled)]),
                 np.array([a_id[t["area"]] if ok else -1 for t, ok in zip(ticks, labelled)]),
                 np.array([r_id[t["room"]] if ok else -1 for t, ok in zip(ticks, labelled)]),
                 [t["group"] for t in ticks], np.zeros(len(ticks), dtype=bool),
                 [[a_id[a] for a in (t.get("area_set") or [t["area"]])] if ok else [] for t, ok in zip(ticks, labelled)],
                 [[r_id[r] for r in (t.get("room_set") or [t["room"]])] if ok else [] for t, ok in zip(ticks, labelled)])


def window_ends(track: Track, length: int, allowed=None, excluded=None) -> np.ndarray:
    """Концы окон длины length с меткой на конце; allowed - только концы в этих группах; excluded - окно не задевает эти группы."""
    out = []
    for e in range(length - 1, len(track.group)):
        if track.area[e] < 0 or (allowed is not None and track.group[e] not in allowed):
            continue
        if excluded and any(g in excluded for g in track.group[e - length + 1:e + 1]):
            continue
        out.append(e)
    return np.array(out, dtype=int)


def stack(track: Track, ends: np.ndarray, length: int) -> np.ndarray:
    return np.stack([track.x[e - length + 1:e + 1] for e in ends]) if len(ends) else np.zeros((0, length, track.x.shape[1]), dtype=np.float32)


# ── синтетика ────────────────────────────────────────────────────────────

def pieces(track: Track, segments: list[dict]) -> dict[str, dict]:
    """Отрезки шкалы по группам: срезы тиков и описание (место стоянки или откуда-куда перехода)."""
    idx: dict[str, list[int]] = {}
    for i, g in enumerate(track.group):
        if g is not None:
            idx.setdefault(g, []).append(i)
    return {s["group"]: {**s, "ticks": np.array(idx[s["group"]])} for s in segments if s["group"] in idx}


def phone_noise(track: Track, stop_pieces: list[dict]) -> np.ndarray:
    """Разброс нормированного уровня каждого телефона на стоянках: предел сдвига на стыке."""
    out = []
    for k in range(len(PHONES)):
        col = k * FEATURES_PER_PHONE
        sds = [float(np.std(track.x[p["ticks"], col])) for p in stop_pieces if len(p["ticks"]) > 5]
        out.append(statistics.median(sds) if sds else 0.3)
    return np.array(out)


def stretched_stop(track: Track, ticks_idx: np.ndarray, factor: float) -> np.ndarray:
    """Индексы тиков стоянки с длительностью factor от записанной: обрезка хвоста или повтор её второй половины (без скачков ходьбы в начале)."""
    n = max(STABLE_TICKS, int(round(len(ticks_idx) * factor)))
    if n <= len(ticks_idx):
        return ticks_idx[:n]
    tail = ticks_idx[len(ticks_idx) // 2:]
    reps = [ticks_idx] + [tail] * math.ceil((n - len(ticks_idx)) / max(1, len(tail)))
    return np.concatenate(reps)[:n]


def jittered_stop(track: Track, ticks_idx: np.ndarray, jitter_ticks: int, rng: random.Random) -> np.ndarray:
    """Тики стоянки со случайно сдвинутыми границами: начало и конец на ±jitter_ticks от найденных, в пределах шкалы."""
    a = int(ticks_idx[0]) + rng.randint(-jitter_ticks, jitter_ticks)
    b = int(ticks_idx[-1]) + rng.randint(-jitter_ticks, jitter_ticks)
    a, b = max(0, a), min(len(track.group) - 1, b)
    return np.arange(a, b + 1) if b - a + 1 >= STABLE_TICKS else ticks_idx


def synthetic(track: Track, segs: dict[str, dict], allowed: set, just_s: float, target_ticks: int, rng: random.Random, stretch=(0.5, 2.0), jitter_s: float = 0.0) -> Track:
    """Склейки обучающих отрезков: стоянка -> переход из этого места -> стоянка в месте его конца ...; уровень следующего куска сдвигается
    на разницу медиан концов (не больше шума телефона), стык помечается; границы стоянки сдвинуты на ±jitter_s, метка сдвинутого куска - стоянка
    с этой границы; группа синтетики - «synth»."""
    stops = [s for g, s in segs.items() if g in allowed and s["kind"] == "stop"]
    legs = [s for g, s in segs.items() if g in allowed and s["kind"] == "leg"]
    noise = phone_noise(track, stops)
    jitter = int(round(jitter_s / TICK_S))
    xs, st, ar, rm, jn = [], [], [], [], []
    total = 0
    while total < target_ticks and stops:
        cur = rng.choice(stops)
        for n_piece in range(SYNTH_MAX_PIECES):
            if cur["kind"] == "stop":
                base = jittered_stop(track, cur["ticks"], jitter, rng) if jitter else cur["ticks"]
                idx = stretched_stop(track, base, rng.uniform(*stretch))
                states = np.array([state_of(i * TICK_S, just_s) for i in range(len(idx))])
                area_id, room_id = track.area[cur["ticks"][len(cur["ticks"]) // 2]], track.room[cur["ticks"][len(cur["ticks"]) // 2]]
                areas_, rooms_ = np.full(len(idx), area_id), np.full(len(idx), room_id)
            else:
                idx = cur["ticks"]
                states = np.zeros(len(idx), dtype=int)
                areas_, rooms_ = track.area[idx], track.room[idx]
            x = track.x[idx].copy()
            if xs:
                for k in range(len(PHONES)):
                    col = k * FEATURES_PER_PHONE
                    gap = float(np.median(xs[-1][-STABLE_TICKS:, col]) - np.median(x[:STABLE_TICKS, col]))
                    x[:, col] += float(np.clip(gap, -noise[k], noise[k]))
            flag = np.zeros(len(idx), dtype=bool)
            flag[0] = bool(xs)
            xs.append(x); st.append(states); ar.append(areas_); rm.append(rooms_); jn.append(flag)
            total += len(idx)
            if cur["kind"] == "stop":
                nxt = [l for l in legs if l["from"] == cur["place"]]
            else:
                nxt = [s for s in stops if s["place"] == cur["to"]]
            if not nxt:
                break
            cur = rng.choice(nxt)
    if not xs:
        return Track(np.zeros((0, track.x.shape[1]), dtype=np.float32), np.zeros(0, int), np.zeros(0, int), np.zeros(0, int), [], np.zeros(0, bool))
    n = sum(len(a) for a in ar)
    return Track(np.concatenate(xs), np.concatenate(st), np.concatenate(ar), np.concatenate(rm), ["synth"] * n, np.concatenate(jn))


def balanced_ends(track: Track, ends: np.ndarray, long_keep_s: float) -> np.ndarray:
    """Прореживание: «стоит давно» не больше long_keep_s секунд на стоянку, равномерно по её длине; остальное (ход, «только что») целиком."""
    keep = int(round(long_keep_s / TICK_S))
    out, long_by_group = [], {}
    for e in ends:
        if track.state[e] == 2:
            long_by_group.setdefault(track.group[e], []).append(e)
        else:
            out.append(e)
    for es in long_by_group.values():
        out += [es[int(i)] for i in np.linspace(0, len(es) - 1, min(keep, len(es)))]
    return np.array(sorted(set(out)), dtype=int)


def seconds_by_class(track: Track, ends: np.ndarray, names_state, names_area) -> dict:
    return {"state": {names_state[i]: round(float((track.state[ends] == i).sum() * TICK_S), 1) for i in range(len(names_state))},
            "area": {names_area[i]: round(float((track.area[ends] == i).sum() * TICK_S), 1) for i in range(len(names_area))}}


# ── модели ───────────────────────────────────────────────────────────────

def summary_features(windows: np.ndarray) -> np.ndarray:
    """Сводка окна для бустинга: по каждому признаку среднее, разброс, последнее, минимум, максимум."""
    return np.concatenate([windows.mean(1), windows.std(1), windows[:, -1], windows.min(1), windows.max(1)], axis=1)


def class_weights(labels: np.ndarray, n: int) -> np.ndarray:
    counts = np.bincount(labels, minlength=n).astype(np.float64)
    w = np.where(counts > 0, counts.sum() / np.maximum(counts, 1) / n, 0.0)
    return (w / w[w > 0].mean()).astype(np.float32)


def make_cnn(n_features: int, n_states: int, n_areas: int):
    import torch
    from torch import nn

    class TinyCnn(nn.Module):
        """Две свёртки по времени (ядро 3, вторая с прореживанием 2), сводка окна - среднее и последний шаг, две головы."""
        def __init__(self):
            super().__init__()
            self.body = nn.Sequential(nn.Conv1d(n_features, HIDDEN, 3, padding=1), nn.ReLU(), nn.Conv1d(HIDDEN, HIDDEN, 3, padding=2, dilation=2), nn.ReLU())
            self.state = nn.Linear(2 * HIDDEN, n_states)
            self.area = nn.Linear(2 * HIDDEN, n_areas)

        def forward(self, x):                    # x [batch, time, features]
            h = self.body(x.transpose(1, 2))
            z = torch.cat([h.mean(2), h[:, :, -1]], 1)
            return self.state(z), self.area(z)
    return TinyCnn()


def soft_targets(sets: list, n: int) -> np.ndarray:
    """Цель области: поровну по верным областям тика (у стоянки и пути - одна область, у зоны - все её)."""
    out = np.zeros((len(sets), n), dtype=np.float32)
    for i, s in enumerate(sets):
        out[i, s] = 1.0 / len(s)
    return out


def train_cnn(xw: np.ndarray, ys: np.ndarray, ya, n_states: int, n_areas: int, epochs: int, device: str, seed: int, init: dict | None = None):
    """ya - номера областей [N] или мягкие цели [N, n_areas]; состояние -1 не учится (зоны); init - веса кодировщика (body.*) после предобучения."""
    import torch
    torch.manual_seed(seed)
    model = make_cnn(xw.shape[2], n_states, n_areas).to(device)
    if init:
        model.load_state_dict(init, strict=False)
    opt = torch.optim.Adam(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
    soft = ya if ya.ndim == 2 else soft_targets([[int(a)] for a in ya], n_areas)
    ws = torch.tensor(class_weights(ys[ys >= 0], n_states), device=device)
    wa = torch.tensor(class_weights(soft.argmax(1), n_areas), device=device)
    x, s, a = torch.tensor(xw, device=device), torch.tensor(ys, device=device), torch.tensor(soft, device=device)
    loss_s = torch.nn.CrossEntropyLoss(weight=ws, ignore_index=-1)
    gen = torch.Generator(device="cpu").manual_seed(seed)
    for _ in range(epochs):
        order = torch.randperm(len(x), generator=gen).to(device)
        for b in range(0, len(x), BATCH):
            i = order[b:b + BATCH]
            out_s, out_a = model(x[i])
            target = a[i]
            area_loss = -(target * torch.log_softmax(out_a, 1) * wa).sum(1).mean()
            loss = area_loss + (loss_s(out_s, s[i]) if (s[i] >= 0).any() else 0.0)
            opt.zero_grad(); loss.backward(); opt.step()
    return model.eval()


def predict_cnn(model, xw: np.ndarray, device: str) -> tuple[np.ndarray, np.ndarray]:
    import torch
    with torch.no_grad():
        out_s, out_a = model(torch.tensor(xw, device=device))
    return out_s.argmax(1).cpu().numpy(), out_a.argmax(1).cpu().numpy()


def train_boost(xw, ys, ya, seed):
    from sklearn.ensemble import HistGradientBoostingClassifier
    f = summary_features(xw)
    known = ys >= 0
    ms = HistGradientBoostingClassifier(max_iter=150, learning_rate=0.08, max_leaf_nodes=15, class_weight="balanced", random_state=seed).fit(f[known], ys[known])
    ma = HistGradientBoostingClassifier(max_iter=150, learning_rate=0.08, max_leaf_nodes=15, class_weight="balanced", random_state=seed).fit(f, ya)
    return ms, ma


# ── проверка ─────────────────────────────────────────────────────────────

def group_folds(groups: list[str], k: int, seed: int) -> list[set]:
    g = sorted(groups)
    random.Random(seed).shuffle(g)
    return [set(g[i::k]) for i in range(k)]


def settle_delay(track: Track, ends: np.ndarray, pred_area: np.ndarray, pred_room: np.ndarray, stop_groups: set) -> dict:
    """Через сколько секунд от начала отложенной стоянки ответ области (и комнаты) верен STABLE_TICKS тиков подряд; не установился - в счёт failed."""
    by_end = {int(e): (int(pa), int(pr)) for e, pa, pr in zip(ends, pred_area, pred_room)}
    out = {"area": [], "room": [], "area_failed": 0, "room_failed": 0}
    for g in stop_groups:
        idx = [i for i, gg in enumerate(track.group) if gg == g and i in by_end]
        for key, col, truth in (("area", 0, track.area), ("room", 1, track.room)):
            ok = [by_end[i][col] == truth[i] for i in idx]
            first = next((j for j in range(len(ok) - STABLE_TICKS + 1) if all(ok[j:j + STABLE_TICKS])), None)
            if first is None:
                out[f"{key}_failed"] += 1
            else:
                out[key].append(round(first * TICK_S, 1))
    return out


def confusion(truth: np.ndarray, pred: np.ndarray, n: int) -> list[list[int]]:
    m = np.zeros((n, n), dtype=int)
    for t, p in zip(truth, pred):
        m[t, p] += 1
    return m.tolist()


def evaluate(track, segs, areas, rooms, length, folds, model_kind, epochs, device, just_s, synth: bool, area_room: np.ndarray, balance_s: float | None = None,
             jitter_s: float = 0.0) -> dict:
    """Предсказания по всем отложенным окнам по разбиениям; метрики обоих выходов, задержка на стоянках, путаница."""
    truth_s, truth_a, truth_r, pred_s, pred_a, delays = [], [], [], [], [], {"area": [], "room": [], "area_failed": 0, "room_failed": 0}
    n_train = []
    for fold, held in enumerate(folds):
        train_groups = set(segs) - held
        tr = window_ends(track, length, allowed=train_groups, excluded=held)
        if balance_s is not None:
            tr = balanced_ends(track, tr, balance_s)
        te = window_ends(track, length, allowed=held)
        xw, ys, ya = stack(track, tr, length), track.state[tr], track.area[tr]
        if synth:
            sy = synthetic(track, segs, train_groups, just_s, int(len(tr) * SYNTH_ROUTES_PER_TICK), random.Random(SEED + fold), jitter_s=jitter_s)
            se = window_ends(sy, length)
            if balance_s is not None:
                sy.group = [f"synth:{i // max(1, int(balance_s / TICK_S))}" for i in range(len(sy.group))]
                se = balanced_ends(sy, se, balance_s)
            xw = np.concatenate([xw, stack(sy, se, length)]); ys = np.concatenate([ys, sy.state[se]]); ya = np.concatenate([ya, sy.area[se]])
        n_train.append(len(xw))
        xt = stack(track, te, length)
        if model_kind == "cnn":
            m = train_cnn(xw, ys, ya, len(STATES), len(areas), epochs, device, SEED + fold)
            ps, pa = predict_cnn(m, xt, device)
        else:
            ms, ma = train_boost(xw, ys, ya, SEED + fold)
            ps, pa = ms.predict(summary_features(xt)), ma.predict(summary_features(xt))
        truth_s.append(track.state[te]); truth_a.append(track.area[te]); truth_r.append(track.room[te]); pred_s.append(ps); pred_a.append(pa)
        stop_groups = {g for g in held if segs[g]["kind"] == "stop"}
        d = settle_delay(track, te, pa, area_room[pa], stop_groups)
        for k in ("area", "room"):
            delays[k] += d[k]; delays[f"{k}_failed"] += d[f"{k}_failed"]
    ts, ta, tr_, ps, pa = (np.concatenate(v) for v in (truth_s, truth_a, truth_r, pred_s, pred_a))
    pr = area_room[pa]
    return {"windows_test": int(len(ts)), "windows_train_mean": int(np.mean(n_train)),
            "state_acc": round(float((ps == ts).mean()), 3), "area_acc": round(float((pa == ta).mean()), 3), "room_acc": round(float((pr == tr_).mean()), 3),
            "both_acc": round(float(((ps == ts) & (pa == ta)).mean()), 3),
            "stop_area_acc": round(float((pa == ta)[ts > 0].mean()), 3) if (ts > 0).any() else None,
            "moving_area_acc": round(float((pa == ta)[ts == 0].mean()), 3) if (ts == 0).any() else None,
            "delay_area_s_median": statistics.median(delays["area"]) if delays["area"] else None, "delay_area_failed": delays["area_failed"],
            "delay_room_s_median": statistics.median(delays["room"]) if delays["room"] else None, "delay_room_failed": delays["room_failed"],
            "state_confusion": confusion(ts, ps, len(STATES)), "_area_pairs": (ta, pa)}


def ring_pairs(ta, pa, areas, pairs) -> dict:
    idx = {a: i for i, a in enumerate(areas)}
    out = {}
    for a, b in pairs:
        if a in idx and b in idx:
            ia, ib = idx[a], idx[b]
            out[f"{a} -> {b}"] = int(((ta == ia) & (pa == ib)).sum()); out[f"{b} -> {a}"] = int(((ta == ib) & (pa == ia)).sum())
            out[f"{a} всего"] = int((ta == ia).sum()); out[f"{b} всего"] = int((ta == ib).sum())
    return out


# ── вывод на CPU ─────────────────────────────────────────────────────────

def export_weights(model, path: Path) -> dict[str, np.ndarray]:
    """Веса свёртки в .npz для вывода без torch."""
    sd = {k: v.detach().cpu().numpy() for k, v in model.state_dict().items()}
    w = {"c1_w": sd["body.0.weight"], "c1_b": sd["body.0.bias"], "c2_w": sd["body.2.weight"], "c2_b": sd["body.2.bias"],
         "s_w": sd["state.weight"], "s_b": sd["state.bias"], "a_w": sd["area.weight"], "a_b": sd["area.bias"]}
    np.savez(path, **w)
    return w


def _conv(x: np.ndarray, w: np.ndarray, b: np.ndarray, pad: int, dil: int) -> np.ndarray:
    """Свёртка по времени: x [T, I], w [O, I, K] -> [T, O], нули по краям, как Conv1d."""
    xp = np.pad(x, ((pad, pad), (0, 0)))
    return sum(xp[k * dil:k * dil + len(x)] @ w[:, :, k].T for k in range(w.shape[2])) + b


def numpy_forward(w: dict[str, np.ndarray], x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Вывод одного окна x [T, F] на numpy: логиты состояния и области."""
    h = np.maximum(_conv(x, w["c1_w"], w["c1_b"], 1, 1), 0)
    h = np.maximum(_conv(h, w["c2_w"], w["c2_b"], 2, 2), 0)
    z = np.concatenate([h.mean(0), h[-1]])
    return w["s_w"] @ z + w["s_b"], w["a_w"] @ z + w["a_b"]


def export_and_time(model, length: int, n_features: int, path: Path) -> dict:
    """Веса в .npz, сверка numpy с torch, время одного окна на numpy на CPU, число параметров."""
    import torch
    model = model.to("cpu").eval()
    w = export_weights(model, path)
    x = np.random.default_rng(0).normal(size=(length, n_features)).astype(np.float32)
    with torch.no_grad():
        ts, ta = model(torch.tensor(x[None]))
    ns, na = numpy_forward(w, x)
    match = float(max(np.abs(ns - ts.numpy()[0]).max(), np.abs(na - ta.numpy()[0]).max()))
    for _ in range(50):
        numpy_forward(w, x)
    t0 = time.perf_counter()
    for _ in range(TIMING_CALLS):
        numpy_forward(w, x)
    per = (time.perf_counter() - t0) / TIMING_CALLS
    params = sum(p.numel() for p in model.parameters())
    return {"params": int(params), "npz_bytes": path.stat().st_size, "weights_kb": round(params * 4 / 1024, 1),
            "cpu_ms_per_window": round(per * 1000, 3), "numpy_vs_torch_max_diff": round(match, 6)}


def time_boost(xw, ys, ya, length) -> dict:
    ms, ma = train_boost(xw, ys, ya, SEED)
    f = summary_features(xw[:1])
    for _ in range(20):
        ms.predict(f); ma.predict(f)
    t0 = time.perf_counter()
    for _ in range(200):
        ms.predict(f); ma.predict(f)
    nodes = sum(len(p.nodes) for m in (ms, ma) for pr in m._predictors for p in pr)
    return {"tree_nodes": int(nodes), "cpu_ms_per_window": round((time.perf_counter() - t0) / 200 * 1000, 3)}


def walk_metrics(track: Track, ends: np.ndarray, ps: np.ndarray, pa: np.ndarray, area_room: np.ndarray, kinds: dict) -> dict:
    """Доли верного на окнах отложенной проходки: состояние (где известно), область и комната по наборам, по видам отрезков."""
    ok_area = np.array([int(a) in track.area_sets[e] for e, a in zip(ends, pa)])
    ok_room = np.array([int(area_room[a]) in track.room_sets[e] for e, a in zip(ends, pa)])
    st = track.state[ends]
    known = st >= 0
    kind = np.array([kinds.get(track.group[e], "?") for e in ends])
    out = {"windows": int(len(ends)), "state_acc": round(float((ps[known] == st[known]).mean()), 3) if known.any() else None,
           "area_acc": round(float(ok_area.mean()), 3), "room_acc": round(float(ok_room.mean()), 3)}
    for k in ("stop", "leg", "zone"):
        m = kind == k
        if m.any():
            out[f"{k}_area_acc"] = round(float(ok_area[m].mean()), 3)
            out[f"{k}_room_acc"] = round(float(ok_room[m].mean()), 3)
    out["state_confusion"] = confusion(st[known], ps[known], len(STATES)) if known.any() else None
    return out


def majority(train_tracks: list[Track], test: Track, ends: np.ndarray, area_room: np.ndarray, n_areas: int) -> dict:
    """Всегда самый частый класс обучающих проходок: область и её комната."""
    counts = np.bincount(np.concatenate([t.area[t.area >= 0] for t in train_tracks]), minlength=n_areas)
    a = int(counts.argmax())
    return {"area": a, "area_acc": round(float(np.mean([a in test.area_sets[e] for e in ends])), 3),
            "room_acc": round(float(np.mean([int(area_room[a]) in test.room_sets[e] for e in ends])), 3)}


def main() -> None:
    ap = argparse.ArgumentParser(description="мини-модель состояния и области по короткой серии уровня; проверка по проходкам")
    ap.add_argument("--data", type=Path, default=Path("data/walks/nn"))
    ap.add_argument("--tag", required=True)
    ap.add_argument("--windows", type=float, nargs="+", default=[2, 3, 5, 8])
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--device", default="auto")
    args = ap.parse_args()
    import torch
    device = ("cuda" if torch.cuda.is_available() else "cpu") if args.device == "auto" else args.device
    ticks, summary, classes = load(args.data, args.tag)
    just = float(classes.get("just_stopped_s", 5.0))
    areas = sorted({a for t in ticks if t["area"] is not None for a in (t.get("area_set") or [t["area"]])})
    rooms = sorted({r for t in ticks if t["room"] is not None for r in (t.get("room_set") or [t["room"]])})
    cr = classes.get("class_room") or {}
    area_room = np.array([rooms.index(cr.get(a, a)) if cr.get(a, a) in rooms else 0 for a in areas])
    walks = sorted({t["walk"] for t in ticks})
    tracks = {w: real_track([t for t in ticks if t["walk"] == w], areas, rooms, just) for w in walks}
    kinds = {s["group"]: s["kind"] for s in summary["segments"]}
    results = {"device_train": device, "areas": areas, "rooms": rooms, "states": STATES, "epochs": args.epochs, "walks": walks,
               "ticks_by_walk": {w: len(tracks[w].group) for w in walks}, "runs": []}
    started = time.time()
    for test in walks:
        train = [w for w in walks if w != test]
        for w in args.windows:
            length = int(round(w / TICK_S))
            tr_parts = [(tracks[x], window_ends(tracks[x], length)) for x in train]
            xw = np.concatenate([stack(tk, e, length) for tk, e in tr_parts])
            ys = np.concatenate([tk.state[e] for tk, e in tr_parts])
            ya_soft = np.concatenate([soft_targets([tk.area_sets[i] for i in e], len(areas)) for tk, e in tr_parts])
            ya_hard = np.concatenate([tk.area[e] for tk, e in tr_parts])
            te = window_ends(tracks[test], length)
            xt = stack(tracks[test], te, length)
            base = majority([tracks[x] for x in train], tracks[test], te, area_room, len(areas))
            for kind in ("boost", "cnn"):
                if kind == "cnn":
                    m = train_cnn(xw, ys, ya_soft, len(STATES), len(areas), args.epochs, device, SEED)
                    ps, pa = predict_cnn(m, xt, device)
                else:
                    ms, ma = train_boost(xw, ys, ya_hard, SEED)
                    f = summary_features(xt)
                    ps, pa = ms.predict(f), ma.predict(f)
                r = walk_metrics(tracks[test], te, ps, pa, area_room, kinds)
                stop_groups = {g for g in set(tracks[test].group) if g and kinds.get(g) == "stop"}
                d = settle_delay(tracks[test], te, pa, area_room[pa], stop_groups)
                r.update(test=test, train=train, window_s=w, model=kind, majority=base,
                         delay_area_s_median=statistics.median(d["area"]) if d["area"] else None, delay_area_failed=d["area_failed"],
                         delay_room_s_median=statistics.median(d["room"]) if d["room"] else None, delay_room_failed=d["room_failed"], stops=len(stop_groups))
                results["runs"].append(r)
                print(f"проверка {test}, окно {w} с, {kind:5s}: состояние {r['state_acc']} область {r['area_acc']} комната {r['room_acc']} "
                      f"(частый класс {base['area_acc']}/{base['room_acc']}) стоянки {r.get('stop_area_acc')} путь {r.get('leg_area_acc')} зоны {r.get('zone_area_acc')}/{r.get('zone_room_acc')}, "
                      f"{time.time() - started:.0f} с", flush=True)
    results["cpu"] = []
    all_parts = [(tracks[x], window_ends(tracks[x], int(round(w / TICK_S)))) for x in walks for w in [args.windows[0]]]
    for w in args.windows:
        length = int(round(w / TICK_S))
        parts = [(tracks[x], window_ends(tracks[x], length)) for x in walks]
        xw = np.concatenate([stack(tk, e, length) for tk, e in parts])
        ys = np.concatenate([tk.state[e] for tk, e in parts])
        ya_soft = np.concatenate([soft_targets([tk.area_sets[i] for i in e], len(areas)) for tk, e in parts])
        m = train_cnn(xw, ys, ya_soft, len(STATES), len(areas), args.epochs, device, SEED)
        results["cpu"].append({"window_s": w, "cnn": export_and_time(m, length, xw.shape[2], args.data / f"model-{w:g}s-{args.tag}.npz")})
        print(f"CPU окно {w} с: {results['cpu'][-1]['cnn']}", flush=True)
    (args.data / f"results-walks-{args.tag}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"готово за {time.time() - started:.0f} с: {args.data / f'results-walks-{args.tag}.json'}")


if __name__ == "__main__":
    main()
