"""Треки устройств радара (SAM-105): ряд «время - уровень сигнала» по каждому устройству, копится и хранится.

«треки устройств сохраняем тоже»: по такому ряду видно, как
устройство носили по квартире (сигнал падает за стеной и растёт у роутера), и из рядов потом уточняется карта. Файл на
сутки, строка на отсчёт, отсчёт не чаще TRACK_EVERY_S на устройство; старые сутки удаляются, когда ряды занимают больше
потолка. Запись и чтение файлов здесь, решения (когда писать, что удалять) - в чистых функциях.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

TRACK_EVERY_S = 1.0          # на устройство не чаще: опрос роутера идёт раз в 0,5 с, шаг человека длиннее
MAX_TRACK_BYTES = 40 * 2**20 # потолок всех суток вместе: около сотни тысяч строк ещё читаются быстро
FRESH_MS = 3000              # молчащая станция показывает старый сигнал, её отсчёт в ряд не идёт
DAY_FORMAT = "%Y%m%d"
FILE_PREFIX = "tracks-"
RAW_PREFIX = "raw-"            # сырая запись: каждый круг опроса и каждая станция, с возрастом кадра; отсекается потом по отметкам проходки
RAW_MAX_BYTES = 500 * 2**20  # потолок сырой записи: два круга в секунду по трём станциям - около 150 МБ в сутки
RAW_ENV = "SAM_PRESENCE_RAW_TRACKS"


def due(last_written: float | None, now: float, every_s: float = TRACK_EVERY_S) -> bool:
    return last_written is None or now - last_written >= every_s


def to_delete(files: list[tuple[str, int]], max_bytes: int = MAX_TRACK_BYTES) -> list[str]:
    """Имена самых старых суток, которые надо убрать, чтобы уложиться в потолок; последние сутки остаются всегда."""
    ordered = sorted(files)                                   # имя содержит дату, по имени - по времени
    total, out = sum(size for _, size in ordered), []
    for name, size in ordered[:-1]:
        if total <= max_bytes:
            break
        out.append(name)
        total -= size
    return out


class TrackLog:
    def __init__(self, directory: Path | None, every_s: float = TRACK_EVERY_S, max_bytes: int = MAX_TRACK_BYTES, prefix: str = FILE_PREFIX,
                 fresh_ms: int | None = FRESH_MS) -> None:
        """fresh_ms None - пишется и молчащая станция (её кадр стареет, возраст в поле in), every_s 0 - каждый круг опроса."""
        self.directory = directory
        self.every_s = every_s
        self.max_bytes = max_bytes
        self.prefix, self.fresh_ms = prefix, fresh_ms
        self._last: dict[str, float] = {}

    def record(self, stations, now: float) -> int:
        """Отсчёты свежих станций в файл суток; возвращает число записанных строк. Нет папки - не пишем."""
        if not self.directory:
            return 0
        rows = []
        for s in stations:
            if (self.fresh_ms is not None and s.inactive_ms > self.fresh_ms) or not due(self._last.get(s.sid), now, self.every_s):
                continue
            self._last[s.sid] = now
            row = {"t": round(now, 2), "id": s.sid, "name": s.name, "db": round(s.signal_db, 1), "if": s.iface, "in": s.inactive_ms}     # in - мс с последнего кадра станции
            if s.link is not None and s.link.as_row():
                row["ln"] = s.link.as_row()                   # признаки линка: независимые измерения того же положения
            rows.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        if not rows:
            return 0
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            day = datetime.fromtimestamp(now).strftime(DAY_FORMAT)
            with (self.directory / f"{self.prefix}{day}.jsonl").open("a", encoding="utf-8") as f:
                f.write("\n".join(rows) + "\n")
            self._prune()
        except OSError as e:
            print(f"[Presence] треки не записаны в {self.directory}: {e}; проверь SAM_PRESENCE_TRACKS_DIR")
            return 0
        return len(rows)

    def _prune(self) -> None:
        files = [(p.name, p.stat().st_size) for p in self.directory.glob(f"{self.prefix}*.jsonl")]
        for name in to_delete(files, self.max_bytes):
            (self.directory / name).unlink(missing_ok=True)

    def read(self, name_part: str, since: float, limit: int = 5000) -> list[dict]:
        """Ряд устройства, чьё имя содержит name_part (без учёта регистра), начиная с since; последние limit отсчётов."""
        key = name_part.lower()
        return [row for row in self._rows(since) if key in (row.get("name") or "").lower()][-limit:]

    def read_range(self, since: float, until: float) -> list[dict]:
        """Отсчёты всех устройств на отрезке [since, until] по возрастанию времени: перемотка кормит ими поток как живыми опросами."""
        return sorted((row for row in self._rows(since, until) if row["t"] <= until), key=lambda row: row["t"])

    def span(self) -> tuple[float, float] | None:
        """Первая и последняя секунда записи по файлам суток; записи нет - None."""
        files = self._files()
        if not files:
            return None
        first = next((row["t"] for row in self._parse(files[0])), None)
        last = next((row["t"] for row in reversed(list(self._parse(files[-1])))), None)
        return None if first is None or last is None else (first, last)

    def _files(self) -> list[Path]:
        return sorted(self.directory.glob(f"{self.prefix}*.jsonl")) if self.directory and self.directory.is_dir() else []

    def _rows(self, since: float, until: float | None = None):
        """Строки всех суток с since (и не позже суток until): файл суток пропускается целиком, если его день вне отрезка."""
        first_day = datetime.fromtimestamp(since).strftime(DAY_FORMAT)
        last_day = None if until is None else datetime.fromtimestamp(until).strftime(DAY_FORMAT)
        for path in self._files():
            day = path.stem[len(self.prefix):]
            if day < first_day or (last_day is not None and day > last_day):
                continue
            for row in self._parse(path):
                if row["t"] >= since:
                    yield row

    @staticmethod
    def _parse(path: Path):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                yield json.loads(line)
            except ValueError:
                continue                                   # оборванная строка при остановке записи
