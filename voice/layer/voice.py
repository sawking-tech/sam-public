"""Звук одним процессом: слух (voice_in) и озвучка (say_out) вместе.


  python -m voice.layer.voice [папка-шины] [--device N] [--no-aec]

Почему один процесс: два процесса со своими аудиопотоками мешали друг другу (запись и
воспроизведение дрались за поток), а эхоподавлению нужен в одном месте и сигнал колонок,
и сигнал микрофона. Разовые звуки (пробы, тесты) - только через say.py, не отдельным скриптом.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from . import health, recorder
from .audio_core import RATE, AudioCore
from .bus import bus_dir
from .hearing import STT, Listener, pick_device
from .say_out import serve
from .vmic import FramePublisher
from gateway import stand_watch_keeper
from gateway.env import env_bool

DEBUG_AEC = env_bool("SAM_VOICE_DEBUG_AEC", False)  # печатать замер эха, пока я говорю


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    p = argparse.ArgumentParser()
    p.add_argument("session", nargs="?", help="папка шины; по умолчанию SAM_VOICE_BUS_DIR")
    p.add_argument("--device", type=int)
    p.add_argument("--no-aec", action="store_true")
    a = p.parse_args()
    d = bus_dir(a.session)
    health.clear(d / health.VOICE_HEALTH)   # пока грузится модель, надзор судит по процессу, не по старому файлу
    stt = STT()  # модели грузим до открытия звука: 20 с без потоков, без накопления очереди
    core = AudioCore(device_in=pick_device(a.device), aec=not a.no_aec,
                     pick_in=lambda: pick_device(a.device))
    hub = FramePublisher()
    hub.start()
    core.mic_tap = hub.publish   # узел звука: чистый звук микрофона - подписчикам (ADR-017)
    core.start()
    core.watch(d)  # смена системного выхода, флаг calibrate и сторож микрофона
    rec = recorder.OutputRecorder(d / recorder.SPEAKER_WAV, RATE) if recorder.enabled(d) else None
    core.tap = rec.feed if rec else None
    threading.Thread(target=serve, args=(d, core, rec), daemon=True, name="say").start()

    def report() -> None:  # обрывы звука и громкость микрофона во время моей речи - в лог
        seen = (0, 0)
        speaking = d / "speaking"
        while True:
            time.sleep(1)
            if speaking.exists() and DEBUG_AEC:  # замер эха - только для отладки, мой текст печатает [say]
                print(f"[звук] я говорю: микрофон до AEC {core.raw_peak:.0f}, после {core.clean_peak:.0f}",
                      flush=True)
            core.raw_peak = core.clean_peak = 0.0
            now = (core.underflows, core.overflows)
            if now != seen:
                print(f"[звук] обрывы колонок {now[0]}, переполнения микрофона {now[1]}", flush=True)
                seen = now
    threading.Thread(target=report, daemon=True, name="report").start()
    health.start_voice_beat(d)   # health check слуха для надзора: файл voice.health в шине (SAM-195)
    stand_watch_keeper.start("voice")   # надзор лежит - поднять (SAM-195, холодный старт)
    try:
        Listener(d, stt, None, core).run()
    finally:
        core.close()


if __name__ == "__main__":
    main()
