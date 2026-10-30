"""Голос как свидетельство радару (radar-in-sam): услышанная фраза - человек у микрофона узла.

Мост берёт законченную фразу (не эхо, не кусок) и шлёт радару сообщение канала voice: узел, кто опознан и
громкость. Узел в записи слуха не указан: слух один на стенде, его место - имя устройства из конфига
(`SAM_VOICE_RADAR_DEVICE`, ключ места устройства в разметке). Опознание пишет вариант голоса через дефис
(«owner-loud»): радару идёт человек до дефиса, если он из списка людей; иначе - голос без человека
(«услышан голос у узла»). Радар сам решает вес по громкости (`presence_messages.proximity`).

Флаг `SAM_VOICE_RADAR`, по умолчанию выключено. Решение (что слать) - чистая функция message_for.
"""
from __future__ import annotations

import os

from gateway.env import parse_bool

ENABLED_ENV = "SAM_VOICE_RADAR"
DEVICE_ENV = "SAM_VOICE_RADAR_DEVICE"
DEFAULT_DEVICE = "workpc"            # слух стенда стоит на рабочем ПК; ключ места устройства в data/home_layout.json
PEOPLE_ENV = "SAM_VOICE_RADAR_PEOPLE"
SPEAKERS_ENV = "SAM_SPEAKER_NAMES"   # профили голоса: без списка людей людьми считаются все профили
VARIANT_SEP = "-"                    # вариант голоса одного человека: owner-loud
CHANNEL = "voice"


def person_of(who: str | None, people: frozenset[str]) -> str | None:
    """Человек по метке опознания: вариант через дефис сводится к основе; не из списка - None."""
    base = str(who or "").strip().lower().split(VARIANT_SEP, 1)[0]
    return base if base in people else None


def message_for(rec: dict, device: str, people: frozenset[str]) -> dict:
    """Сообщение радару по фразе слуха: канал, узел, человек (или None) и громкость."""
    level = rec.get("rms")
    return {"channel": CHANNEL, "device": device, "who": person_of(rec.get("who"), people),
            "level": float(level) if isinstance(level, (int, float)) else None}


class RadarFeed:
    """Подача фраз радару: выключена флагом - ничего не делает. client - gateway.radar_client.RadarClient."""

    def __init__(self, client, enabled: bool, device: str, people: frozenset[str]) -> None:
        self._client, self.enabled, self._device, self._people = client, enabled, device, people

    def heard(self, rec: dict) -> None:
        if self.enabled:
            self._client.note_message(**message_for(rec, self._device, self._people))


def _people(env) -> frozenset[str]:
    """Кого опознание называет людьми: список из настройки, иначе профили голоса без вариантов (до дефиса)."""
    speakers = (p.split("=", 1)[0].split(VARIANT_SEP, 1)[0] for p in (env.get(SPEAKERS_ENV) or "").split(","))
    raw = env.get(PEOPLE_ENV) or ",".join(speakers)
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def build(env=os.environ, client=None) -> RadarFeed:
    """client None - клиент радара шлюза по адресу из env; тесты подают свой."""
    from gateway import radar_client
    people = _people(env)
    raw = (env.get(ENABLED_ENV) or "").strip()
    enabled = parse_bool(ENABLED_ENV, raw) if raw else False
    return RadarFeed(client or radar_client.build(env), enabled, env.get(DEVICE_ENV) or DEFAULT_DEVICE, people)
