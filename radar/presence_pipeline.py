"""Цепочка обработки людей радара (SAM-105): список именованных шагов `rows, now -> rows`, собранный в одном месте.

Шаг знает только свою работу; порядок и состав - в хабе одним списком. Трасса (необязательная) записывает, какой шаг что изменил у каждого человека.
Чистый модуль: сети, часов и файлов нет.
"""
from __future__ import annotations

import math
from typing import Callable

Step = Callable[[list[dict], float], list[dict]]
POS_EPS_M = 0.02
FACE_EPS_RAD = 0.02
PROB_EPS = 0.005


def _snapshot(row: dict) -> tuple:
    return (row.get("x"), row.get("z"), row.get("pose"), row.get("face"), row.get("confidence"))


def _xy(x, z) -> str:
    return "нет" if x is None else f"({x:.2f}; {z:.2f})"


def _deg(a) -> str:
    return "нет" if a is None else f"{math.degrees(a):.0f} град"


def _changes(before: tuple, after: tuple) -> list[str]:
    (bx, bz, bp, bf, bc), (ax, az, ap, af, ac) = before, after
    out = []
    if (bx is None) != (ax is None) or (ax is not None and math.hypot(ax - bx, az - bz) > POS_EPS_M):
        out.append(f"место {_xy(bx, bz)} -> {_xy(ax, az)}")
    if bp != ap:
        out.append(f"поза {bp} -> {ap}")
    if (bf is None) != (af is None) or (af is not None and abs(math.atan2(math.sin(af - bf), math.cos(af - bf))) > FACE_EPS_RAD):
        out.append(f"взгляд {_deg(bf)} -> {_deg(af)}")
    if bc is not None and ac is not None and abs(ac - bc) > PROB_EPS:
        out.append(f"вероятность {bc:.2f} -> {ac:.2f}")
    return out


def run(steps: list[tuple[str, Step]], rows: list[dict], now: float, trace: dict[str, list[str]] | None = None) -> list[dict]:
    """Прогнать строки через шаги по порядку; если дан словарь `trace`, в нём по людям копится «шаг: что изменил»."""
    for name, step in steps:
        before = {r["person"]: _snapshot(r) for r in rows} if trace is not None else {}
        rows = step(rows, now)
        if trace is None:
            continue
        for r in rows:
            prev = before.get(r["person"])
            notes = [f"появился {_xy(r.get('x'), r.get('z'))}"] if prev is None else _changes(prev, _snapshot(r))
            if notes:
                trace.setdefault(r["person"], []).append(f"{name}: " + ", ".join(notes))
        for gone in set(before) - {r["person"] for r in rows}:
            trace.setdefault(gone, []).append(f"{name}: убран")
    return rows
