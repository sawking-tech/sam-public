"""Живой поток положения (SAM-118): опросы станций роутера идут в окно путей по субъекту, наружу уходит состояние для страницы и для SAM.

Корень композиции потока: из плана строится мир, из подогнанных моделей уровня - ожидаемый уровень по клеткам для пары «устройство и диапазон», из карты предпочтений
устройств и привычек - поле предпочтений, из названных мест - список мест с вероятностями. Срез потока это секунда: опросы роутера приходят чаще, из них берётся медиана за срез.
Станция без модели уровня не считается и попадает в `skipped`. Хаб только передаёт опросы и отдаёт состояние, считать здесь можно без SSH. Модуль чистый: время приходит параметром."""
from __future__ import annotations

import os

import copy
from datetime import datetime
from statistics import median
from typing import Callable, Mapping, Sequence

import numpy as np

from . import presence_growth as pg
from . import presence_habits as ph
from . import presence_level_delta as pld
from . import presence_motion as pm
from . import presence_near as pn
from . import presence_paths as ppaths
from . import presence_placed as pl
from . import presence_preference as pp
from . import presence_prefs as prefs_map
from . import presence_rings as pr
from . import presence_rssi as rs
from . import presence_subject as ps
from . import presence_topview as ptv
from . import presence_window as pw_
from . import presence_walk_fields as pwf
from . import presence_world as pw
from .presence_nav import Nav
from .presence_radio_map import WallModel, doorway_openings, expected_levels
from .presence_random import Randomness
from .presence_report import PlaceReport

RADIO_MODEL_ENV = "SAM_RADIO_MODEL"
WINDOW_SLICES_ENV = "SAM_PRESENCE_WINDOW_SLICES"
MOTION_SIGMA_ENV = "SAM_PRESENCE_MOTION_SIGMA_M"
MOTION_SIGMA_M: float | None = None   # инерция перехода, м: None - равномерно по радиусу локальности; значение подбирается проигрышем (SAM-118)
STAY_SHARE_ENV = "SAM_PRESENCE_STAY_SHARE"
STAY_SHARE = 0.0                       # доля «остался в клетке» в переходе: подбирается проигрышем и симуляцией дня (SAM-118)
DELTA_WEIGHT_ENV = "SAM_PRESENCE_DELTA_WEIGHT"
DELTA_WEIGHT = 0.0                     # вес разности уровня как свидетельства о переходе; нуль выключает фактор: на размеченной прогулке он ухудшает вершину с 65% до 2-56% при любом разбросе (SAM-118), помогает только на синтетике с тихим сигналом
DELTA_SD_ENV = "SAM_PRESENCE_DELTA_SD_DB"
DELTA_SD_DB = pld.DELTA_SD_DB          # разброс разности уровней соседних секунд, дБ
HOLD_RATIO_ENV = "SAM_PRESENCE_POINT_HOLD_RATIO"
HOLD_TICKS_ENV = "SAM_PRESENCE_POINT_HOLD_TICKS"     # липкость пятна точки: перевес веса и число тиков подряд; значения по умолчанию в presence_cloud
PREFERENCE_WEIGHT_ENV = "SAM_PRESENCE_PREFERENCE_WEIGHT"
PREFERENCE_WEIGHT = 0.1          # степень карты предпочтений телефона на выходе: на размеченных стоянках вес 1 даёт вершину 31% против 65% без неё, 0,25 даёт 63%, 0,1 даёт 64% (SAM-118)
VIEW_PATHS_ENV = "SAM_PRESENCE_VIEW_PATHS"
VIEW_PATHS = 10                 # путей окна для слоя: десять самых широких из сотни (`presence_paths.widest`), точка и метрики считаются по полной выборке шага
LIVE_START_CERTAINTY = 0.95         # боевое начало окна: на записи для окна 60 лучшая строка (вершина 39% против 26%, тройка 67% против 42%); подробности и пределы доказательства в SAM-118
LIVE_START_MIN_SHARE = 0.3         # слабые клетки начала (ниже 0,3 максимума) отбрасываются; на записи 0,1 хуже нуля, 0,3 лучше: одна запись и один сид, проверить на второй
START_CERTAINTY_ENV = "SAM_PRESENCE_START_CERTAINTY"      # доля прошлого в начале окна: 1 - только прошлое, остаток ровный фон как путь возврата
START_MIN_SHARE_ENV = "SAM_PRESENCE_START_MIN_SHARE"      # клетки начала ниже этой доли максимума отбрасываются: пути стартуют с точки прошлого и ещё нескольких вероятных
DEFAULT_RADIO_MODEL_FILE = "data/presence_radio_model.json"     # подгонка: python scripts/radar_radio_fit.py
SLICE_S = 1.0                   # срез потока: опросы роутера чаще, берётся медиана за срез; параметры подобраны на записи с секундным срезом
FRESH_MS = 3000                 # молчащая станция показывает старый сигнал, её отсчёт в срез не идёт: как в записи треков
STALE_S = 60.0                  # от устройства больше минуты нет свежих опросов: окно сбрасывается (за минуту человек мог уйти куда угодно), из состояния оно уходит; телефон сидит на одном диапазоне, замолчавший не показывается (или-или, диапазоны не смешиваются)
PROBABILITY_DIGITS = 4          # знаков в вероятностях состояния: страница не различает меньшего
ALL_SECONDS = -1                # second=-1: законные пути и пути окна по всем секундам сразу; стоп времени забирает их один раз
GROWTH_AREA_DIGITS = 2          # площадь поля секунды в м2 с точностью до сотых: клетка мира 0,09 м2 и крупнее
PERSON_IFACE = "person"         # «диапазон» субъекта человека в состоянии: страница различает людей и устройства по нему
PERSON_ID = "person:"           # ключ субъекта человека в состоянии: person:anna


