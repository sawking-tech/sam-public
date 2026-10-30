"""Виртуальные входы узла звука (ADR-017): узел раздаёт очищенный звук микрофона по TCP, подписчик слушает.

  python -m voice.layer.vmic [--host H] [--port P]   подписчик для проверки: строка с громкостью раз в секунду

Узел - процесс голосового слоя (voice.py): FramePublisher вешается на AudioCore.mic_tap. Подписчик - VirtualMic:
итератор блоков int16 16 кГц; узел пропал - переподключается сам, строка в лог, без падения.
Состояние выхода (кто говорит в колонки) подписчики читают из шины, как сейчас (bus.py).

Транспорт - локальный TCP: замер 9 окт, кадр 640 байт с темпом 20 мс, TCP p50 0,11 мс, p99 0,17 мс, UDP не
быстрее; TCP держит порядок и переносим на Linux.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from typing import Iterator

import numpy as np

from gateway.env import env_float, env_int

from .frames import Decoder, Fanout, Subscriber, encode

HOST = os.getenv("SAM_VOICE_HUB_HOST", "127.0.0.1")
PORT = env_int("SAM_VOICE_HUB_PORT", 10210)      # рядом с портами синтеза 10202-10203
RECONNECT_S = env_float("SAM_VOICE_HUB_RECONNECT_S", 2.0)
TAKE_TIMEOUT_S = 1.0           # ждём кадр не дольше: закрытие подписчика замечается за секунду
RECV_BYTES = 65_536


class FramePublisher:
    """TCP-сервер узла: каждый подключившийся получает все кадры с момента подключения."""

    def __init__(self, host: str = HOST, port: int = PORT, fanout: Fanout | None = None) -> None:
        self.host, self.port = host, port
        self.fanout = fanout or Fanout()
        self._srv: socket.socket | None = None

    def publish(self, block: np.ndarray) -> None:
        if self.fanout.count:
            self.fanout.publish(encode(block, time.time()))

    def start(self) -> None:
        self._srv = socket.create_server((self.host, self.port))
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True, name="vmic-accept").start()
        print(f"[узел] раздаю звук микрофона на {self.host}:{self.port}", flush=True)

    def close(self) -> None:
        if self._srv:
            self._srv.close()

    def _accept(self) -> None:
        while True:
            try:
                conn, addr = self._srv.accept()
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sub = self.fanout.add()
            print(f"[узел] подписчик {addr[0]}:{addr[1]}, всего {self.fanout.count}", flush=True)
            threading.Thread(target=self._send, args=(conn, sub), daemon=True, name="vmic-send").start()

    def _send(self, conn: socket.socket, sub: Subscriber) -> None:
        try:
            while not sub.closed:
                frame = sub.take(TAKE_TIMEOUT_S)
                if frame is not None:
                    conn.sendall(frame)
        except OSError:
            pass
        finally:
            self.fanout.remove(sub)
            conn.close()
            print(f"[узел] подписчик ушёл, потерял кадров {sub.dropped}, осталось {self.fanout.count}", flush=True)


class VirtualMic:
    """Подписчик: блоки звука узла по мере прихода; узла нет - ждёт и переподключается."""

    def __init__(self, host: str = HOST, port: int = PORT, reconnect_s: float = RECONNECT_S) -> None:
        self.host, self.port, self.reconnect_s = host, port, reconnect_s

    def blocks(self) -> Iterator[np.ndarray]:
        while True:
            try:
                with socket.create_connection((self.host, self.port)) as sock:
                    print(f"[вход] слушаю узел {self.host}:{self.port}", flush=True)
                    decoder = Decoder()
                    while data := sock.recv(RECV_BYTES):
                        for _ts, block in decoder.feed(data):
                            yield block
            except OSError as exc:
                print(f"[вход] узла нет на {self.host}:{self.port} ({exc}); повтор через {self.reconnect_s:g} с",
                      flush=True)
            time.sleep(self.reconnect_s)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    p = argparse.ArgumentParser(description="подписчик узла звука для проверки")
    p.add_argument("--host", default=HOST)
    p.add_argument("--port", type=int, default=PORT)
    a = p.parse_args()
    last, peak, n = time.time(), 0.0, 0
    for block in VirtualMic(a.host, a.port).blocks():
        peak = max(peak, float(np.sqrt(np.mean(block.astype(np.float32) ** 2))))
        n += 1
        if time.time() - last >= 1:
            print(f"[вход] кадров {n}, громкость {peak:.0f}", flush=True)
            last, peak, n = time.time(), 0.0, 0


if __name__ == "__main__":
    main()
