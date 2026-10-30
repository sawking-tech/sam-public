"""Копилка привычек (SAM-118): сколько секунд человек бывал в каком месте, по дате, слоту времени и составу гостей, только когда SAM уверена.

Из неё считается настоящий приор: вероятность места по человеку, дню недели, слоту и гостям за окно дат (скажем, 30 дней), с запасом на меньшие данные и смесью
со словами из `presence_habits`. В копилку идёт только то, что человек точно был дома и в этом месте, поэтому приор из неё условен на «дома»; ушёл он или нет, решают слова.
Модуль чистый: файл и время читает вызывающий."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import presence_habits as ph

DEFAULT_STEP_MIN = ph.DEFAULT_STEP_MIN
SURE_CONFIDENCE = 0.95            # наблюдение с меньшей уверенностью в копилку не идёт: ошибка в статистике держится месяцами
MIN_SLOT_SECONDS = 600.0          # меньше стольких секунд в слоте приору из копилки верить нельзя: десять минут - один-два захода
BLEND_STRENGTH_S = 3600.0         # столько секунд в слоте уравнивают копилку и слова: час данных - половина веса
GUEST_SEP = ","
DAY_FORMAT = "%Y-%m-%d"


def guests_key(guests) -> str:
    """Состав гостей одной строкой: имена по алфавиту без повторов, пусто - никого нет."""
    return GUEST_SEP.join(sorted({g.strip().lower() for g in guests if g and g.strip()}))


@dataclass(frozen=True)
class StatPrior:
    places: dict[str, float]      # вероятность места среди времени дома, сумма единица
    seconds: float                # на скольких секундах держится оценка
    exact_guests: bool            # оценка по точному составу гостей; иначе по всем составам вместе


class HabitStats:
    def __init__(self, step_min: int = DEFAULT_STEP_MIN, days: dict | None = None, poses: dict | None = None) -> None:
        self.step_min = step_min
        self.days: dict[str, dict] = days or {}      # дата -> человек -> слот ЧЧ:ММ -> состав гостей -> место -> секунды
        self.poses: dict[str, dict] = poses or {}    # дата -> человек -> слот ЧЧ:ММ -> место -> поза -> секунды (SAM-120)

    def _slot(self, when: datetime) -> str:
        minute = (when.hour * 60 + when.minute) // self.step_min * self.step_min
        return f"{minute // 60:02d}:{minute % 60:02d}"

    def observe(self, person: str, place: str, when: datetime, seconds: float, guests=(), confidence: float = 1.0) -> bool:
        """Записать, что человек был в месте столько секунд; неуверенное наблюдение отбрасывается, возвращает признак записи."""
        if confidence < SURE_CONFIDENCE or seconds <= 0 or not place:
            return False
        slot = (self.days.setdefault(when.strftime(DAY_FORMAT), {}).setdefault(person.lower(), {})
                .setdefault(self._slot(when), {}).setdefault(guests_key(guests), {}))
        slot[place] = slot.get(place, 0.0) + seconds
        return True

    def observe_pose(self, person: str, place: str, pose: str, when: datetime, seconds: float, confidence: float = 1.0) -> bool:
        """Записать наблюдённую позу на месте; неуверенное наблюдение отбрасывается, возвращает признак записи."""
        if confidence < SURE_CONFIDENCE or seconds <= 0 or not place or not pose:
            return False
        slot = self.poses.setdefault(when.strftime(DAY_FORMAT), {}).setdefault(person.lower(), {}).setdefault(self._slot(when), {}).setdefault(place, {})
        slot[pose] = slot.get(pose, 0.0) + seconds
        return True

    def pose_prior(self, person: str, place: str, when: datetime, min_seconds: float = MIN_SLOT_SECONDS) -> StatPrior | None:
        """Доли поз на месте по копилке за дни с тем же днём недели и слотом; данных мало - None. В StatPrior.places лежат позы."""
        slot, weekday, totals = self._slot(when), when.weekday(), {}
        for day, people in self.poses.items():
            date = datetime.strptime(day, DAY_FORMAT).date()
            if date > when.date() or date.weekday() != weekday:
                continue
            for pose, seconds in people.get(person.lower(), {}).get(slot, {}).get(place, {}).items():
                totals[pose] = totals.get(pose, 0.0) + seconds
        seconds = sum(totals.values())
        return StatPrior({p: v / seconds for p, v in totals.items()}, seconds, False) if seconds >= min_seconds else None

    def _collect(self, person: str, when: datetime, key: str | None, window_days: int | None, as_of: datetime | None) -> dict[str, float]:
        """Секунды по местам за дни окна с тем же днём недели и слотом; key None - все составы гостей."""
        slot, weekday = self._slot(when), when.weekday()
        newest = (as_of or when).date()
        oldest = newest - timedelta(days=window_days) if window_days else None
        totals: dict[str, float] = {}
        for day, people in self.days.items():
            date = datetime.strptime(day, DAY_FORMAT).date()
            if date > newest or (oldest and date <= oldest) or date.weekday() != weekday:
                continue
            for guests, places in people.get(person.lower(), {}).get(slot, {}).items():
                if key is None or guests == key:
                    for place, seconds in places.items():
                        totals[place] = totals.get(place, 0.0) + seconds
        return totals

    def prior(self, person: str, when: datetime, guests=(), window_days: int | None = None, as_of: datetime | None = None,
              min_seconds: float = MIN_SLOT_SECONDS) -> StatPrior | None:
        """Приор места по копилке: точный состав гостей, при нехватке данных все составы вместе; данных мало - None."""
        for key in (guests_key(guests), None):
            totals = self._collect(person, when, key, window_days, as_of)
            seconds = sum(totals.values())
            if seconds >= min_seconds:
                return StatPrior({p: v / seconds for p, v in totals.items()}, seconds, key is not None)
        return None

    def occupancy(self) -> dict[str, float]:
        """Сколько секунд все люди суммарно бывали в каждом месте за всё время копилки (по любым гостям и слотам)."""
        total: dict[str, float] = {}
        for people in self.days.values():
            for slots in people.values():
                for guests in slots.values():
                    for places in guests.values():
                        for place, seconds in places.items():
                            total[place] = total.get(place, 0.0) + seconds
        return total

    def to_json(self) -> str:
        return json.dumps({"step_min": self.step_min, "days": self.days, "poses": self.poses}, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "HabitStats":
        raw = json.loads(text)
        return cls(raw.get("step_min", DEFAULT_STEP_MIN), raw.get("days"), raw.get("poses"))


def blend(words: dict[str, float], stats: StatPrior | None, strength_s: float = BLEND_STRENGTH_S) -> dict[str, float]:
    """Смесь слов и копилки: вес копилки растёт с секундами, «нет дома» остаётся как в словах, а места из копилки занимают долю «дома» из слов."""
    if stats is None:
        return dict(words)
    weight = stats.seconds / (stats.seconds + strength_s)
    home = sum(v for p, v in words.items() if p != ph.AWAY) or 1.0
    out = {p: (1.0 - weight) * v for p, v in words.items() if p != ph.AWAY}
    for place, q in stats.places.items():
        out[place] = out.get(place, 0.0) + weight * home * q
    if ph.AWAY in words:
        out[ph.AWAY] = words[ph.AWAY]
    return out


DEBUG_SOURCE = "debug"             # источник строки: ручная вероятность из панели отладки, в копилку не идёт (это тестовый инструмент)
SAVE_EVERY_S = 60.0                # как часто копилка пишется на диск


class HabitRecorder:
    """Наполняет копилку живыми данными и хранит её в файле: уверенные строки и ещё не выдохшиеся слова о месте.

    Зависимости приходят снаружи: как назвать место по координатам, как записать файл. Хаб только вызывает record и maybe_save."""

    def __init__(self, stats: HabitStats, place_of: Callable[[float, float], str | None], path: Path | None,
                 write: Callable[[Path | None, str], None], save_every_s: float = SAVE_EVERY_S, now: float = 0.0) -> None:
        self.stats, self._place_of, self._path, self._write, self._every, self._last_save = stats, place_of, path, write, save_every_s, now

    @classmethod
    def load(cls, path: Path | None) -> HabitStats:
        """Копилка из файла; нет файла или он испорчен - чистая, сбой записывается одной строкой в stdout."""
        if path and path.exists():
            try:
                return HabitStats.from_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as e:
                print(f"[Presence] копилка привычек не прочитана из {path}: {e}; начинаю с чистой")
        return HabitStats()

    def record(self, rows: list[dict], told: list[dict], seconds: float, now: float) -> None:
        """В копилку идут уверенные строки без ручной вероятности и слова о месте; гости - другие уверенные люди в этот кадр.
        Поза пишется только наблюдённая (поле pose_seen строки), выбранная радаром поза в копилку не идёт."""
        sure: dict[str, tuple[str, float]] = {}                                    # человек -> (место, уверенность)
        when = datetime.fromtimestamp(now)
        for row in rows:
            if row.get("x") is None or row.get("z") is None or DEBUG_SOURCE in (row.get("sources") or []):
                continue
            if (row.get("confidence") or 0.0) >= SURE_CONFIDENCE and (place := self._place_of(row["x"], row["z"])):
                sure[row["person"]] = (place, row["confidence"])
                if row.get("pose_seen"):
                    self.stats.observe_pose(row["person"], place, row["pose_seen"], when, seconds, row["confidence"])
        for item in told:
            sure[item["person"]] = (item["place"], SURE_CONFIDENCE)
        for person, (place, confidence) in sure.items():
            self.stats.observe(person, place, when, seconds, [other for other in sure if other != person], confidence)

    def save(self) -> None:
        self._write(self._path, self.stats.to_json())

    def maybe_save(self, now: float) -> None:
        if now - self._last_save >= self._every:
            self._last_save = now
            self.save()
