"""Сканирование (radar-in-sam): заполняет пробелы в сводке дня. Пробел - SAM не знает, где человек или что он
делает (радар видит его плохо или он давно не отмечался), а для сводки это важно.

Ворота workflow `workflows/scanning.yml`: `python -m gateway.scanning` печатает повод для основной модели или
ничего (пустой вывод - модель не зовётся). Повод из двух частей:
- услышанное о положении: фразы после прошлого запуска, где названо место квартиры или слово о положении
  («лёг», «иду», «сижу») - модель вносит сказанное в радар инструментом radar_tell;
- вопрос: кого спросить, где он и чем занят, или его партнёра, если сам человек промолчал.

Ограничения в коде, не в промпте:
- вопрос человеку не чаще раза в SAM_SCANNING_EVERY_MIN минут;
- ни одного вопроса в тихом режиме сессии и в ночные часы (SAM_SCANNING_NIGHT_FROM_H - SAM_SCANNING_NIGHT_TO_H);
- спрашивает, только если уверенность радара ниже SAM_SCANNING_SURE или человек не отмечался голосом
  SAM_SCANNING_STALE_MIN минут;
- нет ответа за SAM_SCANNING_WAIT_MIN минут - один вопрос партнёру (поле `partner` записи человека в каталоге
  Mongo), без повторов; партнёра нет - вопрос просто закрывается.
Ответом считается фраза спрошенного или фраза без опознания говорящего после вопроса: опознание голоса
не всегда называет человека, и неопознанный ответ лучше, чем лишний вопрос партнёру.

Флаг SAM_SCANNING, по умолчанию выключено. Решение - чистая функция decide; радар, шина, каталог и файл
состояния читаются функциями read_* и main.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import quiet_mode, radar_client
from .env import env_bool, env_float
from .workflow_gates import _parse_ts, read_phrases

ENABLED_ENV = "SAM_SCANNING"
EVERY_MIN = env_float("SAM_SCANNING_EVERY_MIN", 120.0)     # N: вопрос человеку не чаще; реже раза в два часа - SAM не превращается в допрос
WAIT_MIN = env_float("SAM_SCANNING_WAIT_MIN", 10.0)        # M: столько ждать ответа, потом вопрос партнёру
SURE = env_float("SAM_SCANNING_SURE", 0.5)                 # порог уверенности радара; тот же, что у уверенной строки истории потока (presence_flow_history.SURE_SHARE)
STALE_MIN = env_float("SAM_SCANNING_STALE_MIN", 180.0)     # не отмечался голосом столько - спросить и при уверенном радаре
NIGHT_FROM_H = env_float("SAM_SCANNING_NIGHT_FROM_H", 23.0)
NIGHT_TO_H = env_float("SAM_SCANNING_NIGHT_TO_H", 8.0)
LOOKBACK_S = env_float("SAM_SCANNING_LOOKBACK_S", 1800.0)  # первый запуск и долгий простой не тащат весь журнал
SESSION_ENV = "SAM_SCANNING_SESSION"
DEFAULT_SESSION = "voice"                                  # голосовая сессия моста: её тишина глушит вопросы
STATE_ENV = "SAM_SCANNING_STATE"
DEFAULT_STATE = "data/scanning_state.json"
WORDS_ENV = "SAM_SCANNING_WORDS"
DEFAULT_WORDS = ("сплю,лёг,легла,ложусь,лежу,встал,встала,сижу,стою,иду,пошёл,пошла,пришёл,пришла,ушёл,ушла,"
                 "вернулся,вернулась,дома,на кухне,в ванной,в туалете,в спальне,в зале,в кабинете")
MONGO_ENV = "MONGODB_URI"
DEFAULT_MONGO = "mongodb://localhost:27017"
MONGO_DB = "sam"
CATALOG = "catalog"
PARTNER_FIELD = "partner"
TIMEOUT_S = 3.0                                            # ворота дешёвые: шлюз ждёт их не дольше SAM_WORKFLOW_GATE_TIMEOUT_S
VARIANT_SEP = "-"                                          # вариант голоса одного человека в опознании: anna-loud
HEARD_HEADER = "Услышано о положении (внеси сказанное в радар через radar_tell, вслух не отвечай):"
UNKNOWN_SPEAKER = "неизвестный"
STEM_LONG, STEM_CUT_LONG = 6, 2                            # слово от шести букв теряет два последних знака («кровать» - «крова»)
STEM_SHORT = 4                                             # от четырёх - один («кухня» - «кухн», «зал» остаётся целым)
VOICE_IN ="voice-in.jsonl"                                # то же имя, что voice/layer/bus.py VOICE_IN (шлюз не импортирует голосовой слой)


@dataclass(frozen=True)
class Config:
    every_s: float = EVERY_MIN * 60
    wait_s: float = WAIT_MIN * 60
    sure: float = SURE
    stale_s: float = STALE_MIN * 60
    night_from_h: float = NIGHT_FROM_H
    night_to_h: float = NIGHT_TO_H


@dataclass
class State:
    cursor: float = 0.0                                    # последняя разобранная фраза (epoch)
    asked: dict[str, float] = field(default_factory=dict)  # когда человека спрашивали
    marked: dict[str, float] = field(default_factory=dict)  # когда человек последний раз отмечался голосом или ответом
    pending: dict | None = None                            # открытый вопрос: person, asked_at

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)

    @staticmethod
    def from_json(text: str) -> "State":
        data = json.loads(text)
        return State(float(data.get("cursor") or 0.0), dict(data.get("asked") or {}), dict(data.get("marked") or {}), data.get("pending"))


@dataclass(frozen=True)
class Heard:
    t: float
    who: str | None                                        # человек по опознанию, None - не опознан
    text: str


def is_night(hour: float, from_h: float, to_h: float) -> bool:
    return hour >= from_h or hour < to_h if from_h > to_h else from_h <= hour < to_h


def speaker(raw: str | None) -> str | None:
    base = str(raw or "").strip().lower().split(VARIANT_SEP, 1)[0]
    return base or None


def heard_from(phrases: list[dict]) -> list[Heard]:
    """Законченные фразы людей: без эха и кусков, со временем."""
    out = []
    for p in phrases:
        ts = _parse_ts(p.get("ts"))
        text = str(p.get("text") or "").strip()
        if ts is not None and text and not p.get("partial") and not p.get("echo"):
            out.append(Heard(ts.timestamp(), speaker(p.get("who")), text))
    return out


def _tokens(text: str) -> list[str]:
    return re.sub(r"[^\w-]+", " ", text.lower().replace("ё", "е")).split()


def _stem(word: str) -> str:
    """Основа без падежного окончания: разметка называет «кухня», люди говорят «на кухне»."""
    return word[:-STEM_CUT_LONG] if len(word) >= STEM_LONG else word[:-1] if len(word) >= STEM_SHORT else word


def mentions_position(text: str, words: tuple[str, ...]) -> bool:
    """Названо ли в фразе место или слово о положении: подряд идущие слова по основам, без регистра и знаков."""
    said = _tokens(text)
    for w in words:
        stems = [_stem(t) for t in _tokens(w)]
        if stems and any(all(said[i + k].startswith(s) for k, s in enumerate(stems)) for i in range(len(said) - len(stems) + 1)):
            return True
    return False


def _answered(heard: list[Heard], person: str, since: float) -> bool:
    return any(h.t > since and h.who in (person, None) for h in heard)


def decide(now: float, hour: float, quiet: bool, people: list[dict], partners: dict[str, str], names: dict[str, str],
           state: State, heard: list[Heard], words: tuple[str, ...], cfg: Config = Config()) -> tuple[list[str], State]:
    """Повод строками и новое состояние. people - строки radar_where (person, confidence): дома по радару;
    partners - ключ человека -> ключ партнёра (из каталога); names - ключ -> имя словами."""
    fresh = [h for h in heard if h.t > state.cursor]
    marked = dict(state.marked)
    for h in fresh:
        if h.who:
            marked[h.who] = max(marked.get(h.who, 0.0), h.t)
    seen = {str(p["person"]): p for p in people}
    for person in seen:
        marked.setdefault(person, now)                      # впервые увиден: отсчёт «давно не отмечался» с этого запуска
    asked = dict(state.asked)
    lines: list[str] = []
    about = [h for h in fresh if mentions_position(h.text, words)]
    if about:
        lines.append(HEARD_HEADER)
        lines += [f"{datetime.fromtimestamp(h.t):%H:%M} {names.get(h.who or '', h.who or UNKNOWN_SPEAKER)}: {h.text}" for h in about]
    cursor = max([state.cursor, *(h.t for h in fresh)])
    if quiet or is_night(hour, cfg.night_from_h, cfg.night_to_h):
        return lines, State(cursor, asked, marked, None)    # тишина и ночь: ни вопроса, ни вопроса партнёру потом
    pending = state.pending
    if pending:
        person, asked_at = pending["person"], float(pending["asked_at"])
        if _answered(heard, person, asked_at):
            marked[person] = now
            pending = None
        elif now - asked_at >= cfg.wait_s:
            partner = partners.get(person)
            if partner and partner in seen and partner != person:
                lines.append(f"Вопрос: {names.get(person, person)} не ответил(а) за {cfg.wait_s / 60:.0f} мин. Спроси человека по имени "
                             f"{names.get(partner, partner)}, где сейчас {names.get(person, person)} и чем занят(а). Одна короткая фраза.")
            pending = None                                  # один вопрос партнёру и всё: без повторов
        if pending is not None:
            return lines, State(cursor, asked, marked, pending)
    due = [p for key, p in seen.items()
           if now - asked.get(key, 0.0) >= cfg.every_s
           and (float(p.get("confidence") or 0.0) < cfg.sure or now - marked.get(key, now) >= cfg.stale_s)]
    if due and not any(line.startswith("Вопрос:") for line in lines):
        target = min(due, key=lambda p: float(p.get("confidence") or 0.0))
        key = str(target["person"])
        asked[key] = now
        lines.append(f"Вопрос: спроси человека по имени {names.get(key, key)}, где он(а) сейчас и чем занят(а). Одна короткая живая фраза по имени.")
        pending = {"person": key, "asked_at": now}
    return lines, State(cursor, asked, marked, pending)


def radar() -> radar_client.RadarClient:
    """Клиент функций радара; пустой SAM_RADAR_URL - адрес по умолчанию, как у сканирования до клиента."""
    return radar_client.RadarClient(os.getenv(radar_client.URL_ENV) or radar_client.DEFAULT_URL)


def read_radar(client: radar_client.RadarClient | None = None) -> tuple[list[dict], dict[str, str], tuple[str, ...]] | None:
    """Люди дома по радару, их имена и имена мест - функциями радара where и layout; радар молчит - None."""
    client = client or radar()
    where, layout = client.read(radar_client.WHERE), client.read(radar_client.LAYOUT)
    if not isinstance(where, list) or not isinstance(layout, dict):
        return None
    place_words = tuple(p["name"] for p in layout.get("places") or []) + tuple(r.lower() for r in layout.get("rooms") or [])
    return [p for p in where if p.get("person")], dict(layout.get("people") or {}), place_words


def read_partners() -> dict[str, str]:
    """Партнёры из каталога: поле partner записи человека. Mongo нет - пусто, вопрос партнёру не задаётся."""
    try:
        from pymongo import MongoClient
        db = MongoClient(os.getenv(MONGO_ENV) or DEFAULT_MONGO, serverSelectionTimeoutMS=int(TIMEOUT_S * 1000))[MONGO_DB]
        return {d["name"]: str(d[PARTNER_FIELD]).lower() for d in db[CATALOG].find({"type": "person", PARTNER_FIELD: {"$exists": True}})}
    except Exception:
        return {}


def bus_path() -> Path:
    return Path(os.getenv("SAM_VOICE_BUS_DIR", "").strip() or Path(__file__).resolve().parents[1] / "data" / "voice" / "bus")


def load_state(path: Path, now: float) -> State:
    try:
        return State.from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return State(cursor=now - LOOKBACK_S)


def main() -> int:
    if not env_bool(ENABLED_ENV, False):
        return 0
    radar = read_radar()
    if radar is None:
        print("[сканирование] радар не отвечает: вопросов нет", file=sys.stderr)
        return 0
    people, names, place_words = radar
    words = tuple(w.strip() for w in (os.getenv(WORDS_ENV) or DEFAULT_WORDS).split(",") if w.strip()) + place_words
    now = time.time()
    state_path = Path(os.getenv(STATE_ENV) or DEFAULT_STATE)
    state = load_state(state_path, now)
    bus = bus_path()
    session = os.getenv(SESSION_ENV) or DEFAULT_SESSION
    quiet = quiet_mode.QuietBook(bus / quiet_mode.QUIET_FILE).is_quiet(session)
    local = datetime.fromtimestamp(now)
    lines, new_state = decide(now, local.hour + local.minute / 60, quiet, people, read_partners(), names,
                              state, heard_from(read_phrases(bus / VOICE_IN)), words)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(new_state.to_json(), encoding="utf-8")
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
