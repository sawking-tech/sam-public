"""Ручная вероятность человека для отладки радара (SAM-105): ползунок в панели ставит уверенность прямо.

Переопределение ложится поверх сведённых
свидетельств: уверенность берётся из ползунка, место - из выбранной точки плана или из того, что радар уже знал.
Ноль убирает человека. Чистый модуль: хранилище и сеть в хабе.
"""
from __future__ import annotations

SOURCE = "debug"          # метка в sources: на странице видно, что уверенность задана вручную


def apply(identities: list[dict], overrides: dict[str, dict], spots: dict[str, dict]) -> list[dict]:
    """Идентичности с учётом ручных вероятностей; `overrides` - человек -> {confidence, place}."""
    rows = {row["person"]: dict(row) for row in identities}
    for person, o in overrides.items():
        confidence = o["confidence"]
        row = rows.get(person)
        spot = spots.get(o.get("place") or "")
        if confidence <= 0:
            rows.pop(person, None)
            continue
        if row is None:
            if not spot:
                continue                                  # места нет ни у радара, ни в выборе: ставить человека некуда
            row = {"person": person, "pose": spot.get("pose"), "face": None, "activity": 0.0, "sources": [], "notes": []}
        if spot:
            row.update(x=spot["x"], z=spot["z"], pose=spot.get("pose") or row.get("pose"), face=spot.get("face"))   # взгляд прежнего места не годится: хаб достроит по окружению
        row["confidence"] = round(confidence, 3)
        row["sources"] = sorted({*row.get("sources", []), SOURCE})
        rows[person] = row
    return sorted(rows.values(), key=lambda r: -r["confidence"])
