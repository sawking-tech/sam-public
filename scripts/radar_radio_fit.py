"""Подгонка многостенной модели уровня по отпечаткам (SAM-118): одна модель на диапазон роутера (2,4 и 5 ГГц), потому что телефон на разных диапазонах видит разный дом; устройства одного диапазона различаются только сдвигом L0.

Вход: отпечатки `data/presence_fingerprints.json` (устройство@интерфейс -> место -> уровни), положения мест `data/radio_controls.json` (место -> x, z, по размеченной карте)
и план. Выход: `data/presence_radio_model.json`, откуда берёт параметры карта покрытия. Показатель затухания n подбирается по ошибке «без одной точки».

Запуск: python scripts/radar_radio_fit.py [fingerprints.json] [radio_controls.json] [layout.json] [out.json]"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))        # запуск из любой папки: корень репозитория на пути импорта
from radar import presence_radio_map as rm                       # noqa: E402

DEFAULT_FINGERPRINTS = "data/presence_fingerprints.json"
DEFAULT_CONTROLS = "data/radio_controls.json"
DEFAULT_LAYOUT = "data/home_layout.json"
DEFAULT_OUT = "data/presence_radio_model.json"
EXPONENTS = (2.0, 2.5, 3.0)                     # какие n пробуем: от свободного пространства до плотной застройки
IFACE_BANDS = {"phy0-ap0": "2,4 ГГц", "phy1-ap0": "5 ГГц"}   # интерфейс роутера -> диапазон
IFACE_SEP = "@"                                  # ключ отпечатка: имя устройства, @, интерфейс
MIN_CONTROLS = rm.MIN_CONTROLS + 3               # меньше точек подгонка с тремя параметрами и выбором n ничего не стоит
DEFAULT_DEVICE_FIRST = "phone"                    # у этого устройства модель первой в списке: страница открывает её по умолчанию


def load(path: str) -> dict:
    with open(path, encoding="utf8") as f:
        return json.load(f)


def mean_level(rows: list) -> float:
    """Средний уровень пары устройство-место; запись - число или список, где уровень первый."""
    return float(np.mean([r if isinstance(r, (int, float)) else r[0] for r in rows]))


def band_rows(places_by_key: dict, controls: dict) -> dict[str, list[tuple[str, float, float, float]]]:
    """Строки подгонки по диапазонам: (устройство, x, z, средний уровень) у каждого места с известным положением, всех устройств диапазона."""
    rows: dict[str, list[tuple[str, float, float, float]]] = {}
    for key, places in places_by_key.items():
        device, _, iface = key.partition(IFACE_SEP)
        for place, levels in places.items():
            if place in controls:
                rows.setdefault(iface, []).append((device, controls[place]["x"], controls[place]["z"], mean_level(levels)))
    return rows


def _design(rows: list, devices: list[str], layout: dict, n: float) -> tuple[np.ndarray, np.ndarray]:
    """Матрица подгонки и целевой вектор: уровень + 10 n lg d = L0 + сдвиг устройства - стена * толщина; сдвиг опорного устройства нулевой."""
    router = (layout["router"]["x"], layout["router"]["z"])
    xy = np.array([[r[1], r[2]] for r in rows])
    dist = rm.distance3d(xy, router)
    thick = rm.wall_thickness_on_path(layout["walls"], router, xy, rm.doorway_openings(layout.get("objects") or []))
    target = np.array([r[3] for r in rows]) + 10.0 * n * np.log10(dist)
    shifts = [np.array([1.0 if r[0] == device else 0.0 for r in rows]) for device in devices[1:]]
    return np.column_stack([np.ones(len(rows)), -thick, *shifts]), target


def _leave_one_out(rows: list, devices: list[str], layout: dict, n: float) -> float:
    """Ошибка в дБ на исключённой строке: модель по остальным, предсказание для исключённой."""
    matrix, target = _design(rows, devices, layout, n)
    errors = []
    for i in range(len(rows)):
        keep = [j for j in range(len(rows)) if j != i]
        beta, *_ = np.linalg.lstsq(matrix[keep], target[keep], rcond=None)
        errors.append(target[i] - matrix[i] @ beta)
    return float(np.sqrt(np.mean(np.square(errors))))


def fit_band(rows: list, layout: dict) -> dict | None:
    """Модель одного диапазона по строкам всех его устройств: общие n и потери стены, L0 опорного устройства и сдвиг L0 у остальных. None, если строк мало."""
    if len(rows) < MIN_CONTROLS:
        return None
    devices = sorted({r[0] for r in rows}, key=lambda d: (not d.startswith(DEFAULT_DEVICE_FIRST), d))
    loo = {n: _leave_one_out(rows, devices, layout, n) for n in EXPONENTS}
    best = min(loo, key=loo.get)
    matrix, target = _design(rows, devices, layout, best)
    beta, *_ = np.linalg.lstsq(matrix, target, rcond=None)
    return {"l0_db": float(beta[0]), "n": best, "wall_db_per_m": float(beta[1]), "controls": len(rows), "leave_one_out_rms_db": loo[best],
            "offsets": {device: float(shift) for device, shift in zip(devices, [0.0, *beta[2:]])}}


def fit_all(fingerprints: dict, controls: dict, layout: dict) -> dict:
    """Файл подгонки: по одной модели на диапазон и сдвиг L0 каждого устройства на каждом диапазоне (ключ «устройство@интерфейс»)."""
    models, offsets = {}, {}
    for iface, rows in band_rows(fingerprints, controls).items():
        fitted = fit_band(rows, layout)
        if fitted:
            shifts = fitted.pop("offsets")
            models[iface] = {"iface": iface, "band": IFACE_BANDS.get(iface, iface), **fitted}
            offsets.update({f"{device}{IFACE_SEP}{iface}": round(shift, 3) for device, shift in shifts.items()})
    ordered = sorted(models.items(), key=lambda kv: (kv[1]["band"] != "5 ГГц", kv[0]))
    return {"models": dict(ordered), "device_offsets_db": offsets}


def main(argv: list[str]) -> int:
    paths = (argv + [DEFAULT_FINGERPRINTS, DEFAULT_CONTROLS, DEFAULT_LAYOUT, DEFAULT_OUT][len(argv):])[:4]
    fingerprints_path, controls_path, layout_path, out_path = paths
    result = fit_all(load(fingerprints_path), load(controls_path), load(layout_path))
    with open(out_path, "w", encoding="utf8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    for key, m in result["models"].items():
        print(f"{key}: {m['band']}, n {m['n']}, стена {m['wall_db_per_m']:.0f} дБ/м, точек {m['controls']}, без одной {m['leave_one_out_rms_db']:.1f} дБ")
    for key, shift in result["device_offsets_db"].items():
        print(f"  сдвиг L0 {key}: {shift:+.1f} дБ")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
