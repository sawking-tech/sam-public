"""Мини-сеть радара как свидетельство потока положения (SAM-118): маленький GRU по короткой серии опросов станции даёт вероятности областей.

Сеть своя на каждый диапазон (телефоны переносятся между проходками, диапазоны - нет), веса и описание лежат в data/ (`SAM_PRESENCE_NN_DIR`):
`gru-<интерфейс>.npz` (веса GRU и головы) и `gru-<интерфейс>.json` (интерфейс, области, их доли в обучении, длина окна в опросах, нормировка
признаков, таблица областей плана: ключевые места и участки коридора). Вывод - чистый numpy, torch не нужен.
В окне потока сеть - фактор-множитель: клетке области достаётся (вероятность области / её доля в обучении) в степени веса (`SAM_PRESENCE_NN_WEIGHT`),
клеткам вне известных областей - единица. Флаг `SAM_PRESENCE_NN=1`; без флага поток собирается как раньше.
Признаки опроса те же, что в обучении (scripts/radar_nn_train.py): уровень, свежесть, возраст кадра, разница антенн, подтверждения, скорости, диапазон."""
from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np

from .presence_factors import Context

NN_ENV = "SAM_PRESENCE_NN"
NN_DIR_ENV = "SAM_PRESENCE_NN_DIR"
DEFAULT_NN_DIR = "data/walks/nn"
NN_WEIGHT_ENV = "SAM_PRESENCE_NN_WEIGHT"
DEFAULT_NN_WEIGHT = 0.2          # степень множителя в срезе окна: рядом с кольцом (вес кольца 0,3), сеть подсказывает, а не решает
FRESH_MS = 1000                  # кадр старше - уровень устаревший, как в обучении
BAND_5GHZ = "phy1-ap0"
RATIO_FLOOR = 1e-3               # нижний предел отношения вероятности к доле класса: логарифм не уходит в минус бесконечность
OUTSIDE = None
DOORWAY_REACH_M = 0.5            # точка в проёме между комнатами отходит ближайшей комнате, как в разметке набора


@dataclass(frozen=True)
class Norm:
    db_zero: float
    db_scale: float
    db_missing: float
    age_cap_s: float
    rate_log_scale: float


class Features:
    """Ряд признаков одной станции на каждый опрос с переносом последнего известного (роутер держит последнее значение)."""

    def __init__(self, norm: Norm, length: int) -> None:
        self._norm = norm
        self.rows: deque = deque(maxlen=length)
        self._last = {"db": norm.db_missing, "ant": 0.0, "ack": norm.db_missing, "tx": 0.0, "rx": 0.0}

    def push(self, signal_db: float, inactive_ms: int, iface: str, link) -> None:
        n, last = self._norm, self._last
        last["db"] = signal_db
        if link is not None:
            if len(link.avg_chains) >= 2:
                last["ant"] = link.avg_chains[0] - link.avg_chains[1]
            acks = [v for v in (link.ack_last_db, link.ack_avg_db) if v is not None]
            if acks:
                last["ack"] = sum(acks) / len(acks)
            last["tx"] = link.tx.mbit or last["tx"]
            last["rx"] = link.rx.mbit or last["rx"]
        age = min(inactive_ms / 1000.0, n.age_cap_s)
        self.rows.append([(last["db"] - n.db_zero) / n.db_scale, 1.0 if inactive_ms < FRESH_MS else 0.0, age / n.age_cap_s, last["ant"] / n.db_scale,
                          (last["ack"] - n.db_zero) / n.db_scale, math.log1p(last["tx"]) / n.rate_log_scale, math.log1p(last["rx"]) / n.rate_log_scale,
                          1.0 if iface == BAND_5GHZ else 0.0])

    def window(self) -> np.ndarray | None:
        return np.asarray(self.rows, dtype=np.float32) if len(self.rows) == self.rows.maxlen else None