def expected_db(world: pw.World, layout: dict, fitted: Mapping) -> np.ndarray:
    """Ожидаемый уровень по клеткам мира для подогнанной модели «устройство и диапазон»."""
    model = WallModel(fitted["l0_db"], fitted["n"], fitted["wall_db_per_m"], fitted.get("near_gain_db", 0.0))
    return expected_levels(model, world.points, layout["walls"], (layout["router"]["x"], layout["router"]["z"]), doorway_openings(layout["objects"]))


def phone_preference(prefs: Mapping, device_part: str, place_xy: Mapping[str, tuple[float, float]], world: pw.World, weight: float = PREFERENCE_WEIGHT,
                     layout: Mapping | None = None) -> pp.Preference:
    """Карта предпочтений устройства по клеткам мира: вес каждого места из карты устройств ложится на зону места сверху (подошва предмета или комната по точке места,
    `presence_topview.place_zones`), без плана - пятном вокруг точки; ровный фон внутри поля."""
    prior = {place: prefs_map.weight(prefs, device_part, prefs_map.place_key(place)) for place in place_xy}
    zones = ptv.place_zones(dict(place_xy), dict(layout), world.points) if layout is not None else None
    field = ph.cell_field(prior, world.points, place_xy, zones=zones)
    return pp.Preference(lambda now: field, weight=weight)


RING_SD_KEY = "device_ring_sd_db"        # в файле модели уровня: «устройство@диапазон» -> ширина кольца, дБ


def resolve_models(data: Mapping) -> Mapping[str, Mapping]:
    """Модели уровня для потока по ключу «устройство@диапазон» из файла подгонки. Файл с `device_offsets_db` хранит одну модель на диапазон и сдвиг L0 каждого устройства: здесь они
    собираются в запись устройства. Прежний файл (модель на устройство и диапазон) отдаётся как есть. Необязательная `device_ring_sd_db` - ширина кольца уровня пары
    «устройство@диапазон» (шум пары вместе с ошибкой модели); нет записи - кольцо по умолчанию (`presence_rings.RING_SD_DB`)."""
    models, offsets, ring_sd = data.get("models", {}), data.get("device_offsets_db"), data.get(RING_SD_KEY) or {}
    if not offsets:
        return models
    return {key: {**models[iface], "l0_db": models[iface]["l0_db"] + shift, **({"ring_sd_db": ring_sd[key]} if key in ring_sd else {})}
            for key, shift in offsets.items() if (iface := key.rpartition("@")[2]) in models}


