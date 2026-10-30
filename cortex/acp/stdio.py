"""Потоки протокола ACP на исходных stdin/stdout процесса, отдельно от печати.

Цикл агента и клиент LLM пишут журнал через print, а в процессе Cortex stdout - канал JSON-RPC: строка журнала в нём
ломает протокол. Поэтому исходные двоичные stdin/stdout забираются под протокол, а sys.stdout переводится в stderr.
SDK на Windows пишет в sys.stdout в момент записи, поэтому потоки собираются здесь и передаются в run_agent явно.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from typing import BinaryIO


class _WriterProtocol(asyncio.BaseProtocol):
    """Протокол для StreamWriter: запись синхронная, ждать опустошения буфера нечего."""

    async def _drain_helper(self) -> None:
        return None


class _BinaryTransport(asyncio.WriteTransport):
    def __init__(self, out: BinaryIO) -> None:
        super().__init__()
        self._out = out
        self._closing = False

    def write(self, data: bytes) -> None:
        if self._closing:
            return
        self._out.write(data)
        self._out.flush()

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True
        with contextlib.suppress(Exception):
            self._out.flush()

    def can_write_eof(self) -> bool:
        return False

    def abort(self) -> None:
        self.close()


def _feed(loop: asyncio.AbstractEventLoop, source: BinaryIO, reader: asyncio.StreamReader) -> None:
    """Строки источника - в reader из фонового потока: чтение stdin на Windows не асинхронное."""
    try:
        while line := source.readline():
            loop.call_soon_threadsafe(reader.feed_data, line)
    finally:
        loop.call_soon_threadsafe(reader.feed_eof)


async def protocol_streams(source: BinaryIO, sink: BinaryIO,
                           limit: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=limit)
    threading.Thread(target=_feed, args=(loop, source, reader), daemon=True).start()
    writer = asyncio.StreamWriter(_BinaryTransport(sink), _WriterProtocol(), None, loop)
    return reader, writer


def take_stdio() -> tuple[BinaryIO, BinaryIO]:
    """Забрать двоичные stdin/stdout под протокол; дальше print пишет в stderr."""
    source, sink = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr
    return source, sink
