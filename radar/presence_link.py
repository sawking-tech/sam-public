"""Признаки линка станции из `iw station dump` (SAM-118): уровни по цепочкам антенн, среднее роутера, уровень подтверждений, скорость и MCS.

Кроме среднего уровня, это независимые измерения одного и того же положения телефона: разность цепочек зависит от угла прихода и позы, скорость и MCS
подбираются роутером под качество канала. Модуль чистый: разбор текста одной станции, запись и время снаружи."""
from __future__ import annotations

import re
from dataclasses import dataclass

_SIGNAL_AVG = re.compile(r"^\s*signal avg:\s+(-?\d+)\s*(?:\[([^\]]*)\])?", re.M)
_ACK_LAST = re.compile(r"^\s*last ack signal:\s*(-?\d+)", re.M)
_ACK_AVG = re.compile(r"^\s*avg ack signal:\s*(-?\d+)", re.M)
_BITRATE = {"tx": re.compile(r"^\s*tx bitrate:(.*)$", re.M), "rx": re.compile(r"^\s*rx bitrate:(.*)$", re.M)}
_MBIT = re.compile(r"([\d.]+)\s*MBit/s")
_MCS = re.compile(r"\bMCS\s+(\d+)")
_NSS = re.compile(r"\bNSS\s+(\d+)")
_CHAINS = re.compile(r"-?\d+")
COUNTERS = ("rx bytes", "tx bytes", "rx packets", "tx packets", "tx retries", "tx failed")      # накопленные счётчики станции в порядке строки `cnt`: по разности соседних строк видна активность и качество канала
_COUNTER = {name: re.compile(rf"^\s*{name}:\s*(\d+)", re.M) for name in COUNTERS}


@dataclass(frozen=True)
class LinkRate:
    mbit: float | None = None
    mcs: int | None = None
    nss: int | None = None


@dataclass(frozen=True)
class LinkFeatures:
    avg_db: float | None = None                  # скользящее среднее уровня, как его ведёт роутер
    avg_chains: tuple[float, ...] = ()
    ack_last_db: float | None = None             # уровень последнего подтверждения от станции
    ack_avg_db: float | None = None
    tx: LinkRate = LinkRate()                    # скорость от роутера к станции
    rx: LinkRate = LinkRate()                    # скорость от станции к роутеру
    counters: tuple[int | None, ...] = ()        # накопленные счётчики станции в порядке COUNTERS; чего нет в выводе, то None

    def as_row(self) -> dict:
        """Компактная строка для файла треков: только то, что есть."""
        row: dict = {}
        if self.avg_db is not None:
            row["avg"] = self.avg_db
        if self.avg_chains:
            row["avgch"] = list(self.avg_chains)
        if self.ack_last_db is not None or self.ack_avg_db is not None:
            row["ack"] = [self.ack_last_db, self.ack_avg_db]
        for name, rate in (("tx", self.tx), ("rx", self.rx)):
            if rate.mbit is not None:
                row[name] = [rate.mbit, rate.mcs, rate.nss]
        if any(c is not None for c in self.counters):
            row["cnt"] = list(self.counters)
        return row


def _number(pattern: re.Pattern, text: str) -> float | None:
    found = pattern.search(text)
    return float(found.group(1)) if found else None


def _rate(pattern: re.Pattern, text: str) -> LinkRate:
    line = pattern.search(text)
    if not line:
        return LinkRate()
    body = line.group(1)
    mbit, mcs, nss = _MBIT.search(body), _MCS.search(body), _NSS.search(body)
    return LinkRate(float(mbit.group(1)) if mbit else None, int(mcs.group(1)) if mcs else None, int(nss.group(1)) if nss else None)


def parse_link(body: str) -> LinkFeatures:
    """Признаки линка из тела одной станции; чего в выводе нет, то None."""
    avg = _SIGNAL_AVG.search(body)
    return LinkFeatures(
        avg_db=float(avg.group(1)) if avg else None,
        avg_chains=tuple(float(c) for c in _CHAINS.findall(avg.group(2) or "")) if avg else (),
        ack_last_db=_number(_ACK_LAST, body), ack_avg_db=_number(_ACK_AVG, body),
        tx=_rate(_BITRATE["tx"], body), rx=_rate(_BITRATE["rx"], body),
        counters=tuple(int(m.group(1)) if (m := _COUNTER[name].search(body)) else None for name in COUNTERS))
