"""Отпечатки уровня сигнала по местам (SAM-117): с одного роутера положение по Wi-Fi не определить, но уровень сигнала устройства в известном месте повторяется.
Каждый раз, когда человек называет место («я на диване»), радар записывает, какой уровень был у устройства; со временем у устройства копится набор «уровень -> место».
Чистый модуль: ряды уровней и классификация, файл и время - в хабе."""
from __future__ import annotations

import json
import math
import os
import statistics

KEEP_LAST = 60              # уровней на пару «устройство - место»: старые вытесняются, отпечаток следует за перестановкой мебели и роутера
MIN_SAMPLES = 3             # меньше стольких записей пару не берём в классификацию: одно слово человека - ещё не отпечаток
IFACE_SEP = "@"               # ключ устройства: имя и интерфейс роутера; на разных диапазонах уровень одного телефона разный
TOLERANCE_DB = 4.0          # уровень ближе этого к среднему отпечатка - место совпало
OUTLIER_SHARE = 0.05       # доля отсчётов, не похожих ни на одно место (рука на телефоне, чужое тело): один выброс не должен назначать место уверенно
LEVEL_SPAN_DB = 60.0        # ширина диапазона уровней дома (около -30...-90 дБ): плотность выброса равномерна по нему
MIN_SD_DB = 3.0             # нижний предел шума места: замеры дали медиану 1,7-4,1 дБ по рядам, пять отсчётов подряд шум недооценивают, а стоячая волна даёт скачки


def device_key(name: str | None, iface: str) -> str:
    """Ключ устройства в отпечатках: имя и интерфейс (диапазон), строчными."""
    return f"{name or ''}{IFACE_SEP}{iface}".lower()


class Fingerprints:
    def __init__(self, data: dict[str, dict[str, list[float]]] | None = None) -> None:
        self.data: dict[str, dict[str, list[float]]] = data or {}      # устройство (строчными) -> место (строчными) -> последние уровни, дБ

    def add(self, device: str, place: str, db: float) -> int:
        """Записать уровень; возвращает, сколько записей теперь у пары."""
        series = self.data.setdefault(device.lower(), {}).setdefault(place.lower(), [])
        series.append(round(float(db), 1))
        del series[:-KEEP_LAST]
        return len(series)

    def remove(self, place: str, device_part: str | None = None) -> int:
        """Стереть место у устройств, чьё имя содержит device_part (у всех, если не задано): ошибочный замер не должен ждать перезапуска; возвращает число стёртых рядов."""
        low, part, removed = place.lower(), (device_part or "").lower(), 0
        for device, places in self.data.items():
            if part in device and places.pop(low, None) is not None:
                removed += 1
        return removed

    def classify(self, device: str, db: float, tolerance: float = TOLERANCE_DB) -> str | None:
        """Ближайшее по среднему уровню место устройства (не менее MIN_SAMPLES записей, не дальше tolerance); нет такого - None."""
        best = None
        for place, series in self.data.get(device.lower(), {}).items():
            if len(series) < MIN_SAMPLES:
                continue
            gap = abs(statistics.fmean(series) - db)
            if gap <= tolerance and (best is None or gap < best[0]):
                best = (gap, place)
        return best[1] if best else None

    def place_probabilities(self, device: str, db: float, prior: dict[str, float] | None = None, min_sd: float = MIN_SD_DB,
                            power: float = 1.0) -> dict[str, float]:
        """Вероятность каждого места устройства по уровню: нормальное правдоподобие вокруг среднего отпечатка, шум места не ниже min_sd,
        априор по местам (равный, если не задан); power < 1 ослабляет отсчёт, почти повторяющий прошлый; сумма единица. Нет мест с достаточным числом записей - пустой словарь."""
        weights = {}
        for place, series in self.data.get(self.stand_in(device), {}).items():
            if len(series) < MIN_SAMPLES:
                continue
            sd = max(min_sd, statistics.pstdev(series))
            z = (db - statistics.fmean(series)) / sd
            density = (1.0 - OUTLIER_SHARE) * math.exp(-0.5 * z * z) / (sd * math.sqrt(2.0 * math.pi)) + OUTLIER_SHARE / LEVEL_SPAN_DB
            weights[place] = (prior or {}).get(place, 1.0) * density ** power
        total = sum(weights.values())
        return {place: w / total for place, w in weights.items()} if total > 0 else {}

    def stand_in(self, device: str) -> str:
        """Чьи отпечатки брать устройству: свои, а у нового (гость, свежий телефон) - устройства того же диапазона с самым
        длинным общим началом имени (та же модель), при равенстве - с большим числом замеров. Без этого новый телефон
        не получал ни места, ни точек: отпечатки сняты только с телефонов проходок."""
        key = device.lower()
        if key in self.data:
            return key
        iface = key.rpartition(IFACE_SEP)[2]
        same_band = [k for k in self.data if k.rpartition(IFACE_SEP)[2] == iface]
        if not same_band:
            return key
        prefix = lambda k: len(os.path.commonprefix([k, key]))
        return max(same_band, key=lambda k: (prefix(k), sum(len(s) for s in self.data[k].values())))

    def summary(self) -> list[dict]:
        return [{"device": device, "place": place, "n": len(series), "mean_db": round(statistics.fmean(series), 1), "min_db": min(series), "max_db": max(series)}
                for device, places in sorted(self.data.items()) for place, series in sorted(places.items())]

    def to_json(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "Fingerprints":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("отпечатки: ждали словарь устройство -> место -> уровни")
        return cls({str(d): {str(p): [float(v) for v in vs] for p, vs in ps.items()} for d, ps in data.items()})
