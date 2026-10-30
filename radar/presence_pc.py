"""Активность рабочего компьютера как свидетельство «кто где» (SAM-121): игра идёт без ввода - хозяин на диване с контроллером, ввод есть - за столом.

Компьютер стоит в кабинете. Идёт игра, а клавиатура и мышь молчат - хозяин скорее на диване, и второй игрок по конфигу рядом с ним; ввод был только что -
хозяин за столом, чем бы компьютер ни был занят. Выключенный или спящий компьютер ничего не говорит: отрицательных свидетельств нет, вес «за этим
компьютером» просто не появляется. Что считать игрой, кто хозяин и второй игрок, где стол и диван, порог простоя - конфиг в data/ (`SAM_PRESENCE_PC_FILE`),
не код. Чтение состояния машины (процессы, простой ввода) отделено от решения: решение чистое и проверяется без Windows."""
from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Callable, Mapping

from .presence_fuse import Evidence

SOURCE = "pc"
PC_FILE_ENV = "SAM_PRESENCE_PC_FILE"
DEFAULT_PC_FILE = "data/pc_activity.json"
POLL_S = 10.0                  # состояние машины читается не чаще: процессы и простой меняются медленнее, чем кадр радара
DEFAULT_IDLE_S = 120.0         # без ввода дольше этого при игре - играют с контроллером, не за столом
GAME_WEIGHT = 0.6              # игра без ввода: хозяин на диване; не факт - контроллер могли отложить
PARTNER_WEIGHT = 0.4           # второй игрок рядом: игра часто вдвоём, но не всегда; ставится, только если он дома по другим каналам
INPUT_WEIGHT = 0.7             # ввод был только что: кто-то за столом, и почти наверняка хозяин


@dataclass(frozen=True)
class PcState:
    on: bool                   # компьютер работает и состояние прочитано
    game: bool                 # идёт процесс из списка игр
    idle_s: float              # секунд без ввода с клавиатуры и мыши


def evidence_from_state(state: PcState | None, config: Mapping, places: Mapping[str, Mapping], now: float) -> list[Evidence]:
    """Свидетельства по состоянию машины и конфигу {person, second, desk, sofa, idle_s}: места - ключи точек плана; нет состояния, хозяина или места - пусто."""
    if state is None or not state.on or not config.get("person"):
        return []
    idle_limit = float(config.get("idle_s", DEFAULT_IDLE_S))
    desk, sofa = places.get(str(config.get("desk", "")).lower()), places.get(str(config.get("sofa", "")).lower())
    out = []
    if state.idle_s < idle_limit and desk:
        out.append(Evidence(source=SOURCE, person=config["person"], weight=INPUT_WEIGHT, t=now, x=desk["x"], z=desk["z"], pose=desk.get("pose") or "sit",
                            note=f"за компьютером ввод {state.idle_s:.0f} с назад"))
    elif state.game and sofa:
        out.append(Evidence(source=SOURCE, person=config["person"], weight=GAME_WEIGHT, t=now, x=sofa["x"], z=sofa["z"], pose=sofa.get("pose") or "sit",
                            note=f"игра идёт, ввода нет {state.idle_s:.0f} с: с контроллером на диване"))
        if config.get("second"):
            out.append(Evidence(source=SOURCE, person=config["second"], weight=PARTNER_WEIGHT, t=now, x=sofa["x"], z=sofa["z"], pose=sofa.get("pose") or "sit",
                                note="игра идёт: вдвоём на диване"))
    return out


def drop_unsupported_partner(evidence: list[Evidence], partner: str | None) -> list[Evidence]:
    """Убрать строки канала про второго игрока, если других положительных свидетельств о нём нет: игра на компьютере не говорит, что он дома."""
    if not partner:
        return evidence
    supported = any(e.person == partner and e.source != SOURCE and not e.away for e in evidence)
    return evidence if supported else [e for e in evidence if not (e.source == SOURCE and e.person == partner)]


def windows_idle_s() -> float:
    """Секунд с последнего ввода по GetLastInputInfo; не Windows - бесконечность (ввода не видно)."""
    if sys.platform != "win32":
        return float("inf")

    class LastInputInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return float("inf")
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


def running_processes() -> set[str]:
    """Имена процессов строчными; psutil нет или не вышло - пусто."""
    try:
        import psutil
        return {(p.info["name"] or "").lower() for p in psutil.process_iter(["name"])}
    except Exception:        # noqa: BLE001 - любой сбой чтения процессов равен «машину не вижу»
        return set()


def read_windows_state(config: Mapping) -> PcState:
    """Состояние этой машины: игра идёт, если есть процесс из списка `games` конфига (имена exe строчными)."""
    games = {str(g).lower() for g in config.get("games") or []}
    return PcState(on=True, game=bool(games & running_processes()), idle_s=windows_idle_s())


class PcChannel:
    """Канал для хаба: читает состояние машины не чаще POLL_S, точки мест берёт у хаба при каждом вызове; без хозяина в конфиге молчит и машину не читает."""

    def __init__(self, config: Mapping, places: Callable[[], Mapping[str, Mapping]], reader: Callable[[Mapping], PcState | None] = read_windows_state,
                 poll_s: float = POLL_S) -> None:
        self._config, self._places, self._reader, self._poll_s = config, places, reader, poll_s
        self._state: PcState | None = None
        self._read_at = -float("inf")

    @property
    def partner(self) -> str | None:
        return self._config.get("second") or None

    def evidence(self, now: float) -> list[Evidence]:
        if not self._config.get("person"):
            return []
        if now - self._read_at >= self._poll_s:
            self._state, self._read_at = self._reader(self._config), now
        return evidence_from_state(self._state, self._config, self._places(), now)
