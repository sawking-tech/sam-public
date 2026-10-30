"""
SAM Voice Client  •  rev 2026-04-11

Wake word : faster-whisper tiny  (локально, "Сэм"/"Сам")
STT       : faster-whisper       (bzikst/faster-whisper-large-v3-russian, локально)
              - дообучена на 200k+ русских сэмплов, WER 6.39% vs 9.84% у base v3
              - vad_filter=True (Silero VAD — убирает галлюцинации на тишине)
              - condition_on_prev_text=False (нет накопления галлюцинаций)
              Альтернативы: large-v3-turbo (быстрее), bzikst/...-int8 (меньше VRAM)
              Диаризация: когда появится HuggingFace токен — WhisperX + pyannote
TTS       : Wyoming Piper        :10200  (ru_RU-irina-medium)
Agent     : SAM Gateway          :8000

Запуск:
  python voice/voice_client.py
  python voice/voice_client.py --list-devices
  python voice/voice_client.py --device 1

Переменные окружения:
  SAM_SERVER=http://localhost:8000
  DEVICE_NAME=homepc
  VOICE_SESSION=voice
  WYOMING_PIPER=localhost:10200
  WHISPER_MODEL=bzikst/faster-whisper-large-v3-russian
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import re
import threading
import wave
import io
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import httpx
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
import sys

# Ключ SAM_API_KEY для запросов к gateway (SAM-79): модуль доступа общий с gateway, корень репозитория в пути
sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from gateway.access import auth_headers  # noqa: E402
from gateway.env import env_float, env_int  # noqa: E402
from gateway.stt_dedup import INPUT_HEADER, SPEECH_INPUT  # noqa: E402
AUTH_HEADERS = auth_headers()
try:
    import noisereduce as nr
    _NOISEREDUCE = True
except ImportError:
    _NOISEREDUCE = False

try:
    from pedalboard import Pedalboard, NoiseGate, Compressor, HighpassFilter, \
                           LowShelfFilter, HighShelfFilter, Limiter
    _PEDALBOARD = True
except ImportError:
    _PEDALBOARD = False

# ─── Логирование ──────────────────────────────────────────────────────────────

class _DedupeFilter(logging.Filter):
    """Подавляет одинаковые сообщения после MAX повторений подряд."""
    MAX = 5

    def __init__(self):
        super().__init__()
        self._last_msg  = None
        self._count     = 0

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if msg == self._last_msg:
            self._count += 1
            if self._count == self.MAX:
                record.msg = record.msg + "  (повторы скрыты...)"
                record.args = ()
                return True
            return self._count < self.MAX
        self._last_msg = msg
        self._count    = 1
        return True


class _CleanHandler(logging.StreamHandler):
    """Перед каждой строкой лога очищает текущую строку (спиннер/live waveform)."""
    _CLEAR = "\r" + " " * 80 + "\r"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.stream.write(self._CLEAR)
            self.stream.flush()
        except Exception:
            pass
        super().emit(record)


_handler = _CleanHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))

logging.root.setLevel(logging.INFO)
logging.root.addHandler(_handler)

log = logging.getLogger("sam.voice")
log.addFilter(_DedupeFilter())

# Заглушить спам от faster-whisper и Wyoming
logging.getLogger("faster_whisper").setLevel(logging.WARNING)
logging.getLogger("wyoming").setLevel(logging.WARNING)

# ─── Константы ────────────────────────────────────────────────────────────────

SAMPLE_RATE  = 16_000
CHANNELS     = 1
BLOCK_SIZE   = 1_600            # 100 мс

SILENCE_SECS    = env_float("VOICE_SILENCE_SECS", 1.5)
SILENCE_RMS     = env_int("VOICE_SILENCE_RMS", 80)  # int16 RMS порог (тише = чувствительнее)
MAX_PHRASE_SECS = 600.0  # аварийный предохранитель, реальная остановка — только по тишине

# Pre-trigger: запоминаем целую МИНУТУ до wake-word. Концепт SAM —
# пользователь может говорить долго и упомянуть «Сэм» в самом конце:
# «открой калькулятор и проверь сколько весит файл, Сэм». Вся минута
# речи попадёт в STT, а wake word срабатывает в момент произнесения.
# STT-dedup в gateway отфильтрует уже отправленные куски (через TF-IDF).
PRE_TRIGGER_SECS = env_float("VOICE_PRE_TRIGGER_SECS", 60.0)
RING_SECS        = PRE_TRIGGER_SECS + 10.0
VAD_TRIM_SECS    = 0.3

# Wake word detector: Whisper tiny, чанк 1.5с, overlap 50%
WAKE_CHUNK_SECS = 1.5
_WAKE_CHUNK_N   = int(WAKE_CHUNK_SECS * SAMPLE_RATE)   # 24 000
_WAKE_OVERLAP_N = _WAKE_CHUNK_N // 2                   # 12 000
WAKE_WORDS      = frozenset({"сэм", "сам", "sam"})

assert RING_SECS >= PRE_TRIGGER_SECS + WAKE_CHUNK_SECS * 1.5, \
    f"RING_SECS ({RING_SECS}) must be >= PRE_TRIGGER_SECS ({PRE_TRIGGER_SECS}) + wake buffer"
assert PRE_TRIGGER_SECS > WAKE_CHUNK_SECS

# ─── Auto device selection ────────────────────────────────────────────────────

_MIN_USABLE_RMS = 50   # ниже этого — устройство считается мёртвым/отключённым

def _probe_rms(device: Optional[int]) -> float:
    """Записывает 0.5с и возвращает RMS. -1.0 при ошибке."""
    try:
        buf = sd.rec(
            int(0.5 * SAMPLE_RATE),
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            device=device,
            blocking=True,
        )
        return float(np.sqrt(np.mean(buf.astype(np.float32) ** 2)))
    except Exception:
        return -1.0


def pick_best_input_device() -> Optional[int]:
    """
    1. Пробуем системный дефолт — если rms >= MIN_USABLE_RMS, используем его.
    2. Иначе сканируем WASAPI input-устройства, берём с наибольшим rms.
    3. Если ни одно не подошло — возвращаем None (системный дефолт всё равно).
    """
    # 1. Системный дефолт
    default_rms = _probe_rms(None)
    default_name = sd.query_devices(kind="input")["name"]
    log.info("[mic] Default device: %r  rms=%.0f", default_name, default_rms)
    if default_rms >= _MIN_USABLE_RMS:
        log.info("[mic] Using default device.")
        return None

    # 2. Сканируем WASAPI (чище MME/KS на Windows)
    log.info("[mic] Default too quiet, scanning WASAPI inputs...")
    devices  = sd.query_devices()
    hostapis = sd.query_hostapis()
    wasapi   = [
        i for i, d in enumerate(devices)
        if d["max_input_channels"] > 0
        and "WASAPI" in hostapis[d["hostapi"]]["name"]
    ]
    # fallback: все input-устройства
    if not wasapi:
        wasapi = [i for i, d in enumerate(devices) if d["max_input_channels"] > 0]

    best_idx, best_rms = None, _MIN_USABLE_RMS - 1
    for idx in wasapi:
        rms = _probe_rms(idx)
        log.info("  [%2d] rms=%5.0f  %s", idx, rms, devices[idx]["name"][:45])
        if rms > best_rms:
            best_rms, best_idx = rms, idx

    if best_idx is not None:
        log.info("[mic] Best: [%d] %s  rms=%.0f", best_idx, devices[best_idx]["name"], best_rms)
        return best_idx

    log.warning("[mic] No usable input found, falling back to default.")
    return None


# ─── Endpoints ────────────────────────────────────────────────────────────────

def _parse(env: str, default: str) -> tuple[str, int]:
    h, p = os.getenv(env, default).rsplit(":", 1)
    return h, int(p)

PIPER_HOST, PIPER_PORT = _parse("WYOMING_PIPER", "localhost:10200")

# Модель для STT. Загружается с HuggingFace при первом запуске (автоматически).
# bzikst/faster-whisper-large-v3-russian — дообучена на русском, WER 6.39%
# bzikst/faster-whisper-large-v3-russian-int8 — то же, но int8 (меньше VRAM, быстрее)
# large-v3-turbo — стандартная, быстрее, но больше галлюцинаций на русском
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "bzikst/faster-whisper-large-v3-russian")

SAM_URL    = os.getenv("SAM_SERVER",    "http://localhost:8000")
DEVICE_ID  = os.getenv("DEVICE_NAME",  "workpc")
SESSION_ID = os.getenv("VOICE_SESSION","voice")


# self_config — runtime настройки SAM (voice_speed/pitch/volume etc).
# Кешируем 5 сек чтобы не дёргать gateway на каждую фразу.
_SELF_CONFIG_CACHE = {"data": {}, "ts": 0.0}
_SELF_CONFIG_TTL = 5.0


async def _fetch_self_config() -> dict:
    import time
    now = time.time()
    if now - _SELF_CONFIG_CACHE["ts"] < _SELF_CONFIG_TTL and _SELF_CONFIG_CACHE["data"]:
        return _SELF_CONFIG_CACHE["data"]
    try:
        import aiohttp
        async with aiohttp.ClientSession(headers=AUTH_HEADERS) as session:
            async with session.get(f"{SAM_URL}/sam/config", timeout=aiohttp.ClientTimeout(total=2)) as r:
                if r.status == 200:
                    data = await r.json()
                    _SELF_CONFIG_CACHE["data"] = data
                    _SELF_CONFIG_CACHE["ts"] = now
                    return data
    except Exception as e:
        log.debug("[self_config] fetch failed: %s", e)
    return _SELF_CONFIG_CACHE["data"] or {}

# ─── State ────────────────────────────────────────────────────────────────────

_IDLE      = "idle"
_RECORDING = "recording"

# ─── Wyoming протокол ─────────────────────────────────────────────────────────

async def wy_send(
    writer: asyncio.StreamWriter,
    event_type: str,
    data: dict,
    payload: bytes = b"",
) -> None:
    header = json.dumps({
        "type":           event_type,
        "data":           data,
        "payload_length": len(payload),
    })
    writer.write(header.encode() + b"\n")
    if payload:
        writer.write(payload)
    await writer.drain()


async def wy_recv(
    reader: asyncio.StreamReader,
) -> tuple[str, dict, bytes]:
    line = await reader.readline()
    if not line:
        raise ConnectionError("Wyoming: connection closed")
    msg     = json.loads(line.decode())
    ev_type = msg.get("type", "")

    # Wyoming wire format: data идёт отдельными байтами после заголовка
    ev_data: dict = msg.get("data", {})
    data_len = msg.get("data_length", 0)
    if data_len > 0:
        data_bytes = await reader.readexactly(data_len)
        ev_data = json.loads(data_bytes.decode("utf-8"))

    plen    = msg.get("payload_length", 0)
    payload = await reader.readexactly(plen) if plen else b""
    return ev_type, ev_data, payload


# ─── Ring Buffer ──────────────────────────────────────────────────────────────

class RingBuffer:
    """Потокобезопасный кольцевой буфер int16."""

    def __init__(self, secs: float = RING_SECS) -> None:
        self._maxlen = int(secs * SAMPLE_RATE)
        self._buf: collections.deque[np.ndarray] = collections.deque()
        self._total  = 0
        self._lock   = threading.Lock()

    def push(self, chunk: np.ndarray) -> None:
        with self._lock:
            self._buf.append(chunk)
            self._total += len(chunk)
            while self._total > self._maxlen:
                self._total -= len(self._buf.popleft())

    def last(self, secs: float) -> np.ndarray:
        n = int(secs * SAMPLE_RATE)
        with self._lock:
            chunks = list(self._buf)
        if not chunks:
            return np.zeros(n, dtype=np.int16)
        data = np.concatenate(chunks)
        return data[-n:] if len(data) >= n else data

    def clear(self) -> None:
        """Очистить буфер — после отправки фразы, чтобы пре-триггер
        следующего wake не зацепил уже отправленный кусок."""
        with self._lock:
            self._buf.clear()
            self._total = 0


# ─── Audio visualisation ──────────────────────────────────────────────────────

def _waveform(audio: np.ndarray, width: int = 60) -> str:
    """ASCII waveform нормализованная к пику буфера."""
    BARS = "▁▂▃▄▅▆▇█"
    if len(audio) == 0:
        return "∅"
    step = max(1, len(audio) // width)
    rms_vals = []
    for i in range(0, len(audio) - step + 1, step):
        rms_vals.append(float(np.sqrt(np.mean(audio[i:i+step].astype(np.float32) ** 2))))
        if len(rms_vals) >= width:
            break
    peak = max(rms_vals) or 1.0
    out  = [BARS[min(int(v / peak * (len(BARS) - 1)), len(BARS) - 1)] for v in rms_vals]
    avg_rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    return f"{''.join(out)}  rms={avg_rms:.0f}"


# ─── VAD обрезка ──────────────────────────────────────────────────────────────

def find_speech_start(audio: np.ndarray) -> int:
    """
    Найти начало последнего непрерывного речевого блока.
    Скользим с конца окнами 100 мс, ищем паузу >= VAD_TRIM_SECS.

      [тишина][фраза А][пауза 2с][монолог 30с][«Сэм?»]
                                  ▲ возвращаем это
    """
    silence_n = int(VAD_TRIM_SECS * SAMPLE_RATE)
    window    = int(0.1 * SAMPLE_RATE)
    consec    = 0

    for i in range(len(audio) - window, 0, -window):
        rms = float(np.sqrt(
            np.mean(audio[i:i + window].astype(np.float32) ** 2)
        ))
        if rms < SILENCE_RMS:
            consec += window
            if consec >= silence_n:
                log.debug("[vad] trimmed %.1fs", i / SAMPLE_RATE)
                return i + window
        else:
            consec = 0
    return 0


DIARIZE_TOKEN = os.getenv("DIARIZE_TOKEN", "")  # HuggingFace token для pyannote


# ─── STT (faster-whisper, русская дообученная модель) ─────────────────────────

class WhisperSTT:
    """
    faster-whisper STT с антигаллюцинационными параметрами.
    Синхронный, вызывается только из ThreadPoolExecutor.

    Ключевые параметры против галлюцинаций:
      vad_filter=True            — Silero VAD: не транскрибирует тишину
      condition_on_prev_text=False — не накапливает галлюцинации между сегментами
      no_speech_threshold=0.6    — агрессивнее отсекает non-speech
      compression_ratio_threshold=2.0 — отсекает повторяющийся текст
    """

    def __init__(self, model_name: str = WHISPER_MODEL, device: str = "cuda") -> None:
        log.info("Loading STT model (%s)...", model_name)
        self._model = WhisperModel(
            model_name,
            device=device,
            compute_type="float16",
        )
        log.info("STT model ready.")

    def transcribe(self, audio: np.ndarray) -> str:
        """Вызывать только из executor. audio — int16."""
        audio_f32 = audio.astype(np.float32) / 32768.0
        segs, _ = self._model.transcribe(
            audio_f32,
            language="ru",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.0,
            temperature=0.0,
        )
        return " ".join(s.text for s in segs).strip()


# ─── STT с диаризацией (WhisperX + pyannote) — SAM-30 ────────────────────────

class WhisperXDiarizeSTT:
    """
    WhisperX + pyannote диаризация.
    Требует HuggingFace токен (DIARIZE_TOKEN в .env).
    Используется только при --diarize флаге.
    Реализация диаризации: см. SAM-30.
    """

    def __init__(self, model_name: str = WHISPER_MODEL, device: str = "cuda",
                 hf_token: str = "") -> None:
        try:
            import whisperx as _wx
        except ImportError:
            raise RuntimeError("whisperx not installed: pip install whisperx")
        if not hf_token:
            raise RuntimeError(
                "DIARIZE_TOKEN не задан. Получите токен на huggingface.co "
                "и примите лицензию pyannote/speaker-diarization-3.1. "
                "См. SAM-30."
            )
        log.info("Loading WhisperX model (%s) with diarization...", model_name)
        self._wx     = _wx
        self._model  = _wx.load_model(model_name, device=device,
                                      compute_type="float16", language="ru")
        self._token  = hf_token
        self._device = device
        log.info("WhisperX + diarization ready.")

    def transcribe(self, audio: np.ndarray) -> str:
        """Вызывать только из executor. audio — int16. Возвращает текст с speaker тегами."""
        audio_f32 = audio.astype(np.float32) / 32768.0
        result = self._model.transcribe(audio_f32, batch_size=16, language="ru")
        # TODO SAM-30: align + diarize + assign_word_speakers
        # Пока — просто текст без диаризации
        return " ".join(s["text"] for s in result.get("segments", [])).strip()


# ─── Wake Word Detector ───────────────────────────────────────────────────────

class WakeWordDetector:
    """
    faster-whisper tiny — детектор wake word для русского.
    Синхронный, вызывается только из ThreadPoolExecutor.
    """

    def __init__(self, device: str = "cuda") -> None:
        log.info("Loading wake word model (whisper-tiny)...")
        self._model = WhisperModel(
            "tiny", device=device, compute_type="float16"
        )
        log.info("Wake word model ready.")

    def detect(self, audio: np.ndarray) -> bool:
        """Вызывать только в executor. audio — int16."""
        if len(audio) < 800:
            return False
        # Конвертируем int16 → float32 для faster-whisper
        audio_f32 = audio.astype(np.float32) / 32768.0
        segs, _ = self._model.transcribe(
            audio_f32,
            language="ru",
            beam_size=1,
            best_of=1,
            temperature=0.0,
            no_speech_threshold=0.7,
            vad_filter=True,
        )
        text = " ".join(s.text for s in segs).lower()
        if text.strip():
            log.debug("[wake] %r", text.strip())
        return any(w in text for w in WAKE_WORDS)


# ─── Audio prep + STT ─────────────────────────────────────────────────────────

_TARGET_RMS = 3000  # нормализуем к этому уровню перед STT

# Точные фразы-галлюцинации Whisper — всегда вырезаются из транскрипта
_HALLUCINATION_EXACT = [
    "Продолжение следует...",
    "Субтитры сделал DimaTorzok",
    "Субтитры создавал DimaTorzok",
    "Подписывайтесь на канал",
    "Не забудьте поставить лайк",
    "Спасибо за просмотр",
    "Ставьте лайки",
    "Подписывайтесь",
    "Спасибо за внимание",
    "Смотрите также",
    "Продолжение в следующей части",
    "Всем пока!",
    "До свидания!",
    "functionCalling",
]

def _strip_hallucinations(text: str) -> str:
    for phrase in _HALLUCINATION_EXACT:
        text = text.replace(phrase, "")
    # Whisper иногда повторяет слова подряд: "открой, открой, пожалуйста".
    # Схлопываем подряд идущие одинаковые слова (case-insensitive, через
    # запятую/пробел). Не трогаем нормальные повторы вроде "да-да" или
    # "очень-очень" (через дефис).
    text = re.sub(
        r'\b(\w+)\b(?:[,\s]+\b\1\b)+',
        r'\1', text, flags=re.IGNORECASE,
    )
    return text.strip(" .,!?\n")

# Pedalboard broadcast chain (создаётся один раз)
_board: object = None

def _get_board():
    global _board
    if _board is None and _PEDALBOARD:
        _board = Pedalboard([
            HighpassFilter(cutoff_frequency_hz=80),           # убирает гул
            NoiseGate(threshold_db=-45, ratio=10,             # тишина → 0 (нет галлюцинаций)
                      attack_ms=5, release_ms=200),
            Compressor(threshold_db=-18, ratio=4.0,           # выравнивает динамику
                       attack_ms=10, release_ms=100),
            LowShelfFilter(cutoff_frequency_hz=250, gain_db=-3),   # убирает мутность
            HighShelfFilter(cutoff_frequency_hz=3000, gain_db=3),  # разборчивость
            Limiter(threshold_db=-1, release_ms=50),          # не клипает при крике
        ])
        log.info("[stt] pedalboard broadcast chain ready")
    return _board


def _speech_filter(audio: np.ndarray) -> np.ndarray:
    """Универсальное шумоподавление — stationary mode, работает для любого источника
    (микрофон, телефон, другой ПК). Убирает стационарный шум без noise sample."""
    if not _NOISEREDUCE:
        return audio
    f32 = audio.astype(np.float32) / 32767.0
    denoised = nr.reduce_noise(y=f32, sr=SAMPLE_RATE, stationary=True, prop_decrease=0.75)
    return np.clip(denoised * 32767.0, -32767, 32767).astype(np.int16)


_DEBUG_WAV_DIR = os.path.join(os.path.dirname(__file__), "..", "logs", "stt_debug")

def _save_debug_wav(audio: np.ndarray) -> None:
    """Сохранить последний STT-запрос в logs/stt_debug/ для диагностики."""
    import time
    os.makedirs(_DEBUG_WAV_DIR, exist_ok=True)
    path = os.path.join(_DEBUG_WAV_DIR, f"stt_{time.strftime('%H%M%S')}.wav")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    log.debug("[stt] saved debug wav: %s", path)


def _prepare_audio(audio: np.ndarray) -> np.ndarray:
    """Шумоподавление + gain-нормализация перед STT. Возвращает int16.

    Порядок: сначала фильтр (noise reduce может ослабить уровень),
    затем gain пересчитывается на чистый сигнал → точно даёт _TARGET_RMS.
    """
    if len(audio) < int(SAMPLE_RATE * 0.3):
        return audio
    raw_rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    audio = _speech_filter(audio)
    # rms ПОСЛЕ фильтра — иначе noise reduce ослабляет сигнал, а gain
    # рассчитан на исходный → результирующий уровень ниже target.
    clean_rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    gain = min(_TARGET_RMS / clean_rms, 100.0) if clean_rms > 1 else 1.0
    if gain > 1.1:
        log.info("[stt] gain x%.1f (raw_rms %d, clean_rms %d → %d)",
                 gain, int(raw_rms), int(clean_rms), int(clean_rms * gain))
        audio = np.clip(audio.astype(np.float32) * gain, -32767, 32767).astype(np.int16)
    _save_debug_wav(audio)
    return audio


# ─── Wyoming TTS ──────────────────────────────────────────────────────────────

class PiperTTS:
    """
    Wyoming Piper → pygame воспроизведение.

    SAM-34: внутренняя очередь + worker task — speak() не воспроизводит
    сразу, а ставит фразу в очередь. Один worker последовательно
    синтезирует и проигрывает. Это решает кейс когда два `<voice>`
    ответа приходят почти одновременно — они теперь не перебивают
    друг друга, а играются строго по очереди.

    busy — True если worker сейчас что-то синтезирует/проигрывает
    ИЛИ в очереди есть что играть. Используется sounddevice callback
    чтобы не писать в ring buffer пока TTS активен.
    """

    def __init__(self) -> None:
        self._speaking = threading.Event()
        self._current_text = ""  # текст текущей озвучки — для self-mention detection
        self._pygame   = None
        self._init_pygame()
        # SAM-34: очередь фраз + воркер
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._current_playing: str | None = None
        # для barge-in (SAM-29) — флаг отмены текущего воспроизведения
        self._cancel_current = asyncio.Event()

    def _init_pygame(self) -> None:
        try:
            import pygame
            pygame.mixer.init(frequency=22050, size=-16, channels=1)
            self._pygame = pygame
            log.info("pygame.mixer ready.")
        except Exception as exc:
            log.warning("pygame unavailable (%s), using ffplay.", exc)

    @property
    def busy(self) -> bool:
        """True если есть что играть сейчас или в очереди."""
        return self._speaking.is_set() or not self._queue.empty()

    def _ensure_worker(self) -> None:
        """Поднять воркер если он ещё не запущен (на первой speak())."""
        if self._worker_task is None or self._worker_task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # speak вызвали вне event loop, проиграем синхронно
            self._worker_task = loop.create_task(
                self._queue_worker(), name="piper-tts-worker"
            )

    async def _queue_worker(self) -> None:
        """Однотонный потребитель очереди — синтезирует и проигрывает по одной."""
        log.info("[piper-worker] started")
        try:
            while True:
                text = await self._queue.get()
                if text is None:  # sentinel для остановки
                    self._queue.task_done()
                    break
                self._current_playing = text
                self._cancel_current.clear()
                try:
                    await self._speak_one(text)
                except Exception as exc:
                    log.error("[piper-worker] speak failed: %s", exc)
                finally:
                    self._current_playing = None
                    self._queue.task_done()
        finally:
            log.info("[piper-worker] stopped")

    async def speak(self, text: str) -> None:
        """Поставить фразу в TTS-очередь. Возвращается сразу.

        Реальное воспроизведение делает worker, последовательно по
        одной фразе. Параллельные вызовы speak() не перебивают
        друг друга.
        """
        if not text:
            return
        self._ensure_worker()
        await self._queue.put(text)
        log.info("[piper] queued (q=%d): %r", self._queue.qsize(), text[:60])

    def clear_queue(self) -> int:
        """Сбросить накопленные фразы (для отмены / нового разговора).
        Текущая играющая фраза НЕ прерывается. Возвращает сколько удалили.
        """
        dropped = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        if dropped:
            log.info("[piper] cleared queue: dropped %d", dropped)
        return dropped

    def stop_current(self) -> None:
        """Прервать текущую играющую фразу (для barge-in, SAM-29).
        Worker подхватит флаг и перейдёт к следующей или встанет в await."""
        if self._pygame and self._pygame.mixer.music.get_busy():
            try:
                self._pygame.mixer.music.stop()
                log.info("[piper] stopped current playback (barge-in)")
            except Exception as exc:
                log.warning("[piper] stop_current failed: %s", exc)
        self._cancel_current.set()

    async def _speak_one(self, text: str) -> None:
        """Реальная синтезация + воспроизведение одной фразы."""
        # Запоминаем текущую фразу — wake_loop использует чтобы понять
        # если SAM сейчас сама произносит "Сэм" (не реагировать на эхо).
        self._current_text = text
        self._speaking.set()
        try:
            # Fallback: нормализуем оставшиеся голые цифры в слова.
            # LLM по промпту должна это делать сама, мы — на случай если
            # упустила (HH:MM, N мая, YYYY год).
            try:
                from .tts_normalize import normalize_numbers_for_tts
            except ImportError:
                from tts_normalize import normalize_numbers_for_tts
            normalized = normalize_numbers_for_tts(text)
            if normalized != text:
                log.info("[piper/normalize] %r → %r", text[:80], normalized[:80])
                text = normalized

            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(PIPER_HOST, PIPER_PORT),
                timeout=5.0,
            )
            await wy_send(writer, "synthesize", {"text": text})

            chunks: list[bytes] = []
            rate, width = 22050, 2
            while True:
                ev_type, ev_data, payload = await asyncio.wait_for(
                    wy_recv(reader), timeout=15.0
                )
                if ev_type == "audio-chunk":
                    chunks.append(payload)
                    rate  = ev_data.get("rate",  rate)
                    width = ev_data.get("width", width)
                elif ev_type in ("audio-stop", "error"):
                    break

            writer.close()
            await writer.wait_closed()

            if chunks and not self._cancel_current.is_set():
                await self._play(b"".join(chunks), rate, width)

        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            log.error("[piper] failed: %s", exc)
        finally:
            self._speaking.clear()

    async def _play(self, pcm: bytes, rate: int, width: int) -> None:
        """PCM → WAV в памяти → pygame.

        self_config:
        - tts_enabled=false → silent return
        - voice_speed → time-stretch без pitch shift (pedalboard Rubber Band)
        - voice_pitch → semitone shift через pedalboard.PitchShift
        - voice_volume → амплитуда PCM с clipping
        """
        cfg = await _fetch_self_config()
        if not cfg.get("tts_enabled", True):
            log.info("[piper] tts_enabled=false — skipping playback")
            return

        speed  = float(cfg.get("voice_speed",  1.0))
        pitch  = float(cfg.get("voice_pitch",  1.0))
        volume = float(cfg.get("voice_volume", 1.0))

        # Time-stretch (без pitch shift) + опциональный pitch shift
        if width == 2 and (abs(speed - 1.0) > 0.01 or abs(pitch - 1.0) > 0.01):
            try:
                import numpy as np
                samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

                if abs(speed - 1.0) > 0.01:
                    from pedalboard import time_stretch
                    # pedalboard ждёт (channels, samples)
                    audio = samples.reshape(1, -1)
                    stretched = time_stretch(
                        audio, samplerate=rate, stretch_factor=speed,
                    )
                    samples = stretched.reshape(-1)

                if abs(pitch - 1.0) > 0.01:
                    from pedalboard import Pedalboard, PitchShift
                    import math
                    # voice_pitch как ratio → semitones
                    semitones = 12.0 * math.log2(pitch)
                    board = Pedalboard([PitchShift(semitones=semitones)])
                    audio = samples.reshape(1, -1)
                    shifted = board(audio, sample_rate=rate)
                    samples = shifted.reshape(-1)

                samples = np.clip(samples * volume, -1.0, 1.0)
                pcm_arr = (samples * 32767.0).astype(np.int16)
                pcm = pcm_arr.tobytes()
            except Exception as e:
                log.warning("[piper] time-stretch failed (%s) — fallback raw", e)
        elif volume != 1.0 and width == 2:
            import numpy as np
            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            samples = np.clip(samples * volume, -32768, 32767).astype(np.int16)
            pcm = samples.tobytes()

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(width)
            wf.setframerate(rate)
            wf.writeframes(pcm)
        buf.seek(0)

        if self._pygame:
            self._pygame.mixer.music.load(buf)
            self._pygame.mixer.music.play()
            # SAM-34: проверяем флаг отмены каждые 50мс — это позволяет
            # barge-in (SAM-29) или clear_queue прервать воспроизведение
            while self._pygame.mixer.music.get_busy():
                if self._cancel_current.is_set():
                    self._pygame.mixer.music.stop()
                    break
                await asyncio.sleep(0.05)
        else:
            # ffplay fallback
            import tempfile, os as _os
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(buf.getvalue())
                tmp = f.name
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffplay", "-nodisp", "-autoexit", tmp,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            finally:
                _os.unlink(tmp)


# ─── SAM Client ───────────────────────────────────────────────────────────────

class SAMClient:

    def __init__(
        self,
        url:        str = SAM_URL,
        device_id:  str = DEVICE_ID,
        session_id: str = SESSION_ID,
    ) -> None:
        self._url        = url.rstrip("/")
        self._device_id  = device_id
        self._session_id = session_id

    async def voice_state(self, state: str, **kw) -> None:
        """Шлём voice-state event в gateway → monitor индикатор."""
        try:
            async with httpx.AsyncClient(headers=AUTH_HEADERS, timeout=2.0) as client:
                await client.post(
                    f"{self._url}/sam/voice/state",
                    json={"session": self._session_id, "state": state, **kw},
                )
        except Exception:
            pass  # не блокируем pipeline

    async def ask(self, text: str) -> str:
        async with httpx.AsyncClient(headers=AUTH_HEADERS, timeout=60.0) as client:
            resp = await client.post(
                f"{self._url}/v1/chat/completions",
                json={"messages": [{"role": "user", "content": text}]},
                headers={
                    "X-Device-ID":  self._device_id,
                    "X-Session-ID": self._session_id,
                    INPUT_HEADER:   SPEECH_INPUT,      # речь: шлюз вырежет эхо буфера микрофона (SAM-32)
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]

        m = re.search(r"<voice>(.*?)</voice>", content, re.DOTALL | re.IGNORECASE)
        result = m.group(1).strip() if m else content.strip()
        # Не зачитывать tool calls которые модель вывела как текст
        if re.match(r"^\s*[\(<\[{]?\s*(create_task|tool_call|function_call|<tool_call)", result, re.IGNORECASE):
            log.warning("[pipeline] Skipping tool call text: %r", result[:60])
            return ""
        return result


class WSReplyListener:
    """Подписка на /ws/monitor — ловим assistant_reply для нашей сессии.

    Зачем: ask() даёт ОДИН синхронный ответ (intro). А TASK_DONE
    follow-up (когда worker заканчивается) приходит асинхронно через
    event_bus → WS. Раньше второй voice терялся. Теперь — слушаем.

    Дедуп: ask() добавляет свой текст в `spoken_recently` до speak(),
    handler пропускает совпадения чтобы не дублировать.
    """

    def __init__(self, server_url: str, session_id: str,
                 tts_speak: Callable[[str], "asyncio.Future"]):
        # server_url=http(s)://host:port → ws(s)://host:port/ws/monitor
        url = server_url.replace("http://", "ws://").replace("https://", "wss://")
        self._ws_url = f"{url.rstrip('/')}/ws/monitor"
        self._session_id = session_id
        self._tts_speak = tts_speak
        self._spoken_recently: deque = deque(maxlen=20)
        self._seen_msg_ids: deque = deque(maxlen=200)
        self._task: Optional[asyncio.Task] = None

    def mark_spoken(self, text: str) -> None:
        """Помечаем text как уже озвученный (вызов из ask() перед speak)."""
        if text and text.strip():
            self._spoken_recently.append(text.strip())

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="ws-reply-listener")

    async def _loop(self) -> None:
        import websockets  # type: ignore
        backoff = 1.0
        while True:
            try:
                log.info("[ws] connecting %s", self._ws_url)
                async with websockets.connect(self._ws_url, ping_interval=20, additional_headers=AUTH_HEADERS) as ws:
                    log.info("[ws] connected")
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            ev = json.loads(raw)
                        except Exception:
                            continue
                        await self._on_event(ev)
            except Exception as e:
                log.warning("[ws] disconnected: %s — reconnect in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.8, 30.0)

    async def _on_event(self, ev: dict) -> None:
        if ev.get("type") != "assistant_reply":
            return
        # Только для нашей сессии
        if ev.get("session") != self._session_id:
            return
        voice = ev.get("voice")
        if not voice or not isinstance(voice, str):
            return
        # Дедуп по msg_id
        mid = ev.get("msg_id")
        if mid:
            if mid in self._seen_msg_ids:
                return
            self._seen_msg_ids.append(mid)
        # Дедуп по тексту — если ask() уже озвучивает то же самое
        v_norm = voice.strip()
        if v_norm in self._spoken_recently:
            log.debug("[ws] dedup: already spoken via ask(): %r", v_norm[:40])
            return
        log.info("[ws] follow-up voice (system=%s): %r",
                 ev.get("system"), v_norm[:60])
        self._spoken_recently.append(v_norm)
        try:
            await self._tts_speak(v_norm)
        except Exception as e:
            log.error("[ws] speak failed: %s", e)


# ─── Voice Pipeline ───────────────────────────────────────────────────────────

class VoicePipeline:

    def __init__(
        self,
        audio_device:   Optional[int] = None,
        whisper_device: str           = "cuda",
        session_id:     str           = SESSION_ID,
        server_url:     str           = SAM_URL,
        device_name:    str           = DEVICE_ID,
        diarize:        bool          = False,
    ) -> None:
        if audio_device is None:
            env_mic = os.getenv("MIC_DEVICE", "").strip()
            if env_mic:
                # MIC_DEVICE может быть индекс (число) или подстрока имени
                if env_mic.isdigit():
                    audio_device = int(env_mic)
                    log.info("[mic] Using MIC_DEVICE index=%d (env override)", audio_device)
                else:
                    for i, d in enumerate(sd.query_devices()):
                        if d["max_input_channels"] > 0 and env_mic.lower() in d["name"].lower():
                            audio_device = i
                            log.info("[mic] Using MIC_DEVICE match [%d] %s", i, d["name"])
                            break
                    if audio_device is None:
                        log.warning("[mic] MIC_DEVICE=%r не найден, fallback на auto", env_mic)
                        audio_device = pick_best_input_device()
            else:
                audio_device = pick_best_input_device()
        self.ring = RingBuffer()
        # Раньше был один executor max_workers=1 — wake и STT делили поток.
        # Пока STT обрабатывал длинное аудио (10-20s), wake-detect ждал в
        # очереди → SAM не реагировала на "Sam". Теперь два executor'а:
        # wake (tiny, быстро) живёт параллельно с STT (large, медленно).
        # GPU один, но CUDA сама сериализует kernel-launch'и — последовательно
        # внутри. Главное — Python-стороны не блокируем друг друга.
        self._wake_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="wake",
        )
        self._stt_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="stt",
        )
        # Backward-compat: _executor → STT (used in _process)
        self._executor = self._stt_executor
        self.wake = WakeWordDetector(device=whisper_device)
        if diarize:
            self.stt = WhisperXDiarizeSTT(device=whisper_device, hf_token=DIARIZE_TOKEN)
        else:
            self.stt = WhisperSTT(device=whisper_device)
        self.tts  = PiperTTS()
        self.sam  = SAMClient(
            url=server_url,
            device_id=device_name,
            session_id=session_id,
        )
        # SAM-09: WS-подписка — ловим TASK_DONE follow-up voice (которое
        # не возвращается через ask(), приходит как assistant_reply event).
        self.ws_listener = WSReplyListener(
            server_url=server_url,
            session_id=session_id,
            tts_speak=self.tts.speak,
        )
        self._audio_device   = audio_device
        self._state          = _IDLE
        self._open_mic_until = 0.0   # timestamp до которого работает open mic после TTS
        self._audio_q: asyncio.Queue[np.ndarray]

    # ── sounddevice callback ───────────────────────────────────────────────

    def _make_callback(self, loop: asyncio.AbstractEventLoop):
        def _cb(indata, frames, time_info, status):
            if status:
                log.debug("sd: %s", status)
            chunk = indata[:, 0].copy()
            # int16 для Wyoming и ring buffer
            chunk_i16 = (chunk * 32767).astype(np.int16) \
                if chunk.dtype == np.float32 else chunk.astype(np.int16)
            if not self.tts.busy:
                self.ring.push(chunk_i16)
            try:
                loop.call_soon_threadsafe(self._audio_q.put_nowait, chunk_i16)
            except Exception:
                pass
        return _cb

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _strip_wake(text: str) -> str:
        low = text.lower()
        for ww in sorted(WAKE_WORDS, key=len, reverse=True):
            if low.startswith(ww):
                return text[len(ww):].lstrip(" ,.:!").strip()
        return text

    def _drain_queue(self) -> None:
        while not self._audio_q.empty():
            try:
                self._audio_q.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _enter_idle(self) -> None:
        self._drain_queue()
        self._state = _IDLE
        log.info("[mic] Listening... say 'Sam'")

    # ── pipeline ──────────────────────────────────────────────────────────

    # ── Cancel commands (voice-side short-circuit) ───────────────────────────
    _CANCEL_TURN_PATTERNS = (
        "отмена", "отмени", "стоп", "остановись", "хватит",
        "молчи", "тише", "замолчи",
    )
    _CANCEL_TASK_PATTERNS = (
        "отмена задачи", "отмени задачу", "стоп задача", "стоп задачу",
        "отмени таску", "отмена таски", "отмени все таски", "отмени все задачи",
    )

    async def _handle_cancel_command(self, text: str) -> bool:
        """Если text — команда отмены, выполняем напрямую (без LLM).
        Возвращает True если обработали (не отправлять в /chat)."""
        t = text.lower().strip().rstrip(".!? ")
        # Длинная команда «отмена задачи» — проверяем первой
        if any(t == p or t.startswith(p + " ") for p in self._CANCEL_TASK_PATTERNS):
            log.info("[voice] cancel-task: %r", text)
            try:
                import aiohttp
                async with aiohttp.ClientSession(headers=AUTH_HEADERS) as s:
                    async with s.get(f"{SAM_URL}/sam/tasks", timeout=aiohttp.ClientTimeout(total=3)) as r:
                        if r.status == 200:
                            tasks = await r.json()
                            running = [t_ for t_ in (tasks if isinstance(tasks, list) else [])
                                       if t_.get("status") in ("running", "pending")]
                            for t_ in running:
                                tid = t_.get("id") or t_.get("_id") or ""
                                if tid:
                                    await s.post(f"{SAM_URL}/sam/tasks/{tid[:8]}/cancel", json={},
                                                 timeout=aiohttp.ClientTimeout(total=3))
                            log.info("[voice] cancelled %d running tasks", len(running))
                            return True
            except Exception as e:
                log.warning("[voice] cancel-task failed: %s", e)
            return True

        if any(t == p or t.startswith(p + " ") for p in self._CANCEL_TURN_PATTERNS):
            log.info("[voice] cancel-turn: %r", text)
            try:
                import aiohttp
                async with aiohttp.ClientSession(headers=AUTH_HEADERS) as s:
                    await s.post(
                        f"{SAM_URL}/sam/session/{self.sam._session_id}/cancel-turn", json={},
                        timeout=aiohttp.ClientTimeout(total=3),
                    )
                # Также прервать TTS если играет
                if self.tts.busy:
                    self.tts.stop_current()
            except Exception as e:
                log.warning("[voice] cancel-turn failed: %s", e)
            return True

        return False

    async def _process(self, audio: np.ndarray) -> None:
        if len(audio) < int(SAMPLE_RATE * 0.3):
            return
        audio = _prepare_audio(audio)
        loop  = asyncio.get_running_loop()
        text  = await loop.run_in_executor(self._executor, self.stt.transcribe, audio)
        log.info("[stt] %r  (%.1fs audio)", text, len(audio) / SAMPLE_RATE)
        text = self._strip_wake(text)

        if not text:
            log.warning("[pipeline] Whisper returned empty (rms too low or no speech). Skipping.")
            return

        text = _strip_hallucinations(text)
        if not text:
            log.warning("[pipeline] Only hallucinations, skipping.")
            return

        # STT distilled — шлём preview в монитор сразу, до отправки в SAM.
        # Иначе пользователь не понимает услышала ли система его.
        asyncio.create_task(self.sam.voice_state("stt_done", text=text))

        # Voice-side interrupt patterns: прерываем текущий turn / таску
        # БЕЗ отправки в LLM (быстрая реакция, не ждём embedder/RAG).
        if await self._handle_cancel_command(text):
            return

        log.debug("[pipeline] → SAM: %r", text)
        try:
            reply = await self.sam.ask(text)
        except Exception as exc:
            log.error("[sam] %s", exc)
            reply = "Cannot connect to server."

        log.info("[pipeline] ← SAM: %r", reply)
        # Помечаем как спикнутое ДО tts.speak — иначе WS-handler параллельно
        # увидит ту же фразу в assistant_reply event и попытается продублировать.
        self.ws_listener.mark_spoken(reply)
        await self.tts.speak(reply)
        # После ответа открываем mic на 8 сек — можно говорить без "Сэм"
        import time as _time
        self._open_mic_until = _time.monotonic() + 8.0
        log.info("[pipeline] open mic 8s")

    # ── spinner ───────────────────────────────────────────────────────────

    async def _spinner_loop(self) -> None:
        frames = ["·", "−", "+", "✦", "+", "−"]
        i = 0
        while True:
            if self.tts.busy:
                print(f"\r  ♪  speaking...{' ' * 60}", end="", flush=True)
            elif self._state == _RECORDING:
                wave = getattr(self, "_live_wave", "")
                print(f"\r  ●  {wave}", end="", flush=True)
            else:
                print(f"\r  {frames[i % len(frames)]}  listening...{' ' * 40}", end="", flush=True)
                i += 1
            await asyncio.sleep(0.25)

    # ── wake word loop (отдельная Task) ───────────────────────────────────

    async def _wake_loop(self) -> None:
        loop = asyncio.get_running_loop()
        wake_chunks: collections.deque[np.ndarray] = collections.deque()
        wake_total  = 0

        while True:
            # Barge-in: пока TTS играет — слушаем только wake-word.
            # Услышали "Сэм" → останавливаем TTS и переходим в recording.
            if self.tts.busy:
                try:
                    chunk = await asyncio.wait_for(
                        self._audio_q.get(), timeout=0.2,
                    )
                except asyncio.TimeoutError:
                    continue
                wake_chunks.append(chunk)
                wake_total += len(chunk)
                if wake_total >= _WAKE_CHUNK_N:
                    audio = np.concatenate(list(wake_chunks))
                    detect_audio = audio[:_WAKE_CHUNK_N]
                    leftover = audio[_WAKE_CHUNK_N - _WAKE_OVERLAP_N:]
                    wake_chunks.clear()
                    wake_chunks.append(leftover)
                    wake_total = len(leftover)
                    detected = await loop.run_in_executor(
                        self._wake_executor, self.wake.detect, detect_audio,
                    )
                    if detected:
                        # Self-mention guard: если SAM сейчас сама произносит
                        # "Сэм" (история про себя в 3-м лице) — это эхо,
                        # не настоящий barge-in. Игнорируем.
                        current = (getattr(self.tts, "_current_text", "") or "").lower()
                        is_self_mention = any(
                            w in current for w in WAKE_WORDS
                        )
                        if is_self_mention:
                            log.debug(
                                "[wake] self-mention detected in TTS — ignoring "
                                "(current playback contains wake word)",
                            )
                            continue
                        log.info("[wake] BARGE-IN — stopping TTS")
                        self.tts.stop_current()
                        self.tts.clear_queue()
                        self._state = _RECORDING
                continue

            if self._state != _IDLE:
                # Дренируем очередь чтобы она не переполнялась эхом TTS.
                # НЕ обрабатываем — просто выбрасываем.
                try:
                    self._audio_q.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.02)
                wake_chunks.clear()
                wake_total = 0
                continue

            try:
                chunk = await asyncio.wait_for(
                    self._audio_q.get(), timeout=0.2
                )
            except asyncio.TimeoutError:
                continue

            # Open mic: после TTS любой звук выше порога = начало записи
            import time as _time
            if _time.monotonic() < self._open_mic_until:
                rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
                if rms > SILENCE_RMS:
                    log.info("[wake] Open mic triggered (rms=%.0f)", rms)
                    self._state = _RECORDING
                    wake_chunks.clear()
                    wake_total = 0
                continue

            wake_chunks.append(chunk)
            wake_total += len(chunk)

            if wake_total >= _WAKE_CHUNK_N:
                audio        = np.concatenate(list(wake_chunks))
                detect_audio = audio[:_WAKE_CHUNK_N]

                leftover = audio[_WAKE_CHUNK_N - _WAKE_OVERLAP_N:]
                wake_chunks.clear()
                wake_chunks.append(leftover)
                wake_total = len(leftover)

                detected = await loop.run_in_executor(
                    self._wake_executor, self.wake.detect, detect_audio,
                )
                if detected:
                    log.info("[wake] Detected! Recording...")
                    self._state = _RECORDING
                    asyncio.create_task(self.sam.voice_state("wake_detected"))

    # ── main loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._audio_q = asyncio.Queue(
            maxsize=int(20 * SAMPLE_RATE / BLOCK_SIZE)
        )

        silence_limit_n = int(SILENCE_SECS * SAMPLE_RATE)
        max_phrase_n    = int(MAX_PHRASE_SECS * SAMPLE_RATE)

        log.info("STT     : faster-whisper %s (local)", WHISPER_MODEL)
        log.info("Piper   : %s:%d", PIPER_HOST, PIPER_PORT)
        log.info("SAM     : %s  device=%s  session=%s",
                 self.sam._url, self.sam._device_id, self.sam._session_id)

        with sd.InputStream(
            samplerate = SAMPLE_RATE,
            channels   = CHANNELS,
            dtype      = "int16",
            blocksize  = BLOCK_SIZE,
            device     = self._audio_device,
            callback   = self._make_callback(loop),
        ):
            log.info("[mic] Active.")
            wake_task    = asyncio.create_task(self._wake_loop())
            spinner_task = asyncio.create_task(self._spinner_loop())
            self.ws_listener.start()

            try:
                while True:
                    if self._state == _IDLE:
                        await asyncio.sleep(0.05)
                        continue

                    # ── RECORDING ─────────────────────────────────────────
                    pre = self.ring.last(PRE_TRIGGER_SECS)
                    phrase_chunks: collections.deque[np.ndarray] = collections.deque()
                    phrase_chunks.append(pre)
                    phrase_total = len(pre)
                    post_n    = 0   # only post-wake audio for limit check
                    silence_n = 0

                    log.info("[rec] Start (pre=%.1fs)  %s", len(pre) / SAMPLE_RATE, _waveform(pre))

                    while self._state == _RECORDING:
                        try:
                            chunk = await asyncio.wait_for(
                                self._audio_q.get(), timeout=0.5
                            )
                        except asyncio.TimeoutError:
                            continue

                        phrase_chunks.append(chunk)
                        phrase_total += len(chunk)
                        post_n       += len(chunk)

                        rms       = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
                        silence_n = silence_n + len(chunk) if rms < SILENCE_RMS else 0

                        # обновляем live waveform для спиннера
                        live = np.concatenate(list(phrase_chunks))
                        self._live_wave = f"+{post_n/SAMPLE_RATE:.1f}s {_waveform(live, width=50)}"

                        if silence_n >= silence_limit_n or post_n >= max_phrase_n:
                            reason = "silence" if silence_n >= silence_limit_n else "limit"
                            log.info("[rec] End (%s, %.1fs post-wake).",
                                     reason, post_n / SAMPLE_RATE)
                            break

                    audio = np.concatenate(list(phrase_chunks))
                    log.info("[rec] Total %.1fs  %s", len(audio) / SAMPLE_RATE, _waveform(audio))
                    asyncio.create_task(self.sam.voice_state(
                        "rec_end", rms=int(np.sqrt(np.mean(audio.astype(np.float32) ** 2))),
                    ))
                    self._enter_idle()
                    await self._process(audio)
                    self._enter_idle()   # дренируем эхо TTS
                    # Очистим ring buffer — следующий wake не должен зацепить
                    # уже отправленные ms (пользователь только что говорил,
                    # это уже ушло в SAM). Защита от дублей в STT pipeline.
                    self.ring.clear()

            finally:
                wake_task.cancel()
                spinner_task.cancel()
                await asyncio.gather(wake_task, spinner_task, return_exceptions=True)
                print()  # newline after spinner
                self._wake_executor.shutdown(wait=False)
                self._stt_executor.shutdown(wait=False)


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main() -> None:
    global PIPER_HOST, PIPER_PORT
    ap = argparse.ArgumentParser(description="SAM Voice Client")
    ap.add_argument("--device",       type=int,  default=None)
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--server",       default=SAM_URL)
    ap.add_argument("--device-name",  default=DEVICE_ID)
    ap.add_argument("--session",      default=SESSION_ID)
    ap.add_argument("--piper",        default=f"{PIPER_HOST}:{PIPER_PORT}")
    ap.add_argument("--cpu",          action="store_true")
    ap.add_argument("--diarize",      action="store_true",
                    help="WhisperX + pyannote диаризация (нужен DIARIZE_TOKEN в .env)")
    ap.add_argument("--debug",        action="store_true")
    args = ap.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_devices:
        print(sd.query_devices())
        return

    PIPER_HOST, PIPER_PORT = args.piper.rsplit(":", 1)[0], int(args.piper.rsplit(":", 1)[1])

    pipeline = VoicePipeline(
        audio_device   = args.device,
        whisper_device = "cpu" if args.cpu else "cuda",
        session_id     = args.session,
        server_url     = args.server,
        device_name    = args.device_name,
        diarize        = args.diarize,
    )

    try:
        asyncio.run(pipeline.run())
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()