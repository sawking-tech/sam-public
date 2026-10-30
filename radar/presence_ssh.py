"""Поток станций роутера по SSH (SAM-105): единственное место радара, которое ходит в сеть.

Роутер только читается: на нём крутится цикл `iw dev <iface> station dump`, ничего не ставится, не перезагружается
и не перенастраивается. Разбор потока -
чистый класс StreamParser, процесс запускается через переданную функцию, поэтому без SSH проверяется на подставных данных.
При сбое поток сам переподключается с нарастающей паузой и пишет одну строку, что не так и что делать.
"""
from __future__ import annotations

import asyncio
import shlex
from typing import AsyncIterator, Awaitable, Callable

from . import presence_rssi as rs

IFACE_MARK = "#IFACE "
LEASES_MARK = "#LEASES"
END_MARK = "#END"
LEASES_FILE = "/tmp/dhcp.leases"      # аренды DHCP роутера: только чтение, по ним станции получают имена
CONNECT_TIMEOUT_S = 6
RETRY_MIN_S = 2.0
RETRY_MAX_S = 30.0


def remote_script(ifaces: list[str], interval_s: float) -> str:
    """Цикл на роутере: по кругу все интерфейсы и пауза; busybox sleep принимает дробные секунды."""
    dumps = "; ".join(f"echo '{IFACE_MARK}{i}'; iw dev {shlex.quote(i)} station dump" for i in ifaces)
    leases = f"echo '{LEASES_MARK}'; cat {LEASES_FILE} 2>/dev/null"
    return f"while true; do {dumps}; {leases}; echo '{END_MARK}'; sleep {interval_s}; done"


class StreamParser:
    """Строки потока в циклы опроса: после END_MARK отдаёт станции всех интерфейсов за один круг."""

    def __init__(self) -> None:
        self._iface = ""
        self._chunks: dict[str, list[str]] = {}
        self._leases: list[str] = []

    def feed(self, line: str) -> list[rs.Station] | None:
        line = line.rstrip("\r\n")
        if line.startswith(IFACE_MARK):
            self._iface = line[len(IFACE_MARK):].strip()
            self._chunks[self._iface] = []
        elif line.strip() == LEASES_MARK:
            self._iface, self._leases = LEASES_MARK, []
        elif line.strip() == END_MARK:
            names = rs.parse_leases("\n".join(self._leases))
            stations = [s for iface, rows in self._chunks.items()
                        for s in rs.parse_station_dump("\n".join(rows), iface, names)]
            self._chunks, self._iface, self._leases = {}, "", []
            return stations
        elif self._iface == LEASES_MARK:
            self._leases.append(line)
        elif self._iface:
            self._chunks[self._iface].append(line)
        return None


Spawn = Callable[..., Awaitable["asyncio.subprocess.Process"]]


async def station_stream(target: str, ifaces: list[str], interval_s: float, spawn: Spawn = asyncio.create_subprocess_exec,
                         sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> AsyncIterator[list[rs.Station]]:
    """Бесконечный поток кругов опроса; разрыв SSH - пауза и новое подключение, причина одной строкой в лог."""
    delay = RETRY_MIN_S
    while True:
        try:
            proc = await spawn("ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
                               "-o", "ServerAliveInterval=5", target, remote_script(ifaces, interval_s),
                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            parser = StreamParser()
            async for raw in proc.stdout:
                stations = parser.feed(raw.decode("utf-8", "replace"))
                if stations is not None:
                    delay = RETRY_MIN_S
                    yield stations
            reason = f"SSH закрылся с кодом {await proc.wait()}"
        except OSError as e:
            reason = f"ssh не запустился: {e}"
        print(f"[Presence] роутер {target}: {reason}; повтор через {delay:.0f} с, проверь SAM_PRESENCE_ROUTER и доступ по ключу")
        await sleep(delay)
        delay = min(delay * 2, RETRY_MAX_S)
