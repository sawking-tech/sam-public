"""Озвучка реплик: хвост say-out.jsonl → движок синтеза (tts_client) → колонки/наушники.


  python -m voice.layer.say_out [папка-шины]
  python -m voice.layer.say_out --say "проверка связи"        одна фраза и выход
  python -m voice.layer.say_out --say "..." --wav out.wav      синтез в файл, без воспроизведения

Запись say-out.jsonl: {"ts": ..., "text": "...", "interrupt": false, "voice": null}
  Голос: поле voice записи, иначе "voice" в session.json, иначе SAM_VOICE (xenia).
  Скорость: "speed" в session.json, иначе SAM_VOICE_SPEED (time-stretch без смены высоты).
  Реплика режется на предложения; предложение играет по мере синтеза, с первого куска
  (Synthesis + audio_stream), следующее синтезируется, пока звучит текущее.
  interrupt=true обрывает текущую реплику и сбрасывает очередь.
  После перебивания недоговорённое лежит в resume.txt; «продолжай», «дальше» - договариваю.
  Реплика с back=true - короткий отклик быстрой модели посреди его речи: звучит сразу, тише.
  hold=true: пока играет, громкость его не перебивает (смех, фон), стоп-слово перебивает.
  Кто звучит, решает turns.py: ответ быстрого голоса с reply_to на фразу, после которой была
  новая, не звучит (stale) ни при приёме, ни в очереди; fix=true (поправка) отзывает его реплики
  с последней фразы человека (retracted).

Флаги в папке шины:
  speaking  лежит, пока играет звук (плюс TAIL_SECS): voice_in слушает только перебивание
  hearing   он говорит: новая реплика ждёт конца его фразы
  barge     он перебил: текущая реплика обрывается, очередь сбрасывается

Протокол Wyoming - tts_client.py, нормализация чисел - voice/tts_normalize.py.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
import queue
import re
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from .bus import (SAID, BARGE_FLAG, HEARING_FLAG, HOLD_FLAG, SAY_OUT, SESSION_JSON, VOICE_IN,
                    SPEAKING_FLAG, append_jsonl, read_json, bus_dir, tail_jsonl)
from .audio_core import RATE as OUT_RATE
from .audio_stream import Gain, Pipeline, Resampler, Tempo, to_int16
from .stress import plain
from .tts_client import DEFAULT_VOICE, SPEED, segment_chars, session_voice, stream, synthesize, tts_backend
from .turns import FAST, RETRACTED, STALE, is_observer, is_stale, verdict
from gateway.env import env_float

# Синтез и выбор движка - tts_client; проигрыватель не знает, какой движок говорит: любой отдаёт
# поток кусков (Silero - фразу одним куском, CosyVoice - по мере готовности), дальше путь один.
TAIL_SECS = 0.4
BARGE_KEEP_SECS = 1.1   # после перебивания ещё 2-3 слова (1,5 с давали слово лишнее)
BARGE_FADE_SECS = 0.4   # затем затухание
# Поправка посреди длинной реплики быстрой (SAM-167): звучащее не обрывается, а тихо затихает и
# замолкает в паузе между словами, поправка идёт следом - без шва-обрыва. Короче перебивания: слово-два
FIX_KEEP_SECS = env_float("SAM_FIX_KEEP_SECS", 0.6)
FIX_FADE_SECS = env_float("SAM_FIX_FADE_SECS", 0.3)
FIX_LEVEL = env_float("SAM_FIX_LEVEL", 0.3)   # к какой громкости затихает до паузы
# Короткий отклик, пока он говорит долго: заготовки убраны, отклик придумывает быстрая
# модель по ходу его речи и шлёт с back=true. Звучит сразу, тише и медленнее, тишины не ждёт,
# громкостью не перебивается.
BACK_SPEED = 0.95
BACK_GAIN = 0.6
LOG_CUT = 60          # столько символов реплики в строке лога перебивания
# После перебивания остаток реплики лежит в resume.txt; на эти слова договариваю сразу.
RESUME_TTL = 120.0   # остаток живёт до следующего перебивания или столько секунд
RESUME_WORDS = ("продолж", "дальше", "договори", "договаривай")  # корни: «продолжение», «продолжай»
HEARING_WAIT_SECS = 30.0  # дольше его фразу не ждём
# Выдержка после первого куска до начала звука: пока звучит первый, должен успеть прийти второй.
# Замер 8 окт 23:19, CosyVoice рядом с Whisper: первый кусок 0,84 с звука считается 1,0-1,6 с, второй
# (2 с звука) приходит через 0,9-1,6 с после первого - без выдержки паузы 0,7 с посреди фразы
# - речь звучала с запинками. 0,8 с покрывают второй кусок до 1,7 с ценой 0,8 с задержки (SAM_VOICE_PREBUFFER).
PREBUFFER_SECS = env_float("SAM_VOICE_PREBUFFER", 0.8)
POLL_SECS = 0.03          # шаг проверки перебивания и новых кусков во время звука
GAP_LOG_SECS = 0.1        # ожидание следующего предложения дольше этого - в лог: слышна пауза
SYNTH_FIRST_TIMEOUT = 30.0  # дольше первого куска не ждём: движок завис


def last_human_ts(d: Path) -> float:
    """Время последней фразы человека (не эха) в voice-in.jsonl."""
    path = d / VOICE_IN
    if not path.exists():
        return 0.0
    for line in reversed(path.read_text(encoding="utf-8").splitlines()[-20:]):
        try:
            r = json.loads(line)
            if not r.get("echo"):
                return datetime.fromisoformat(r["ts"]).timestamp()
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return 0.0


def last_phrase_ts(d: Path) -> float:
    """Время последней законченной фразы человека: куски по ходу речи не в счёт, ответ ещё впереди."""
    path = d / VOICE_IN
    if not path.exists():
        return 0.0
    for line in reversed(path.read_text(encoding="utf-8").splitlines()[-40:]):
        try:
            r = json.loads(line)
            if not r.get("echo") and not r.get("partial"):
                return datetime.fromisoformat(r["ts"]).timestamp()
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return 0.0


def iso_ts(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp() if value else None
    except ValueError:
        return None


def sentences(text: str) -> list[str]:
    return [p for p in re.split(r"(?<=[.!?…])\s+", text.strip()) if p.strip()]


def segments(text: str, min_chars: int) -> list[str]:
    """Отрезки для синтеза: предложения, склеенные до min_chars. Каждый отрезок движок начинает
    заново, а первый кусок CosyVoice стоит 1-1,5 с: короткое предложение кончается раньше, чем
    готов следующий, и слышна пауза (23:23: 1,35 с после «Проверяю запись звука.»)."""
    out: list[str] = []
    for sentence in sentences(text):
        if out and len(out[-1]) < min_chars:
            out[-1] = f"{out[-1]} {sentence}"
        else:
            out.append(sentence)
    return out


class Synthesis:
    """Синтез одной фразы в фоне: куски, уже готовые для колонок (темп, частота, громкость),
    копятся в очереди, проигрыватель забирает их по мере прихода. close() бросает синтез:
    соединение закрывается, сервер дорабатывает фразу у себя и следующую не задерживает."""

    def __init__(self, text: str, voice: str, speed: float, gain: float = 1.0) -> None:
        self.text, self.voice, self.speed, self.gain = text, voice, speed, gain
        self.chunks: queue.Queue[np.ndarray] = queue.Queue()
        self.done = threading.Event()
        self.stopped = threading.Event()
        self.error: Exception | None = None
        self.buffered = 0  # сэмплов принято за всё время
        self.first_at: float | None = None  # когда пришёл первый кусок: от него считается выдержка
        self.started_at = time.time()
        threading.Thread(target=self._run, daemon=True, name="synth").start()

    def _pipeline(self, rate: int) -> Pipeline:
        return Pipeline([Tempo(rate, self.speed), Resampler(rate, OUT_RATE), Gain(self.gain)])

    def _put(self, audio: np.ndarray) -> None:
        if len(audio):
            self.first_at = self.first_at or time.time()
            self.buffered += len(audio)
            self.chunks.put(to_int16(audio))

    def _run(self) -> None:
        source = stream(self.text, self.voice)
        pipe = None
        try:
            for audio, rate in source:
                if self.stopped.is_set():
                    return
                pipe = pipe or self._pipeline(rate)
                self._put(pipe.push(audio))
            if pipe:
                self._put(pipe.flush())
        except Exception as exc:
            self.error = exc
        finally:
            source.close()
            self.done.set()

    def wait_ready(self, hold_secs: float, timeout: float = SYNTH_FIRST_TIMEOUT) -> bool:
        """Дождаться первого куска и ещё hold_secs (или конца фразы, если раньше): запас, чтобы
        следующий кусок пришёл, пока звучит первый. False - звука не будет."""
        deadline = time.time() + timeout
        while not self.buffered and not self.done.is_set() and time.time() < deadline:
            time.sleep(POLL_SECS)
        # от прихода первого куска, а не от вызова: следующее предложение, синтезированное, пока звучало
        # текущее, начинается сразу (23:22: лишние 0,8 с на каждом стыке предложений)
        hold_until = (self.first_at or time.time()) + hold_secs
        while not self.done.is_set() and time.time() < hold_until:
            time.sleep(POLL_SECS)
        return self.buffered > 0

    def take(self) -> list[np.ndarray]:
        """Всё, что пришло с прошлого раза."""
        out = []
        while not self.chunks.empty():
            out.append(self.chunks.get_nowait())
        return out

    @property
    def drained(self) -> bool:
        return self.done.is_set() and self.chunks.empty()

    def close(self) -> None:
        self.stopped.set()


class Speaker:
    """Одна очередь реплик; внутри реплики предложения синтезируются на шаг вперёд."""

    def __init__(self, d: Path, core=None, recorder=None) -> None:
        self.d = d
        self.core = core
        self.recorder = recorder  # пишет звук колонок на время реплики (SAM_VOICE_RECORD_OUT)
        self.flag = d / SPEAKING_FLAG
        self.hearing = d / HEARING_FLAG
        self.barge = d / BARGE_FLAG
        self.hold = d / HOLD_FLAG
        # (текст, голос, hold, стиль, чей ответ, reply_to)
        self.q: queue.Queue[tuple[str, str | None, bool, str | None, str | None, float | None]] = queue.Queue()
        self.cancel = threading.Event()
        self.last_put = 0.0
        self.barged_at = 0.0
        self.barged_kind = ""                # stop - реплики, написанные до перебивания, не звучат; loud - звучат
        self.current = ""                    # реплика, которая сейчас звучит: для лога перебивания
        self.current_who: str | None = None  # чья она: поправка обрывает текущую реплику быстрого голоса
        self.discard = threading.Event()     # текущая отозвана: оборвать без остатка в resume.txt
        self.soften = threading.Event()      # текущую отозвала поправка: затихнуть до паузы, а не оборвать (SAM-167)
        self.busy = False                    # реплика взята в работу (синтез, ожидание тишины, звук)
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self.watch_voice, daemon=True).start()

    def watch_voice(self) -> None:
        """Его фраза дошла - если просит продолжить, договорить остаток после перебивания."""
        for rec in tail_jsonl(self.d / VOICE_IN):
            if rec.get("echo") or rec.get("partial"):
                continue
            low = rec.get("text", "").lower()
            resume = self.d / "resume.txt"
            if resume.exists() and time.time() - resume.stat().st_mtime > RESUME_TTL:
                resume.unlink(missing_ok=True)
            if resume.exists() and any(w in low for w in RESUME_WORDS):
                rest = resume.read_text(encoding="utf-8").strip()
                resume.unlink(missing_ok=True)
                if rest:
                    print(f"[речь] договариваю: {plain(rest)[:60]}", flush=True)
                    self.q.put((rest, None, False, None, None, None))

    def settings(self, override: str | None) -> tuple[str, float]:
        voice, speed = session_voice(self.d)
        return override or voice, speed

    def put(self, text: str, interrupt: bool, voice: str | None = None, hold: bool = False,
            style: str | None = None, who: str | None = None, reply_to: float | None = None) -> None:
        self.last_put = time.time()
        if interrupt:
            self._drop_queue()
            self.cancel.set()
            self._halt()
        self.q.put((text, voice, hold, style, who, reply_to))

    def _said(self, text: str, status: str, who: str | None = None) -> None:
        """Судьба реплики в said.jsonl: тикер будит наблюдателя на cut и dropped, чтобы важное повторить.
        who - источник реплики (turns: FAST, MAIN, OBSERVER), он доходит до подписи в тикере."""
        rec = {"text": plain(text), "status": status}
        if who:
            rec["from"] = who
        append_jsonl(self.d / SAID, rec)

    def retract_fast(self) -> list[str]:
        """Поправка: реплики быстрого голоса, ещё не прозвучавшие, выбросить, звучащую оборвать
        (22:01: отозванная реплика уже синтезировалась и доиграла); остальное оставить по порядку."""
        keep, gone = [], []
        if self.busy and self.current_who == FAST:
            gone.append((self.current,))
            self.discard.set()
            self.cancel.set()
            if self.core is not None:   # звучащее затихает до паузы между словами (_play_stream), поправка следом
                self.soften.set()
            else:
                self._halt()
        while not self.q.empty():
            item = self.q.get_nowait()
            (gone if item[4] == FAST else keep).append(item)
        for item in keep:
            self.q.put(item)
        return [item[0] for item in gone]

    def _drop_queue(self) -> list[tuple[str, str | None]]:
        """Сбросить очередь; (текст, источник) сброшенного."""
        dropped = []
        while not self.q.empty():
            item = self.q.get_nowait()
            dropped.append((item[0], item[4]))
        return dropped

    def _wait_silence(self) -> None:
        """Он говорит: не начинать поверх, дождаться конца фразы."""
        deadline = time.time() + HEARING_WAIT_SECS
        while self.hearing.exists() and time.time() < deadline:
            time.sleep(0.05)

    def _barge_kind(self) -> str | None:
        """stop - оборвать сразу; loud - договорить текущее предложение и замолчать."""
        if not self.barge.exists():
            return None
        try:
            kind = self.barge.read_text(encoding="utf-8").strip() or "stop"
        except OSError:
            kind = "stop"
        self.barge.unlink(missing_ok=True)
        self.barged_at, self.barged_kind = time.time(), kind  # после стоп-слова записанное раньше не звучит
        dropped = self._drop_queue()
        # видно, какая фраза оборвана и что не прозвучало: без этой строки обрыв в логе не виден
        print(f"[речь] перебит ({kind}) на: {plain(self.current)[:LOG_CUT]}", flush=True)
        self._said(self.current, "cut", self.current_who)
        for text, who in dropped:
            print(f"[речь] не прозвучало: {plain(text)[:LOG_CUT]}", flush=True)
            self._said(text, "dropped", who)
        return kind

    def _loud_while_silent(self) -> bool:
        """Громкое перебивание, пока я ещё молчу, - это он начал говорить, а не перебил меня: реплику
        не сбрасывать, дождаться тишины и сказать, иначе реплика перед реакцией пропадает.
        Стоп-слово по-прежнему обрывает."""
        try:
            kind = self.barge.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if kind != "loud":
            return False
        self.barge.unlink(missing_ok=True)
        print(f"[речь] он заговорил до моей реплики, жду: {plain(self.current)[:LOG_CUT]}", flush=True)
        return True

    def _barged(self) -> bool:
        kind = self._barge_kind()
        if kind:
            self.cancel.set()
            self._halt()
        return bool(kind)

    # Воспроизведение: через ядро звука (одно с микрофоном, с эхоподавлением), если оно есть;
    # иначе прямо через sounddevice, как в отдельном запуске say_out.py.
    def _start(self, audio: np.ndarray, rate: int) -> None:
        if self.core:
            self.core.play(audio, rate)
        else:
            sd.play(audio, rate)

    def _active(self) -> bool:
        return self.core.busy() if self.core else sd.get_stream().active

    def _soften(self) -> bool:
        """Отзыв поправкой: затихнуть к FIX_LEVEL и замолчать в паузе между словами; True - затихание пошло."""
        if not self.soften.is_set() or self.core is None:
            return False
        self.soften.clear()
        self.core.wind_down(FIX_KEEP_SECS, FIX_FADE_SECS, FIX_LEVEL)
        return True

    def _halt(self) -> None:
        if self.core:
            self.core.stop()
        else:
            sd.stop()

    def _play(self, audio: np.ndarray, rate: int) -> None:
        """Целый буфер (файл, синтез без ядра): играть и следить за перебиванием."""
        self.flag.touch()
        self._start(audio, rate)
        time.sleep(0.02)
        while self._active():
            if self.cancel.is_set():
                self._halt()
                return
            if self._barge_kind():
                self.cancel.set()
                self._halt()
                return
            time.sleep(POLL_SECS)

    def _play_stream(self, syn: Synthesis) -> None:
        """Играть фразу по мере синтеза: куски уходят в колонки, как приходят. Громкое перебивание -
        договорить 3-4 слова вполголоса до паузы между словами (ядро, WindDown), синтез при этом
        докармливает ядро; стоп-слово и отмена обрывают сразу."""
        if not self.core:
            syn.done.wait()
            chunks = syn.take()
            if chunks:
                self._play(np.concatenate(chunks), OUT_RATE)
            return
        self.flag.touch()
        winding = False
        starved_at = None  # ядро доиграло, а синтез ещё не прислал следующий кусок: слышна пауза
        while True:
            for chunk in syn.take():
                self.core.play(chunk, OUT_RATE)
                if starved_at:
                    print(f"[речь] пауза в потоке {time.time() - starved_at:.2f} с: синтез медленнее звука",
                          flush=True)
                    starved_at = None
            if not syn.done.is_set() and not self.core.busy() and not starved_at:
                starved_at = time.time()
            if syn.drained:
                self.core.end_phrase()
                if not self.core.busy():
                    return
            if winding and not self.core.winding:  # договорил до паузы: тишина
                syn.close()
                return
            if self.cancel.is_set() and not winding:
                if self._soften():
                    winding = True
                else:
                    self._halt()
                    syn.close()
                    return
            kind = self._barge_kind()
            if kind == "stop":
                self.cancel.set()
                self._halt()
                syn.close()
                return
            if kind == "loud" and not winding:
                self.cancel.set()  # следующих предложений не будет
                self.core.wind_down(BARGE_KEEP_SECS, BARGE_FADE_SECS)
                winding = True
            time.sleep(POLL_SECS)

    def _save_rest(self, parts: list[str]) -> None:
        """Недоговорённое после перебивания - в resume.txt, на «продолжай»; отозванное - никогда."""
        rest = " ".join(parts).strip()
        if rest and not self.discard.is_set():
            self.d.joinpath("resume.txt").write_text(rest, encoding="utf-8")

    def _play_file(self, path: str) -> None:
        try:
            with wave.open(path, "rb") as wf:
                rate = wf.getframerate()
                audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            self._play(audio, rate)
        except Exception as exc:
            print(f"[речь] файл не проигран: {exc}", file=sys.stderr, flush=True)

    @staticmethod
    def _log_latency(syn: Synthesis, reply_to: float | None = None) -> None:
        """Из чего сложилась задержка до первого звука реплики: синтез первого куска, выдержка и
        ожидание тишины (23:26: до звука 5,7 с при первом куске движка за 0,95 с). С reply_to (метка записи фразы,
        после распознавания) - ещё два отрезка: от записи фразы до текста ответа (шлюз и модель) и до звука."""
        now = time.time()
        first = (syn.first_at or now) - syn.started_at
        turn = (f"; от записи фразы {datetime.fromtimestamp(reply_to):%H:%M:%S.%f}"[:-3]
                + f": текст ответа через {syn.started_at - reply_to:.2f} с, "
                f"звук через {now - reply_to:.2f} с") if reply_to else ""
        print(f"[речь] до звука {now - syn.started_at:.2f} с: первый кусок {first:.2f} с, "
              f"затем выдержка и тишина {now - syn.started_at - first:.2f} с{turn}", flush=True)

    def _speak(self, text: str, voice: str, speed: float, style: str | None, reply_to: float | None) -> None:
        """Реплика по предложениям: каждое играет по мере синтеза, следующее синтезируется, пока
        звучит текущее. Перед первым звуком - дождаться тишины и проверить, не устарел ли ответ."""
        gain = BACK_GAIN if style == "back" else 1.0
        parts = segments(text, segment_chars(tts_backend(voice)))  # движок - по голосу реплики
        current = Synthesis(parts[0], voice, speed, gain) if parts else None
        for i in range(len(parts)):
            waited = time.time()
            if not current.wait_ready(PREBUFFER_SECS):
                print(f"[речь] ошибка синтеза: {current.error or 'нет звука'}", file=sys.stderr, flush=True)
                # ни звука - dropped, хвост не прозвучал - cut, как при перебивании: played не пишется, тикер будит
                # ведущего; отклик и отозванная реплика судьбы не пишут и на «продолжай» не возвращаются
                spoken_line = style != "back" and not self.discard.is_set()
                if spoken_line:
                    self._said(text, "cut" if i else "dropped", self.current_who)
                if i and spoken_line:
                    self._save_rest(parts[i:])
                self.cancel.set()
                current.close()
                break
            if i and time.time() - waited > GAP_LOG_SECS:
                print(f"[речь] пауза между отрезками {time.time() - waited:.2f} с", flush=True)
            nxt = Synthesis(parts[i + 1], voice, speed, gain) if i + 1 < len(parts) else None
            if style != "back":  # отклик звучит поверх его речи, тишины не ждёт
                self._wait_silence()
            if i == 0 and self._loud_while_silent():  # между предложениями громкость - перебивание
                self._wait_silence()
            stale = i == 0 and is_stale(reply_to, last_phrase_ts(self.d))
            if stale:  # пока ждал очереди и тишины, он сказал новое: ответ на старое не звучит
                print(f"[речь] устарел: {plain(text)[:LOG_CUT]}", flush=True)
                self._said(text, STALE, self.current_who)
                self.cancel.set()
            elif self.cancel.is_set() or self._barged():
                self._save_rest(parts[i:])
            else:
                if i == 0:
                    self._log_latency(current, reply_to)
                self._play_stream(current)
                if self.cancel.is_set():
                    self._save_rest(parts[i + 1:])
            if self.cancel.is_set():
                current.close()
                if nxt:
                    nxt.close()
                break
            current = nxt

    def _worker(self) -> None:
        while True:
            text, override, hold, style, who, reply_to = self.q.get()
            self.current, self.current_who, self.busy = text, who, True
            self.cancel.clear()
            self.discard.clear()
            self.soften.clear()   # отзыв прошлой реплики не смягчает эту (поправку)
            if hold:
                self.hold.touch()
            else:
                self.hold.unlink(missing_ok=True)
            self.barge.unlink(missing_ok=True)
            voice, speed = self.settings(override)
            if style == "back":
                speed = BACK_SPEED
            if style == "wav":
                self._play_file(text)
            else:
                if self.recorder:
                    self.recorder.active = True
                self._speak(text, voice, speed, style, reply_to)
                if self.recorder:
                    self.recorder.active = False
            self.hold.unlink(missing_ok=True)
            self.busy = False
            if not self.cancel.is_set() and style != "back":
                print(f"[речь] прозвучало: {plain(text)[:LOG_CUT]}", flush=True)
                self._said(text, "played", who)
            if self.q.empty():
                if not self.cancel.is_set():
                    time.sleep(TAIL_SECS)
                self.flag.unlink(missing_ok=True)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    p = argparse.ArgumentParser()
    p.add_argument("session", nargs="?")
    p.add_argument("--say")
    p.add_argument("--wav")
    p.add_argument("--voice", default=DEFAULT_VOICE)
    p.add_argument("--speed", type=float, default=SPEED)
    a = p.parse_args()

    if a.say:
        audio, rate = synthesize(a.say, a.voice, a.speed)
        if a.wav:
            with wave.open(a.wav, "wb") as wf:
                wf.setnchannels(1), wf.setsampwidth(2), wf.setframerate(rate)
                wf.writeframes(audio.tobytes())
            print(f"{a.wav}: {len(audio) / rate:.1f} с")
        else:
            sd.play(audio, rate)
            sd.wait()
        return

    serve(bus_dir(a.session))


def floor_step(d: Path, speaker: "Speaker") -> str | None:
    """Воля: если сейчас можно взять слово - поставить намерение в очередь озвучки.
    Возвращает id взятого намерения или None."""
    from . import agenda
    intent = agenda.pick(agenda.load(d), speaking=speaker.flag.exists(),
                         hearing=speaker.hearing.exists(),
                         since_human=time.time() - last_human_ts(d),
                         queue_busy=not speaker.q.empty())
    if not intent:
        return None
    hold = intent.priority == "high"  # важное: громкость не перебивает, стоп-слово - да
    if intent.wav:
        speaker.q.put((str(intent.wav), None, hold, "wav", None, None))
    else:
        speaker.q.put((intent.text, None, hold, None, None, None))
    agenda.mark_done(d, intent.id)
    print(f"[очередь] беру слово ({intent.priority}): {plain(intent.text)}", flush=True)
    return intent.id


def floor_loop(d: Path, speaker: "Speaker") -> None:
    while True:
        try:
            floor_step(d, speaker)
        except Exception as exc:
            print(f"[очередь] ошибка: {exc}", file=sys.stderr, flush=True)
        time.sleep(0.3)


def serve(d: Path, core=None, recorder=None) -> None:
    """Принимать реплики из say-out.jsonl и озвучивать. Запускается в процессе voice.py
    рядом со слухом: запись и воспроизведение - один процесс."""
    for f in (SPEAKING_FLAG, BARGE_FLAG):
        (d / f).unlink(missing_ok=True)
    speaker = Speaker(d, core, recorder)
    threading.Thread(target=floor_loop, args=(d, speaker), daemon=True, name="agenda").start()
    print(f"[речь] слушаю {d / SAY_OUT}", flush=True)
    last_observer = 0.0
    fast_said: list[tuple[float, str]] = []  # реплики быстрого голоса: (когда записана, текст) - для отзыва
    for rec in tail_jsonl(d / SAY_OUT, backlog_secs=15):
        if rec.get("wav"):  # проиграть готовый файл (пробы голосов) тем же процессом
            print(f"[речь] файл {rec['wav']}", flush=True)
            speaker.q.put((rec["wav"], None, bool(rec.get("hold")), "wav", None, None))
            continue
        text = rec.get("text", "").strip()
        if not text:
            continue
        # Перебили: строки, записанные до перебивания, но дочитанные после, не звучат
        # (8 окт: четыре реплики подряд продолжали звучать после «стой»)
        try:
            written = datetime.fromisoformat(rec["ts"]).timestamp()
        except (KeyError, ValueError):
            written = time.time()
        # Только после стоп-слова: громкость - это он заговорил, мой ответ ему ещё нужен
        if written < speaker.barged_at and speaker.barged_kind == "stop" and not rec.get("interrupt"):
            print(f"[речь] отброшено после перебивания: {plain(text)[:60]}", flush=True)
            speaker._said(text, "stale", rec.get("from"))
            continue
        # Одно лицо: на одну фразу человека отвечает кто-то один (turns.py)
        back = bool(rec.get("back"))
        if back:  # отклик посреди его речи: не дубль ответа, громкостью не перебивается
            print(f"[речь] отклик: {plain(text)}", flush=True)
            speaker.put(text, False, rec.get("voice"), True, "back")
            continue
        who = rec.get("from")
        reply_to = iso_ts(rec.get("reply_to"))
        status = verdict(who, reply_to, last_phrase=last_phrase_ts(d), last_human=last_human_ts(d),
                         last_observer=last_observer)
        if status:
            print(f"[речь] не звучит ({status}): {plain(text)}", flush=True)
            speaker._said(text, status, who)
            continue
        if who == FAST:
            fast_said.append((written, text))
        else:
            if is_observer(who):   # ответил ведущий; своя строка основной модели - не повод глушить её же продолжение
                last_observer = time.time()
            if rec.get("fix"):
                retract(speaker, fast_said, last_human_ts(d))
                fast_said.clear()
        print(f"[речь] {plain(text)}", flush=True)
        speaker.put(text, bool(rec.get("interrupt")), rec.get("voice"), bool(rec.get("hold")),
                    who=who, reply_to=reply_to)


def retract(speaker: "Speaker", fast_said: list[tuple[float, str]], since: float) -> None:
    """Поправка: реплики быстрого голоса с последней фразы человека - неправда. Не прозвучавшие
    выбросить из очереди, все отметить в said.jsonl: next_voice покажет их ему как отозванные."""
    queued = set(speaker.retract_fast())
    for written, text in fast_said:
        if written >= since or text in queued:
            print(f"[речь] отозвано: {plain(text)[:LOG_CUT]}", flush=True)
            speaker._said(text, RETRACTED, FAST)


if __name__ == "__main__":
    main()
