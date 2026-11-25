"""Имена разметки квартиры для SAM (radar-in-sam): места и люди так, как их понимает радар.

SAM называет место словами («диван в зале», «кухня»), радар ставит человека по ключу места плана
(`PresenceHub.place_points`: комнаты, зоны, мебель с позой, подписанные места, места устройств). Здесь -
оглавление этих ключей с комнатой и позой и сверка названного слова с ним: точное совпадение без регистра,
иначе отказ со списком похожих. Модуль чистый: точки, комнаты и люди приходят аргументами.
"""
from __future__ import annotations

import difflib

from .presence_where import room_at

CLOSE_LIMIT = 5            # сколько похожих мест назвать в отказе: длиннее список не помогает выбрать
CLOSE_CUTOFF = 0.4         # порог похожести difflib: «диван» находит «диван в зале»
POSES = ("stand", "sit", "lie")


def norm(text: str) -> str:
    return " ".join(str(text or "").replace("(примерно)", "").lower().split())


def place_index(points: dict[str, dict], rooms: list[dict]) -> list[dict]:
    """Места плана для SAM: имя (ключ для ручки tell), комната и поза, по комнатам и именам."""
    out = [{"name": key, "room": room_at(rooms, p["x"], p["z"]), "pose": p.get("pose")} for key, p in points.items()]
    return sorted(out, key=lambda r: (r["room"] or "", r["name"]))


def people_index(people: dict[str, dict]) -> dict[str, str]:
    """Люди разметки: ключ радара -> имя словами."""
    return {key: str((info or {}).get("name") or key) for key, info in (people or {}).items()}


def close_names(word: str, names: list[str], limit: int = CLOSE_LIMIT) -> list[str]:
    """Похожие имена: сперва те, что содержат слово, затем похожие по написанию."""
    w = norm(word)
    containing = [n for n in names if w and (w in n or n in w)]
    similar = difflib.get_close_matches(w, names, n=limit, cutoff=CLOSE_CUTOFF)
    return list(dict.fromkeys(containing + similar))[:limit]


def resolve_place(word: str, points: dict[str, dict]) -> tuple[str | None, list[str]]:
    """Ключ места по слову и пустой список; нет такого - None и похожие имена."""
    key = norm(word)
    if key in points:
        return key, []
    return None, close_names(key, sorted(points))


def resolve_person(word: str, people: dict[str, str]) -> str | None:
    """Ключ человека по ключу или имени словами («anna», «Анна»), без регистра; не нашёлся - None."""
    w = norm(word)
    for key, name in people.items():
        if w in (key.lower(), norm(name)):
            return key
    return None


def unknown_place_detail(word: str, close: list[str]) -> str:
    hint = f"; похожие: {', '.join(close)}" if close else "; список мест - ручка layout радара"
    return f"места «{word}» нет в разметке{hint}"


def unknown_person_detail(word: str, people: dict[str, str]) -> str:
    return f"человека «{word}» нет в разметке; есть: {', '.join(f'{n} ({k})' for k, n in people.items())}"
