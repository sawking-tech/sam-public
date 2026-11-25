"""RSSI-источник радара (SAM-105): станции роутера, дрожание сигнала, активность вокруг роутера.

Без доп. железа и без перезагрузки роутера: SAM читает по SSH `iw dev <iface> station dump` и по дрожанию уровня сигнала
подключённых устройств оценивает, есть ли рядом движение. Это присутствие и активность, не позиции и не силуэты:
координат людей RSSI с одного роутера не даёт (по исследованию). Модуль чистый: разбор текста и детектор по числам,
SSH и время снаружи.
"""
from __future__ import annotations

import hashlib
import re
import statistics
from collections import deque
from dataclasses import dataclass, field

from .presence_link import LinkFeatures, parse_link
from .presence_tuning import Tuning

WINDOW = 20                  # отсчётов в окне дрожания: 10 секунд при опросе раз в 0,5 с
MIN_SAMPLES = 6              # меньше - окно слишком короткое, активность не считаем
ACTIVE_MS = 3000             # станция молчит дольше - её сигнал устарел и дрожание не считается
SIGNAL_RANGE_DB = (-95.0, -30.0)   # правдоподобный уровень; вне - строка разбора испорчена, отсчёт отбрасывается

_STATION = re.compile(r"^Station\s+([0-9a-f:]{17})\b", re.I | re.M)
_FIELD = {
    "signal": re.compile(r"^\s*signal:\s+(-?\d+)\s*(?:\[([^\]]*)\])?", re.M),
    "inactive": re.compile(r"^\s*inactive time:\s+(\d+)\s*ms", re.M),
}


@dataclass(frozen=True)
class Station:
    sid: str                 # короткий отпечаток MAC: сам адрес наружу не идёт
    iface: str
    signal_db: float         # среднее по цепочкам антенн, точнее целого dBm
    inactive_ms: int
    name: str | None = None  # имя устройства из аренды DHCP роутера (Phone-..., Office-PC); нет аренды - None
    link: LinkFeatures | None = None   # цепочки, среднее роутера, подтверждения, скорость и MCS; нет - None


def station_id(mac: str) -> str:
    """Отпечаток MAC для страницы и событий: по нему станцию отличают, но адрес не восстановить."""
    return hashlib.sha1(mac.lower().encode()).hexdigest()[:8]


def parse_leases(text: str) -> dict[str, str]:
    """Имена устройств из /tmp/dhcp.leases роутера: «срок MAC IP имя id»; имя «*» значит, что клиент его не назвал."""
    names = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3] != "*":
            names[parts[1].lower()] = parts[3]
    return names


def parse_station_dump(text: str, iface: str, names: dict[str, str] | None = None) -> list[Station]:
    """Станции из вывода `iw dev <iface> station dump`; строка без сигнала или с неправдоподобным уровнем пропускается."""
    out = []
    parts = _STATION.split(text)                       # [мусор, mac, тело, mac, тело, ...]
    for mac, body in zip(parts[1::2], parts[2::2]):
        sig, inactive = _FIELD["signal"].search(body), _FIELD["inactive"].search(body)
        if not sig or not inactive:
            continue
        chains = [float(c) for c in re.findall(r"-?\d+", sig.group(2) or "")]
        level = sum(chains) / len(chains) if chains else float(sig.group(1))
        if not SIGNAL_RANGE_DB[0] <= level <= SIGNAL_RANGE_DB[1]:
            continue
        out.append(Station(station_id(mac), iface, level, int(inactive.group(1)), (names or {}).get(mac.lower()), parse_link(body)))
    return out


@dataclass(frozen=True)
class Reading:
    activity: float          # 0 - тихо, 1 - заметное движение рядом с линиями связи
    stations_total: int
    stations_active: int     # сколько станций сейчас передают и участвуют в оценке
    quiet_db: float          # база: типичное дрожание в покое, для отладки порогов
    per_station: dict = field(default_factory=dict)   # отпечаток станции -> её активность 0..1: какая линия связи дрожит
    lines: dict = field(default_factory=dict)         # отпечаток -> {name, db, std, base, activity}: сырьё для графика панели отладки


@dataclass
class MotionDetector:
    """Дрожание сигнала каждой станции в окне против её же спокойной базы; активность - сильнейшая из станций."""
    window: int = WINDOW
    tuning: Tuning = field(default_factory=Tuning)        # пороги; хаб отдаёт свой объект, ползунки меняют его на ходу
    _samples: dict[str, deque] = field(default_factory=dict)
    _baseline: dict[str, float] = field(default_factory=dict)

    def update(self, stations: list[Station]) -> Reading:
        seen = {s.sid for s in stations}
        for gone in [k for k in self._samples if k not in seen]:
            self._samples.pop(gone, None)                            # ушла из сети - окно и база не нужны; базы у станции может не быть, пока она не накопила окно
            self._baseline.pop(gone, None)
        best, active, quiet, per, lines = 0.0, 0, 0.0, {}, {}
        t = self.tuning
        for s in stations:
            if s.inactive_ms > ACTIVE_MS:
                continue                                              # молчит: сигнал с прошлых пакетов, дрожания нет
            window = self._samples.setdefault(s.sid, deque(maxlen=self.window))
            window.append(s.signal_db)
            if len(window) < MIN_SAMPLES:
                continue
            active += 1
            std = statistics.pstdev(window)
            base = self._baseline.get(s.sid, std)
            if std <= base + t.noise_floor_db:                          # тихо: база медленно подтягивается к норме
                base += (std - base) * t.baseline_rate
            self._baseline[s.sid] = base
            quiet = max(quiet, base)
            per[s.sid] = round(min(1.0, max(0.0, std - base - t.noise_floor_db) / t.full_activity_db), 3)
            lines[s.sid] = {"name": s.name, "db": round(s.signal_db, 1), "std": round(std, 2), "base": round(base, 2), "activity": per[s.sid]}
            best = max(best, per[s.sid])
        return Reading(round(best, 3), len(stations), active, round(quiet, 3), per, lines)