def model_for(models: Mapping[str, Mapping], name: str | None, iface: str) -> Mapping | None:
    """Подогнанная модель для устройства по имени из аренды роутера и диапазону. Своей нет (новый телефон, гость) - модель
    устройства того же диапазона с самым длинным общим началом имени (та же модель телефона), как у отпечатков
    (presence_fingerprint.Fingerprints.stand_in); на диапазоне моделей нет или имени нет - None."""
    low = (name or "").lower()
    if not low:
        return None
    own = models.get(f"{low}@{iface}")
    if own is not None:
        return own
    same_band = [key for key in models if key.rpartition("@")[2] == iface]
    if not same_band:
        return None
    return models[max(same_band, key=lambda key: len(os.path.commonprefix([key, f"{low}@{iface}"])))]


class _Tracked:
    """Состояние одной станции на одном диапазоне: буфер опросов среза и субъект потока."""

    def __init__(self, subject: ps.Subject, level_holder: dict, world: pw.World, report: PlaceReport) -> None:
        self.subject, self.holder, self.world, self.report = subject, level_holder, world, report     # мир субъекта: у телефона пол и поверхности, у человека только пол
        self.buffer: list[float] = []
        self.last_step: float | None = None
        self.last_heard: float | None = None           # когда пришёл последний свежий опрос станции: по нему устройство молчит или нет
        self.estimate: ps.Estimate | None = None
        self.name: str | None = None
        self.iface = ""
        self.expected: np.ndarray | None = None       # ожидаемый уровень по клеткам для этой станции: слою нужен его градиент в точке
        self.nn = None                                  # (сеть диапазона, признаки опросов) при SAM_PRESENCE_NN=1; иначе None


