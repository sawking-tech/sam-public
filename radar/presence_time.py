"""Время суток как свидетельство «кто где» (SAM-105): распорядок дня даёт слабую догадку, где человек в этот час.

Ночью человек скорее в постели, днём у компьютера. Распорядок - данные плана
(`routines`), сам модуль расписания не придумывает: записи появляются со слов человека или из накопленных наблюдений.
Вес малый, потому что распорядок про привычку, а не про сейчас. Модуль чистый: время приходит параметром.
"""
from __future__ import annotations

from datetime import datetime

from radar.presence_fuse import Evidence

SOURCE = "time"
DEFAULT_WEIGHT = 0.3          # запись без своего веса: привычка, не факт


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(minute_of_day: int, start: str, end: str) -> bool:
    """Попадает ли минута суток в окно; окно через полночь («23:00» - «08:00») тоже."""
    a, b = _minutes(start), _minutes(end)
    return a <= minute_of_day < b if a <= b else minute_of_day >= a or minute_of_day < b


def evidence_from_routines(routines: list[dict], spots: dict, now: float) -> list[Evidence]:
    """Записи распорядка {person, from, to, place, [pose], [weight]}: place - ключ точки в spots плана; нет точки - записи нет."""
    t = datetime.fromtimestamp(now)
    minute = t.hour * 60 + t.minute
    out = []
    for r in routines:
        place = spots.get(r.get("place"))
        if not place or not in_window(minute, r["from"], r["to"]):
            continue
        out.append(Evidence(source=SOURCE, person=r["person"], weight=r.get("weight", DEFAULT_WEIGHT), t=now, x=place["x"], z=place["z"],
                            pose=r.get("pose") or place.get("pose"), note=f"в {r['from']}-{r['to']} обычно здесь"))
    return out


class TimeChannel:
    def __init__(self, routines: list[dict], spots: dict, clock=None) -> None:
        self.routines, self.spots = routines, spots
        self._clock = clock

    def evidence(self, now: float) -> list[Evidence]:
        return evidence_from_routines(self.routines, self.spots, now)
