"""Хаб радара присутствия (SAM-105): флаг SAM_PRESENCE, источник кадров, карта посещений, зоны, рассылка страницам.

Функция за флагом: по умолчанию
`off`, тогда хаб ничего не запускает, не тратит процессор, а страница и API отвечают «выключено». `mock` - демо
на выдуманной планировке, пока нет реального источника CSI. Решение (step) отделено от цикла и сокетов; файлы карты
посещений и зон - единственное, что хаб пишет на диск.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
from dataclasses import asdict
from enum import StrEnum
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from gateway.env import env_float, env_int

from . import presence as pr
from . import presence_devices as pdv
from . import presence_fingerprint as pfp
from . import presence_level as plevel
from . import presence_continuity as pcont
from . import presence_pick as ppick
from .presence_random import SEED_ENV, Randomness
from . import presence_radio_map as prm
from . import presence_prefs as pprefs
from . import presence_prefs as ppref
from . import presence_window as pwindow
from . import presence_smooth as psmooth
from . import presence_transition as ptrans
from . import presence_fuse as fuse
from . import presence_messages as pmsg
from . import presence_time as ptime
from . import presence_wifi as pwifi
from . import presence_nav as pnav
from . import presence_rssi as rs
from . import presence_flow_history as pfh
from . import presence_pc as ppc
from . import presence_nn as pnn
from . import presence_pose as ppose
from . import presence_rewind as prw
from . import presence_tracks as ptr
from . import presence_flow_live as pfl
from . import presence_ssh as ssh
from . import presence_track as tk
from . import presence_ahead as ahead
from . import presence_areas as pareas
from . import presence_animation as panim
from . import presence_autopilot as autopilot
from . import presence_autopilot as autopilot
from . import presence_override as override
from . import presence_pipeline as pipeline
from . import presence_routine as routine
from . import presence_seats as seats
from . import presence_separate as separate
from . import presence_tuning as tune
from . import presence_habit_stats as pstats
from . import presence_state as pstate
from . import presence_walk as pwalk
from . import presence_stay as pstay
from . import presence_where as pwhere

FLAG_ENV = "SAM_PRESENCE"
MODE_OFF = "off"
MODE_MOCK = "mock"
MODE_RSSI = "rssi"            # сигнал станций реального роутера по SSH: присутствие и активность, без позиций
MODES = (MODE_OFF, MODE_MOCK, MODE_RSSI)
ROUTER_ENV = "SAM_PRESENCE_ROUTER"
DEFAULT_ROUTER = "root@192.168.1.1"
IFACES_ENV = "SAM_PRESENCE_IFACES"
DEFAULT_IFACES = "phy0-ap0,phy1-ap0"              # интерфейсы точки доступа OpenWrt: 2,4 и 5 ГГц
POLL_ENV = "SAM_PRESENCE_POLL_S"
DEFAULT_POLL_S = 0.5          # опрос роутера: чаще незачем, окно детектора рассчитано на такой шаг
TICK_ENV = "SAM_PRESENCE_TICK_S"
DEFAULT_TICK_S = 0.2          # 5 кадров в секунду: страница достраивает кадры сама, источнику чаще не нужно
HEAT_FILE_ENV = "SAM_PRESENCE_HEAT_FILE"
DEFAULT_HEAT_FILE = "data/presence_heat.json"      # карта посещений копится между запусками
WALKS_CLEARED_KEY = "walks_cleared_at"             # в файле карты: когда её очистили; записанные прогулки, кончившиеся раньше, на полу не показываются
ZONES_FILE_ENV = "SAM_PRESENCE_ZONES_FILE"
DEFAULT_ZONES_FILE = "data/presence_zones.json"    # подписи зон, которые дал человек
DEVICES_FILE_ENV = "SAM_PRESENCE_DEVICES_FILE"
DEFAULT_DEVICES_FILE = "data/presence_devices.json"  # где и когда видели устройства; переживает перезапуск
TRACKS_DIR_ENV = "SAM_PRESENCE_TRACKS_DIR"
DEFAULT_TRACKS_DIR = "data/presence_tracks"       # ряды сигнала устройств по суткам
WALKS_FILE_ENV = "SAM_PRESENCE_WALKS_FILE"
DEFAULT_WALKS_FILE = "data/presence_walks.json"      # размеченные прогулки: точки маршрута с временем, для показа трека
PLACES_FILE_ENV = "SAM_PRESENCE_PLACES_FILE"
DEFAULT_PLACES_FILE = "data/presence_places.json"   # где лежат устройства, со слов человека
TUNING_FILE_ENV = "SAM_PRESENCE_TUNING_FILE"
ANIMATION_FILE_ENV = "SAM_PRESENCE_ANIMATION_FILE"
FLOW_ENV = "SAM_PRESENCE_FLOW"                           # 1 - поток положения (окно путей по клеткам) считается рядом со старым радаром и отдаётся на /sam/presence/flow
LEVEL_EVIDENCE_ENV = "SAM_PRESENCE_LEVEL_EVIDENCE"      # 1 - место снятого устройства по уровню сигнала идёт свидетельством о людях; по умолчанию выключено, пока оценка не стабильна
FINGERPRINTS_FILE_ENV = "SAM_PRESENCE_FINGERPRINTS_FILE"
SNAPSHOT_NAME, SNAPSHOT_IFACE, SNAPSHOT_SIGNAL_DB, SNAPSHOT_SEEN_AGO, SNAPSHOT_PRESENT = "name", "iface", "signal_db", "seen_ago_s", "present"   # поля строки снимка устройств (presence_devices.snapshot)
FIELD_MIN_P = 0.02               # места слабее этого в слой поля не идут
LAYOUT_PLACE_LABEL = "label"      # поле названия места в плане


class DeviceRow(StrEnum):
    """Поля строки устройства, которые хаб добавляет для страницы."""
    WHERE = "where"           # лучшие места по уровню сигнала с вероятностями
    AT = "at"                 # место, выбранное выборкой из оценки: {place, x, z}; страница рисует метку там
    FIELD = "field"           # вся оценка мест устройства для слоя «поле телефона»: {place, p, x, z}


DEVICE_WHERE, DEVICE_AT, DEVICE_FIELD = DeviceRow.WHERE, DeviceRow.AT, DeviceRow.FIELD
LAYOUT_PLACES = "places"          # названные места плана с координатами
STATE_FILE_ENV = "SAM_PRESENCE_STATE_FILE"
CONTROLS_FILE_ENV = "SAM_PRESENCE_CONTROLS_FILE"
DEFAULT_CONTROLS_FILE = "data/radio_controls.json"       # положения мест отпечатков на плане: место -> x, z (по размеченной карте)
HABITS_FILE_ENV = "SAM_PRESENCE_HABITS_FILE"
PREFS_FILE_ENV = "SAM_PRESENCE_DEVICE_PREFS_FILE"
OWNERS_FILE_ENV = "SAM_PRESENCE_OWNERS_FILE"          # хозяева устройств: часть имени устройства -> человек
DEFAULT_OWNERS_FILE = "data/presence_owners.json"
DEFAULT_HABITS_FILE = "data/home_habits.json"            # привычки людей по словам: время по местам для карты предпочтений устройств
SLOT_PREFS_KEEP = 128         # сколько слотов карты предпочтений держим: сутки на хозяина с запасом
DEFAULT_PREFS_FILE = "data/device_prefs.json"             # где устройство лежит чаще: вес места по устройству, простая карта (телефон в туалете реже, чем в спальне)
DEFAULT_STATE_FILE = "data/presence_state.json"          # переключатели панели, последние слова и кто был на карте: переживают перезапуск радара
STATE_SAVE_EVERY_S = 20.0
HABIT_STATS_FILE_ENV = "SAM_PRESENCE_HABIT_STATS_FILE"
DEFAULT_HABIT_STATS_FILE = "data/presence_habit_stats.json"    # копилка привычек: где кто бывает по дню недели, слоту времени и гостям, только при уверенности
TOLD_SURE_STRENGTH = 0.5      # слова сильнее этого идут в копилку как уверенное: вес 0,85 падает до половины за 46 минут
HABIT_PLACE_M = 1.2           # ближе этого к названному месту плана человек считается в нём; дальше - в комнате
DEFAULT_FINGERPRINTS_FILE = "data/presence_fingerprints.json"   # уровни сигнала устройств по названным местам, копятся между запусками
DEFAULT_ANIMATION_FILE = "data/presence_animation.json"   # параметры анимации роя с ползунков страницы; переживают обновление страницы и перезапуск
DEFAULT_TUNING_FILE = "data/presence_tuning.json"   # пороги детектора с ползунков панели отладки; переживают перезапуск
DEVICE_EVENTS_KEEP = 50       # последние события устройств для монитора; старые не копим
CHANNELS_ENV = "SAM_PRESENCE_CHANNELS"
DEFAULT_CHANNELS = ("wifi", "messages", "time", "pc")   # каналы свидетельств «кто где»; told (со слов человека) по умолчанию выключен; pc молчит без data/pc_activity.json
PRESENCE_DT_CAP_S = 1.0       # за один кадр в карту не больше стольких секунд: пауза между кадрами не должна рисовать пребывание
HEAT_CELL_LIMIT_S = 14400.0   # клетка карты копит не больше четырёх часов: одно место не заливает остальные
STEPS_HALF_LIFE_S = 600.0     # шаги протухают: через десять минут след ходьбы вдвое слабее
STAY_RADIUS_M = 1.0           # человек в этом радиусе от точки, где встал, считается остающимся на месте
STAY_ALERT_S = 3600.0         # на месте дольше часа - пора размяться
DEFAULT_HEAT_HALF_LIFE_S = 21600.0     # карта пребывания остывает за шесть часов: за день видно, где сидели и стояли, но не прошлые сутки
HEAT_HALF_LIFE_ENV = "SAM_PRESENCE_HEAT_HALF_LIFE_S"
HEAT_SEND_EVERY = 25          # карта меняется медленно: со снимком раз в 25 кадров (5 секунд при тике 0,2 с)
HEAT_SAVE_EVERY_S = 60.0      # как часто карта пишется на диск
CLIENT_QUEUE = 4              # отставшему клиенту хватит свежих кадров; старые не копим


def mode_from_env(environ: dict | None = None) -> str:
    raw = (os.environ if environ is None else environ).get(FLAG_ENV, MODE_OFF).strip().lower()
    if raw not in MODES:
        print(f"[Presence] {FLAG_ENV}={raw!r} неизвестно, радар выключен; допустимо: {', '.join(MODES)}")
        return MODE_OFF
    return raw


def _read_json(path: Path | None, default, what: str):
    """JSON из файла; нет файла - default, битый - default и одна строка в лог (старый файл удали или почини)."""
    if not path or not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        print(f"[Presence] {what} {path} не прочитана ({e}); начинаю заново, старый файл удали или почини")
        return default


def _write_text(path: Path | None, text: str, what: str) -> None:
    """Запись файла; нет пути - не пишем. Сбой записи не роняет радар: одна строка в лог."""
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"[Presence] {what} не сохранена в {path}: {e}; проверь путь в настройках радара")


def load_devices(path: Path | None) -> pdv.DeviceBook | None:
    if not path or not path.is_file():
        return None
    try:
        return pdv.DeviceBook.from_json(path.read_text(encoding="utf-8"))
    except (ValueError, KeyError, TypeError, OSError) as e:
        print(f"[Presence] учёт устройств {path} не прочитан ({e}); начинаю заново, старый файл удали или почини")
        return None


def _with_places(book: pdv.DeviceBook | None, places_file: Path | None) -> pdv.DeviceBook:
    book = book or pdv.DeviceBook()
    book.places = _read_json(places_file, {}, "места устройств")
    return book


def _build_nav(layout: dict) -> pnav.Nav | None:
    try:
        return pnav.Nav(layout)
    except ValueError:
        return None                                           # плана со стенами нет: пол и трек остаются как в раннем виде


def load_tuning(path: Path | None) -> tune.Tuning:
    tuning = tune.Tuning()
    try:
        tuning.update(_read_json(path, {}, "пороги детектора"))
    except ValueError as e:
        print(f"[Presence] пороги детектора {path} не применены ({e}); беру значения по умолчанию")
        return tune.Tuning()
    return tuning


def load_fingerprints(path: Path | None) -> pfp.Fingerprints:
    data = _read_json(path, None, "отпечатки уровня сигнала")
    try:
        return pfp.Fingerprints.from_json(json.dumps(data)) if data else pfp.Fingerprints()
    except (ValueError, TypeError, AttributeError) as e:
        print(f"[Presence] отпечатки {path} не разобраны ({e}); начинаю заново")
        return pfp.Fingerprints()


def load_animation(path: Path | None) -> dict[str, float]:
    try:
        return panim.parse(_read_json(path, {}, "параметры анимации")) if _read_json(path, {}, "параметры анимации") else {}
    except ValueError as e:
        print(f"[Presence] параметры анимации {path} не применены ({e}); беру значения страницы по умолчанию")
        return {}


def load_heat(path: Path | None) -> pr.Heatmap:
    data = _read_json(path, None, "карта посещений")
    try:
        return pr.Heatmap.from_json(json.dumps(data)) if data else pr.Heatmap()
    except (KeyError, TypeError, ValueError) as e:
        print(f"[Presence] карта посещений {path} не разобрана ({e}); начинаю заново")
        return pr.Heatmap()


log = logging.getLogger("sam.radar")        # настраивается сервисом (presence_log.setup); в тестах без обработчиков молчит
SLOW_TICK_S = 0.15                         # тик кадра дольше этого пишется в лог как медленный: так видно, что радар «замирает»

class ZoneLocked(Exception):
    """Логическую зону (рабочая, домашняя) не стирают, пока человек не снимет замок."""


SEAT_SNAP_M = 0.3             # подписанная точка ближе этого к краю мебели с позой - место на ней
PLACE_OBJECT_POSES = (("кроват", "lie"), ("диван", "sit"), ("кресл", "sit"), ("стул", "sit"), ("унитаз", "sit"))     # мебель, которую можно выбрать местом, и поза на ней


def _place_name(title: str) -> str:
    """Название места из названия предмета плана: без пометки «(примерно)», строчными."""
    return title.replace("(примерно)", "").strip().lower()


class PresenceHub:
    def __init__(self, mode: str, layout: dict, tick_s: float = DEFAULT_TICK_S,
                 clock: Callable[[], float] = time.time, heat: pr.Heatmap | None = None,
                 heat_file: Path | None = None, zones: list[dict] | None = None,
                 zones_file: Path | None = None, stations=None, devices: pdv.DeviceBook | None = None,
                 devices_file: Path | None = None, places_file: Path | None = None,
                 tracks: ptr.TrackLog | None = None, walks_file: Path | None = None,
                 heat_half_life_s: float = DEFAULT_HEAT_HALF_LIFE_S, channels: tuple[str, ...] = DEFAULT_CHANNELS,
                 tuning: tune.Tuning | None = None, tuning_file: Path | None = None, animation_file: Path | None = None,
                 fingerprints_file: Path | None = None, level_evidence: bool = False, state_file: Path | None = None, habit_stats_file: Path | None = None,
                 rng: random.Random | None = None, track_window_s: float | None = None, controls: dict | None = None, device_prefs: dict | None = None, habits: dict | None = None,
                 flow: pfl.LiveFlow | None = None, owners: dict[str, str] | None = None, randomness: Randomness | None = None,
                 rewind: prw.Rewind | None = None, flow_history: pfh.FlowHistory | None = None, pc: dict | None = None,
                 pc_reader: Callable = ppc.read_windows_state, pose_sources: tuple[str, ...] = ppose.DEFAULT_POSE_SOURCES, raw_tracks: ptr.TrackLog | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._sleep = sleep                                    # пауза между тиками цикла; тест подменяет её и гоняет цикл шагами без настенного времени
        self.enabled_channels = frozenset(channels)
        self.flow = flow                                       # поток положения за флагом; None - выключен
        self.rewind = rewind                                   # перемотка потока по трекам; None - без неё
        self.flow_history = flow_history or pfh.FlowHistory(None)      # куда поток ставил субъектов по секундам; без папки не пишет
        self._level_evidence = level_evidence
        self._owners = {key.lower(): person for key, person in (owners or {}).items()}      # часть имени устройства -> человек; положения у устройства из плана больше нет
        if flow is not None:
            flow.bind_places(self._device_place)
        self.mode = mode
        extra = pnav.describe(layout)                          # наружные стены и пол из карты проходимости; нет стен - план как есть
        self.layout = {**layout, **extra} if extra else layout
        self._nav = _build_nav(layout)
        self.messages = pmsg.MessageChannel(dict(self.layout.get("spots") or {}), objects=self.layout.get("objects"), places=lambda: self.place_points())   # откуда пишут и говорят: имя и устройство
        places = dict(self.layout.get("spots") or {})
        available: dict[str, fuse.EvidenceSource] = {      # каналы свидетельств «кто где»; камеры и другие источники добавляются сюда
            "wifi": pwifi.WifiChannel(lambda now: self.devices.snapshot(now, self._reading.per_station if self._reading else None), self._owner_of, self._device_spots),
            "messages": self.messages, "time": ptime.TimeChannel(self.layout.get("routines") or [], places),
            "pc": ppc.PcChannel(pc or {}, self.place_points, pc_reader)}        # рабочий компьютер: игра без ввода - на диване, ввод - за столом; без конфига молчит
        self._pc_partner = available["pc"].partner                # второго игрока канал ПК ставит, только если он дома по другим каналам
        self._available, self._env_channels, self.real, self._debug_before_real = available, self.enabled_channels, False, False
        self.channels: list[fuse.EvidenceSource] = [ch for name, ch in available.items() if name in self.enabled_channels]
        self._tracks_cache: dict[str, list[dict]] = {}
        self.tick_s = tick_s
        self.heat = heat or pr.Heatmap()
        self.steps = pr.Heatmap(self.heat.cell_m)         # слой шагов: ходьба, остывает за минуты; пребывание (self.heat) - за часы
        self._time_still: dict[str, float] = {}            # человек -> секунд на месте (сидит, лежит, стоит), с запуска радара
        self._time_moving: dict[str, float] = {}           # человек -> секунд в движении
        self._stay: dict[str, dict] = {}                   # человек -> {x, z, since}: где встал и с какого момента не уходил дальше STAY_RADIUS_M
        if self._nav is not None:                       # накопленное за стенами (точка «за устройством» у края комнаты) ложится на ближайший пол
            self.heat = self.heat.moved(self._nav.floor_point)
        self.zones = zones or []
        self._clock = clock
        self._heat_file = heat_file
        self._zones_file = zones_file
        self._store = pr.PresenceStore()
        self._tracker = tk.Tracker()
        self._clients: set[asyncio.Queue] = set()
        self._t0 = clock()
        self._frames = 0
        self._last_save = clock()
        self._last_step = clock()
        self._stations = stations              # фабрика асинхронного потока станций роутера (режим rssi)
        self.debug, self._debug_last = bool(os.environ.get("SAM_RADAR_DEBUG")), {}      # отладка: подробные строки состояния в лог
        self.autopilot_mode, self._routine, self._routine_logged, self._routine_reassert = "routine", None, None, False                               # по умолчанию быт; random - случайные места
        self.autopilot_on, self.autopilot_period_s, self._autopilot_next = False, autopilot.DEFAULT_PERIOD_S, 0.0     # автопилот отладки: выключен, пока не включат
        self.overrides: dict[str, dict] = {}        # ручные вероятности людей из панели отладки; в памяти, на диск не идут
        self.tuning = tuning or tune.Tuning()
        self._tuning_file = tuning_file
        self._animation_file = animation_file
        self._fingerprints_file = fingerprints_file
        self.fingerprints = load_fingerprints(fingerprints_file)
        self._level_smoothers: dict[str, psmooth.LevelSmoother] = {}   # ключ устройства -> медиана его уровня по короткому окну
        self._place_trackers: dict[str, ptrans.PlaceTracker] = {}       # ключ устройства -> оценка его места во времени
        self._place_distances: dict[tuple[str, str], float] = {}        # путь между местами по плану, метры; места стоят на месте, считается один раз
        self.animation: dict[str, float] = load_animation(animation_file)
        self._detector = rs.MotionDetector(tuning=self.tuning)
        residents = list(self.layout.get("people") or {})
        lanes = {p: (k - (len(residents) - 1) / 2) * pwalk.LANE_M for k, p in enumerate(residents)}      # по очереди жильцов: левее, посередине, правее
        self.walkers = pwalk.Walkers(self._nav.route if self._nav else None, self.tuning, lanes=lanes, snap=self._nav.snap if self._nav else None,
                                    free=self._nav.segment_free if self._nav else None, entry=self._entry_point)
        self.areas = pareas.Areas(self.layout.get("areas") or [], self.layout.get("rooms") or [], self.layout.get("objects") or [])
        self.devices = devices or pdv.DeviceBook()
        self._devices_file = devices_file
        self._places_file = places_file
        self.tracks = tracks or ptr.TrackLog(None)
        self.raw_tracks = raw_tracks or ptr.TrackLog(None)                 # сырая запись каждого круга опроса (SAM_PRESENCE_RAW_TRACKS=1), без неё не пишется
        self._walks_file = walks_file
        self._walks_cleared_at = float((_read_json(heat_file, {}, "карта посещений") or {}).get(WALKS_CLEARED_KEY, 0.0)) if heat_file else 0.0
        self.device_events: list[dict] = []     # появилось и ушло: забирает монитор и память SAM
        self._reading: rs.Reading | None = None
        self._reading_at = clock()
        self._last_cool = clock()
        self._last_motion_at = clock()
        self._last_traffic_at = clock()
        self.heat_half_life_s = heat_half_life_s
        self._habits = habits or {}                                                                          # привычки людей по словам: время по местам
        self._fingerprint_xy = {k.lower(): v for k, v in (controls or {}).items()}      # места отпечатков с координатами: диван, кровать, туалет и остальные из замеров
        self.track_window_s = track_window_s                               # окно оценки места устройства, секунды; None - рекурсивный фильтр прежний
        walls, doors = self.layout.get("walls") or [], prm.doorway_openings(self.layout.get("objects") or [])
        self._continuity = pcont.Continuity((lambda a, b: prm.crosses_wall(walls, a, b, doors)) if walls else None, report=lambda text: log.warning("непрерывность: %s", text))
        self._fused_at: dict[str, tuple[float, float]] = {}                  # где слияние поставило человека в прошлый раз: липкость выбора места
        self._random = randomness or Randomness()                      # одна случайность радара: сид один, потоки по именам
        self._sampler = ppick.StickySampler(rng or self._random.python("sampler"))                         # положение устройства - выборка из распределения, а не вершина
        self._state_store = pstate.StateStore(state_file, lambda path, text: _write_text(path, text, "состояние радара"))
        self._last_state_save = clock()
        self.habit_stats = pstats.HabitRecorder.load(habit_stats_file)
        self.recorder = pstats.HabitRecorder(self.habit_stats, self._habit_place, habit_stats_file,
                                             lambda path, text: _write_text(path, text, "копилка привычек"), now=clock())
        self._poses = ppose.PoseSampler(self._habits, self.habit_stats, self._random.python("pose"))     # поза одинокого на диване по долям и копилке
        self._pose_sources = frozenset(pose_sources)                     # каналы, чья поза наблюдена: только она идёт в копилку поз
        self.prefs = pprefs.DevicePrefs(device_prefs or {}, self._habits, self.habit_stats, self.layout.get("objects") or [],
                                        {name: (xy["x"], xy["z"]) for name, xy in self._fingerprint_xy.items()}, self._owner_of)
        self._restore_state()

    def _restore_state(self) -> None:
        """Применяет сохранённое состояние: слова не старше срока, прежних жильцов без прихода от двери, переключатели панели."""
        saved = self._state_store.load()
        if saved is None:
            return
        now = self._clock()
        for m in pstate.fresh_messages(saved.messages, now):
            self.messages.feed(pmsg.Message(**{k: m.get(k) for k in pmsg.Message.__dataclass_fields__}))
        self.walkers.seed_home(pstate.home_after_restart(saved, now))
        if saved.real:
            self.set_real(True)
        elif saved.autopilot.get("on"):
            self.set_autopilot(True, saved.autopilot.get("period_s"), saved.autopilot.get("mode"))
        log.info("состояние восстановлено: реальное %s, автопилот %s, слов %d, дома %s", saved.real, saved.autopilot.get("on"), len(self.messages.recent()), ", ".join(self.walkers._home) or "-")

    def save_habit_stats(self) -> None:
        self.recorder.save()

    def _habit_place(self, x: float, z: float) -> str | None:
        """Место для копилки: ближайшее название места плана в HABIT_PLACE_M, иначе комната, иначе None (в проходе или вне плана)."""
        near = min(((math.hypot(p["x"] - x, p["z"] - z), name) for name, p in self.place_points().items()), default=None)
        if near and near[0] <= HABIT_PLACE_M:
            return near[1]
        return pwhere.room_at(self.layout.get("rooms") or [], x, z)

    def _record_habits(self, rows: list[dict], seconds: float, now: float) -> None:
        self.recorder.record(rows, self.messages.told_now(now, TOLD_SURE_STRENGTH), seconds, now)

    def connected_devices(self) -> list[dict]:
        """Кто сейчас виден роутеру и на каком интерфейсе (диапазоне): карта покрытия выбирается по паре «устройство и интерфейс»."""
        return [{"name": d["name"], "iface": d["iface"], "signal_db": d["signal_db"]} for d in self.devices.snapshot(self._clock()) if d["present"]]

    def save_state(self) -> None:
        """Пишет переключатели, последние слова и кто сейчас на карте."""
        self._state_store.save(pstate.snapshot(self._clock(), self.real, {"on": self.autopilot_on, "period_s": self.autopilot_period_s, "mode": self.autopilot_mode},
                                               self.messages.recent(), self.walkers.on_map()))

    def note_message(self, channel: str, device: str | None, who: str | None = None, level: float | None = None,
                     now: float | None = None, place: str | None = None, pose: str | None = None) -> bool:
        """Сообщение с именем и устройством (чат, голос) - свидетельство, где человек: сводится в identities.
        False - сообщение не принято: слова человека (told) выключены каналами."""
        if device and place:
            self._record_fingerprint(device, place)             # отпечаток копится и при выключенных словах: так набирают базу, не включая реальное определение
        if channel == "told" and "told" not in self.enabled_channels:
            return False                                        # слова человека выключены: тестируем сканер по косвенным признакам
        self.messages.feed(pmsg.Message(self._clock() if now is None else now, channel, device, who, place=place, level=level, pose=pose))
        self.save_state()
        return True

    def _record_fingerprint(self, device: str, place: str) -> None:
        """Названо место устройства: запоминается его уровень сигнала сейчас (по подстроке имени среди видимых роутеру устройств)."""
        low = device.lower()
        matches = [d for d in self.devices.devices.values() if d.present and (low in (d.name or "").lower())]
        seen = max(matches, key=lambda d: d.last_seen, default=None)    # после смены диапазона старая запись ещё «в сети»: берётся свежая
        if seen is None or seen.last_signal_db is None:
            return
        key = pfp.device_key(seen.name or low, seen.iface)
        n = self.fingerprints.add(key, place, seen.last_signal_db)
        self._place_trackers.setdefault(key, ptrans.PlaceTracker()).set_known(place.lower(), list(self.fingerprints.data[key]), self._clock())
        _write_text(self._fingerprints_file, self.fingerprints.to_json(), "отпечатки уровня сигнала")
        log.info("отпечаток: %s в «%s» %.1f дБ (записей %d)", seen.name, place, seen.last_signal_db, n)

    def forget_fingerprint(self, place: str, device_part: str | None = None) -> int:
        """Стереть названное место из отпечатков и записать файл."""
        removed = self.fingerprints.remove(place, device_part)
        if removed:
            _write_text(self._fingerprints_file, self.fingerprints.to_json(), "отпечатки уровня сигнала")
        return removed

    def identities(self, now: float) -> list[dict]:
        """Кто где и насколько уверены: все каналы свидетельств (Wi-Fi, сообщения, время) сведены и прошли цепочку шагов; живёт, пока есть хоть один признак."""
        evidence = ppc.drop_unsupported_partner([e for channel in self.channels for e in channel.evidence(now)], self._pc_partner)
        trace: dict[str, list[str]] | None = {} if self.debug else None
        rows = pipeline.run(self._steps(), self._with_seen_pose(self._fused_identities(evidence, now), evidence), now, trace)
        if trace is not None:
            self._log_debug(rows, trace)
        return rows

    def _steps(self) -> list[tuple[str, pipeline.Step]]:
        """Состав и порядок обработки людей - одно место (корень композиции): добавить шаг значит добавить строку."""
        objects = self.layout.get("objects") or []
        people = self.layout.get("people") or {}
        nav = self._nav
        return [
            ("ручная вероятность", lambda rows, now: override.apply(rows, self.overrides, self.place_points()) if self.overrides else rows),
            ("ход", self.walkers.apply),
            ("комнаты", lambda rows, now: self.areas.apply(rows, now, self.zones, objects)),
            ("опоры", lambda rows, now: seats.settle(
                rows, objects, front=nav.front_of if nav else None, now=now, genders={k: v.get("gender") for k, v in people.items()},
                order={k: i for i, k in enumerate(people)}, door_at=nav.door_direction if nav else None,
                sit_only={k for k, v in people.items() if v.get("bed") == "sit"}, rooms=self.layout.get("rooms") or [],
                lie_alone={k for k, v in people.items() if v.get("sofa") == "lie"},
                sits_with={k: v["sits_with"] for k, v in people.items() if v.get("sits_with")},
                chair_owners=[(p["x"], p["z"], p["owner"]) for p in self.place_points().values() if p.get("owner") and p.get("pose") == "sit"],
                hugs={k: v["hugs"] for k, v in people.items() if v.get("hugs")}, hug_always=self.tuning.hug_force >= 0.5,
                sofa_pose=lambda person, title, now: self._poses.pose(person, _place_name(title), now))),
            ("взгляд сидящих", lambda rows, now: [self._faced(r) if r.get("pose") == "sit" else r for r in rows]),     # до разведения: зона перед лицом нужна ему
            ("разведение", lambda rows, now: separate.separate(rows, free=nav.segment_free if nav else None, nodes=nav.free_nodes if nav else None)),
            ("препятствие впереди", lambda rows, now: ahead.mark_blocked(rows, nav.segment_free) if nav else rows),
            ("поза над опорой", lambda rows, now: seats.ground_poses(rows, objects)),
            ("взгляд", lambda rows, now: [self._faced(r) for r in rows]),
            ("непрерывность", self._continuity.apply),                 # после неё только поза и обратная связь хода: инвариант «не сквозь стену и не дальше шага» держится независимо от того, какой шаг ошибся
            ("поза после непрерывности", lambda rows, now: seats.ground_poses(rows, objects)),     # возвращённый на прежнюю точку не лежит в коридоре с позой цели
            ("ход видит возврат", self._walk_feedback),                 # ход берёт возвращённую точку за текущую и строит путь от неё
        ]

    def _walk_feedback(self, rows: list[dict], now: float) -> list[dict]:
        """Кого непрерывность вернула на прежнюю точку, того ход считает стоящим там: иначе ход думает, что человек уже в цели, и путь не строит."""
        for r in rows:
            if r.get(pcont.BLOCKED_KEY):
                self.walkers.held_back(r["person"], r["x"], r["z"], now)
        return rows

    def set_real(self, on: bool) -> bool:
        """Тест реального времени: только настоящие источники по всем каналам (Wi-Fi, сообщения, время, со слов), автопилот и ручные вероятности сняты, подробный журнал
        состояния людей включён. Выключение возвращает каналы из настройки и прежнее состояние отладки."""
        on = bool(on)
        if on == self.real:
            return self.real
        self.real = on
        if on:
            if self.autopilot_on:
                self.set_autopilot(False)
            for person in list(self.overrides):
                self.clear_person(person)
            self._debug_before_real = self.debug
            self.enabled_channels = frozenset((*self._available, "told"))
            self.set_debug(True)
        else:
            self.enabled_channels = self._env_channels
            self.set_debug(self._debug_before_real)
        self.channels = [ch for name, ch in self._available.items() if name in self.enabled_channels]
        log.info("реальное определение: %s, каналы %s", "включено" if on else "выключено", ", ".join(sorted(self.enabled_channels)))
        self.save_state()
        return self.real

    def set_debug(self, on: bool) -> bool:
        """Режим отладки: по каждому человеку в лог пишется положение, поза, взгляд, окружение и что на каком шаге их изменило - строка на каждую смену."""
        self.debug = bool(on)
        self._debug_last.clear()
        log.info("отладка радара: %s", "включена" if self.debug else "выключена")
        return self.debug

    def _log_debug(self, rows: list[dict], trace: dict[str, list[str]]) -> None:
        objects = self.layout.get("objects") or []
        for row in rows:
            key = (round(row["x"], 1) if row.get("x") is not None else None, round(row["z"], 1) if row.get("z") is not None else None, row.get("pose"),
                   round(math.degrees(row["face"]) / 10) if row.get("face") is not None else None)
            if self._debug_last.get(row["person"]) == key:
                continue
            self._debug_last[row["person"]] = key
            if row.get("x") is None or row.get("z") is None:
                log.info("состояние %s: места нет | уверенность %.2f, источники %s", row["person"], row.get("confidence", 0.0), ", ".join(row.get("sources") or []) or "-")
                continue                                                   # нет положения (устройство без места): комнату и взгляд считать не из чего
            around = (pwhere.where([row], self.layout, self.zones) or [{}])[0]
            log.info("состояние %s: место (%.2f; %.2f) поза %s взгляд %s | комната %s, зона %s, рядом %s, смотрит на %s | уверенность %.2f, источники %s | шаги: %s",
                     row["person"], row["x"], row["z"], row.get("pose"), "нет" if row.get("face") is None else f"{math.degrees(row['face']):.0f} град",
                     around.get("room"), around.get("zone"), around.get("near"), pwhere.facing(row, objects), row.get("confidence", 0.0),
                     row.get("sources"), "; ".join(trace.get(row["person"], [])) or "без изменений")

    def _faced(self, row: dict) -> dict:
        """Куда смотрит человек, если взгляд неизвестен: по окружению (за столом - к столу, у стены - в комнату), чтобы не сидел спиной к монитору."""
        if row.get("face") is None and self._nav is not None and row.get("x") is not None:
            row = {**row, "face": self._nav.face_at(row["x"], row["z"], row.get("pose") or "stand")}
        return row

    def _entry_point(self) -> tuple[float, float] | None:
        """Вход в квартиру для пришедших: место «вход» плана."""
        door = self.place_points().get("вход")
        return (door["x"], door["z"]) if door else None

    def _seat_under(self, point: dict) -> dict:
        """Подписанная точка у края дивана, кровати или кресла считается ими: человек садится или ложится на предмет, а не стоит рядом (метка легла у угла дивана кабинета)."""
        best = None
        for o in self.layout.get("objects") or []:
            pose = next((p for word, p in PLACE_OBJECT_POSES if word in (o.get("title") or "").lower()), None)
            if not pose or o.get("kind") == "opening":
                continue
            dx = max(o["x"] - point["x"], 0.0, point["x"] - (o["x"] + o["w"]))
            dz = max(o["z"] - point["z"], 0.0, point["z"] - (o["z"] + o["d"]))
            gap = math.hypot(dx, dz)
            if gap <= SEAT_SNAP_M and (best is None or gap < best[0]):
                best = (gap, {"x": o["x"] + o["w"] / 2, "z": o["z"] + o["d"] / 2, "pose": pose})
        return best[1] if best else point

    def place_points(self) -> dict[str, dict]:
        """Куда можно поставить человека: комнаты, подписанные зоны, мебель с позой (кровать - лёжа, диван и стулья - сидя), подписанные места
        плана и места устройств. Позднее перекрывает раннее: места устройств точнее остального."""
        points: dict[str, dict] = {}
        for r in self.layout.get("rooms") or []:
            if r.get("title"):
                points[str(r["title"]).lower()] = {"x": r["x"] + r["w"] / 2, "z": r["z"] + r["d"] / 2, "pose": "stand"}
        for o in self.layout.get("objects") or []:
            pose = next((p for word, p in PLACE_OBJECT_POSES if word in (o.get("title") or "").lower()), None)
            if pose and o.get("kind") != "opening":
                points[_place_name(o["title"])] = {"x": o["x"] + o["w"] / 2, "z": o["z"] + o["d"] / 2, "pose": pose}
        for zn in self.zones:
            points[str(zn["label"]).lower()] = {"x": zn["x"] + zn["w"] / 2, "z": zn["z"] + zn["d"] / 2, "pose": "stand"}
        for p in self.layout.get("places") or []:
            if isinstance(p, dict) and "label" in p:
                points[str(p["label"]).lower()] = self._seat_under({"x": p["x"], "z": p["z"], "pose": "stand"})
        if self._nav is not None:                                      # центр зоны «холодильник» лежит внутри холодильника: встают перед ним, а не в него
            for point in points.values():
                if point["pose"] == "stand":
                    point["x"], point["z"] = self._nav.snap(point["x"], point["z"])
        return {**points, **self.messages.spots}

    def _device_spots(self, d: dict) -> list:
        """Где хозяин устройства (x, z, доля, место): точка субъекта человека в потоке, пока телефон слышно; нет её - точка самого телефона, одна, чтобы слияние не прыгало между пятнами;
        без потока - по уровню и отпечаткам, если включено свидетельство уровня; иначе места нет."""
        if self.flow is not None:
            owner = self._owner_of(d.get("name"))
            return (self.flow.person_spot(owner) if owner else []) or self.flow.point_spot(d.get("name"), d.get("iface"))
        return self._level_spots(d) if self._level_evidence else []

    def _level_spots(self, d: dict) -> list[plevel.Spot]:
        """Где устройство по уровню сигнала: вероятности мест из отпечатков его диапазона с учётом прошлой оценки, отобранные по местам плана."""
        key = self._device_key(d)
        tracker = self._place_trackers.setdefault(key, ptrans.PlaceTracker())
        observed_at = self._clock() - d[SNAPSHOT_SEEN_AGO]
        smoother = self._level_smoothers.setdefault(key, psmooth.LevelSmoother())
        level = smoother.add(observed_at, d[SNAPSHOT_SIGNAL_DB])
        if self.track_window_s and observed_at > tracker.observed_at:          # скользящее окно: последние секунды перемножаются заново, прошлое держит только momentum
            tracker.posterior = pwindow.estimate(smoother.recent(observed_at, self.track_window_s),
                                                 lambda prior, db, power: self.fingerprints.place_probabilities(key, db, prior, power=power), tracker.posterior or None)
            tracker.observed_at = observed_at
            probs = tracker.posterior
        elif self.track_window_s:
            probs = tracker.posterior
        else:
            stay = ptrans.STILL_STAY_PER_S if smoother.is_still(observed_at) else ptrans.STAY_PER_S          # лежит - держится на месте, идёт в руке - может уйти
            probs = tracker.update(observed_at, self._place_distance, lambda prior, power: self.fingerprints.place_probabilities(key, level, prior, power=power), stay)
        return plevel.spots(pprefs.apply(probs, self.prefs.table(key, self._clock()), key), self.layout.get(LAYOUT_PLACES) or [])

    @staticmethod
    def _device_key(row: dict) -> str:
        return pfp.device_key(row[SNAPSHOT_NAME], row[SNAPSHOT_IFACE])

    def _devices_view(self, now: float) -> list[dict]:
        """Строки устройств для страницы: к каждой присутствующей добавлены места по уровню сигнала."""
        rows = self.devices.snapshot(now, self._reading.per_station if self._reading else None)
        out = []
        for row in rows:
            where = self._where_by_level(row) if row[SNAPSHOT_PRESENT] else []
            at = None
            if row[SNAPSHOT_PRESENT]:
                at = (self.flow.point_of(row.get("name"), row.get("iface")) if self.flow is not None else None) or self._sampled_place(row, now)
            out.append({**row, DEVICE_WHERE: where, DEVICE_AT: at, "owner": self._owner_of(row.get("name")),
                        DEVICE_FIELD: self._field_of(row) if row[SNAPSHOT_PRESENT] else []})
        return out

    def _field_of(self, row: dict) -> list[dict]:
        """Все места устройства с вероятностью выше FIELD_MIN_P и координатами: слой «поле телефона» на странице."""
        posterior = self._preferred(self._device_key(row))
        return [{"place": place, "p": round(p, 3), **xy} for place, p in sorted(posterior.items(), key=lambda kv: -kv[1])
                if p >= FIELD_MIN_P and (xy := self._place_xy(place))]

    def _sampled_place(self, row: dict, now: float) -> dict | None:
        """Место устройства выборкой по полной оценке (не вершиной): {place, x, z} или None, если оценки или координат места нет."""
        key = self._device_key(row)
        tracker = self._place_trackers.get(key)
        place = self._sampler.pick(key, self._preferred(key), now)
        spot = self._place_xy(place) if place else None
        return {"place": place, **spot} if spot else None

    def _where_by_level(self, row: dict) -> list[dict]:
        """Лучшие места устройства по уровню с вероятностями, чтобы на странице было видно, что решил радар и насколько уверенно."""
        self._level_spots(row)                                          # новый отсчёт обновляет оценку; прежний второй раз не считается
        posterior = self._preferred(self._device_key(row))
        best = sorted(posterior.items(), key=lambda item: -item[1])[:plevel.TOP_PLACES]
        return [{"place": place, "p": round(p, 2), **(self._place_xy(place) or {})} for place, p in best if p >= plevel.MIN_PLACE_P]

    def _owner_of(self, key: str) -> str | None:
        """Хозяин устройства по файлу хозяев: ключ, который входит в имя устройства."""
        low = (key or "").lower()
        return next((person for part, person in self._owners.items() if part in low), None) if low else None

    def _preferred(self, key: str) -> dict[str, float]:
        """Оценка мест устройства с предпочтениями: уровень задаёт правдоподобие, карта предпочтений разводит равные места."""
        tracker = self._place_trackers.get(key)
        return pprefs.apply(dict(tracker.posterior), self.prefs.table(key, self._clock()), key) if tracker and tracker.posterior else {}

    def _place_xy(self, place: str) -> dict | None:
        """Координаты места отпечатка: замеренные положения, иначе подписанная точка плана, иначе место плана по названию; нет нигде - None."""
        known = self._fingerprint_xy.get(place)
        if known:
            return {"x": known["x"], "z": known["z"]}
        point = self.place_points().get(place)
        return {"x": point["x"], "z": point["z"]} if point else None

    def _place_distance(self, a: str, b: str) -> float:
        """Путь между двумя местами по плану (через двери, в обход мебели); нет карты проходимости или места - прямая."""
        if (a, b) not in self._place_distances:
            self._place_distances[(a, b)] = self._place_distances[(b, a)] = self._path_length(a, b)
        return self._place_distances[(a, b)]

    def _path_length(self, a: str, b: str) -> float:
        coords = {(p.get(plevel.PLACE_LABEL) or "").lower(): (p[plevel.PLACE_X], p[plevel.PLACE_Z]) for p in self.layout.get(LAYOUT_PLACES) or []}
        if a not in coords or b not in coords:
            return math.inf
        points = self._nav.route([coords[a], coords[b]]) if self._nav else [coords[a], coords[b]]
        return sum(math.dist(p, q) for p, q in zip(points, points[1:]))

    def _with_seen_pose(self, rows: list[dict], evidence: list) -> list[dict]:
        """Поза из свидетельства канала, который позу видит (pose_seen): её пишет копилка; поза, выбранная шагами дальше, туда не попадает."""
        seen: dict[str, tuple[float, str]] = {}
        for e in evidence:
            if e.person and e.pose and e.source in self._pose_sources and not e.away and e.weight > seen.get(e.person, (-1.0, ""))[0]:
                seen[e.person] = (e.weight, e.pose)
        return [{**r, "pose_seen": seen[r["person"]][1]} if r["person"] in seen else r for r in rows]

    def _fused_identities(self, evidence: list, now: float) -> list[dict]:
        rows = [{"person": e.person, "confidence": round(e.confidence, 3), "x": e.x, "z": e.z, "pose": e.pose, "face": e.face, "activity": round(e.activity, 3), "sources": e.sources, "evidence": e.evidence, "notes": e.notes[:3]}
                for e in fuse.fuse(evidence, now, previous=self._fused_at)]
        self._fused_at = {r["person"]: (r["x"], r["z"]) for r in rows if r["x"] is not None}
        return rows

    @property
    def enabled(self) -> bool:
        return self.mode != MODE_OFF

    def snapshot(self, now: float | None = None) -> dict:
        if not self.enabled:
            return {"enabled": False, "mode": self.mode}
        now = self._clock() if now is None else now
        snap = self._store.snapshot(now)
        if self.mode == MODE_RSSI:
            return {"enabled": True, "mode": self.mode, "ts": now, "people": [], "links": [],
                    "identities": self.identities(now), "signal": self._signal(now),
                    "devices": self._devices_view(now)}
        people = [pr.Person(p["id"], p["room"], tuple(tuple(pt) for pt in p["points"])) for p in snap["people"]]
        for entry, person in zip(snap["people"], people):
            entry["zone"] = pr.zone_at(self.zones, *pr.centroid(person)) if person.points else None
        return {"enabled": True, "mode": self.mode, **snap, "links": pr.interactions(people, self.layout)}

    def step(self, now: float | None = None) -> dict | None:
        """Один тик: кадр источника в хранилище и карту посещений, снимок для рассылки; None - радар выключен."""
        if not self.enabled:
            return None
        now = self._clock() if now is None else now
        self._cool(now)
        if self.mode == MODE_RSSI:
            self._frames += 1
            snap = self.snapshot(now)
            self._accumulate_presence(snap["identities"], now)
            if self._frames % HEAT_SEND_EVERY == 1:
                snap["heat"] = self.heat_payload()
            return snap
        frame = self._tracked(pr.mock_frame(self.layout, now - self._t0, ts=now), now)
        self._store.update(frame)
        seconds, self._last_step = now - self._last_step, now
        for person in frame.people:
            self.heat.add(*pr.centroid(person), seconds)
        self._frames += 1
        snap = self.snapshot(now)
        if self._frames % HEAT_SEND_EVERY == 1:
            snap["heat"] = self.heat_payload()
        return snap

    def _tracked(self, frame: pr.Frame, now: float) -> pr.Frame:
        """Стабильные id по габаритам и близости (radar/presence_track.py); шум без класса выбрасывается."""
        tracks = self._tracker.assign([tk.measure(p.points) for p in frame.people], now)
        people = tuple(pr.Person(t.tid, p.room, p.points, p.identity, p.who, t.kind)
                       for p, t in zip(frame.people, tracks) if t is not None)
        return pr.Frame(frame.ts, frame.source, people)

    def _signal(self, now: float) -> dict:
        """Активность по роутеру; возраст растёт без новых кругов, страница по нему гасит ауру при обрыве SSH."""
        if self._reading is None:
            return {"activity": 0.0, "stations": 0, "active": 0, "age_s": None}
        r = self._reading
        return {"activity": r.activity, "stations": r.stations_total, "active": r.stations_active,
                "age_s": round(max(0.0, now - self._reading_at), 2), "lines": r.lines}

    def feed_stations(self, stations: list[rs.Station], now: float | None = None) -> None:
        """Круг опроса роутера в детектор; вызывается потоком SSH и проверяется без сети."""
        self._reading = self._detector.update(stations)
        self._reading_at = self._clock() if now is None else now
        events = self.devices.update(stations, self._reading_at)
        if self.flow is not None:
            self.flow.observe(stations, self._reading_at)
            self.flow_history.record(self.flow.rows(), self._reading_at)
        self.tracks.record(stations, self._reading_at)
        self.raw_tracks.record(stations, self._reading_at)
        if events:
            self.device_events = (self.device_events + events)[-DEVICE_EVENTS_KEEP:]
            self.save_devices()

    def _device_place(self, name: str | None) -> str | None:
        """Место, на которое устройство посадили данными (карта мест устройств): по части имени."""
        low = (name or "").lower()
        return next((place for key, place in self.devices.places.items() if key.lower() in low), None) if low else None

    async def _consume(self) -> None:
        async for stations in self._stations():
            self.feed_stations(stations)

    def heat_payload(self, now: float | None = None) -> dict:
        """Карта для страницы: накопленное и остывшее плюс тепло записанных прогулок по свежести; дрожание Wi-Fi в неё не идёт."""
        return {"cell_m": self.heat.cell_m, "cells": self.heat.cells(), "steps": self.steps.cells()}

    def _cool(self, now: float) -> None:
        """Остывание накопленной карты по времени между вызовами."""
        dt, self._last_cool = now - self._last_cool, now
        if dt > 0:
            self.heat.decay(0.5 ** (dt / self.heat_half_life_s))
            self.steps.decay(0.5 ** (dt / STEPS_HALF_LIFE_S))

    def hello(self) -> dict:
        """Первое сообщение странице: планировка, карта посещений и зоны один раз, дальше только снимки."""
        if not self.enabled:
            return {"type": "hello", "enabled": False, "mode": self.mode, "layout": None}
        return {"type": "hello", "enabled": True, "mode": self.mode, "layout": self.layout,
                "heat": self.heat_payload(), "zones": self.zones,
                "stale_s": self._store.stale_s, "lost_s": self._store.lost_s,
                "tuning": self.tuning.to_dict(), "knobs": tune.knobs_payload(),
                "place_keys": sorted(self.place_points()), "overrides": self.overrides}   # по ним страница рассеивает точки

    def add_zone(self, data: dict) -> dict:
        """Подпись зоны человеком (кровать, диван, коридор, кухня); ValueError с понятной причиной - в ответ API."""
        zone = pr.validate_zone(data, {z["id"] for z in self.zones})
        zone["locked"] = bool(data.get("locked", False))
        self.zones.append(zone)
        self.save_zones()
        log.info("зона добавлена: %s (%d всего)", zone["label"], len(self.zones))
        return zone

    def set_zone_lock(self, zone_id: str, locked: bool) -> bool:
        """Замок на зоне: закрытую не стереть; решает человек (логические зоны - рабочие, домашняя, полки питомцев)."""
        zone = next((z for z in self.zones if z["id"] == zone_id), None)
        if zone is None:
            return False
        zone["locked"] = bool(locked)
        self.save_zones()
        log.info("зона %s: замок %s", zone_id, "поставлен" if locked else "снят")
        return True

    def remove_zone(self, zone_id: str) -> bool:
        if any(z["id"] == zone_id and z.get("locked") for z in self.zones):
            raise ZoneLocked(zone_id)
        kept = [z for z in self.zones if z["id"] != zone_id]
        removed = len(kept) != len(self.zones)
        self.zones = kept
        if removed:
            self.save_zones()
        log.info("зона %s: %s (%d всего)", zone_id, "удалена" if removed else "не найдена", len(self.zones))
        return removed

    def clear_heat(self) -> int:
        """Карта посещений обнуляется и сразу пишется на диск; возвращает, сколько клеток было. Карта посещений это только сохранённые координаты с вероятностью; записанные прогулки в неё не входят, а очистка прячет их линию с пола (файл прогулок остаётся данными)."""
        cells = len(self.heat.counts)
        self.heat = pr.Heatmap(self.heat.cell_m)
        self.steps = pr.Heatmap(self.heat.cell_m)
        self._walks_cleared_at = self._clock()
        self.save_heat()
        log.info("карта посещений очищена: было %d клеток", cells)
        return cells

    def save_heat(self) -> None:
        if not self.enabled:
            return                       # выключенный радар - no-op, в том числе для диска
        _write_text(self._heat_file, json.dumps({**json.loads(self.heat.to_json()), WALKS_CLEARED_KEY: self._walks_cleared_at}, separators=(",", ":")), "карта посещений")

    def _accumulate_presence(self, rows: list[dict], now: float) -> None:
        """Каждый кадр: пребывание (где человек сейчас, вес - уверенность) в долгую карту, идущие - ещё в слой шагов, учёт «сколько уже на месте»."""
        seconds, self._last_traffic_at = min(max(0.0, now - self._last_traffic_at), PRESENCE_DT_CAP_S), now
        present = set()
        for row in rows:
            if row.get("x") is None or row.get("z") is None:
                continue
            person, x, z = row["person"], row["x"], row["z"]
            present.add(person)
            self.heat.add(x, z, seconds * float(row.get("confidence") or 0.0), limit=HEAT_CELL_LIMIT_S)
            if row.get("walking"):
                self.steps.add(x, z, seconds)
                self._time_moving[person] = self._time_moving.get(person, 0.0) + seconds
            else:
                self._time_still[person] = self._time_still.get(person, 0.0) + seconds
            held = self._stay.get(person)
            if held is None or row.get("walking") or math.hypot(x - held["x"], z - held["z"]) > STAY_RADIUS_M:
                self._stay[person] = {"x": x, "z": z, "since": now, "pose": row.get("pose")}                          # сдвинулся дальше радиуса или идёт: отсчёт заново
        for person in [p for p in self._stay if p not in present]:
            del self._stay[person]                                                           # человека нет на карте: сколько он на месте, неизвестно
        self._record_habits(rows, seconds, now)

    def stay_report(self, now: float | None = None) -> list[dict]:
        """Сколько каждый уже на одном месте и пора ли размяться (дольше STAY_ALERT_S): основа для фразы SAM «прогуляйся»."""
        now = self._clock() if now is None else now
        out = []
        for person, held in sorted(self._stay.items()):
            minutes = max(0.0, now - held["since"]) / 60.0
            place = (pwhere.where([{"person": person, "x": held["x"], "z": held["z"]}], self.layout, self.zones) or [{}])[0]
            still, moving = self._time_still.get(person, 0.0), self._time_moving.get(person, 0.0)
            share = still / (still + moving) if still + moving > 0 else 0.0
            stretch = minutes * 60.0 >= STAY_ALERT_S
            entry = {"person": person, "minutes": round(minutes, 1), "x": round(held["x"], 2), "z": round(held["z"], 2), "room": place.get("room"), "zone": place.get("zone"),
                     "pose": held.get("pose"), "still_share": round(share, 3), "stretch": stretch}
            if stretch:
                name = ((self.layout.get("people") or {}).get(person) or {}).get("name") or person
                entry.update(pstay.stretch_message(name, minutes, share, held.get("pose"), place.get("zone") or place.get("room")))
            out.append(entry)
        return out

    def set_place(self, name: str, place: str) -> None:
        """Где лежит устройство, как сказал человек («S24» - «стол рабочего ПК»); пустое место снимает привязку."""
        key = name.strip().lower()
        if not key:
            raise ValueError("нужна часть имени устройства, например S24 или Redmi")
        if place.strip():
            self.devices.places[key] = place.strip()
        else:
            self.devices.places.pop(key, None)
        _write_text(self._places_file, json.dumps(self.devices.places, ensure_ascii=False, indent=1), "места устройств")

    def walks(self) -> list[dict]:
        """Размеченные прогулки (точки маршрута с временем); нет файла - пусто, битый - пусто и строка в лог."""
        data = _read_json(self._walks_file, [], "прогулки")
        return [self._with_track(w) for w in data if self._after_clear(w)] if isinstance(data, list) else []

    def _after_clear(self, walk: dict) -> bool:
        """Прогулка, которая кончилась до очистки карты, не показывается: очистка убирает все следы с пола, файл прогулок остаётся данными."""
        return max((pt["t"] for pt in walk.get("points") or []), default=float("inf")) > self._walks_cleared_at

    def _with_track(self, walk: dict) -> dict:
        """К прогулке добавляется track - координаты по времени по проходимому пути; считается один раз на набор точек."""
        if self._nav is None or not walk.get("points"):
            return walk
        key = json.dumps(walk["points"], sort_keys=True)
        if key not in self._tracks_cache:
            self._tracks_cache[key] = self._nav.timed_track(walk["points"])
        return {**walk, "track": self._tracks_cache[key]}

    def save_devices(self) -> None:
        if not self.enabled:
            return
        _write_text(self._devices_file, self.devices.to_json(), "учёт устройств")

    def where(self, person: str = "", now: float | None = None) -> list[dict]:
        """Где кто словами: комната, зона, мебель рядом, поза; для SAM: ручка where (radar/presence_sam_api.py)."""
        now = self._clock() if now is None else now
        return pwhere.where(self.identities(now), self.layout, self.zones, person) if self.enabled else []

    def set_autopilot(self, on: bool, period_s: float | None = None, mode: str | None = None) -> dict:
        """Автопилот маршрутов для отладки анимации; выключение снимает назначенные им места."""
        if period_s is not None:
            self.autopilot_period_s = min(120.0, max(2.0, float(period_s)))
        if mode in ("random", "routine"):
            if mode != self.autopilot_mode:
                self._routine = None                  # смена режима начинает быт заново с «сна»; тот же режим (страница шлёт его с каждым ползунком) быт не сбрасывает
            self.autopilot_mode = mode
        if on and not self.autopilot_on:
            self._autopilot_next = 0.0
            self._routine_reassert = True             # включили снова: быт продолжается с текущей сцены, места расставляются заново
        if self.autopilot_on and not on:
            for person in list((self.layout.get("people") or {})):
                self.clear_person(person)
            self.walkers.reset()                      # без автопилота люди встают на свои места сразу, а не идут к ним ещё полминуты
            self._routine_logged = None
        self.autopilot_on = bool(on)
        log.info("автопилот: %s, период %.0f с", "включён" if on else "выключен", self.autopilot_period_s)
        self.save_state()
        return {"on": self.autopilot_on, "period_s": self.autopilot_period_s, "mode": self.autopilot_mode}

    def _autopilot_step(self, now: float) -> None:
        if self.autopilot_on and self.autopilot_mode == "routine":
            self._routine_step(now)
            return
        if not self.autopilot_on or now < self._autopilot_next:
            return
        self._autopilot_next = now + self.autopilot_period_s
        places = self.place_points()
        rooms = [str(r["title"]).lower() for r in self.layout.get("rooms") or [] if r.get("title") and str(r["title"]).lower() in places]
        plan = autopilot.plan(list(self.layout.get("people") or {}), sorted(places), rooms, self._random.python("autopilot"))
        for person, key in plan.items():
            self.set_person(person, 0.9, key)
        log.info("автопилот: %s", ", ".join(f"{p} -> {k}" for p, k in plan.items()))

    def _routine_step(self, now: float) -> None:
        """Быт: сцена за сценой по кругу; назначения применяются как ручные вероятности (нет дома - вероятность 0)."""
        if self._routine is None:
            self._routine = routine.Routine(routine.load_scenes(os.environ.get(routine.ROUTINE_FILE_ENV) or routine.DEFAULT_ROUTINE_FILE), busy=self.walkers.is_walking, random_start=True, rng=self._random.python("routine"))
        places = self.place_points()
        people = set(self.layout.get("people") or {})
        changes = self._routine.step(now)
        if self._routine_reassert and self._routine.scene:
            changes = {**self._routine.places, **changes}            # после паузы автопилота места текущей сцены ставятся заново
        self._routine_reassert = False
        for person, place in changes.items():
            if person not in people:
                continue
            if place is None:
                self.set_person(person, 0.0, None)
            elif place.lower() in places:
                self.set_person(person, 0.9, place)
            else:
                log.warning("быт: места %r нет в плане, %s остаётся где был", place, person)
        if self._routine.scene and self._routine.scene != self._routine_logged:               # строка на смену сцены, не на каждый тик
            self._routine_logged = self._routine.scene
            log.info("быт: сцена «%s», места %s", self._routine.scene, {p: v for p, v in self._routine.places.items()})

    def set_person(self, person: str, confidence: float, place: str | None = None) -> None:
        """Задать вероятность человека вручную (отладка); 0 убирает его с карты, ValueError - если вне 0..1."""
        if not person.strip():
            raise ValueError("нужно имя или ключ человека из data/presence_owners.json")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("вероятность - число от 0 до 1")
        self.overrides[person.strip().lower()] = {"confidence": confidence, "place": (place or "").strip().lower() or None}
        log.info("ручная вероятность: %s %.2f место %s", person.strip().lower(), confidence, (place or "").strip().lower() or "-")

    def clear_person(self, person: str) -> bool:
        removed = self.overrides.pop(person.strip().lower(), None) is not None
        log.info("ручная вероятность снята: %s (%s)", person.strip().lower(), "была" if removed else "не было")
        return removed

    def set_tuning(self, data: dict) -> dict:
        """Пороги детектора с ползунков: действуют сразу, пишутся на диск; ValueError с причиной - в ответ."""
        self.tuning.update(data)
        _write_text(self._tuning_file, json.dumps(self.tuning.to_dict(), indent=1), "пороги детектора")
        return self.tuning.to_dict()

    def set_animation(self, data: dict) -> dict:
        """Параметры анимации роя с ползунков страницы: пишутся на диск сразу, каждое изменение - строка журнала; ValueError с причиной - в ответ."""
        patch = panim.parse(data)
        for line in panim.changes(self.animation, patch):
            log.info("анимация: %s", line)
        self.animation = {**self.animation, **patch}
        _write_text(self._animation_file, json.dumps(self.animation, indent=1), "параметры анимации")
        return self.animation

    def reset_animation(self) -> dict:
        """Сброс анимации: на сервере пусто, страница вернёт свои значения по умолчанию."""
        log.info("анимация: сброшена к значениям по умолчанию")
        self.animation = {}
        _write_text(self._animation_file, "{}", "параметры анимации")
        return self.animation

    def reset_tuning(self) -> dict:
        return self.set_tuning(tune.Tuning().to_dict())

    def save_zones(self) -> None:
        _write_text(self._zones_file, json.dumps(self.zones, ensure_ascii=False, indent=1), "зоны")

    def add_client(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self._clients.add(q)
        return q

    def remove_client(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    async def run(self) -> None:
        """Цикл тиков; выключенный радар сразу возвращается, без клиентов не считает кадры."""
        if not self.enabled:
            return
        if self.mode == MODE_RSSI and self._stations is not None:
            asyncio.create_task(self._consume(), name="presence-router")
        while True:
            await self._sleep(self.tick_s)
            if not self._clients:
                continue
            try:
                await self._tick()
            except Exception:                                   # один сбойный тик не должен убить цикл: страница замирала бы без единого следа
                log.exception("тик радара упал, цикл продолжается")

    async def _tick(self) -> None:
        started = time.perf_counter()
        self._autopilot_step(self._clock())
        if self._clock() - self._last_save >= HEAT_SAVE_EVERY_S:
            self._last_save = self._clock()
            self.save_heat()
        self.recorder.maybe_save(self._clock())
        if self._clock() - self._last_state_save >= STATE_SAVE_EVERY_S:
            self._last_state_save = self._clock()
            self.save_state()
        message = json.dumps({"type": "frame", **self.step()}, ensure_ascii=False, separators=(",", ":"))
        for q in list(self._clients):
            if q.full():
                q.get_nowait()          # отставший клиент теряет старый кадр, а не растит очередь
            q.put_nowait(message)
        spent = time.perf_counter() - started
        if spent > SLOW_TICK_S:
            log.warning("медленный тик кадра: %.0f мс, клиентов %d", spent * 1000, len(self._clients))


def _build_nn(env: dict, layout: dict) -> pnn.NnBundle | None:
    """Сети по диапазонам за флагом SAM_PRESENCE_NN=1; нет флага - None, поток без сети; сетей в папке нет - None и строка в лог."""
    if env.get(pnn.NN_ENV) != "1":
        return None
    folder = Path(env.get(pnn.NN_DIR_ENV) or pnn.DEFAULT_NN_DIR)
    bundle = pnn.NnBundle.load(folder, layout.get("rooms") or [], env_float(pnn.NN_WEIGHT_ENV, pnn.DEFAULT_NN_WEIGHT, environ=env))
    if not bundle.models:
        print(f"[Presence] сеть включена, но в {folder} нет gru-<интерфейс>.npz/.json: python scripts/radar_nn_gru.py --export; поток без сети")
        return None
    return bundle


def _build_flow(env: dict, layout: dict, prefs: dict | None, habits: dict | None, randomness: Randomness | None = None, owners: dict | None = None) -> pfl.LiveFlow | None:
    """Поток положения за флагом; не собрался (нет стен или роутера в плане, нет моделей уровня) - радар работает без него, причина одной строкой в лог."""
    if env.get(FLOW_ENV) != "1":
        return None
    models_file = Path(env.get(pfl.RADIO_MODEL_ENV) or pfl.DEFAULT_RADIO_MODEL_FILE)
    try:
        return pfl.LiveFlow(layout, pfl.resolve_models(_read_json(models_file, {}, "модели уровня")), prefs or {}, habits or {}, randomness=randomness,
                           window_slices=env_int(pfl.WINDOW_SLICES_ENV, pfl.pw_.WINDOW_SLICES, environ=env),
                           start_certainty=env_float(pfl.START_CERTAINTY_ENV, pfl.LIVE_START_CERTAINTY, environ=env),
                           start_min_share=env_float(pfl.START_MIN_SHARE_ENV, pfl.LIVE_START_MIN_SHARE, environ=env),
                           view_paths=env_int(pfl.VIEW_PATHS_ENV, pfl.VIEW_PATHS, environ=env),
                           preference_weight=env_float(pfl.PREFERENCE_WEIGHT_ENV, pfl.PREFERENCE_WEIGHT, environ=env),
                           motion_sigma_m=env_float(pfl.MOTION_SIGMA_ENV, 0.0, environ=env) or pfl.MOTION_SIGMA_M,      # пусто или нуль - без инерции
                           stay_share=env_float(pfl.STAY_SHARE_ENV, pfl.STAY_SHARE, environ=env),
                           delta_weight=env_float(pfl.DELTA_WEIGHT_ENV, pfl.DELTA_WEIGHT, environ=env),
                           delta_sd_db=env_float(pfl.DELTA_SD_ENV, pfl.DELTA_SD_DB, environ=env),
                           hold_ratio=env_float(pfl.HOLD_RATIO_ENV, pfl.ps.pc.HOLD_RATIO, environ=env),
                           hold_ticks=env_int(pfl.HOLD_TICKS_ENV, pfl.ps.pc.HOLD_TICKS, environ=env), owners=owners,
                           top_view_max_h_m=env_float(pfl.ptv.TOP_VIEW_MAX_H_ENV, pfl.ptv.TOP_VIEW_MAX_H_M, environ=env), nn=_build_nn(env, layout))
    except (KeyError, ValueError) as e:
        print(f"[Presence] поток положения не собран ({e}); радар работает без него: проверь план (стены, роутер) и {models_file}")
        return None


def build(environ: dict | None = None) -> PresenceHub:
    env = os.environ if environ is None else environ
    mode = mode_from_env(env)
    randomness = Randomness.from_environ(env)
    if mode != MODE_OFF:
        print(f"[Presence] сид случайности {randomness.seed}: {SEED_ENV}={randomness.seed} повторит прогон")
    layout = pr.load_layout(env.get(pr.LAYOUT_ENV), pr.EMPTY_LAYOUT if mode == MODE_RSSI else pr.DEMO_LAYOUT) \
        if mode != MODE_OFF else pr.DEMO_LAYOUT
    # демо-треки выдуманного человека не копятся на диск: карта посещений - только от реального источника
    heat_file = Path(env.get(HEAT_FILE_ENV) or DEFAULT_HEAT_FILE) if mode not in (MODE_OFF, MODE_MOCK) else None
    zones_file = Path(env.get(ZONES_FILE_ENV) or DEFAULT_ZONES_FILE)
    zones = _read_json(zones_file, [], "зоны") if mode != MODE_OFF else []
    devices_file = Path(env.get(DEVICES_FILE_ENV) or DEFAULT_DEVICES_FILE) if mode == MODE_RSSI else None
    places_file = Path(env.get(PLACES_FILE_ENV) or DEFAULT_PLACES_FILE) if mode == MODE_RSSI else None
    tracks_dir = Path(env.get(TRACKS_DIR_ENV) or DEFAULT_TRACKS_DIR) if mode == MODE_RSSI else None
    tuning_file = Path(env.get(TUNING_FILE_ENV) or DEFAULT_TUNING_FILE) if mode == MODE_RSSI else None
    animation_file = Path(env.get(ANIMATION_FILE_ENV) or DEFAULT_ANIMATION_FILE) if mode == MODE_RSSI else None
    stations = None
    if mode == MODE_RSSI:
        target = env.get(ROUTER_ENV) or DEFAULT_ROUTER
        ifaces = [i.strip() for i in (env.get(IFACES_ENV) or DEFAULT_IFACES).split(",") if i.strip()]
        poll = float(env.get(POLL_ENV) or DEFAULT_POLL_S)
        stations = lambda: ssh.station_stream(target, ifaces, poll)
    prefs = _read_json(Path(env.get(PREFS_FILE_ENV) or DEFAULT_PREFS_FILE), {}, "предпочтения устройств") if mode == MODE_RSSI else None
    owners = _read_json(Path(env.get(OWNERS_FILE_ENV) or DEFAULT_OWNERS_FILE), {}, "хозяева устройств") if mode == MODE_RSSI else None
    habits = _read_json(Path(env.get(HABITS_FILE_ENV) or DEFAULT_HABITS_FILE), {}, "привычки людей") if mode == MODE_RSSI else None
    pc = _read_json(Path(env.get(ppc.PC_FILE_ENV) or ppc.DEFAULT_PC_FILE), {}, "активность компьютера") if mode == MODE_RSSI else None
    tracks = ptr.TrackLog(tracks_dir)
    raw_tracks = ptr.TrackLog(tracks_dir if env.get(ptr.RAW_ENV) == "1" else None, every_s=0.0, max_bytes=ptr.RAW_MAX_BYTES, prefix=ptr.RAW_PREFIX, fresh_ms=None)
    flow = _build_flow(env, layout, prefs, habits, randomness, owners) if mode == MODE_RSSI else None
    rewind = prw.Rewind(flow.fresh, tracks) if flow is not None else None      # близнец живого потока: тот же план, модели, миры, хозяева и сид, состояние на T совпадает с живым
    flow_history = pfh.FlowHistory(Path(env.get(pfh.HISTORY_DIR_ENV) or pfh.DEFAULT_HISTORY_DIR), keep_days=env_int(pfh.HISTORY_DAYS_ENV, pfh.HISTORY_KEEP_DAYS, environ=env)) if flow is not None else None
    return PresenceHub(mode, layout, float(env.get(TICK_ENV) or DEFAULT_TICK_S),
                       heat=load_heat(heat_file) if heat_file else None, heat_file=heat_file,
                       zones=zones, zones_file=zones_file, stations=stations,
                       devices=_with_places(load_devices(devices_file), places_file), devices_file=devices_file,
                       tuning=load_tuning(tuning_file), tuning_file=tuning_file, animation_file=animation_file,
                       fingerprints_file=Path(env.get(FINGERPRINTS_FILE_ENV) or DEFAULT_FINGERPRINTS_FILE) if mode == MODE_RSSI else None,
                       level_evidence=env.get(LEVEL_EVIDENCE_ENV) == "1",
                       state_file=Path(env.get(STATE_FILE_ENV) or DEFAULT_STATE_FILE) if mode == MODE_RSSI else None,
                       habit_stats_file=Path(env.get(HABIT_STATS_FILE_ENV) or DEFAULT_HABIT_STATS_FILE) if mode == MODE_RSSI else None,
                       controls=_read_json(Path(env.get(CONTROLS_FILE_ENV) or DEFAULT_CONTROLS_FILE), {}, "положения мест отпечатков") if mode == MODE_RSSI else None,
                       device_prefs=prefs,
                       habits=habits, flow=flow, rewind=rewind, flow_history=flow_history, pc=pc, owners=owners, randomness=randomness, raw_tracks=raw_tracks,
                       pose_sources=tuple(s.strip() for s in (env.get(ppose.POSE_SOURCES_ENV) or "").split(",") if s.strip()),
                       places_file=places_file, tracks=tracks, channels=tuple(c.strip() for c in (env.get(CHANNELS_ENV) or ",".join(DEFAULT_CHANNELS)).split(",") if c.strip()), heat_half_life_s=float(env.get(HEAT_HALF_LIFE_ENV) or DEFAULT_HEAT_HALF_LIFE_S),
                       walks_file=Path(env.get(WALKS_FILE_ENV) or DEFAULT_WALKS_FILE) if mode == MODE_RSSI else None)
