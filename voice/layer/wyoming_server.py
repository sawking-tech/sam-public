"""Общий Wyoming-сервер синтеза: say_out ходит к любому движку так же, как к Piper.

Движок отдаёт функцию synth(text, voice) -> (float32 [-1, 1], rate) или итератор таких пар, если
синтезирует потоком; сервер разбирает запросы describe и synthesize, держит один синтез за раз и отвечает
audio-start, audio-chunk (один или по куску на каждую пару итератора), audio-stop.
Ошибка одной реплики не роняет сервер: клиент получает error, в лог - строка.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Iterator

import numpy as np

from .stress import plain

HOST = "127.0.0.1"
LOG_TEXT = 50          # столько символов реплики в строке лога

Chunk = tuple[np.ndarray, int]
Synth = Callable[[str, str | None], Chunk | Iterator[Chunk]]


async def _send(writer: asyncio.StreamWriter, ev: str, data: dict, payload: bytes = b"") -> None:
    writer.write(json.dumps({"type": ev, "data": data, "payload_length": len(payload)}).encode() + b"\n")
    if payload:
        writer.write(payload)
    await writer.drain()


def to_pcm16(wav: np.ndarray) -> bytes:
    return (np.clip(np.asarray(wav, dtype=np.float32), -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def serve(name: str, port: int, synth: Synth, voices: list[str]) -> None:
    """Слушать порт и отвечать на запросы; name - метка в логе и в describe."""
    lock = asyncio.Lock()  # одна модель: запросы по очереди

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line.strip():  # проверка порта при запуске: соединились и закрыли
                return
            msg = json.loads(line.decode())
            plen = msg.get("payload_length", 0)
            if plen:
                await reader.readexactly(plen)
            if msg.get("type") == "describe":
                await _send(writer, "info", {"tts": [{"name": name, "voices": [{"name": v} for v in voices]}]})
                return
            data = msg.get("data", {})
            text = data.get("text", "").strip()
            if msg.get("type") != "synthesize" or not text:
                await _send(writer, "error", {"text": "ожидался synthesize с текстом"})
                return
            voice = (data.get("voice") or {}).get("name")
            async with lock:
                t = time.time()
                result = await asyncio.to_thread(synth, text, voice)
                chunks = iter([result]) if isinstance(result, tuple) else result
                first, seconds = None, 0.0
                try:
                    while (chunk := await asyncio.to_thread(next, chunks, None)) is not None:
                        wav, rate = chunk
                        pcm = to_pcm16(wav)
                        fmt = {"rate": rate, "width": 2, "channels": 1}
                        if first is None:
                            first = time.time() - t
                            await _send(writer, "audio-start", fmt)
                        await _send(writer, "audio-chunk", fmt, pcm)
                        seconds += len(pcm) / 2 / rate
                finally:
                    # клиент ушёл посреди потока: движок дорабатывает реплику под замком, а не рядом со следующей
                    if hasattr(chunks, "close"):
                        await asyncio.to_thread(chunks.close)
            await _send(writer, "audio-stop", {})
            print(f"[синтез] {name}: {first or 0:.2f}s до звука, {time.time() - t:.2f}s всего, {seconds:.1f}s звука: "
                  f"{plain(text)[:LOG_TEXT]}", flush=True)
        except Exception as exc:
            print(f"[синтез] {name}: ошибка: {exc}", flush=True)
            try:
                await _send(writer, "error", {"text": str(exc)})
            except Exception:
                pass
        finally:
            writer.close()

    async def run() -> None:
        server = await asyncio.start_server(handle, HOST, port)
        print(f"[синтез] {name}: слушаю :{port}, голоса: {', '.join(voices)}", flush=True)
        async with server:
            await server.serve_forever()

    asyncio.run(run())
