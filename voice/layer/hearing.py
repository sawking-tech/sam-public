"""Голос на вход: микрофон → фраза по паузе → Whisper → voice-in.jsonl.


  python -m voice.layer.hearing [папка-шины] [--device N] [--no-aec]
  python -m voice.layer.hearing --list-devices
  python -m voice.layer.hearing --file phrase.wav  распознать файл и выйти (проверка)

Запись voice-in.jsonl: {"ts": ..., "text": "...", "secs": 3.2, "rms": 2100, "echo": false}
  echo=true: фраза похожа на мою реплику из say-out.jsonl за последние ECHO_WINDOW
  секунд (синтез из колонок попал в микрофон). ticker.py такие не пересылает.

Пока лежит флаг mute, звук отбрасывается.
Пока лежит speaking (идёт озвучка), микрофон слушает только перебивание: громче BARGE_RMS
0,3 с подряд (голос человека вблизи громче колонок) или стоп-слово в окне Whisper tiny →
флаг barge, озвучка обрывается, фраза пишется дальше целиком, начало берётся из кольца.
Во время фразы человека лежит флаг hearing: озвучка ждёт её конца.
Длинная фраза распознаётся кусками по ходу речи (срез на паузе от CUT_QUIET), к концу
остаётся только хвост; поле stt в записи - сколько ждали распознавания после конца фразы.
Весь звук за запуск пишется в mic.wav; последние 60 с держатся в кольце, из него
берётся начало фразы, когда перебивание замечено с опозданием.

Из SAM (voice/voice_client.py): параметры WhisperSTT, _strip_hallucinations,
_prepare_audio (шумодав + выравнивание громкости), barge-in через tiny и защита от
собственного эха (self-mention guard), стоп-слова (_CANCEL_TURN_PATTERNS), выбор
микрофона по MIC_DEVICE. Пауза по длине фразы - из SAM-29.
Словарь терминов трека - voice/layer/terms.txt, идёт в initial_prompt.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

from gateway.secret_mask import load_secrets, mask

from . import retention, speakers
from .bus import (BARGE_FLAG, HEARING_FLAG, HOLD_FLAG, MIC_WAV, MUTE_FLAG, SAY_OUT,
                  SPEAKING_FLAG, VOICE_IN, append_jsonl, bus_dir)
from .config import TERMS_FILE
from .mic_watch import STALL_SECS
from gateway.env import env_float, env_int

SAMPLE_RATE = 16_000
BLOCK = 1_600                                              # 100 мс
SILENCE_RMS = env_int("VOICE_SILENCE_RMS", 230)   # int16 RMS: ниже - тишина; фон комнаты в замере ~160
SILENCE_SHORT = env_float("VOICE_SILENCE_SECS", 1.5)  # конец короткой фразы
SILENCE_LONG = 2.5                                         # конец фразы длиннее LONG_PHRASE
LONG_PHRASE = 10.0
MIN_PHRASE_SECS = 0.5
PREROLL_SECS = 0.5
MAX_PHRASE_SECS = 120.0
# Распознавание кусками по ходу речи: на внутренней паузе от CUT_QUIET кусок длиннее CUT_MIN
# уходит в Whisper сразу; к концу фразы остаётся распознать только хвост.
CUT_QUIET = 0.5
CUT_MIN = 2.0
ECHO_WINDOW = 60.0              # реплика может ждать в очереди: окно от записи, не от звука
ECHO_COVERAGE = 0.6             # фраза эхо, если столько её текста повторяет мою реплику
BARGE_WINDOW = 1.5                                         # окно tiny во время озвучки
BARGE_MIN_RMS = 150                                        # тише - даже не смотрим
# Перебивание по громкости: в замере голос человека в микрофоне камеры даёт 1170-1380 (90-й процентиль
# громких блоков), мои колонки 376-677. Tiny на моей речи галлюцинирует и для этого не годится;
# через него ловятся только стоп-слова.
BARGE_RMS = env_int("VOICE_BARGE_RMS", 900)
BARGE_BLOCKS = 3                                           # 0,3 с подряд
BARGE_RMS_AEC = env_int("VOICE_BARGE_RMS_AEC", 100)  # замер без шумодава: остаток эха 35-38, голос человека поверх моего 743
# Громче этого при AEC - точно не моё эхо, перебивает без опознания. Замер после калибровки
# колонок: остаток эха 179-225, крик человека 647-2406; ECAPA на крике за 1 с давала 0,11-0,28,
# из семи попыток прошли две. Цена: громкий посторонний звук (стук, упавшая кружка) тоже обрывает речь.
BARGE_RMS_SURE = env_int("VOICE_BARGE_RMS_SURE", 600)
BARGE_BACK_SECS = 1.0          # перебивание по громкости замечено за 0,3 с: начало фразы из кольца
RING_SECS = 60.0                # кольцо последних секунд звука (SAM: PRE_TRIGGER 60 с)
STOP_WORDS = ("стоп", "хватит", "замолчи", "молчи", "тише", "тихо", "подожди", "погоди", "стой")
STOP_STEMS = ("стоп", "хвати", "замолч", "молч", "тише", "тихо", "подожд", "погоди", "стой", "постой")
# Команда стоп - короткая фраза, которая начинается со стоп-слова: «стоп», «подожди, я не то».
# По корню в любом месте фразы ловились обычные слова: «стопится», «тише и тише»
STOP_LEAD_WORDS = 2      # стоп-слово среди первых слов фразы
STOP_MAX_WORDS = 4       # длиннее - рассказ, а не команда (окно слуха 1,5 с вмещает 4-5 слов)
STOP_ENDINGS = ("", "и", "ите", "те", "т")  # к корню: стоп, подожд-и, подожд-ите, стой-те, хвати-т
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "bzikst/faster-whisper-large-v3-russian")
STOP_MODEL = os.getenv("VOICE_STOP_MODEL", "small")
TERMS = TERMS_FILE.read_text(encoding="utf-8").strip()
_TARGET_RMS = 3000

_HALLUCINATION_EXACT = [
    "Продолжение следует...", "Субтитры сделал DimaTorzok", "Субтитры создавал DimaTorzok",
    "Подписывайтесь на канал", "Не забудьте поставить лайк", "Спасибо за просмотр",
    "Ставьте лайки", "Подписывайтесь", "Спасибо за внимание", "Смотрите также",
    "Продолжение в следующей части", "Всем пока!", "До свидания!",
]
# Обрывки, которые Whisper выдаёт на хвосте звука; отбрасываются, только если это вся фраза
_HALLUCINATION_WHOLE = {"идр", "ит.д", "ит.п", "ипр"}


def strip_hallucinations(text: str) -> str:
    for phrase in _HALLUCINATION_EXACT:
        text = text.replace(phrase, "")
    text = re.sub(r'\b(\w+)\b(?:[,\s]+\b\1\b)+', r'\1', text, flags=re.IGNORECASE)
    text = text.strip(" .,!?\n")
    return "" if text.lower().replace(" ", "") in _HALLUCINATION_WHOLE else text


def prepare_audio(audio: np.ndarray) -> np.ndarray:
    """Шумодав, потом выравнивание громкости к _TARGET_RMS (SAM: _prepare_audio)."""
    if len(audio) < int(SAMPLE_RATE * 0.3):
        return audio
    try:
        import noisereduce as nr
        f32 = audio.astype(np.float32) / 32767.0
        f32 = nr.reduce_noise(y=f32, sr=SAMPLE_RATE, stationary=True, prop_decrease=0.75)
        audio = np.clip(f32 * 32767.0, -32767, 32767).astype(np.int16)
    except ImportError:
        pass
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    gain = min(_TARGET_RMS / rms, 100.0) if rms > 1 else 1.0
    if gain > 1.1:
        audio = np.clip(audio.astype(np.float32) * gain, -32767, 32767).astype(np.int16)
    return audio


def words(text: str) -> set[str]:
    return set(re.findall(r"\w{3,}", text.lower()))


class STT:
    def __init__(self) -> None:
        from faster_whisper import WhisperModel
        print(f"[слух] загружаю {WHISPER_MODEL} и {STOP_MODEL}...", flush=True)
        self.model = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")
        # стоп-слова во время моей речи: замер на записях голоса - tiny 62 мс, «погоди» не ловит
        # («погади»); small 144 мс, ловит; ложных 0/20 у обеих
        self.tiny = WhisperModel(STOP_MODEL, device="cuda", compute_type="float16")
        self.lock = threading.Lock()
        print("[слух] модели готовы", flush=True)

    def transcribe(self, audio: np.ndarray) -> str:
        with self.lock:
            segs, _ = self.model.transcribe(
                prepare_audio(audio).astype(np.float32) / 32768.0,
                language="ru",
                beam_size=1,  # замер на 37 с: beam 5 - 6,0-6,7 с, beam 1 - 4,8-5,1 с, текст тот же
                initial_prompt=TERMS,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                compression_ratio_threshold=2.0,
                temperature=0.0,
            )
            # При единственной температуре faster-whisper не отбрасывает сегмент, не прошедший
            # пороги, а возвращает как есть: зацикленный текст на тихом звуке проходил целиком.
            kept = [s.text for s in segs
                    if s.compression_ratio <= 2.4 and s.avg_logprob >= -1.0
                    and s.no_speech_prob <= 0.6]
        return strip_hallucinations(" ".join(kept).strip())

    def quick(self, audio: np.ndarray) -> str:
        """Быстрое распознавание окна для перебивания (SAM: WakeWordDetector)."""
        with self.lock:
            segs, _ = self.tiny.transcribe(
                audio.astype(np.float32) / 32768.0, language="ru", beam_size=1,
                best_of=1, temperature=0.0, no_speech_threshold=0.7, vad_filter=True)
            return " ".join(s.text for s in segs).strip()


def recent_said(d: Path, window: float = ECHO_WINDOW) -> list[str]:
    """Мои реплики за последние window секунд."""
    path = d / SAY_OUT
    if not path.exists():
        return []
    now, out = time.time(), []
    for line in path.read_text(encoding="utf-8").splitlines()[-20:]:
        try:
            rec = json.loads(line)
            if now - datetime.fromisoformat(rec["ts"]).timestamp() <= window:
                out.append(rec.get("text", ""))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return out


def norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def coverage(heard: str, said: list[str]) -> float:
    """Доля символов услышанного, совпавших с моей репликой кусками от 4 знаков в том же
    порядке. Эхо повторяет мой текст по порядку (замер: 0,96-1,0), фраза человека на ту же тему
    совпадает только отдельными словами (0,20-0,29). Сравнение по словам этого не различало."""
    h = norm(heard)
    if not h:
        return 0.0
    best = 0.0
    for s in said:
        m = difflib.SequenceMatcher(None, h, norm(s), autojunk=False)
        best = max(best, sum(b.size for b in m.get_matching_blocks() if b.size >= 4) / len(h))
    return best


def is_echo(text: str, said: list[str]) -> bool:
    return len(words(text)) >= 2 and coverage(text, said) >= ECHO_COVERAGE


def strip_echo(text: str, said: list[str]) -> str:
    """Убрать предложения, похожие на мои: после перебивания фраза начинается с хвоста
    моей речи из кольца, а его слова идут следом."""
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return " ".join(p for p in parts if not is_echo(p, said)).strip()


def is_stop_form(word: str) -> bool:
    """Стоп-слово целиком: корень и короткое окончание («погодите», «подождите» - целое
    «погоди» их не ловило), а не любое слово с этим началом («стопится»)."""
    return word in STOP_WORDS or any(word.startswith(stem) and word[len(stem):] in STOP_ENDINGS
                                     for stem in STOP_STEMS)


def has_stop_word(heard: str, said: list[str] = ()) -> bool:
    """Команда стоп: короткая фраза начинается со стоп-слова, которого нет в моих репликах
    (своё «хватит» из колонок не в счёт)."""
    words = re.findall(r"\w+", heard.lower())
    if not words or len(words) > STOP_MAX_WORDS:
        return False
    mine = set(re.findall(r"\w+", " ".join(said).lower()))
    return any(is_stop_form(w) and w not in mine for w in words[:STOP_LEAD_WORDS])


def block_rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))


def is_sure_barge(rms: float, aec: bool) -> bool:
    """При AEC звук громче BARGE_RMS_SURE - точно не моё эхо, перебивает без опознания."""
    return aec and rms >= BARGE_RMS_SURE


def pick_device(arg: int | None) -> int | None:
    """--device, иначе MIC_DEVICE (индекс или часть имени), иначе системный (SAM)."""
    if arg is not None:
        return arg
    env = os.getenv("MIC_DEVICE", "").strip()
    if env.isdigit():
        return int(env)
    if env:
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0 and env.lower() in dev["name"].lower():
                return i
        print(f"[слух] MIC_DEVICE={env!r} не найден, беру системный", flush=True)
    return None


def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        rate, width, ch = wf.getframerate(), wf.getsampwidth(), wf.getnchannels()
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    assert width == 2, "ожидается 16-bit PCM"
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if rate != SAMPLE_RATE:  # линейная пересэмплировка, для проверки достаточно
        n = int(len(audio) * SAMPLE_RATE / rate)
        audio = np.interp(np.linspace(0, len(audio), n, endpoint=False),
                          np.arange(len(audio)), audio).astype(np.int16)
    return audio


def save_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())


def mic_path(d: Path, now: datetime) -> Path:
    """Файл звука за запуск: mic.wav, на перезапуске mic-ЧЧММСС.wav; занятое имя (перезапуск в ту же секунду)
    получает номер - прежняя запись не затирается."""
    path = d / MIC_WAV
    if not path.exists():
        return path
    stem = f"mic-{now:%H%M%S}"
    path, n = d / f"{stem}.wav", 2
    while path.exists():
        path, n = d / f"{stem}-{n}.wav", n + 1
    return path


class Listener:
    def __init__(self, d: Path, stt: STT, device: int | None, core=None) -> None:
        self.d, self.stt, self.device = d, stt, device
        self.core = core
        self.secret = load_secrets()   # SAM-88, SAM-104: ключи маскируются до записи в шину
        # С эхоподавлением моя речь в микрофоне падает до ~11 RMS (замер): перебивание
        # можно ловить почти обычным порогом речи, в том числе издалека.
        self.barge_rms = BARGE_RMS_AEC if core is not None and core.apm else BARGE_RMS
        self.speaking, self.mute = d / SPEAKING_FLAG, d / MUTE_FLAG
        self.hearing, self.barge_flag = d / HEARING_FLAG, d / BARGE_FLAG
        self.hold = d / HOLD_FLAG
        self.blocks: queue.Queue[np.ndarray] = queue.Queue()
        self.preroll = deque(maxlen=int(PREROLL_SECS * SAMPLE_RATE / BLOCK))
        self.ring = deque(maxlen=int(RING_SECS * SAMPLE_RATE / BLOCK))
        self.mic = None
        self.reset()
        # окно для перебивания проверяется в отдельном потоке, чтобы не копить блоки
        self.barge_jobs: queue.Queue[np.ndarray] = queue.Queue(maxsize=1)
        self.barged = threading.Event()
        threading.Thread(target=self._barge_worker, daemon=True).start()
        self.stt_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        # кто говорит - параллельно с распознаванием; без библиотеки или профилей - без пометки
        try:
            speakers.encoder()
            self.who_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="who")
        except Exception as exc:
            print(f"[слух] опознание голоса отключено: {exc}", flush=True)
            self.who_pool = None

    def reset(self) -> None:
        self.phrase: list[np.ndarray] = []
        self.levels: list[float] = []
        self.quiet = 0.0
        self.window: list[np.ndarray] = []
        self.loud_run = 0
        self.loud_check = None               # (опознание голоса, rms): громко - а человек ли это
        self.cut = 0                         # индекс блока, до которого фраза уже отправлена
        self.parts: list[Future] = []        # распознавание кусков по порядку

    def _barge_worker(self) -> None:
        while True:
            self.check_barge(self.barge_jobs.get())

    def check_barge(self, audio: np.ndarray) -> None:
        """Окно звука поверх моей озвучки: стоп-слово человека, а не моё из колонок, поднимает флаг перебивания."""
        heard = self.stt.quick(audio)
        if not self.speaking.exists():
            return
        said = recent_said(self.d, 60)
        if has_stop_word(heard, said) and coverage(heard, said) < ECHO_COVERAGE:
            print(f"[слух] стоп-слово: {heard!r}", flush=True)
            self.barge_flag.write_text("stop", encoding="utf-8")
            self.barged.set()

    def _barge_loud(self, rms: float, who: str | None, score: float) -> None:
        print(f"[слух] перебил по громкости ({who or '?'} {score:.2f}, rms {rms:.0f})", flush=True)
        self.barge_flag.write_text("loud", encoding="utf-8")
        self.barged.set()

    def _start_phrase(self, rms: float, back_secs: float = 0.0) -> None:
        if back_secs:
            n = int(back_secs * SAMPLE_RATE / BLOCK)
            start = list(self.ring)[-n:]
        else:
            start = list(self.preroll)
        self.phrase, self.levels, self.quiet = start, [rms], 0.0
        self.hearing.touch()

    def _send_part(self, partial: bool = False) -> None:
        """Отправить в распознавание кусок фразы с прошлого среза до текущего блока.
        partial=True (срез на паузе посреди фразы): готовый текст куска сразу уходит в
        voice-in с пометкой partial - речь видна по ходу, а не только после конца фразы."""
        if len(self.phrase) > self.cut:
            chunk = np.concatenate(self.phrase[self.cut:])
            fut = self.stt_pool.submit(self.stt.transcribe, chunk)
            if partial:
                fut.add_done_callback(lambda f, a=chunk: self._emit_partial(f, a))
            self.parts.append(fut)
            self.cut = len(self.phrase)

    def _emit_partial(self, fut: Future, chunk: np.ndarray) -> None:
        """Кусок готов: текст и говорящий именно этого куска (в одной фразе могут говорить
        двое: в записи фраза с меткой одного профиля содержала реплику другого человека)."""
        try:
            text = fut.result()
        except Exception:
            return
        if not text or is_echo(text, recent_said(self.d)):
            return
        who, score = (speakers.identify(chunk) if self.who_pool else (None, 0.0))
        if who == speakers.CLAUDE:
            return
        # «стой» в куске речи человека, пока я говорю, - обрыв сразу, без договаривания и анализа
        if self.speaking.exists() and has_stop_word(text, recent_said(self.d, 60)):
            self.barge_flag.write_text("stop", encoding="utf-8")
            print(f"[слух] стоп-слово в куске: {mask(text, self.secret).text!r}", flush=True)
        heard = mask(text, self.secret)   # SAM-88: секрет дальше слуха не идёт
        append_jsonl(self.d / VOICE_IN, {"text": heard.text, "partial": True, "who": who,
                                         "score": round(score, 2), **({"secret": sorted(heard.names)} if heard.names else {})})
        # кусок по ходу речи в лог не идёт: его повторяет готовая фраза, он есть в voice-in.jsonl

    def _finish(self) -> None:
        secs = len(self.phrase) * BLOCK / SAMPLE_RATE
        audio = np.concatenate(self.phrase)
        peak = float(np.percentile(self.levels, 90)) if self.levels else 0.0
        quiet = self.quiet
        t0 = time.time()
        if secs - quiet >= MIN_PHRASE_SECS:
            self._send_part()
        parts = self.parts
        self.reset()
        self.hearing.unlink(missing_ok=True)
        if secs - quiet < MIN_PHRASE_SECS:
            return
        save_wav(self.d / "last-phrase.wav", audio)
        wav = self.d / "phrases" / f"{datetime.now():%H%M%S}.wav"
        wav.parent.mkdir(exist_ok=True)
        save_wav(wav, audio)
        who_job = self.who_pool.submit(speakers.identify, audio) if self.who_pool else None
        heard = mask(" ".join(t for t in (f.result() for f in parts) if t).strip(), self.secret)
        text = heard.text   # SAM-88: дальше слуха - только фраза с маской вместо секрета
        stt_secs = time.time() - t0
        if not text:
            return
        who, score = who_job.result() if who_job else (None, 0.0)
        said = recent_said(self.d)
        own = strip_echo(text, said)
        # Голос решает раньше текста: человек, опознанный по голосу, эхом не бывает,
        # даже если повторил мою фразу слово в слово (при записи образца голоса).
        if who == speakers.CLAUDE:
            echo = True
        elif who is not None:
            echo, own = False, text
        else:
            echo = not own
        append_jsonl(self.d / VOICE_IN, {"text": own or text, "secs": round(secs, 1),
                                         "rms": int(peak), "echo": echo,
                                         "who": who, "score": round(score, 2),
                                         "wait": round(quiet, 1), "stt": round(stt_secs, 2),
                                         "wav": f"phrases/{wav.name}",
                                         **({"secret": sorted(heard.names)} if heard.names else {}),
                                         **({"raw": text} if own and own != text else {})})
        print(f"[слух] {'эхо' if echo else who or 'неизвестный'} {score:.0%}: {text}", flush=True)

    def step(self, block: np.ndarray) -> None:
        """Один блок микрофона: записать, затем либо ждать перебивания, пока я говорю, либо слушать фразу."""
        rms = block_rms(block)
        self._record(block)
        if self.mute.exists():
            self._go_deaf()
            return
        speaking = self.speaking.exists()
        self._collect_stop_window(block, speaking)
        if speaking and not self.phrase:
            self._watch_barge(block, rms)
            return
        self.barged.clear()
        if self.phrase:
            self._grow_phrase(block, rms)
        else:
            self._await_phrase(block, rms)

    def _record(self, block: np.ndarray) -> None:
        self.ring.append(block)
        if self.mic:
            self.mic.writeframes(block.tobytes())

    def _mic_silent(self) -> None:
        """Блоков нет дольше STALL_SECS (микрофон замолчал): недослушанную фразу
        бросить и снять hearing, иначе озвучка ждёт конца фразы, которой не будет."""
        if self.phrase or self.hearing.exists():
            self._go_deaf()

    def _go_deaf(self) -> None:
        self.reset()
        self.preroll.clear()
        self.hearing.unlink(missing_ok=True)

    def _collect_stop_window(self, block: np.ndarray, speaking: bool) -> None:
        """Пока я говорю, окна стоп-слов собираются всегда, и во время записи фразы человека тоже:
        иначе мягкое перебивание начинает фразу, сбор окон прекращается, и «стой» не ловится."""
        if not speaking:
            self.window = []
            return
        self.window.append(block)
        if len(self.window) * BLOCK < BARGE_WINDOW * SAMPLE_RATE:
            return
        audio = np.concatenate(self.window)
        self.window = self.window[len(self.window) // 2:]   # перекрытие 50%
        if block_rms(audio) >= BARGE_MIN_RMS:
            try:
                self.barge_jobs.put_nowait(audio)
            except queue.Full:
                pass

    def _watch_barge(self, block: np.ndarray, rms: float) -> None:
        """Я говорю: следить за громкостью, проверять, человек ли это, и начать фразу человека при перебивании."""
        self.preroll.append(block)
        self._track_loudness(rms)
        if self.loud_run >= BARGE_BLOCKS and self.loud_check is None:
            self.loud_run = 0
            self._judge_loud(rms)
        self._take_who_result()
        if self.barged.is_set():
            self.barged.clear()
            self._start_phrase(rms, BARGE_BACK_SECS)

    def _track_loudness(self, rms: float) -> None:
        """Счёт громких блоков подряд. Как человек: на первую помеху притихнуть, не решив ещё, перебивают ли;
        помеха ушла - вернуть громкость (перебивание ниже гасит до нуля)."""
        was = self.loud_run
        self.loud_run = self.loud_run + 1 if rms >= self.barge_rms and not self.hold.exists() else 0
        if not self.core:
            return
        if self.loud_run == 1 and was == 0:
            self.core.duck(0.6)
        elif self.loud_run == 0 and was > 0:
            self.core.duck(1.0)

    def _judge_loud(self, rms: float) -> None:
        """Громкость - ещё не человек: моё же эхо (rms ~230 при пороге 100) обрывало длинные реплики,
        пока все молчали. Тише BARGE_RMS_SURE перебивает только узнанный голос человека; гость без профиля
        перебивает стоп-словом."""
        aec = self.core is not None and bool(self.core.apm)
        if self.who_pool and not is_sure_barge(rms, aec):
            last = np.concatenate(list(self.ring)[-int(SAMPLE_RATE / BLOCK):])
            self.loud_check = (self.who_pool.submit(speakers.identify, last), rms)
        else:
            self._barge_loud(rms, None, 0.0)

    def _take_who_result(self) -> None:
        if self.loud_check is None or not self.loud_check[0].done():
            return
        job, loud_rms = self.loud_check
        self.loud_check = None
        who, score = job.result()
        if who and who != speakers.CLAUDE:
            self._barge_loud(loud_rms, who, score)
            return
        print(f"[слух] громко, но не человек ({who or '?'} {score:.2f}, rms {loud_rms:.0f})", flush=True)
        if self.core:
            self.core.duck(1.0)

    def _await_phrase(self, block: np.ndarray, rms: float) -> None:
        self.preroll.append(block)
        if rms >= SILENCE_RMS:
            self._start_phrase(rms)

    def _grow_phrase(self, block: np.ndarray, rms: float) -> None:
        """Дописать блок во фразу: на паузе отправить кусок, на долгой тишине закончить."""
        loud = rms >= SILENCE_RMS
        self.phrase.append(block)
        if loud:
            self.levels.append(rms)
        was_quiet = self.quiet
        self.quiet = 0.0 if loud else self.quiet + BLOCK / SAMPLE_RATE
        secs = len(self.phrase) * BLOCK / SAMPLE_RATE
        paused = was_quiet < CUT_QUIET <= self.quiet
        if paused and (len(self.phrase) - self.cut) * BLOCK / SAMPLE_RATE >= CUT_MIN:
            self._send_part(partial=True)
        limit = SILENCE_LONG if secs > LONG_PHRASE else SILENCE_SHORT
        if self.quiet >= limit or secs >= MAX_PHRASE_SECS:
            self._finish()

    def run(self) -> None:
        self.hearing.unlink(missing_ok=True)

        def callback(indata, frames, t, status):
            self.blocks.put(indata[:, 0].copy())

        path = mic_path(self.d, datetime.now())
        self.mic = wave.open(str(path), "wb")
        # SAM-106: записи слуха не копятся вечно; файл, в который пишу сейчас, уборка не трогает
        retention.RetentionLoop(self.d, active=lambda: path.name).start()
        self.mic.setnchannels(1)
        self.mic.setsampwidth(2)
        self.mic.setframerate(SAMPLE_RATE)
        if self.core:
            # микрофон из ядра звука: уже очищен от моей речи эхоподавлением, 16 кГц по 100 мс
            if self.core.input_name:  # нет микрофона - ядро уже сказало одной строкой
                print(f"[слух] микрофон: {self.core.input_name} (через ядро, AEC "
                      f"{'вкл' if self.core.apm else 'выкл'}); пишу в {self.d / VOICE_IN}", flush=True)
            while True:
                try:
                    block = self.core.mic_q.get(timeout=STALL_SECS)
                except queue.Empty:
                    self._mic_silent()
                    continue
                self.step(block)
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=BLOCK, device=self.device, callback=callback):
            name = sd.query_devices(self.device, "input")["name"]
            print(f"[слух] микрофон: {name}; пишу в {self.d / VOICE_IN}", flush=True)
            while True:
                self.step(self.blocks.get())


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    p = argparse.ArgumentParser()
    p.add_argument("bus", nargs="?", help="папка шины; по умолчанию SAM_VOICE_BUS_DIR")
    p.add_argument("--device", type=int)
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--file")
    p.add_argument("--no-aec", action="store_true", help="без ядра звука и эхоподавления")
    a = p.parse_args()
    if a.list_devices:
        print(sd.query_devices())
        return
    stt = STT()
    if a.file:
        t = time.time()
        print(stt.transcribe(read_wav(a.file)))
        print(f"[слух] {time.time() - t:.1f} с", file=sys.stderr)
        return
    d = bus_dir(a.bus)
    if a.no_aec:
        Listener(d, stt, pick_device(a.device)).run()
        return
    # Звук одним процессом с ядром AEC; озвучку в него добавит SAM-09
    from .audio_core import AudioCore
    core = AudioCore(device_in=pick_device(a.device), pick_in=lambda: pick_device(a.device))
    core.start()
    core.watch(d)
    try:
        Listener(d, stt, None, core).run()
    finally:
        core.close()


if __name__ == "__main__":
    main()