class LiveFlow:
    def __init__(self, layout: dict, models: Mapping[str, Mapping], prefs: Mapping, habits: Mapping, place_radius_m: float | None = None,
                 step_m: float = pw.WORLD_STEP_M, randomness: Randomness | None = None, window_slices: int | None = None,
                 start_certainty: float = LIVE_START_CERTAINTY, start_min_share: float = LIVE_START_MIN_SHARE, view_paths: int = VIEW_PATHS,
                 output_factors: Sequence = (), preference_weight: float = PREFERENCE_WEIGHT, motion_sigma_m: float | None = MOTION_SIGMA_M,
                 stay_share: float = STAY_SHARE, delta_weight: float = DELTA_WEIGHT, delta_sd_db: float = DELTA_SD_DB,
                 hold_ratio: float = ps.pc.HOLD_RATIO, hold_ticks: int = ps.pc.HOLD_TICKS, owners: Mapping[str, str] | None = None,
                 top_view_max_h_m: float = ptv.TOP_VIEW_MAX_H_M, nn=None) -> None:
        """owners - часть имени устройства (строчными) -> человек: у человека с телефоном в потоке свой субъект, его свидетельство - близость к телефону.
        top_view_max_h_m - мебель ниже этого сверху считается полом для всех субъектов, выше - преграда.
        nn - сети по диапазонам (presence_nn.NnBundle) как фактор окна; None - поток без сети, как до неё."""
        self._layout, self._models, self._prefs, self._random = layout, models, prefs, randomness or Randomness()
        self._owners = {part.lower(): person for part, person in (owners or {}).items()}
        self._start_certainty, self._start_min_share, self._view_paths = start_certainty, start_min_share, view_paths
        self._preference_weight, self._motion_sigma_m, self._stay_share = preference_weight, motion_sigma_m, stay_share
        self._delta_weight, self._delta_sd_db, self._hold_ratio, self._hold_ticks = delta_weight, delta_sd_db, hold_ratio, hold_ticks
        self._extra_output = tuple(output_factors)                       # факторы выхода поверх карты предпочтений и места данными: для проверок и симуляций
        self._window_slices = window_slices or pw_.WINDOW_SLICES                 # срезов в окне; боевое число хаб читает из SAM_PRESENCE_WINDOW_SLICES
        nav = Nav(layout)
        self.world = pw.World(ptv.TopViewNavigation(nav, top_view_max_h_m), step_m)              # один мир сверху для телефона и человека: пол и подошвы низкой мебели
        self.floor_world = self.world                                                             # то же: у человека и телефона одни клетки
        self._place_xy = {name: (xy[0], xy[1]) for name, xy in (habits.get("place_xy") or {}).items()}
        self._named = {**self._place_xy, **{p["label"]: (p["x"], p["z"]) for p in layout.get("places") or []}}
        radius = {} if place_radius_m is None else {"radius_m": place_radius_m}
        self.report = PlaceReport(self.world, self._named, **radius)
        self.floor_report = self.report
        self._placed: Callable[[str | None], str | None] = lambda name: None       # место устройства, заданное данными; хаб подставляет своё
        self._tracked: dict[tuple[str, str], _Tracked] = {}
        self._people: dict[str, _Tracked] = {}                       # человек -> субъект человека; появляется с первым шагом телефона хозяина
        self._expected: dict[str, np.ndarray] = {}                   # ключ модели -> ожидаемый уровень по клеткам
        self.skipped: dict[str, str] = {}                            # устройство -> почему не считается
        self._now = 0.0                                              # время последнего опроса: по нему отличают замолчавших
        self._nn = nn

    def fresh(self) -> "LiveFlow":
        """Пустой поток той же сборки: план, модели, миры, матрицы расстояний и ожидаемые уровни общие (они неизменны), субъектов и состояния нет.
        Перемотка строит поток на каждую секунду записи: сборка мира с матрицей расстояний стоит секунды, близнец - доли миллисекунды."""
        twin = copy.copy(self)
        twin._tracked, twin._people, twin.skipped, twin._now = {}, {}, {}, 0.0
        return twin

    def bind_places(self, place_of_device: Callable[[str | None], str | None]) -> None:
        """Откуда брать место, на которое устройство посадили данными: по имени устройства из аренды роутера."""
        self._placed = place_of_device

    def observe(self, stations: Sequence[rs.Station], now: float) -> None:
        """Опросы роутера: свежие уровни копятся в буфер станции, раз в срез каждая станция шагает потоком; окно сбрасывается только после долгого молчания (STALE_S)."""
        self._now = now
        for st in stations:
            if self._nn is not None and (known := self._tracked.get((st.sid, st.iface))) is not None and known.nn is not None:
                known.nn[1].push(st.signal_db, st.inactive_ms, st.iface, st.link)       # сеть видит и молчащие кадры, как в обучении (свежесть - признак)
            if st.inactive_ms > FRESH_MS:
                continue
            tracked = self._tracked_for(st)
            if tracked is None:
                continue
            if tracked.last_heard is not None and now - tracked.last_heard > STALE_S:
                tracked.subject.reset()
                tracked.buffer, tracked.last_step = [], None
            tracked.buffer.append(st.signal_db)
            tracked.last_heard = now
        for tracked in self._tracked.values():
            self._advance(tracked, now)
        for person in {owner for t in self._tracked.values() if (owner := self._owner_of(t.name))}:
            self._advance_person(person, now)

    def _owner_of(self, name: str | None) -> str | None:
        low = (name or "").lower()
        return next((person for part, person in self._owners.items() if part in low), None) if low else None

    def _phone_of(self, person: str) -> _Tracked | None:
        """Телефон хозяина, который слышно и у которого есть оценка: самый свежий из его устройств."""
        heard = [t for t in self._tracked.values() if self._owner_of(t.name) == person and t.estimate is not None and t.last_heard is not None and self._now - t.last_heard <= STALE_S]
        return max(heard, key=lambda t: t.last_heard) if heard else None

    def _advance_person(self, person: str, now: float) -> None:
        """Срез человека раз в SLICE_S после телефонов: свидетельство - близость к распределению телефона хозяина; телефон замолчал - человек стоит, сброс как у телефона."""
        tracked = self._people.get(person) or self._person_tracked(person)
        phone = self._phone_of(person)
        if phone is None:
            if tracked.last_heard is not None and now - tracked.last_heard > STALE_S and tracked.last_step is not None:
                tracked.subject.reset()
                tracked.last_step = None
            return
        tracked.last_heard = phone.last_heard
        if tracked.last_step is not None and now - tracked.last_step < SLICE_S:
            return
        tracked.estimate = tracked.subject.step(datetime.fromtimestamp(now))
        tracked.last_step = now

    def _person_tracked(self, person: str) -> _Tracked:
        near = pn.NearSubject(lambda: (phone.estimate.distribution if (phone := self._phone_of(person)) else None))
        window = pw_.Window(self.floor_world, pm.Motion(sigma_m=self._motion_sigma_m, stay_share=self._stay_share), [near], size=self._window_slices,
                           start_certainty=self._start_certainty, start_min_share=self._start_min_share)
        subject = ps.Subject(f"{PERSON_ID}{person}", window, (), rng=self._random.numpy(f"flow:{PERSON_ID}{person}"), report=self.floor_report, hold_ratio=self._hold_ratio, hold_ticks=self._hold_ticks)
        self._people[person] = _Tracked(subject, {}, self.floor_world, self.floor_report)
        self._people[person].name, self._people[person].iface = person, PERSON_IFACE
        return self._people[person]

    def person_spot(self, person: str) -> list[tuple[float, float, float, str]]:
        """Где человек одной точкой по его субъекту (x, z, доля пятна, место); субъекта нет, телефон замолчал или шагов не было - пусто."""
        t = self._people.get(person)
        if t is None or t.estimate is None or t.estimate.point is None or t.last_heard is None or self._now - t.last_heard > STALE_S:
            return []
        return [self._spot_of(t)]

    def _spot_of(self, t: _Tracked) -> tuple[float, float, float, str]:
        estimate = t.estimate
        share = next((lobe.share for lobe in estimate.lobes if estimate.point in lobe.cells), max((lobe.share for lobe in estimate.lobes), default=0.0))
        x, z = t.world.points[estimate.point]
        return (round(float(x), 2), round(float(z), 2), share, t.report.name_at(estimate.point))

    def _advance(self, tracked: "_Tracked", now: float) -> None:
        """Срез раз в SLICE_S: медиана свежих опросов секунды; опросов нет - срез без свидетельства: окно идёт дальше и держит 7 срезов, неуверенность растёт переходом.
        О станции, которой не слышно дольше STALE_S, срезов нет."""
        if tracked.last_heard is None or now - tracked.last_heard > STALE_S:
            return
        if tracked.last_step is not None and now - tracked.last_step < SLICE_S:
            return
        tracked.holder["level"] = median(tracked.buffer) if tracked.buffer else None
        if tracked.nn is not None:
            window = tracked.nn[1].window()
            tracked.holder["nn"] = tracked.nn[0].probabilities(window) if window is not None else None
        tracked.estimate = tracked.subject.step(datetime.fromtimestamp(now))
        tracked.buffer, tracked.last_step = [], now

    def locate(self, name: str | None, iface: str, x: float, z: float) -> bool:
        """Положение устройства известно извне (слова человека, событие, проверка): его окно начинается в этой точке. Устройства нет в потоке - False."""
        tracked = next((t for t in self._tracked.values() if t.name == name and t.iface == iface), None)
        if tracked is None:
            return False
        tracked.subject.start_at(self.world.cell_at(x, z))
        tracked.buffer, tracked.last_step = [], None
        return True

    def spots(self, name: str | None, iface: str | None = None) -> list[tuple[float, float, float, str]]:
        """Где устройство по итогу ста бросков последней области: лепестки (x, z, доля, название места) по убыванию доли; устройства нет, оно замолчало или шагов не было - пусто.
        iface не задан - берётся диапазон, на котором устройство сейчас свежо."""
        found = [t for t in self._tracked.values() if t.name == name and t.estimate is not None and t.last_heard is not None
                 and self._now - t.last_heard <= STALE_S and (iface is None or t.iface == iface)]
        if not found:
            return []
        t = max(found, key=lambda tracked: tracked.last_step)
        return [(round(float(t.world.points[lobe.centre][0]), 2), round(float(t.world.points[lobe.centre][1]), 2), lobe.share, t.report.name_at(lobe.centre))
                for lobe in t.estimate.lobes]

    def point_spot(self, name: str | None, iface: str | None = None) -> list[tuple[float, float, float, str]]:
        """Где устройство одной точкой: точка потока (пятно, ближайшее к прошлой точке) с долей её пятна и названием места. В отличие от spots не отдаёт соседние пятна:
        слияние, получив их все, выбирает между ними на каждом шаге и прыгает на метры; точка потока держит непрерывность. Нет точки - пусто."""
        found = [t for t in self._tracked.values() if t.name == name and t.estimate is not None and t.estimate.point is not None and t.last_heard is not None
                 and self._now - t.last_heard <= STALE_S and (iface is None or t.iface == iface)]
        if not found:
            return []
        return [self._spot_of(max(found, key=lambda tracked: tracked.last_heard))]

    def top_place(self, name: str | None, iface: str | None = None) -> str | None:
        """Место с наибольшей вероятностью у устройства среди названных мест; устройства нет, оно замолчало или мест нет - None."""
        found = [t for t in self._tracked.values() if t.name == name and t.estimate is not None and t.estimate.places and t.last_heard is not None
                 and self._now - t.last_heard <= STALE_S and (iface is None or t.iface == iface)]
        return max(found, key=lambda tracked: tracked.last_heard).estimate.places[0][0] if found else None

    def point_of(self, name: str | None, iface: str | None = None) -> dict | None:
        """Точка устройства: итог ста бросков (среднее пятна, ближайшего к прошлой точке) как {place, x, z}; нет данных или устройство замолчало - None."""
        found = [t for t in self._tracked.values() if t.name == name and t.estimate is not None and t.estimate.point is not None and t.last_heard is not None
                 and self._now - t.last_heard <= STALE_S and (iface is None or t.iface == iface)]
        if not found:
            return None
        t = max(found, key=lambda tracked: tracked.last_step)
        x, z = t.world.points[t.estimate.point]
        return {"place": t.report.name_at(t.estimate.point), "x": round(float(x), 2), "z": round(float(z), 2)}

    def _tracked_for(self, st: rs.Station) -> _Tracked | None:
        key = (st.sid, st.iface)
        if key in self._tracked:
            return self._tracked[key]
        fitted = model_for(self._models, st.name, st.iface)
        if fitted is None:
            self.skipped[st.name or st.sid] = f"нет подогнанной модели уровня для {st.iface}: python scripts/radar_radio_fit.py"
            return None
        model_key = f"{(st.name or '').lower()}@{st.iface}"
        expected = self._expected.setdefault(model_key, expected_db(self.world, self._layout, fitted))
        holder: dict = {"level": None}
        pairs = [pld.LevelDelta(lambda: holder["level"], expected, sd_db=self._delta_sd_db, weight=self._delta_weight)] if self._delta_weight > 0 else []
        motion = pm.Motion(sigma_m=self._motion_sigma_m, stay_share=self._stay_share, pairs=pairs)
        rings = pr.Rings(lambda: holder["level"], expected, sd_db=float(fitted.get("ring_sd_db", pr.RING_SD_DB)))     # ширина кольца пары из файла модели, иначе общая
        nn_factor = self._nn.factor_for(st.iface, self.world.points, lambda: holder.get("nn")) if self._nn is not None else None
        window = pw_.Window(self.world, motion, [rings] + ([nn_factor] if nn_factor else []), size=self._window_slices,
                           start_certainty=self._start_certainty, start_min_share=self._start_min_share)
        output = [phone_preference(self._prefs, (st.name or "").lower(), self._place_xy, self.world, self._preference_weight, self._layout)] if self._place_xy else []
        output.extend(self._extra_output)
        output.append(pl.Placed(lambda name=st.name: self._placed(name), self._named))      # место данными - один раз на выходе: в каждом срезе окна оно учлось бы семь раз и перевесило бы сигнал
        self._tracked[key] = _Tracked(ps.Subject(f"{st.name or st.sid}@{st.iface}", window, output, rng=self._random.numpy(f"flow:{st.name or st.sid}@{st.iface}"), report=self.report,
                                                 hold_ratio=self._hold_ratio, hold_ticks=self._hold_ticks), holder, self.world, self.report)
        self._tracked[key].name, self._tracked[key].iface, self._tracked[key].expected = st.name, st.iface, expected
        if nn_factor is not None:
            self._tracked[key].nn = (self._nn.model(st.iface), self._nn.features_for(st.iface))
        self.skipped.pop(st.name or st.sid, None)
        return self._tracked[key]

    def _detail(self, t: _Tracked, second: int | None = None) -> dict:
        """Подробности для слоя объяснения. Поля, точки, свидетельства и рост идут по всем секундам окна. Законные пути (`upto`) и пути окна (`paths`) стоят квадрат от числа срезов, поэтому
        даются только для секунды `second` (с нуля; None - последняя; ALL_SECONDS - для всех секунд)."""
        window, estimate, subject, world = t.subject.window, t.estimate, t.subject, t.world
        rounded = lambda values: [round(float(v), PROBABILITY_DIGITS) for v in values]
        cell = lambda index: None if index is None else {"cell": int(index), "x": round(float(world.points[index][0]), 2), "z": round(float(world.points[index][1]), 2)}
        last = len(window.history) - 1
        wanted = set(range(last + 1)) if second == ALL_SECONDS else {last if second is None else min(max(second, 0), last)}
        fields = [*window.history[:-1], estimate.distribution]                  # поле секунды k: распределение её среза по первым k + 1 секундам; у последней секунды итоговое
        second_points = subject.second_points(estimate)
        growth = pg.window_growth(world, fields, second_points)
        slices = []
        for k, (alpha, second_point, parts, g) in enumerate(zip(window.history, second_points, window.evidence_history, growth)):
            slices.append({"alpha": rounded(alpha), "point": cell(second_point), "ascent": self._ascent(t, second_point), "parts": {name: rounded(field) for name, field in parts.items()},
                           "upto": [rounded(gamma) for gamma in subject.field_upto(k)] if k in wanted else [],
                           "paths": ppaths.widest(world.points, estimate.paths if k == last else subject.paths_upto(k), self._view_paths).tolist() if k in wanted else [],
                           "growth": {"peak": round(g.peak, PROBABILITY_DIGITS), "mass_near": round(g.mass_near, PROBABILITY_DIGITS), "area_m2": round(g.area_m2, GROWTH_AREA_DIGITS)}})
        return {"slices": slices, "transition": None if window.transition is None else rounded(window.transition),
                "output": {name: rounded(field) for name, field in estimate.parts.items()},
                "paths": ppaths.widest(world.points, estimate.paths, self._view_paths).tolist(), "point": cell(estimate.point), "previous_point": cell(estimate.previous_point), "before_point": cell(estimate.before_point),
                "ascent": self._ascent(t, estimate.point)}

    def walk_fields(self, fitted: Mapping, seconds: int | None = None) -> dict:
        """Поля шага к роутеру для модели уровня: клетки мира, для k = 1..окно куда пришёл из каждой клетки и разница ожидаемого уровня, сводка по шуму."""
        router = self._layout["router"]
        target = self.world.cell_at(router["x"], router["z"])
        dist, pred = self.world.path_tree(target)
        fields = pwf.walk_fields(dist, pred, self.world.points, expected_db(self.world, self._layout, fitted), seconds or self._window_slices)
        return {"cells": [[round(float(x), 2), round(float(z), 2)] for x, z in self.world.points], "router_cell": target, "speed_m_s": pwf.WALK_SPEED_M_S,
                "noise_db": pwf.CARRIED_NOISE_DB, "fields": [{**pwf.summary(f), "to": f.to.tolist(), "delta_db": [round(float(v), 1) for v in f.delta_db]} for f in fields]}

    def _ascent(self, t: _Tracked, point: int | None) -> dict | None:
        """Куда в точке растёт ожидаемый уровень (к роутеру): единичный вектор и крутизна в дБ/м по клеткам в радиусе локальности; точки нет - None."""
        if point is None or t.expected is None:
            return None
        neighbours = t.world.reach(pm.LOCALITY_M).getrow(point).indices
        dx, dz, slope = pld.ascent(t.expected, t.world.points, point, neighbours)
        return {"dx": round(dx, PROBABILITY_DIGITS), "dz": round(dz, PROBABILITY_DIGITS), "db_per_m": round(slope, 2)}

    def state(self, detail: bool = False, only: str | None = None, second: int | None = None) -> dict:
        """Состояние потока для страницы и SAM: клетки мира, у каждого устройства распределение, список мест с вероятностями и пятна.
        detail добавляет то, что нужно слою объяснения: срезы окна с вкладом каждого фактора, переход, пути, точку и прошлую точку; only - ключ устройства «id|диапазон»,
        только ему идут подробности, second - секунда окна, для которой в них законные пути и пути окна (с нуля; по умолчанию последняя) (на слой хватает одного устройства, остальным не гнать сотни килобайт каждую секунду)."""
        devices = [self._entry(sid, iface, t, detail, only, second) for (sid, iface), t in self._tracked.items() if self._heard(t)]
        people = [self._entry(f"{PERSON_ID}{person}", PERSON_IFACE, t, detail, only, second) for person, t in self._people.items() if self._heard(t)]
        return {"enabled": True, "step_m": self.world.step_m, "cells": [[round(float(x), 2), round(float(z), 2)] for x, z in self.world.points],
                "devices": devices, "people": people, "skipped": dict(self.skipped)}

    def rows(self) -> list[dict]:
        """Лёгкая сводка по субъектам для истории потока: ключ, имя, точка и места с вероятностями; без клеток и распределений."""
        out = []
        subjects = [*self._tracked.items(), *(((f"{PERSON_ID}{person}", PERSON_IFACE), t) for person, t in self._people.items())]
        for (sid, iface), t in subjects:
            if self._heard(t):
                out.append({"id": sid, "iface": iface, "name": t.name, "point": self._cell(t, t.estimate.point),
                            "places": [{"place": name, "p": round(p, PROBABILITY_DIGITS)} for name, p in t.estimate.places]})
        return out

    def _cell(self, t: _Tracked, index: int | None) -> dict | None:
        return None if index is None else {"cell": int(index), "x": round(float(t.world.points[index][0]), 2), "z": round(float(t.world.points[index][1]), 2)}

    def _heard(self, t: _Tracked) -> bool:
        return t.estimate is not None and t.last_heard is not None and self._now - t.last_heard <= STALE_S

    def _entry(self, sid: str, iface: str, t: _Tracked, detail: bool, only: str | None, second: int | None) -> dict:
        """Запись устройства или человека в состоянии: одна форма, слой страницы рисует обоих одним рендером."""
        return {"id": sid, "name": t.name, "iface": iface,
                "places": [{"place": name, "p": round(p, PROBABILITY_DIGITS)} for name, p in t.estimate.places],
                "p": [round(float(v), PROBABILITY_DIGITS) for v in t.estimate.distribution],
                "lobes": [{"share": round(lobe.share, PROBABILITY_DIGITS), "cells": list(lobe.cells), "centre": lobe.centre} for lobe in t.estimate.lobes],
                **(self._detail(t, second) if detail and only in (None, f"{sid}|{iface}") else {})}
