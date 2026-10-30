"""Wi-Fi как канал свидетельств «кто где» (SAM-105): устройство в сети говорит, что его хозяин дома, а где он, говорит положение устройства.

Хозяин устройства задаётся файлом хозяев, положение устройства - потоком положения: место, где устройство, это распределение по лепесткам итоговой области
(в каждом лепестке центр, доля из ста бросков), человек там же с весом по доле лепестка. Позу и лицо Wi-Fi не называет: их берёт рендер по поверхности под точкой.
Слабее, чем сообщение с именем и устройством (presence_messages), поэтому вес ниже единицы: дрожание линии роутер - устройство добавляет уверенности, потому что рядом
что-то движется. Пропавшее из сети устройство хозяина - свидетельство против. Модуль чистый: снимок устройств, хозяева и места устройств приходят параметрами.
Свидетельства складывает presence_fuse, здесь только формат.
"""
from __future__ import annotations

from typing import Callable

from radar.presence_fuse import Evidence

SOURCE = "wifi"
PRESENT_WEIGHT = 0.35        # устройство в сети и спокойно: нет речи, чата и трека входа в комнату - слабая подсказка, где человек
MOTION_GAIN = 0.45           # линия дрожит - рядом кто-то движется: до этой добавки к весу, по доле активности
AWAY_WEIGHT = 0.5            # устройство хозяина пропало из сети: он мог уйти с ним - уверенность, что он дома, падает вдвое
AWAY_FORGET_S = 6 * 3600.0   # пропавшее дольше этого не считается признаком ухода: телефон просто выключен или разряжен
MAX_WEIGHT = 0.8             # выше не поднимается: Wi-Fi с одного роутера человека не называет

Spot = tuple[float, float, float, str]            # x, z, доля, название места
OwnerOf = Callable[[str | None], str | None]       # имя устройства -> человек
SpotsOf = Callable[[dict], list[Spot]]             # строка устройства -> где оно, лепестками


def evidence_from_devices(devices: list[dict], owner_of: OwnerOf, spots: SpotsOf | None, now: float) -> list[Evidence]:
    """Свидетельства по снимку устройств (presence_devices.snapshot): у устройства должен быть хозяин. Присутствующее даёт свидетельство о хозяине по лепесткам положения устройства;
    нет положения - свидетельство без места. Пропавшее недавно и не перешедшее на другой диапазон - свидетельство против."""
    out = []
    present_names = {d.get("name") for d in devices if d.get("present")}                  # то же устройство на другом диапазоне ещё в сети: он сменил диапазон, а не ушёл
    for d in devices:
        owner = owner_of(d.get("name"))
        if not owner:
            continue
        if not d.get("present"):
            if d.get("seen_ago_s", 0.0) <= AWAY_FORGET_S and d.get("name") not in present_names:
                out.append(Evidence(source=SOURCE, person=owner, weight=AWAY_WEIGHT, t=now, away=True, note=f"{d.get('name')} пропал из сети"))
            continue
        activity = min(1.0, max(0.0, d.get("activity", 0.0)))
        weight = min(MAX_WEIGHT, PRESENT_WEIGHT + MOTION_GAIN * activity)
        t = now - d.get("seen_ago_s", 0.0)
        note = f"{d.get('name')} в сети" + (", линия дрожит" if activity >= 0.12 else "")
        placed = list(spots(d)) if spots else []
        if not placed:
            out.append(Evidence(source=SOURCE, person=owner, weight=weight, t=t, activity=activity, note=note))
            continue
        for x, z, share, label in placed:
            out.append(Evidence(source=SOURCE, person=owner, weight=weight * share, t=t, x=x, z=z, activity=activity, note=f"{note}; по потоку место «{label}» {share:.0%}"))
    return out


class WifiChannel:
    """Канал для хаба: снимок устройств берётся при каждом вызове, хозяева и места устройств - из хаба."""

    def __init__(self, snapshot, owner_of: OwnerOf, spots: SpotsOf | None = None) -> None:
        self._snapshot, self._owner_of, self._spots = snapshot, owner_of, spots

    def evidence(self, now: float) -> list[Evidence]:
        return evidence_from_devices(self._snapshot(now), self._owner_of, self._spots, now)
