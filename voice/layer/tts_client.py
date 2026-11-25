"""Клиент синтеза речи: текст → поток кусков звука от Wyoming-сервера выбранного движка.


  stream(text, voice)   куски (int16 моно, частота) по мере прихода от сервера: звук можно играть
                        с первого куска, не дожидаясь конца фразы (CosyVoice синтезирует потоком)
  synthesize(text, ...) вся фраза одним буфером, с темпом: для заготовок (agenda, speakers, отклики)

Движок - по голосу из реестра data/voice/voices/registry.json (voice_registry): "tts" в session.json (или SAM_TTS),
если голос на нём звучит, иначе движок самого голоса; голоса нет в реестре - просто "tts". Все по Wyoming:
silero - tts_silero_server.py на процессоре, голос по умолчанию xenia; cosy -
tts_cosy_server.py на GPU, голоса - клоны по образцам из реестра. Запасной - silero голосом xenia: на него уходим,
если выбранный сервер не отвечает до первого куска.
Числа, знаки и пути переводятся в слова здесь (tts_normalize.normalize_for_tts), до запроса: движки их не читают. Здесь же словарь произношения
(lexicon, voice/layer/lexicon.txt): «стек» -> «стэк» для всех движков, сервер Silero ставит ударения уже по замене.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import BinaryIO, Iterator

import numpy as np

from .audio_stream import Pipeline, Tempo, to_int16
from .bus import SESSION_JSON, read_json, bus_dir
from .lexicon import Live as LiveLexicon
from ..tts_normalize import normalize_for_tts
from .voice_registry import Live, Registry
from gateway.env import env_float, env_int

# Адрес - 127.0.0.1, а не localhost: на Windows localhost сначала пробует IPv6 ::1, серверы слушают только
# IPv4, и каждое соединение ждало 2,05 с (замер 8 окт 23:49: каждая реплика теряла 2 с до первого звука)
HOST = os.getenv("SAM_TTS_HOST", "127.0.0.1")
TTS_BACKENDS = {"silero": os.getenv("WYOMING_SILERO", f"{HOST}:10202"),
                "cosy": os.getenv("WYOMING_COSY", f"{HOST}:10203")}
DEFAULT_TTS = "silero"
FALLBACK_TTS = os.getenv("SAM_TTS_FALLBACK", "silero")  # основной не ответил - говорим этим
FALLBACK_VOICE = os.getenv("SAM_TTS_FALLBACK_VOICE", "xenia")  # Ксюша (Silero)
DEFAULT_VOICE = os.getenv("SAM_VOICE", "xenia")         # голос реестра; xenia - Ксюша
SPEED = env_float("SAM_VOICE_SPEED", 0.95)              # на слух 1,15 и 1,07 слишком быстро
CONNECT_TIMEOUT = 5.0
REPLY_TIMEOUT = 30.0   # дольше сервер молчать между кусками не может: значит, завис
DEFAULT_RATE = 22050   # если сервер не назвал частоту в audio-start

Chunk = tuple[np.ndarray, int]


class TtsUnavailable(OSError):
    """Сервер движка не принял соединение или не отдал ни одного куска."""


# Длина отрезка синтеза - свойство движка: Silero отдаёт фразу целиком, поэтому короткие
# отрезки дают ранний звук (замер: 33 симв. - 0,22 с, 191 - 1,22 с); CosyVoice потоковый, и каждый новый
# запрос заново платит ~1 с за первый кусок - ему абзац целиком. SAM_TTS_SEGMENT_CHARS_<ДВИЖОК> переопределяет.
SEGMENT_CHARS = {"silero": 80, "cosy": 100_000}
SEGMENT_CHARS_DEFAULT = 120
REGISTRY = Live(label="[речь]")  # перечитывается при правке: голос меняется без перезапуска
LEXICON = LiveLexicon()  # словарь произношения, тоже перечитывается при правке


def segment_chars(backend: str | None = None) -> int:
    """Минимальная длина отрезка синтеза для движка (по умолчанию - выбранного в session.json)."""
    name = backend or tts_backend()
    return env_int(f"SAM_TTS_SEGMENT_CHARS_{name.upper()}", SEGMENT_CHARS.get(name, SEGMENT_CHARS_DEFAULT))


def pick_backend(session: dict, voice: str | None, registry: Registry, env_tts: str | None) -> str:
    """Движок реплики: выбранный в session.json (tts или SAM_TTS), если голос на нём звучит, иначе движок голоса.
    Смена голоса Кэм/Ксюша правкой одного поля voice переключает и движок (план voice-registry)."""
    requested = session.get("tts") or env_tts
    name = registry.engine_for(voice or session.get("voice"), requested) or requested or DEFAULT_TTS
    return name if name in TTS_BACKENDS else DEFAULT_TTS


def _session() -> dict:
    try:
        return read_json(bus_dir() / SESSION_JSON)
    except Exception:
        return {}


def tts_backend(voice: str | None = None) -> str:
    """Движок для голоса voice (по умолчанию - голоса из session.json)."""
    return pick_backend(_session(), voice, REGISTRY.get(), os.getenv("SAM_TTS"))


def _send(sock: socket.socket, event: str, data: dict) -> None:
    sock.sendall(json.dumps({"type": event, "data": data, "payload_length": 0}).encode() + b"\n")


def _recv(f: BinaryIO) -> tuple[str, dict, bytes]:
    line = f.readline()
    if not line:
        raise ConnectionError("Wyoming: сервер закрыл соединение")
    msg = json.loads(line.decode())
    data: dict = msg.get("data", {})
    if msg.get("data_length", 0) > 0:
        data = json.loads(f.read(msg["data_length"]).decode("utf-8"))
    plen = msg.get("payload_length", 0)
    return msg.get("type", ""), data, f.read(plen) if plen else b""


def _stream_at(address: str, text: str, voice: str) -> Iterator[Chunk]:
    """Куски одной фразы от сервера по адресу. Закрытие генератора закрывает соединение:
    сервер дорабатывает фразу у себя под замком и следующую не задерживает."""
    host, port = address.rsplit(":", 1)
    try:
        sock = socket.create_connection((host, int(port)), timeout=CONNECT_TIMEOUT)
    except OSError as exc:
        raise TtsUnavailable(f"{address}: {exc}") from exc
    with sock, sock.makefile("rb") as f:
        sock.settimeout(REPLY_TIMEOUT)
        _send(sock, "synthesize", {"text": text, "voice": {"name": voice}})
        rate = DEFAULT_RATE
        while True:
            event, data, payload = _recv(f)
            if event == "audio-chunk":
                rate = data.get("rate", rate)
                yield np.frombuffer(payload, dtype=np.int16), rate
            elif event == "error":
                raise ConnectionError(f"{address}: {data.get('text', 'ошибка синтеза')}")
            elif event == "audio-stop":
                return


def stream(text: str, voice: str = DEFAULT_VOICE) -> Iterator[Chunk]:
    """Куски фразы выбранным движком; не ответил до первого куска - запасным голосом, а не молчанием."""
    backend = tts_backend(voice)
    text = normalize_for_tts(LEXICON.get()(text))
    chunks = _stream_at(TTS_BACKENDS[backend], text, voice)
    try:
        first = next(chunks, None)
    except (TtsUnavailable, ConnectionError, TimeoutError) as exc:
        if backend == FALLBACK_TTS:
            raise
        print(f"[речь] {backend} не отвечает ({exc}), говорю через {FALLBACK_TTS}", file=sys.stderr, flush=True)
        yield from _stream_at(TTS_BACKENDS[FALLBACK_TTS], text, FALLBACK_VOICE)
        return
    if first is not None:
        yield first
        yield from chunks


def session_voice(d=None) -> tuple[str, float]:
    """Голос и темп из session.json - один источник для проигрывателя и заготовок очереди.
    Без него заготовки звучали голосом по умолчанию, вторым голосом рядом с основным."""
    try:
        s = read_json((d or bus_dir()) / SESSION_JSON)
    except Exception:
        s = {}
    return s.get("voice") or DEFAULT_VOICE, float(s.get("speed") or SPEED)


def synthesize(text: str, voice: str = DEFAULT_VOICE, speed: float = SPEED) -> tuple[np.ndarray, int]:
    """Вся фраза одним буфером int16 в частоте движка, с темпом speed."""
    parts, rate, pipe = [], DEFAULT_RATE, None
    for audio, rate in stream(text, voice):
        pipe = pipe or Pipeline([Tempo(rate, speed)])
        parts.append(pipe.push(audio))
    if pipe:
        parts.append(pipe.flush())
    return to_int16(np.concatenate(parts)) if parts else np.zeros(0, np.int16), rate
