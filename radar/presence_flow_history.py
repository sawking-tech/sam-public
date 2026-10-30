"""История потока положения (SAM-118): куда поток ставил каждого субъекта по секундам, с вероятностями мест.

Строка на субъекта в секунду: время, кто (телефон с диапазоном или человек), точка, названные места с вероятностями и доля пятна точки. Файл на сутки,
старые сутки удаляются по сроку из конфига. Из строк считается тепловая карта за период для SAM («где человек бывал в среду вечером») и берутся только
уверенные строки для обучения приоритетов: слабая строка это догадка, по ней учиться нельзя. Запись и чтение файлов здесь, решения (когда писать, что удалять,
что считать уверенным) в чистых функциях."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence

HISTORY_EVERY_S = 1.0              # на субъекта не чаще: поток шагает раз в секунду
HISTORY_KEEP_DAYS = 14             # сколько суток хранить: привычки учатся по неделям, две недели покрывают обе недели расписания
HISTORY_DAYS_ENV = "SAM_PRESENCE_FLOW_HISTORY_DAYS"
HISTORY_DIR_ENV = "SAM_PRESENCE_FLOW_HISTORY_DIR"
DEFAULT_HISTORY_DIR = "data/presence_flow"
PLACES_KEPT = 5                    # мест с вероятностями в строке: остальные слишком слабые, чтобы на них опираться
SURE_SHARE = 0.5                   # строка уверенная, если верхнее место набрало не меньше этой вероятности: только такие идут в обучение приоритетов
HEAT_CELL_M = 0.25                 # клетка тепловой карты за период, как у карты посещений страницы
DAY_FORMAT = "%Y%m%d"
FILE_PREFIX = "flow-"
PROBABILITY_DIGITS = 3


def row_of(subject: Mapping, now: float) -> dict:
    """Строка истории из записи состояния потока (`LiveFlow.state`, устройство или человек): точки нет - строки нет."""
    point = subject.get("point")
    if not point:
        return {}
    places = [[name_p["place"], round(float(name_p["p"]), PROBABILITY_DIGITS)] for name_p in (subject.get("places") or [])[:PLACES_KEPT]]
    return {"t": round(now, 2), "id": f"{subject['id']}|{subject['iface']}", "name": subject.get("name"), "x": point["x"], "z": point["z"], "places": places,
            "sure": places[0][1] if places else 0.0}


def is_sure(row: Mapping, min_sure: float = SURE_SHARE) -> bool:
    return float(row.get("sure", 0.0)) >= min_sure


def stale_days(files: Iterable[str], today: datetime, keep_days: int = HISTORY_KEEP_DAYS) -> list[str]:
    """Файлы суток старше срока: последние keep_days суток остаются (включая сегодня)."""
    oldest = (today - timedelta(days=keep_days - 1)).strftime(DAY_FORMAT)
    return sorted(name for name in files if name.startswith(FILE_PREFIX) and name[len(FILE_PREFIX):len(FILE_PREFIX) + 8] < oldest)


def heat(rows: Iterable[Mapping], cell_m: float = HEAT_CELL_M, min_sure: float = 0.0) -> dict[str, float]:
    """Секунды по клеткам пола за период: ключ «столбец,ряд» в клетках cell_m; слабее min_sure не считаются."""
    out: dict[str, float] = {}
    for row in rows:
        if not is_sure(row, min_sure):
            continue
        key = f"{math.floor(row['x'] / cell_m)},{math.floor(row['z'] / cell_m)}"
        out[key] = out.get(key, 0.0) + HISTORY_EVERY_S
    return out


class FlowHistory:
    def __init__(self, directory: Path | None, every_s: float = HISTORY_EVERY_S, keep_days: int = HISTORY_KEEP_DAYS) -> None:
        self.directory, self.every_s, self.keep_days = directory, every_s, keep_days
        self._last: dict[str, float] = {}

    def record(self, subjects: Sequence[Mapping], now: float) -> int:
        """Строки по субъектам состояния потока в файл суток; на субъекта не чаще every_s. Нет папки - не пишем."""
        if not self.directory:
            return 0
        rows = []
        for subject in subjects:
            row = row_of(subject, now)
            if not row or now - self._last.get(row["id"], -math.inf) < self.every_s:
                continue
            self._last[row["id"]] = now
            rows.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        if not rows:
            return 0
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / f"{FILE_PREFIX}{datetime.fromtimestamp(now).strftime(DAY_FORMAT)}.jsonl").open("a", encoding="utf-8") as f:
                f.write("\n".join(rows) + "\n")
            self._prune(datetime.fromtimestamp(now))
        except OSError as e:
            print(f"[Presence] история потока не записана в {self.directory}: {e}; проверь {HISTORY_DIR_ENV}")
            return 0
        return len(rows)

    def _prune(self, today: datetime) -> None:
        for name in stale_days((p.name for p in self.directory.glob(f"{FILE_PREFIX}*.jsonl")), today, self.keep_days):
            (self.directory / name).unlink(missing_ok=True)

    def read_range(self, since: float, until: float, subject: str | None = None, min_sure: float = 0.0) -> list[dict]:
        """Строки за отрезок по возрастанию времени; subject - часть ключа или имени (без учёта регистра), min_sure - порог уверенности."""
        if not self.directory or not self.directory.is_dir():
            return []
        first_day, last_day = (datetime.fromtimestamp(t).strftime(DAY_FORMAT) for t in (since, until))
        key = (subject or "").lower()
        out = []
        for path in sorted(self.directory.glob(f"{FILE_PREFIX}*.jsonl")):
            day = path.stem[len(FILE_PREFIX):]
            if day < first_day or day > last_day:
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue                                   # оборванная строка при остановке записи
                if since <= row["t"] <= until and is_sure(row, min_sure) and (not key or key in row["id"].lower() or key in (row.get("name") or "").lower()):
                    out.append(row)
        return sorted(out, key=lambda row: row["t"])
