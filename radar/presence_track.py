"""Трекинг радара присутствия (SAM-105): габариты облака, класс «человек или питомец», профили, стабильные id.

«кошек тоже,
если получится», «автоматически и эффективно». Модуль чистый и лёгкий: чистый Python, на кадр один проход по
парам «трек - обнаружение» (людей дома единицы). Профиль человека копится из подтверждённых наблюдений (голос
или устройство), поэтому по габаритам потом узнаётся и без подтверждения; это подсказка, а не опознание.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import dist

PERSON = "person"
PET = "pet"
PET_MAX_HEIGHT_M = 0.6        # выше - человек; кот на всех четырёх, даже в потягушке, ниже
MIN_HEIGHT_M = 0.15           # ниже - шум, не объект
MAX_HEIGHT_M = 2.3            # выше - артефакт оценки, не человек
MAX_SPEED_MPS = {PERSON: 3.0, PET: 5.0}   # быстрее трек не перепрыгивает (бег человека 3, кошка срывается быстрее)
LOST_S = 8.0                  # трек без обнаружений живёт столько и ищется по габаритам с растущим радиусом
HEIGHT_TOL_M = 0.08           # допуск по росту при сопоставлении с профилем и между кадрами
WIDTH_TOL_M = 0.12            # допуск по ширине
PROFILE_ALPHA = 0.1           # вес нового наблюдения в скользящем среднем профиля: габарит меняется медленно


@dataclass(frozen=True)
class Measure:
    height: float
    width: float
    x: float
    z: float

    @property
    def kind(self) -> str | None:
        if not MIN_HEIGHT_M <= self.height <= MAX_HEIGHT_M:
            return None
        return PET if self.height < PET_MAX_HEIGHT_M else PERSON


def measure(points: tuple[tuple[float, float, float], ...]) -> Measure:
    """Габариты облака: рост - наибольшая высота, ширина - больший из размахов по x и z, центр - по полу."""
    xs = [p[0] for p in points]
    zs = [p[2] for p in points]
    return Measure(max(p[1] for p in points), max(max(xs) - min(xs), max(zs) - min(zs)),
                   sum(xs) / len(xs), sum(zs) / len(zs))


@dataclass
class Profile:
    name: str
    kind: str
    height: float
    width: float
    seen: int = 1

    def enroll(self, m: Measure) -> None:
        """Подтверждённое наблюдение подтягивает профиль: среднее арифметическое первых наблюдений, дальше скользящее."""
        a = max(1 / (self.seen + 1), PROFILE_ALPHA)
        self.height += (m.height - self.height) * a
        self.width += (m.width - self.width) * a
        self.seen += 1


def match_profile(m: Measure, profiles: list[Profile]) -> Profile | None:
    """Ближайший профиль того же класса в допусках по росту и ширине; нет такого - None (гость или чужой)."""
    best, best_score = None, None
    for p in profiles:
        dh, dw = abs(p.height - m.height), abs(p.width - m.width)
        if p.kind != m.kind or dh > HEIGHT_TOL_M or dw > WIDTH_TOL_M:
            continue
        score = dh / HEIGHT_TOL_M + dw / WIDTH_TOL_M
        if best_score is None or score < best_score:
            best, best_score = p, score
    return best


@dataclass
class Track:
    tid: str
    kind: str
    last: Measure
    seen_at: float
    name: str | None = None


@dataclass
class Tracker:
    """Стабильные id между кадрами: обнаружение цепляется к ближайшему треку, который мог дойти за прошедшее время."""
    tracks: dict[str, Track] = field(default_factory=dict)
    _next: int = 1

    def update(self, detections: list[Measure], now: float) -> list[Track]:
        return [t for t in self.assign(detections, now) if t is not None]

    def assign(self, detections: list[Measure], now: float) -> list[Track | None]:
        """Трек для каждого обнаружения в порядке входа; None - шум по габаритам (не человек и не питомец)."""
        self.tracks = {t: tr for t, tr in self.tracks.items() if now - tr.seen_at <= LOST_S}
        taken: set[str] = set()
        out: list[Track | None] = [None] * len(detections)
        for i in sorted(range(len(detections)), key=lambda k: -detections[k].height):   # крупные первыми
            m = detections[i]
            kind = m.kind
            if kind is None:
                continue
            track = self._nearest(m, kind, now, taken)
            if track is None:
                track = Track(f"{kind}-{self._next}", kind, m, now)
                self._next += 1
                self.tracks[track.tid] = track
            taken.add(track.tid)
            track.last, track.seen_at = m, now
            out[i] = track
        return out

    def _nearest(self, m: Measure, kind: str, now: float, taken: set[str]) -> Track | None:
        best, best_d = None, None
        for tr in self.tracks.values():
            if tr.tid in taken or tr.kind != kind:
                continue
            reach = MAX_SPEED_MPS[kind] * max(now - tr.seen_at, 0.05)   # за это время трек мог уйти не дальше
            d = dist((tr.last.x, tr.last.z), (m.x, m.z))
            if d > reach or abs(tr.last.height - m.height) > HEIGHT_TOL_M * 2 or abs(tr.last.width - m.width) > WIDTH_TOL_M * 2:
                continue
            if best_d is None or d < best_d:
                best, best_d = tr, d
        return best