class GruModel:
    """GRU одного диапазона на numpy: окно признаков [T, F] -> вероятности областей."""

    def __init__(self, weights: Mapping[str, np.ndarray], areas: Sequence[str], priors: Sequence[float], length: int, norm: Norm, classes: Mapping | None = None) -> None:
        self.w, self.areas, self.priors, self.length, self.norm = dict(weights), list(areas), np.asarray(priors, dtype=float), int(length), norm
        self.classes = dict(classes or {})                # ключевые места и участки коридора, по которым размечали обучение

    @classmethod
    def load(cls, npz: Path, meta: Path) -> "GruModel":
        m = json.loads(meta.read_text(encoding="utf-8"))
        with np.load(npz) as w:
            weights = {k: w[k] for k in w.files}
        return cls(weights, m["areas"], m["priors"], m["window_ticks"], Norm(**m["norm"]), m.get("classes"))

    def probabilities(self, x: np.ndarray) -> np.ndarray:
        w = self.w
        h = np.zeros(w["w_hh"].shape[1])
        sig = lambda v: 1.0 / (1.0 + np.exp(-v))
        for xt in x:
            gi, gh = w["w_ih"] @ xt + w["b_ih"], w["w_hh"] @ h + w["b_hh"]
            ir, iz, i_n = np.split(gi, 3)
            hr, hz, h_n = np.split(gh, 3)
            r, z = sig(ir + hr), sig(iz + hz)
            h = (1 - z) * np.tanh(i_n + r * h_n) + z * h
        logits = w["a_w"] @ h + w["a_b"]
        e = np.exp(logits - logits.max())
        return e / e.sum()


def area_of_point(classes: Mapping, rooms: Sequence[Mapping], x: float, z: float) -> str | None:
    """Область точки плана: ключевое место, иначе участок коридора (круги из таблицы классов), иначе комната, иначе ближайшая комната у проёма."""
    for key in ("spots", "corridor"):
        hit = next((a["name"] for a in classes.get(key) or [] if math.dist((a["x"], a["z"]), (x, z)) <= a["r"]), None)
        if hit:
            return hit
    inside = next((r["title"] for r in rooms if r["x"] <= x <= r["x"] + r["w"] and r["z"] <= z <= r["z"] + r["d"]), None)
    if inside:
        return inside
    gap = lambda r: math.hypot(max(r["x"] - x, 0.0, x - r["x"] - r["w"]), max(r["z"] - z, 0.0, z - r["z"] - r["d"]))
    best = min(rooms, key=gap, default=None)
    return best["title"] if best is not None and gap(best) <= DOORWAY_REACH_M else OUTSIDE


def cell_areas(points: np.ndarray, model_areas: Sequence[str], classes: Mapping, rooms: Sequence[Mapping]) -> np.ndarray:
    """Номер области сети для каждой клетки мира; клетка вне областей сети - -1."""
    index = {a: i for i, a in enumerate(model_areas)}
    return np.array([index.get(area_of_point(classes, rooms, float(x), float(z)), -1) for x, z in points], dtype=int)


class NnFactor:
    """Фактор окна: вес * log(вероятность области / её доля в обучении) на клетках области, ноль вне областей; вероятностей нет - молчит."""

    def __init__(self, probabilities: Callable[[], np.ndarray | None], priors: np.ndarray, cells: np.ndarray, weight: float = DEFAULT_NN_WEIGHT) -> None:
        self._probs, self._priors, self._cells, self._weight = probabilities, np.asarray(priors, dtype=float), cells, weight

    def log_field(self, context: Context) -> np.ndarray | None:
        p = self._probs()
        if p is None:
            return None
        ratio = np.log(np.maximum(p / np.maximum(self._priors, RATIO_FLOOR), RATIO_FLOOR))
        out = np.zeros(len(self._cells))
        known = self._cells >= 0
        out[known] = self._weight * ratio[self._cells[known]]
        return out


class NnBundle:
    """Сети по диапазонам и общий вес; собирает признаки станций и отдаёт фактор для окна станции."""

    def __init__(self, models: Mapping[str, GruModel], rooms: Sequence[Mapping], weight: float = DEFAULT_NN_WEIGHT) -> None:
        self.models, self.rooms, self.weight = dict(models), rooms, weight

    @classmethod
    def load(cls, folder: Path, rooms: Sequence[Mapping], weight: float) -> "NnBundle":
        models = {}
        for meta in sorted(folder.glob("gru-*.json")):
            npz = meta.with_suffix(".npz")
            band = json.loads(meta.read_text(encoding="utf-8")).get("iface")
            if band and npz.exists():
                models[band] = GruModel.load(npz, meta)
        return cls(models, rooms, weight)

    def features_for(self, iface: str) -> Features | None:
        m = self.models.get(iface)
        return Features(m.norm, m.length) if m else None

    def factor_for(self, iface: str, points: np.ndarray, probabilities: Callable[[], np.ndarray | None]) -> NnFactor | None:
        m = self.models.get(iface)
        return NnFactor(probabilities, m.priors, cell_areas(points, m.areas, m.classes, self.rooms), self.weight) if m else None

    def model(self, iface: str) -> GruModel | None:
        return self.models.get(iface)
