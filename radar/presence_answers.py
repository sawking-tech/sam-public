"""Ответы радара словами для SAM: разбор момента, строка истории, мебель комнаты, разметка, итог «со слов».

Ручки радара (`radar/presence_sam_api.py`) и только они собирают ответы инструментов SAM; MCP-переходник
`mcp/radar_server.py` их пересылает. Функции чистые: хаб, часы и сеть приходят снаружи.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from gateway.env import env_float

from . import presence_names as pnames

OFF_DETAIL = "радар выключен: включи SAM_PRESENCE в .env"
TOLD = "told"
TOLD_OFF_DETAIL = "слова «со слов» записаны не будут: канал told выключен, добавь его в SAM_PRESENCE_CHANNELS"
FURNITURE_LIMIT = 60       # сколько предметов отдаём за раз: весь план в ответ не помещается
HISTORY_WINDOW_S = env_float("SAM_RADAR_HISTORY_WINDOW_S", 90.0)   # строка истории не дальше этого от момента: поток пишет раз в секунды, дыра больше - «нет данных»
HISTORY_PLACES = 3         # сколько мест с вероятностями назвать: дальше доли малы
PERSON_SUBJECT = "person:"  # субъект человека в истории потока: «person:anna|person»
YESTERDAY = "вчера"
NO_DATA = "нет данных"
AT_HINT = "нужно ЧЧ:ММ, «вчера ЧЧ:ММ» или ГГГГ-ММ-ДД ЧЧ:ММ"
FURNITURE_KEYS = ("title", "kind", "x", "z", "w", "d", "h")


class Rejected(ValueError):
    """Слова SAM не легли на разметку: текст - что не так и что есть."""


def parse_at(text: str, now: datetime) -> datetime | None:
    """Момент по словам: «ЧЧ:ММ» - сегодня (позже текущего - вчера), «вчера ЧЧ:ММ», «ГГГГ-ММ-ДД ЧЧ:ММ»; не разобрал - None."""
    raw = " ".join(str(text or "").lower().split())
    try:
        return datetime.fromisoformat(raw.replace(" ", "T")) if re.match(r"\d{4}-\d{2}-\d{2}", raw) else _clock_time(raw, now)
    except ValueError:
        return None


def _clock_time(raw: str, now: datetime) -> datetime | None:
    m = re.fullmatch(rf"(?:({YESTERDAY})\s+)?(?:в\s+)?(\d{{1,2}})[:.](\d{{2}})", raw)
    if not m:
        return None
    at = now.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
    if m.group(1) or at > now:
        at -= timedelta(days=1)
    return at


def pick_row(rows: list[dict], person_key: str, at_s: float, window_s: float = HISTORY_WINDOW_S) -> dict | None:
    """Строка истории человека, ближайшая к моменту и не дальше окна; нет - None."""
    subject = f"{PERSON_SUBJECT}{person_key}|"
    own = [r for r in rows if str(r.get("id", "")).startswith(subject) and abs(r["t"] - at_s) <= window_s]
    return min(own, key=lambda r: abs(r["t"] - at_s), default=None)


def describe_row(row: dict | None, person: str, at: datetime) -> dict:
    if row is None:
        return {"person": person, "at": at.strftime("%Y-%m-%d %H:%M"), "place": None, "result": NO_DATA}
    places = [{"place": p, "p": v} for p, v in row.get("places", [])[:HISTORY_PLACES]]
    return {"person": person, "at": datetime.fromtimestamp(row["t"]).strftime("%Y-%m-%d %H:%M:%S"),
            "place": places[0]["place"] if places else None, "p": row.get("sure"), "places": places,
            "source": f"история потока положения, субъект {row['id']}"}


def person_key(person: str, people: dict[str, str]) -> str:
    """Ключ человека разметки по ключу или имени; нет такого - Rejected с теми, кто есть."""
    key = pnames.resolve_person(person, people)
    if key is None:
        raise Rejected(pnames.unknown_person_detail(person, people))
    return key


def history_window(at: datetime) -> tuple[float, float]:
    """Отрезок истории вокруг момента: строка ищется в нём."""
    t = at.timestamp()
    return t - HISTORY_WINDOW_S, t + HISTORY_WINDOW_S


def places(layout: dict, points: dict[str, dict], channels) -> dict:
    """Разметка словами: комнаты, места плана с комнатой и позой (имя места - ключ для «со слов»), люди."""
    rooms = layout.get("rooms") or []
    return {"rooms": [r.get("title") for r in rooms if r.get("title")],
            "places": pnames.place_index(points, rooms),
            "people": pnames.people_index(layout.get("people") or {}),
            "told": TOLD in channels}


def furniture(layout: dict, room: str = "") -> list[dict]:
    """Предметы плана, чей центр в комнатах с room в названии (пусто - вся квартира), не больше FURNITURE_LIMIT."""
    wanted = room.strip().lower()
    rooms = [r for r in layout.get("rooms") or [] if not wanted or wanted in str(r.get("title", "")).lower()]
    items = [o for o in layout.get("objects") or []
             if any(r["x"] <= o["x"] + o["w"] / 2 <= r["x"] + r["w"] and r["z"] <= o["z"] + o["d"] / 2 <= r["z"] + r["d"] for r in rooms)]
    return [{k: o.get(k) for k in FURNITURE_KEYS} for o in items[:FURNITURE_LIMIT]]


def resolve_told(word_person: str, word_place: str, pose: str | None, points: dict[str, dict],
                 people: dict[str, str]) -> tuple[str, str]:
    """(ключ человека, ключ места) для слов «со слов»; место, человек или поза не из разметки - Rejected."""
    place, close = pnames.resolve_place(word_place or "", points)
    if place is None:
        raise Rejected(pnames.unknown_place_detail(word_place or "", close))
    who = pnames.resolve_person(word_person or "", people)
    if who is None:
        raise Rejected(pnames.unknown_person_detail(word_person or "", people))
    if pose is not None and pose not in pnames.POSES:
        raise Rejected(f"поза «{pose}»: одна из {', '.join(pnames.POSES)}")
    return who, place


def told_answer(applied: bool, person: str, place: str, pose: str | None) -> str:
    return f"внесла: {person} - {place}" + (f", {pose}" if pose else "") if applied else TOLD_OFF_DETAIL
