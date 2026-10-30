"""Пороги детектора радара (SAM-105), которые крутят ползунками в панели отладки страницы.

Чистый модуль: значения по умолчанию - прежние константы детектора и хаба, границы и шаг ползунков, разбор
присланного словаря. Файл и сеть - в хабе. Чем ниже `noise_floor_db` и `full_activity_db` и чем выше
`baseline_rate`-подстройка, тем чаще радар видит движение; чувствительность растёт вместе с ложными срабатываниями.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# (имя, подпись, минимум, максимум, шаг, что делает, группа). Группы: detector - порог движения, animation - шаги и паузы, auto - число выставляет страница, ползунка нет.
KNOBS = (
    ("noise_floor_db", "Порог шума, дБ", 0.0, 3.0, 0.05, "дрожание сигнала выше базы на столько и более считается движением; ниже - вероятность движения растёт", "detector"),
    ("full_activity_db", "Полная активность, дБ", 0.5, 12.0, 0.1, "превышение, при котором активность линии равна 1; ниже - линия быстрее становится активной", "detector"),
    ("baseline_rate", "Скорость базы", 0.0, 0.2, 0.005, "как быстро база подстраивается под спокойную комнату; выше - тихое принимается за норму скорее", "detector"),
    ("motion_min", "Минимум активности в карту", 0.0, 0.6, 0.01, "линия слабее этой активности не попадает в карту посещений; ниже - карта чувствительнее", "detector"),
    ("walk_pause_s", "Пауза в позе, с", 0.2, 4.0, 0.1, "сколько человек стоит в позе после рывка роя, прежде чем сделать следующий шаг; дольше - поза читается лучше", "animation"),
    ("walk_spacing_m", "Метров на реперную точку", 0.5, 4.0, 0.1, "чем длиннее путь, тем больше точек: при 1,5 м на путь в 7 м выходит около пяти; углы маршрута (двери) остаются точками всегда", "animation"),
    ("walk_burst_s", "Длительность рывка, с", 0.1, 2.0, 0.01, "считает страница по ползункам анимации и присылает сюда сама, чтобы шаги не наезжали друг на друга", "auto"),
    ("hug_force", "Обнимать сейчас", 0.0, 1.0, 1.0, "1 - обнимающиеся лежат в объятиях всё время, чтобы подобрать позу; 0 - по ритму, раз в несколько минут", "animation"),
)
GROUPS = ("detector", "animation")          # порядок групп в панели; auto не показывается


@dataclass
class Tuning:
    noise_floor_db: float = 0.6
    full_activity_db: float = 4.0
    baseline_rate: float = 0.02
    motion_min: float = 0.12
    walk_pause_s: float = 1.0
    walk_burst_s: float = 0.59
    hug_force: float = 0.0
    walk_spacing_m: float = 0.9

    def update(self, data: dict) -> None:
        """Применить присланные значения; чужие ключи и не-числа - ValueError, число вне границ прижимается к ним."""
        limits = {k[0]: (k[2], k[3]) for k in KNOBS}
        unknown = [k for k in data if k not in limits]
        if unknown:
            raise ValueError(f"нет такого параметра: {', '.join(sorted(unknown))}")
        parsed = {}
        for name, raw in data.items():
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"{name}: ждали число, пришло {raw!r}")
            lo, hi = limits[name]
            parsed[name] = min(hi, max(lo, float(raw)))
        for name, value in parsed.items():
            setattr(self, name, value)

    def to_dict(self) -> dict:
        return asdict(self)


def knobs_payload() -> list[dict]:
    """Описание ползунков для страницы: имя, подпись, границы, шаг, пояснение, группа, flag (0 или 1: страница рисует галку, не ползунок) (auto сюда не входит: его выставляет сама страница)."""
    return [{"name": n, "label": label, "min": lo, "max": hi, "step": step, "hint": hint, "group": group, "flag": (lo, hi, step) == (0.0, 1.0, 1.0)}
            for n, label, lo, hi, step, hint, group in KNOBS if group in GROUPS]


def defaults() -> Tuning:
    return Tuning()
