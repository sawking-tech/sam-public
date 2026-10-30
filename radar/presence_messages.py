"""Канал сообщений как свидетельство «кто где» (SAM-105): реплика приходит от человека, с устройства, а устройство
стоит в известном месте и рядом с ним что-то есть.

Вход в монитор или SAM с компьютера в кабинете и голос на микрофоне узла означают человека у этого
места. Модуль знает только сообщения; веса и слияние - в `presence_fuse`, кадры веб-камер придут отдельным каналом с
тем же `Evidence`. Чистый: время и план приходят параметрами.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Callable

from radar.presence_fuse import Evidence

SOURCE = "messages"
VOICE_KNOWN_WEIGHT = 0.6      # голос опознан и пришёл с узла в известном месте
LEVEL_FAR = 400.0             # громкость фразы (rms слуха) не выше: говорящий далеко от микрофона узла; по записям: говорящий из зала в медиане 448
LEVEL_NEAR = 1200.0           # не ниже: вплотную; говорящий за столом у микрофона в медиане 1209
FAR_FACTOR = 0.4              # во сколько раз вес голоса меньше, когда говорящий далеко: место узла знаем, место человека хуже
VOICE_UNKNOWN_WEIGHT = 0.3    # опознания нет: место известно, человек нет (SAM-88: опознание пока 59-61%)
TOLD_WEIGHT = 0.85            # человек сказал, где кто («кто-то спит»): надёжно, но не измерение, поэтому ниже сообщения с устройства
TOLD_HALF_LIFE_S = 900.0      # слова о месте, где сидят и стоят, вдвое слабее через четверть часа: человек ходит, и слова с долгим сроком держат его на месте после ухода
TOLD_REST_HALF_LIFE_S = 10800.0   # слова о лежачем месте (сон) живут часами: спящий не встаёт каждые четверть часа
CHAT_WEIGHT = 1.0             # сообщение с устройства, которое само называет имя и устройство
NEAR_M = 1.5                  # предметы плана ближе этого к узлу попадают в «что рядом»
KEEP = 64                     # сколько последних сообщений помнит канал


@dataclass(frozen=True)
class Message:
    t: float
    channel: str                    # voice или chat
    device: str | None              # микрофон или устройство входа, как оно названо в системе
    who: str | None = None          # кто, если опознан (голос) или назван (вход)
    place: str | None = None        # для channel told: ключ места (точка плана), где человек, со слов
    level: float | None = None      # громкость фразы при речи (rms): чем громче, тем ближе человек к микрофону устройства
    pose: str | None = None         # для channel told: поза со слов (stand, sit, lie); нет - поза места


def _spot_for(device: str | None, spots: dict[str, dict]) -> dict | None:
    name = (device or "").lower()
    return next((s for key, s in spots.items() if key in name), None)


def objects_near(x: float, z: float, objects: list[dict], radius_m: float = NEAR_M) -> list[str]:
    """Названия предметов плана, чей центр не дальше radius_m от точки, ближние первыми."""
    found = [(math.hypot(o["x"] + o["w"] / 2 - x, o["z"] + o["d"] / 2 - z), o) for o in objects if o.get("title")]
    return [o["title"] for d, o in sorted(found, key=lambda r: r[0]) if d <= radius_m]


def proximity(level: float | None) -> float | None:
    """Близость к микрофону по громкости: 0 - далеко (LEVEL_FAR и тише), 1 - вплотную (LEVEL_NEAR и громче); нет замера - None."""
    if level is None:
        return None
    return min(1.0, max(0.0, (level - LEVEL_FAR) / (LEVEL_NEAR - LEVEL_FAR)))


def evidence_from(msg: Message, spots: dict[str, dict], people: dict[str, str] | None = None,
                  objects: list[dict] | None = None, places: dict[str, dict] | None = None) -> Evidence | None:
    """Свидетельство из одного сообщения. spots: часть имени устройства (нижний регистр) -> {x, z, pose?, owner?};
    people: как опознание называет человека (нижний регистр) -> имя на странице. Устройства нет в spots - место
    неизвестно, свидетельства нет. places: места плана (диван, комната, подписанная точка) - слова человека могут назвать и их."""
    if msg.channel == "told":                         # со слов человека: место названо ключом, человек - именем
        key = (msg.place or "").lower()
        spot = spots.get(key) or (places or {}).get(key)
        return None if not spot or not msg.who else Evidence(source=SOURCE, person=msg.who, weight=TOLD_WEIGHT, t=msg.t, x=spot["x"], z=spot["z"], pose=msg.pose or spot.get("pose"),
                                                           face=spot.get("face"), half_life_s=told_half_life(spot), note=f"со слов: {msg.place}")
    spot = _spot_for(msg.device, spots)
    if not spot:
        return None
    person = (people or {}).get((msg.who or "").lower(), msg.who) if msg.who else None
    if msg.channel == "chat":
        person, weight = person or spot.get("owner"), CHAT_WEIGHT
    else:
        weight = VOICE_KNOWN_WEIGHT if person else VOICE_UNKNOWN_WEIGHT
    near = objects_near(spot["x"], spot["z"], objects or [])
    closeness = proximity(msg.level) if msg.channel == "voice" else None
    if closeness is not None:
        weight *= FAR_FACTOR + (1.0 - FAR_FACTOR) * closeness
    how = "голос" if msg.channel == "voice" else "вход"
    loud = "" if closeness is None else f", {'близко к микрофону' if closeness > 0.66 else 'далеко от микрофона' if closeness < 0.33 else 'в комнате'}"
    return Evidence(source=SOURCE, person=person, weight=weight, t=msg.t, x=spot["x"], z=spot["z"],
                    pose=spot.get("pose"), note=f"{how} с «{msg.device}»{loud}" + (f", рядом: {', '.join(near[:3])}" if near else ""))


def told_half_life(spot: dict) -> float:
    """Срок полураспада слов по позе места: лежачее живёт дольше сидячего и стоячего."""
    return TOLD_REST_HALF_LIFE_S if spot.get("pose") == "lie" else TOLD_HALF_LIFE_S


def told_strength(msg: Message, now: float, half_life_s: float = TOLD_HALF_LIFE_S) -> float:
    """Сила слов в момент now: вес слов с распадом по периоду полураспада; не слова или из будущего - нуль."""
    if msg.channel != "told" or msg.t > now:
        return 0.0
    return TOLD_WEIGHT * 0.5 ** ((now - msg.t) / half_life_s)


class MessageChannel:
    """Последние сообщения и их свидетельства для слияния."""

    def __init__(self, spots: dict[str, dict], people: dict[str, str] | None = None, objects: list[dict] | None = None,
                 places: Callable[[], dict[str, dict]] | None = None) -> None:
        self.spots, self.people, self.objects = {k.lower(): v for k, v in spots.items()}, people, objects
        self._places = places                              # места плана: вызывается при каждом расчёте, потому что зоны меняются
        self._recent: deque[Message] = deque(maxlen=KEEP)

    def feed(self, msg: Message) -> None:
        self._recent.append(msg)

    def recent(self) -> list[Message]:
        """Последние сообщения по порядку прихода: хаб пишет их в файл состояния, чтобы слова пережили перезапуск."""
        return list(self._recent)

    def _latest_told(self, now: float) -> list[Message]:
        """Слова человека о месте: у каждого человека последние, раньше сказанное не считается: он был в одном месте, а не во всех названных."""
        latest: dict[str, Message] = {}
        for m in self._recent:
            if m.channel == "told" and m.who and m.t <= now and (m.who not in latest or m.t >= latest[m.who].t):
                latest[m.who] = m
        return list(latest.values())

    def told_now(self, now: float, min_strength: float) -> list[dict]:
        """Слова, которые ещё не выдохлись: человек, названное место, его точка на плане и сила; для копилки привычек, куда слова идут как уверенное."""
        places = self._places() if self._places else {}
        found = []
        for m in self._latest_told(now):
            spot = self.spots.get((m.place or "").lower()) or places.get((m.place or "").lower())
            strength = told_strength(m, now, told_half_life(spot)) if spot else 0.0
            if m.who and spot and strength >= min_strength:
                found.append({"person": m.who, "place": (m.place or "").lower(), "x": spot["x"], "z": spot["z"], "strength": strength})
        return found

    def evidence(self, now: float) -> list[Evidence]:
        places = self._places() if self._places else None
        latest = {id(m) for m in self._latest_told(now)}
        rows = (evidence_from(m, self.spots, self.people, self.objects, places) for m in self._recent if m.t <= now and (m.channel != "told" or id(m) in latest))
        return self._latest_per_person([e for e in rows if e])

    @staticmethod
    def _latest_per_person(found: list[Evidence]) -> list[Evidence]:
        """У каждого человека считается последнее сообщение: он в одном месте, и свежий голос из кухни отменяет сообщение со стола минуту назад,
        а не усредняется с ним (иначе фигура встаёт между кухней и столом). Свидетельства без человека остаются все."""
        newest: dict[str, Evidence] = {}
        for e in found:
            if e.person and (e.person not in newest or e.t >= newest[e.person].t):
                newest[e.person] = e
        return [e for e in found if not e.person or newest[e.person] is e]
