"""Перемотка потока положения (SAM-118): состояние окна на любую секунду записи, пересчитанное из треков.

Стоп времени на странице замораживал только то, что окно держало в этот момент. Перемотка строит отдельный поток с тем же планом, моделями и сидом и кормит его
отсчётами треков за минуту до метки T так же, как живыми опросами (срез в секунду, пустые секунды тоже идут, окно сдвигается), поэтому состояние на T совпадает
с тем, что показал бы живой радар в T. Пересчёт стоит доли секунды, результат по T кэшируется: ползунок по записи ходит туда-сюда. Живой поток не трогается.
Модуль чистый: фабрика потока и треки приходят параметрами."""
from __future__ import annotations

from collections import OrderedDict
from typing import Callable, Sequence

from . import presence_flow_live as pfl
from . import presence_rssi as rs
from .presence_tracks import TrackLog

REWIND_SPAN_S = 60.0        # секунд записи до метки: окно 14 с плюс запас, чтобы начало окна успело сложиться, как у смока (WARMUP_S проигрыша 60)
TICK_S = 1.0                # срез перемотки: треки пишутся раз в секунду на устройство
CACHE_SIZE = 64             # состояний по меткам в памяти: ползунок ходит по соседним секундам, каждое состояние несколько мегабайт
NOT_HEARD_MS = 0            # отсчёт из треков считается свежим: в треки идут только свежие (presence_tracks.FRESH_MS)


def stations_of(rows: Sequence[dict]) -> list[rs.Station]:
    """Строки треков одной секунды как станции роутера."""
    return [rs.Station(row["id"], row["if"], float(row["db"]), NOT_HEARD_MS, row.get("name")) for row in rows]


def replay(flow: pfl.LiveFlow, rows: Sequence[dict], at: float, span_s: float = REWIND_SPAN_S, tick_s: float = TICK_S) -> pfl.LiveFlow:
    """Прокормить поток отсчётами с at - span_s до at по тикам: секунда без отсчётов подаётся пустым списком, как на живом радаре."""
    start = at - span_s
    index, tick = 0, start
    ordered = sorted(rows, key=lambda row: row["t"])
    while tick <= at:
        bucket = []
        while index < len(ordered) and ordered[index]["t"] < tick + tick_s:
            if ordered[index]["t"] >= start:
                bucket.append(ordered[index])
            index += 1
        flow.observe(stations_of(bucket), tick + tick_s)
        tick += tick_s
    return flow


class Rewind:
    def __init__(self, make_flow: Callable[[], pfl.LiveFlow | None], tracks: TrackLog, span_s: float = REWIND_SPAN_S, cache_size: int = CACHE_SIZE) -> None:
        self._make_flow, self._tracks, self._span_s, self._cache_size = make_flow, tracks, span_s, cache_size
        self._cache: OrderedDict[int, pfl.LiveFlow | None] = OrderedDict()

    def span(self) -> tuple[float, float] | None:
        """Границы записи, по которым ходит ползунок."""
        return self._tracks.span()

    def flow_at(self, at: float) -> pfl.LiveFlow | None:
        """Поток, прокормленный до секунды at; None - поток не собирается (нет моделей)."""
        key = int(at)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        flow = self._make_flow()
        if flow is not None:
            replay(flow, self._tracks.read_range(key - self._span_s, key), key, self._span_s)
        self._cache[key] = flow
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return flow

    def state(self, at: float, detail: bool = False, only: str | None = None, second: int | None = None) -> dict:
        """Состояние потока на секунду at в формате живого `LiveFlow.state`, с меткой at."""
        flow = self.flow_at(at)
        if flow is None:
            return {"enabled": False, "at": int(at)}
        return {**flow.state(detail, only, second), "at": int(at)}
