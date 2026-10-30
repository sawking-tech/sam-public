"""Учёт устройств радара (SAM-105): кто из устройств дома, когда видели последний раз и с каким сигналом.


«помнить, где последний раз был телефон». Wi-Fi с одного роутера даёт не координаты, а уровень сигнала и диапазон,
поэтому «где» - это грубая близость к роутеру и время. Модуль чистый: время и станции приходят параметрами, файл
пишет вызывающий; книга переживает перезапуск, чтобы «последний раз видели» не пропадало.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from enum import StrEnum

LEFT_AFTER_S = 90.0          # станции нет в списке роутера столько секунд - устройство ушло из сети
NEAR_DB = -55.0              # сигнал сильнее - устройство рядом с роутером
FAR_DB = -70.0               # слабее - далеко или за стеной
MOVE_DB = 7.0               # устойчивый сдвиг сигнала на столько дБ от обычного - устройство, скорее всего, взяли и перенесли
MOVE_SAMPLES = 6            # столько свежих отсчётов подряд сдвинуты, чтобы не принять шаг человека между линией за перенос
USUAL_RATE = 0.01           # скорость, с которой «обычный сигнал» устройства подстраивается; медленно, иначе перенос не заметить
MAX_DEVICES = 64             # запоминаем не больше: случайные MAC гостей и «приватные адреса» не должны раздувать файл


@dataclass
class Device:
    sid: str
    name: str | None
    iface: str
    first_seen: float
    last_seen: float
    last_signal_db: float
    present: bool = True
    usual_db: float | None = None   # обычный уровень сигнала: от него считается перенос
    off_count: int = 0


class Closeness(StrEnum):
    """Зона близости устройства к роутеру; значения те же строки, что читают страница и события перемещения."""
    NEAR = "near"
    MID = "mid"
    FAR = "far"


def closeness(signal_db: float) -> Closeness:
    """Грубая близость к роутеру по сигналу: рядом, средне, далеко."""
    if signal_db >= NEAR_DB:
        return Closeness.NEAR
    return Closeness.MID if signal_db >= FAR_DB else Closeness.FAR


class DeviceBook:
    def __init__(self, left_after_s: float = LEFT_AFTER_S, devices: dict[str, Device] | None = None,
                 places: dict[str, str] | None = None) -> None:
        self.left_after_s = left_after_s
        self.places = places or {}      # часть имени устройства (нижний регистр) -> где оно лежит, как сказал человек
        self.devices: dict[str, Device] = devices or {}

    def update(self, stations, now: float) -> list[dict]:
        """Круг опроса роутера в книгу; события: появилось (appeared) и ушло (left), для монитора и памяти SAM."""
        events = []
        seen = set()
        for s in stations:
            seen.add(s.sid)
            d = self.devices.get(s.sid)
            if d is None:
                d = self.devices[s.sid] = Device(s.sid, s.name, s.iface, now, now, s.signal_db)
                events.append({"event": "appeared", "sid": s.sid, "name": s.name, "ts": now})
            elif not d.present:
                events.append({"event": "appeared", "sid": s.sid, "name": s.name or d.name, "ts": now})
            d.name, d.iface, d.present = s.name or d.name, s.iface, True
            if s.inactive_ms <= 3000:                       # свежий сигнал: молчащая станция показывает старое число
                d.last_signal_db = s.signal_db
                moved = self._moved(d, s.signal_db)
                if moved:
                    events.append({"event": "moved", "sid": d.sid, "name": d.name, "ts": now, **moved})
            d.last_seen = now
        for d in self.devices.values():
            if d.present and d.sid not in seen and now - d.last_seen > self.left_after_s:
                d.present = False
                events.append({"event": "left", "sid": d.sid, "name": d.name, "ts": now})
        self._trim()
        return events

    def _moved(self, d: Device, level: float) -> dict | None:
        """Перенос: сигнал устойчиво ушёл от обычного; обычный уровень после события перенимает новое положение."""
        if d.usual_db is None:
            d.usual_db = level
            return None
        if abs(level - d.usual_db) < MOVE_DB:
            d.off_count = 0
            d.usual_db += (level - d.usual_db) * USUAL_RATE
            return None
        d.off_count += 1
        if d.off_count < MOVE_SAMPLES:
            return None
        before, d.usual_db, d.off_count = d.usual_db, level, 0
        return {"from": closeness(before), "to": closeness(level), "delta_db": round(level - before, 1)}

    def place_of(self, name: str | None) -> str | None:
        low = (name or "").lower()
        return next((place for key, place in self.places.items() if key.lower() in low), None)

    def _trim(self) -> None:
        if len(self.devices) <= MAX_DEVICES:
            return
        # лишнее убираем среди ушедших, самые давние первыми
        gone = sorted((d for d in self.devices.values() if not d.present), key=lambda d: d.last_seen)
        for d in gone[:len(self.devices) - MAX_DEVICES]:
            del self.devices[d.sid]

    def snapshot(self, now: float, activity: dict | None = None) -> list[dict]:
        rows = [{"id": d.sid, "name": d.name, "present": d.present, "iface": d.iface,
                 "signal_db": round(d.last_signal_db, 1), "closeness": closeness(d.last_signal_db),
                 "seen_ago_s": round(max(0.0, now - d.last_seen), 1), "first_seen": d.first_seen,
                 "place": self.place_of(d.name), "activity": (activity or {}).get(d.sid, 0.0)}
                for d in self.devices.values()]
        return sorted(rows, key=lambda r: (not r["present"], r["seen_ago_s"]))

    def to_json(self) -> str:
        return json.dumps([asdict(d) for d in self.devices.values()], ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "DeviceBook":
        book = cls()
        for row in json.loads(text):
            d = Device(**{key: value for key, value in row.items() if key in Device.__dataclass_fields__})      # старые файлы несут поля, которых больше нет
            d.present = False                              # после запуска все «не видны», пока роутер не покажет
            book.devices[d.sid] = d
        return book
