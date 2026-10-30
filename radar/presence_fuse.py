"""Слияние свидетельств «кто где» (SAM-105): каналы дают свидетельства, модуль сводит их в оценку по человеку.

Вывод строится из свидетельств разной силы: чем больше данных, тем больше уверенность;
призрак человека не исчезает, пока есть хоть один признак, по которому видно, кто где. Каналы (сообщения, Wi-Fi,
позже кадры веб-камер) знают только свой формат и отдают общий `Evidence`; сюда они не заходят. Модуль чистый:
время приходит параметром, ничего не читает и не пишет.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

HALF_LIFE_S = 300.0          # через столько секунд свидетельство вдвое слабее («старое устаревает, минуты»)
POSITION_MIN_CONF = 0.08      # слабее этой уверенности координат у человека нет: тень в 3% не должна ходить по квартире и садиться рядом: у неё нет места, и ходьба её не ведёт
CLUSTER_M = 1.5              # свидетельства ближе этого друг к другу - одно место; дальше - разные места, и человек в одном из них, а не между
STICK_BONUS = 1.8            # место, где человек уже стоит, получает такой множитель веса: на другое он уходит, только когда оно заметно тяжелее (слова и уровень телефона не должны гонять его туда-сюда)
FORGET_WEIGHT = 0.02         # слабее после затухания - свидетельство не считается; при весе 0.6 это около 25 минут


@dataclass(frozen=True)
class Evidence:
    source: str                    # канал: messages, wifi, camera; независимыми считаются разные каналы
    person: str | None             # кто; None - человек неизвестен, известно только место
    weight: float                  # 0..1 на момент t
    t: float
    x: float | None = None         # где, в метрах плана; None - место неизвестно, знаем только кто
    z: float | None = None
    pose: str | None = None        # stand, sit, lie - условная, по месту
    face: float | None = None      # куда повёрнут, радианы в осях плана (0 - к +x): сидящий лицом к столу, лежащий вдоль кровати
    note: str = ""                 # что именно увидели, для подписи на странице
    activity: float | None = None  # дрожание линии устройства 0..1: живость силуэта, сам сигнал Wi-Fi рисуется человеком
    half_life_s: float | None = None   # свой период затухания: слова человека живут часы, сообщение с устройства - минуты
    away: bool = False             # свидетельство «против»: признак, что человек ушёл (его устройство пропало из сети); снижает уверенность, не повышает


class EvidenceSource(Protocol):
    def evidence(self, now: float) -> list[Evidence]: ...


@dataclass
class Estimate:
    person: str
    confidence: float
    x: float | None
    z: float | None
    pose: str | None
    face: float | None
    activity: float
    sources: list[str]
    notes: list[str]
    evidence: list[dict] = field(default_factory=list)   # свидетельства с местом: x, z, вес на сейчас, канал, пометка; для слоя «поле человека» на странице


def decayed(e: Evidence, now: float, half_life_s: float = HALF_LIFE_S) -> float:
    return e.weight * 0.5 ** (max(0.0, now - e.t) / (e.half_life_s or half_life_s))


def combine(weights: list[float]) -> float:
    """Вероятность, что верно хотя бы одно из независимых свидетельств: 1 - произведение (1 - вес)."""
    return 1.0 - math.prod(1.0 - w for w in weights)


def _heaviest_place(placed: list[tuple["Evidence", float]], held: tuple[float, float] | None = None) -> list[tuple["Evidence", float]]:
    """Свидетельства того места, у которого суммарный вес больше: разные места не усредняются (среднее диван + зал давало точку в стене), берётся одно.

    Места собираются жадно по убыванию веса: свидетельство входит в место, чей центр не дальше CLUSTER_M. При равенстве веса выигрывает более свежее."""
    clusters: list[list[tuple[Evidence, float]]] = []
    for item in sorted(placed, key=lambda r: -r[1]):
        for cluster in clusters:
            if math.hypot(item[0].x - cluster[0][0].x, item[0].z - cluster[0][0].z) <= CLUSTER_M:
                cluster.append(item)
                break
        else:
            clusters.append([item])
    def score(cluster):
        near_held = held is not None and math.hypot(cluster[0][0].x - held[0], cluster[0][0].z - held[1]) <= CLUSTER_M
        return (sum(w for _, w in cluster) * (STICK_BONUS if near_held else 1.0), max(e.t for e, _ in cluster))
    return max(clusters, key=score, default=[])


def fuse(evidences: list[Evidence], now: float, half_life_s: float = HALF_LIFE_S, previous: dict[str, tuple[float, float]] | None = None) -> list[Estimate]:
    """Оценки по людям, самые уверенные первыми. Свидетельства без человека в оценку не входят: их место
    принадлежит тому, кто объявится рядом, это решает вызывающий."""
    by_person: dict[str, list[tuple[Evidence, float]]] = {}
    away: dict[str, list[float]] = {}
    for e in evidences:
        w = decayed(e, now, half_life_s)
        if e.person and w >= FORGET_WEIGHT:
            (away.setdefault(e.person, []).append(w) if e.away else by_person.setdefault(e.person, []).append((e, w)))
    out = []
    for person, rows in by_person.items():
        strongest: dict[str, float] = {}              # повтор одного канала не складывается: берём сильнейшее
        for e, w in rows:
            strongest[e.source] = max(strongest.get(e.source, 0.0), w)
        placed = _heaviest_place([(e, w) for e, w in rows if e.x is not None and e.z is not None], (previous or {}).get(person))
        total = sum(w for _, w in placed)
        own = placed or rows                          # поза и взгляд - от свидетельств выбранного места: «сидит» от стола не садит человека на кровать
        posed = max((r for r in own if r[0].pose), key=lambda r: r[1], default=None)
        faced = max((r for r in own if r[0].face is not None), key=lambda r: r[1], default=None)
        est = Estimate(
            person=person, confidence=combine(list(strongest.values())) * (1.0 - combine(away.get(person, []))),     # противоречие снижает, не выбирает одно
            x=sum(e.x * w for e, w in placed) / total if total else None,
            z=sum(e.z * w for e, w in placed) / total if total else None,
            pose=posed[0].pose if posed else None, face=faced[0].face if faced else None,
            activity=max((e.activity or 0.0 for e, _ in rows), default=0.0),
            sources=sorted(strongest), notes=[e.note for e, _ in sorted(rows, key=lambda r: -r[1]) if e.note],
            evidence=[{"x": e.x, "z": e.z, "w": round(w, 3), "source": e.source, "note": e.note} for e, w in sorted(rows, key=lambda r: -r[1]) if e.x is not None and e.z is not None])
        if est.confidence < POSITION_MIN_CONF:
            est.x = est.z = est.pose = est.face = None                 # уверенность ниже порога: кто-то есть, где - не говорим
        out.append(est)
    return sorted(out, key=lambda est: -est.confidence)
