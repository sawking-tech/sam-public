"""Silero v5 как Wyoming-сервер: голос SAM по умолчанию, работает на процессоре без видеокарты.

  python -m voice.layer.tts_silero_server [--port 10202] [--model v5_5_ru]

Почему Silero: синтез короткой реплики 0,4-0,6 с на CPU, ударения и ё ставит сам. Голоса - пресеты из
реестра голосов (voice_registry, раздел silero, speaker): eugene
(мужской, по умолчанию) и xenia («Ксения два»). Реестр перечитывается при правке, без перезапуска. Голос из
запроса, которого нет на silero, заменяется голосом по умолчанию: say_out шлёт и имена голосов Piper.
Числа Silero не читает - их переводит в слова say_out (tts_normalize) до запроса. Ударения ставит
stress.Stresser: «+» перед ударной гласной в каждом слове, ручная разметка главнее.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .stress import Stresser
from .voice_registry import Live
from .wyoming_server import serve
from gateway.env import env_int

NAME = "silero"
LABEL = f"[синтез] {NAME}:"
RATE = 48000
# Модель кэшируется вне диска C: он на стенде заполнен (8 окт C забивался до нуля и ронял синтез).
TORCH_HOME = os.getenv("SILERO_TORCH_HOME", r"E:\tts-lab-silero\hub")
CPU_THREADS = env_int("SILERO_THREADS", 4)
PORT = env_int("SILERO_PORT", 10202)   # тот же порт ждёт tts_client (WYOMING_SILERO)
WARMUP = "Проверка связи."


def load(model_id: str, speakers: list[str]):
    os.environ["TORCH_HOME"] = TORCH_HOME
    Path(TORCH_HOME).mkdir(parents=True, exist_ok=True)
    import torch
    torch.set_num_threads(CPU_THREADS)
    model, _ = torch.hub.load("snakers4/silero-models", "silero_tts", language="ru", speaker=model_id,
                              trust_repo=True)
    for v in speakers:  # первый синтез каждым голосом медленный: прогреть до первой реплики
        model.apply_tts(text=WARMUP, speaker=v, sample_rate=RATE)
    return model


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--model", default="v5_5_ru")
    a = p.parse_args()
    live = Live(label=LABEL)
    registry = live.get()
    names = registry.names(NAME)
    model = load(a.model, sorted({registry.choose(n, NAME).speaker for n in names}))
    stress = Stresser()

    def synth(text: str, voice: str | None):
        speaker = live.get().choose(voice, NAME).speaker
        # Разметка уходит только в движок: в логах и контексте обычные слова
        audio = model.apply_tts(text=stress(text), speaker=speaker, sample_rate=RATE, put_accent=True, put_yo=True)
        return audio.numpy(), RATE

    serve(NAME, a.port, synth, names)


if __name__ == "__main__":
    main()
