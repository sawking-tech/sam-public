"""Пограничный пересыльщик живого приложения SAM (SAM-182): слушает порт и пересылает TCP к контейнеру приложения.

Запускается в своём контейнере, который стоит в двух сетях: внутренней сети приложения (`--internal`, выхода
наружу нет) и обычной, где порт опубликован только на 127.0.0.1 хоста. Только стандартная библиотека: файл
монтируется в образ песочницы только для чтения. Аргументы: хост приложения, его порт, свой порт.
"""
from __future__ import annotations

import asyncio
import sys

CHUNK = 65536


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


def handler(host: str, port: int):
    async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.open_connection(host, port)
        except OSError:
            client_w.close()
            return
        await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w), return_exceptions=True)
    return handle


async def serve(host: str, port: int, listen: int) -> None:
    server = await asyncio.start_server(handler(host, port), "0.0.0.0", listen)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(serve(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
