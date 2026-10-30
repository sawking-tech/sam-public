"""Ядро звука: микрофон и колонки в одном месте, эхоподавление WebRTC между ними.


Колонки и микрофон работают на 48 кГц кадрами по 10 мс. Каждый кадр, ушедший в колонки,
идёт в AEC как эталон (process_reverse_stream); каждый кадр микрофона проходит AEC
(process_stream), и моя речь из него вычитается. Очищенный микрофон понижается до 16 кГц
и отдаётся слуху блоками по 100 мс - как раньше отдавал sounddevice.

AEC: livekit rtc AudioProcessingModule (WebRTC APM), найдено поиском 8 окт.

Калибровка колонок. AEC нужна задержка между кадром в колонки и его эхом в микрофоне.
Заявленная устройствами (латентности потоков, ~240 мс) на мониторах по HDMI/DP меньше
настоящей, AEC промахивается, и эхо остаётся громким (8 окт: до 3000 вместо 35-200).
Поэтому задержка меряется: тихий свип 0,5 с в колонки, запись сырого микрофона до AEC,
сдвиг по взаимной корреляции. Результат кэшируется по имени выхода в
data/voice/voices/speaker-calibration.json; при запуске выход из кэша берёт задержку оттуда,
новый калибруется один раз. Слабая корреляция или сдвиг вне 20-600 мс - берётся заявленная.

Смена выхода: раз в 2 с, когда никто не говорит, спрашиваю у winmm системный выход по
умолчанию (список PortAudio застывает на момент запуска). Сменился - колонки переоткрываются
на новом устройстве, задержка из кэша или калибровкой.

Перекалибровать вручную: создать файл calibrate в папке шины
(например, New-Item <папка-шины>/calibrate). В ближайшую тишину ядро калибрует
текущий выход заново, перезаписывает кэш и удаляет файл.

Сторож микрофона (mic_watch): блоков с входа нет дольше STALL_SECS - строка в лог
и событие lost в mic.jsonl (тикер шлёт его наблюдателю как kind mic). Пока входа нет, раз в POLL_SECS
смотрю у winmm список входов; нужный появился - PortAudio перечитывается, вход и колонки
переоткрываются без перезапуска процесса, задержка AEC выставляется заново. Старт без
микрофона не падает: ядро ждёт устройство тем же опросом, колонки работают.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from collections import deque
from math import gcd
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd
from scipy.signal import chirp, firwin, lfilter, resample_poly

from .bus import HEARING_FLAG, MIC_EVENTS, SPEAKING_FLAG, append_jsonl, now_iso
from .config import CALIBRATION_FILE
from .mic_watch import MicWatch, find_input, same_device

RATE = 48_000
FRAME = 480                      # 10 мс - APM принимает только такие кадры
BLOCK = 1_920                    # 40 мс на поток: на MME блок 10 мс не успевает и звук рвётся
OUT_RATE = 16_000                # слуху и Whisper
OUT_BLOCK = 1_600                # 100 мс на 16 кГц, как прежний BLOCK

CAL_FILE = CALIBRATION_FILE
CAL_FLAG = "calibrate"           # файл в папке шины: перекалибровать колонки
CAL_SECS = 0.5                   # длина свипа
CAL_LEVEL = 3_000                # амплитуда свипа, ~-21 дБ от полной: тихо, но слышно
CAL_MIN_MS, CAL_MAX_MS = 20, 600 # правдоподобная задержка; вне этого - не верю замеру
CAL_MIN_CORR = 0.2               # слабее этого совпадение считаю шумом
DEVICE_POLL = 2.0                # как часто проверять системный выход
MIC_WATCH_TICK = 0.25            # шаг сторожа микрофона: точность порога тишины потока, нагрузки нет
WIND_FLOOR = 0.25                # тише этого договаривание не становится до самой остановки
# Паузы в речи Кэма (замер 8 окт 23:14, порог 0,12 от громкости): внутри фраз тишина 20-120 мс
# постоянно - смычки согласных, на запятых 300-570 мс, на точке 1,4 с. Пауза 30 мс резала слово
# на слух, поэтому сначала ищу знак препинания, а паузу между словами беру только длинную.
WIND_EXTRA_SECS = 3.0            # столько после запаса ищу, где замолчать, потом гашу как есть
WIND_PAUSE_RATIO = 0.12          # кадр тише этой доли от громкости речи - тишина
WIND_PAUSE_FRAMES = 8            # пауза между словами - 80 мс тишины: длиннее почти всех смычек
WIND_MARK_FRAMES = 20            # 200 мс и длиннее - запятая или точка
WIND_MARK_REACH_SECS = 0.4       # запятая или точка через одно слово - договорить до неё: три слова,
                                 # со знаком четыре
WIND_FINISH_SECS = 0.6           # до конца фразы меньше этого (одно слово) - договорить до точки (23:06)


class WindDown:
    """Договаривание после перебивания: ещё три слова, каждое тише предыдущего,
    и тишина; замолкать только между словами - оборвать слово на середине нельзя.

    Считает сэмплы с начала: громкость падает по прямой от gain до level к концу keep и дальше тем же
    наклоном, но не ниже WIND_FLOOR; после keep ищет паузу между
    словами по энергии кадров 10 мс. Если в пределах WIND_MARK_REACH_SECS дальше запятая или точка
    (тишина от 200 мс) - замолкает на ней, иначе в паузе от 80 мс. Ничего нет за extra - затухание.
    Звук сверх этого не нужен: allowance режет новые куски, поэтому потоковый синтез может
    докармливать ядро, не выходя за запас."""

    def __init__(self, gain: float, level: float, keep: int, fade: int, extra: int,
                 frame: int = FRAME) -> None:
        self.gain, self.level = gain, level
        self.keep, self.fade, self.extra = keep, fade, extra
        self.frame = frame
        self.pos = 0          # сколько уже сыграно с начала договаривания
        self.queued = 0       # сколько уже принято в очередь с начала
        self.loud = 0.0       # громкость речи: максимум RMS кадров, от неё мерится пауза
        self.quiet = 0        # тихих кадров подряд после keep
        self.stop_at: int | None = None  # позиция, с которой тишина
        self.to_end = False   # договариваю до конца фразы: паузу не ищу, не гашу

    @property
    def total(self) -> int:
        return self.keep + self.extra + self.fade

    @property
    def finished(self) -> bool:
        return self.pos >= (self.stop_at if self.stop_at is not None else self.total)

    def finish_phrase(self, rest: int) -> None:
        """До конца фразы осталось rest сэмплов, меньше слова: договорить её целиком."""
        self.to_end, self.stop_at = True, None
        self.extra = max(self.extra, self.pos + rest - self.keep)

    def allowance(self, n: int) -> int:
        """Сколько из n новых сэмплов ещё принять в очередь."""
        k = max(0, min(n, self.total - self.queued))
        self.queued += k
        return k

    def _gain_at(self, idx: np.ndarray) -> np.ndarray:
        slope = (self.gain - self.level) / max(self.keep, 1)
        env = np.maximum(WIND_FLOOR, self.gain - slope * idx).astype(np.float32)
        late = self.keep + self.extra  # паузы не нашлось: гашу
        f = idx >= late
        env[f] *= np.clip(1.0 - (idx[f] - late) / max(self.fade, 1), 0.0, 1.0)
        if self.stop_at is not None:
            env[idx >= self.stop_at] = 0.0
        return env

    def _rms(self, audio: np.ndarray) -> list[float]:
        return [float(np.sqrt(np.mean(audio[i:i + self.frame].astype(np.float32) ** 2)))
                for i in range(0, len(audio) - self.frame + 1, self.frame)]

    def _quiet_runs(self, levels: list[float], need: int, limit: int) -> int | None:
        """Номер кадра, с которого начинается тишина длиной need кадров, не дальше limit."""
        run = 0
        for i, rms in enumerate(levels[:limit + need]):
            run = run + 1 if rms < WIND_PAUSE_RATIO * self.loud else 0
            if run >= need and i - need + 1 <= limit:
                return i - need + 1
        return None

    def _find_pause(self, audio: np.ndarray, start: int, ahead: np.ndarray) -> None:
        """Пройти кадры audio (позиция первого - start) и отметить, где замолчать после keep.
        ahead - звук, уже лежащий в очереди за audio: в нём ищу ближайшую запятую или точку."""
        levels = self._rms(audio)
        for i, rms in enumerate(levels):
            at = start + i * self.frame
            if at < self.keep:
                self.loud = max(self.loud, rms)
                continue
            self.quiet = self.quiet + 1 if rms < WIND_PAUSE_RATIO * self.loud else 0
            if self.quiet < WIND_PAUSE_FRAMES:
                continue
            word_gap = at - (WIND_PAUSE_FRAMES - 1) * self.frame
            # тишина могла начаться в прошлом куске: знак ищу с начала этого
            first = max(0, i - WIND_PAUSE_FRAMES + 1)
            levels_ahead = levels[first:] + self._rms(ahead)
            mark = self._quiet_runs(levels_ahead, WIND_MARK_FRAMES, int(WIND_MARK_REACH_SECS * RATE) // self.frame)
            if mark is None:
                self.stop_at = word_gap
            else:  # до запятой или точки недалеко: договорить до неё, запас растёт
                self.stop_at = max(word_gap, start + (first + mark) * self.frame)
                self.extra = max(self.extra, self.stop_at - self.keep)
            return

    def apply(self, audio: np.ndarray, ahead: np.ndarray | None = None) -> np.ndarray:
        """Огибающая для следующего куска звука колонок; ahead - что лежит в очереди за ним."""
        if self.stop_at is None and not self.to_end:
            self._find_pause(audio, self.pos, ahead if ahead is not None else np.zeros(0, np.int16))
        idx = np.arange(self.pos, self.pos + len(audio), dtype=np.float32)
        self.pos += len(audio)
        return (audio.astype(np.float32) * self._gain_at(idx)).astype(np.int16)


def cal_signal() -> np.ndarray:
    """Свип 300-5000 Гц с плавными краями: острый пик корреляции и без щелчков."""
    t = np.arange(int(CAL_SECS * RATE)) / RATE
    x = chirp(t, 300, CAL_SECS, 5_000)
    edge = RATE // 50
    env = np.ones_like(x)
    env[:edge] = np.linspace(0, 1, edge)
    env[-edge:] = np.linspace(1, 0, edge)
    return (x * env * CAL_LEVEL).astype(np.int16)


def find_lag(out: np.ndarray, mic: np.ndarray, offset_s: float,
             max_s: float = 1.0) -> tuple[float, float]:
    """Задержка эха в мс и сила совпадения 0..1 по взаимной корреляции.

    out и mic - непрерывные записи колонок и сырого микрофона; offset_s - насколько начало
    mic позже начала out по часам. Сдвиг ищу только в пределах 0..max_s."""
    out = out.astype(np.float64)
    mic = mic.astype(np.float64) - float(np.mean(mic))
    nfft = 1 << (len(out) + len(mic) - 1).bit_length()
    # xc[l] = сумма mic[i + l] * out[i]: эхо сэмпла out[i] лежит в mic[i + l]
    xc = np.fft.irfft(np.fft.rfft(mic, nfft) * np.conj(np.fft.rfft(out, nfft)), nfft)
    lags = np.arange(nfft)
    lags[lags > nfft // 2] -= nfft
    delay = lags / RATE + offset_s
    xc[(delay < 0) | (delay > max_s)] = 0.0
    best = int(np.argmax(np.abs(xc)))   # колонка может перевернуть фазу: берём модуль
    lag = int(lags[best])
    # сила: нормированная корреляция на участке, где в колонках был звук
    nz = np.flatnonzero(out)
    if not len(nz):
        return 0.0, 0.0
    a, b = max(nz[0], -lag), min(nz[-1] + 1, len(mic) - lag)
    if b <= a:
        return delay[best] * 1000, 0.0
    o, m = out[a:b], mic[a + lag:b + lag]
    den = float(np.sqrt(np.dot(o, o) * np.dot(m, m)))
    return delay[best] * 1000, abs(float(np.dot(o, m))) / den if den else 0.0


def _join(blocks: list[tuple[float, np.ndarray]]) -> tuple[np.ndarray, float]:
    """Блоки колбэка (время, сэмплы) в одну запись и время её начала. Начало - медиана
    по всем блокам: время одного колбэка дрожит на миллисекунды."""
    if not blocks:
        return np.zeros(0, np.int16), 0.0
    starts = np.cumsum([0] + [len(b) for _, b in blocks[:-1]])
    t0 = float(np.median([t - n / RATE for (t, _), n in zip(blocks, starts)]))
    return np.concatenate([b for _, b in blocks]), t0


def default_output_name() -> str | None:
    """Системный выход по умолчанию сейчас, у winmm: PortAudio помнит его на момент запуска,
    а перечитать список PortAudio можно только вместе с остановкой всех потоков."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Caps(ctypes.Structure):
            _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                        ("vDriverVersion", wintypes.UINT), ("szPname", wintypes.WCHAR * 32),
                        ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
                        ("wReserved1", wintypes.WORD), ("dwSupport", wintypes.DWORD)]

        w = ctypes.windll.winmm
        w.waveOutMessage.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
        w.waveOutMessage.restype = wintypes.UINT
        w.waveOutGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(Caps), wintypes.UINT]
        dev, flags = wintypes.DWORD(), wintypes.DWORD()
        # WAVE_MAPPER, DRVM_MAPPER_PREFERRED_GET: какое устройство сейчас за «по умолчанию»
        if w.waveOutMessage(ctypes.c_void_p(0xFFFFFFFF), 0x2000 + 21,
                            ctypes.addressof(dev), ctypes.addressof(flags)):
            return None
        caps = Caps()
        if w.waveOutGetDevCapsW(dev.value, ctypes.byref(caps), ctypes.sizeof(caps)):
            return None
        return caps.szPname
    except Exception:
        return None


_same = same_device


def input_names() -> list[str] | None:
    """Входы, которые система видит сейчас, у winmm (список PortAudio застыл на момент запуска).
    None - не Windows или winmm не ответил: список не узнать."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Caps(ctypes.Structure):  # WAVEINCAPSW
            _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                        ("vDriverVersion", wintypes.UINT), ("szPname", wintypes.WCHAR * 32),
                        ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
                        ("wReserved1", wintypes.WORD)]

        w = ctypes.windll.winmm
        w.waveInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(Caps), wintypes.UINT]
        names = []
        for i in range(w.waveInGetNumDevs()):
            caps = Caps()
            if not w.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)):
                names.append(caps.szPname)
        return names
    except Exception:
        return None


def _load_cache() -> dict:
    try:
        return json.loads(CAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


class AudioCore:
    def __init__(self, device_in: int | None = None, device_out: int | None = None,
                 aec: bool = True, pick_in: Callable[[], int | None] | None = None) -> None:
        """pick_in - как заново выбрать вход, когда устройство вернулось (hearing.pick_device);
        без него - системный по умолчанию."""
        self.apm = None
        if aec:
            try:
                from livekit import rtc
                self._rtc = rtc
                self.apm = rtc.AudioProcessingModule(
                    echo_cancellation=True, noise_suppression=False, high_pass_filter=True)  # шумодав срезал его голос при двойной речи
            except Exception as exc:
                print(f"[звук] AEC недоступен, работаю без него: {exc}", flush=True)
        self.mic_q: queue.Queue[np.ndarray] = queue.Queue()
        # Копия каждого чистого блока для раздачи подписчикам узла (vmic.FramePublisher, ADR-017); None - не раздаём
        self.mic_tap: Callable[[np.ndarray], None] | None = None
        self._play: deque[np.ndarray] = deque()
        self._lock = threading.Lock()
        # понижение 48 -> 16 кГц: ФНЧ с сохранением состояния между кадрами, затем каждый 3-й
        self._fir = firwin(63, 7_600, fs=RATE)
        self._zi = np.zeros(len(self._fir) - 1)
        self._acc = np.zeros(0, dtype=np.int16)
        # громкость колонок: текущая и целевая, переход плавный по блокам (без щелчков)
        self.gain = 1.0
        self.target = 1.0
        self.ramp = 0.15                 # шаг громкости за блок 40 мс (~0,3 с на притихание)
        self.fade_stop = False           # дойти до нуля и очистить очередь
        self._wind: WindDown | None = None  # идёт договаривание после перебивания
        self._phrase_end = False         # весь звук фразы уже в очереди (end_phrase): конец известен
        self.tap = None                  # получает каждый блок колонок (recorder.OutputRecorder.feed)
        self.underflows = 0              # обрывы колонок
        self.overflows = 0               # переполнения микрофона
        self.raw_rms = 0.0               # громкость микрофона до AEC (для замеров)
        self.raw_peak = 0.0              # максимум за окно до AEC - для лога диагностики
        self.clean_peak = 0.0            # то же после AEC
        self.clean_rms = 0.0             # после AEC
        self.delay_ms = 0                # задержка, выставленная AEC
        self._cal: dict | None = None    # идёт калибровка: сюда пишутся блоки колонок и микрофона
        self._closed = False
        self._follow = device_out is None  # системный выход: идти за ним при смене
        self._device_in = device_in
        self._pick_in = pick_in or (lambda: None)
        self._dev_lock = threading.RLock()  # переоткрытие потоков: сторож колонок и сторож микрофона
        self._in = self._try_open_in(device_in)
        self.mic_watch = MicWatch(live=self._in is not None)
        if self._in is None:
            want = f"устройство {device_in}" if device_in is not None else "вход по умолчанию"
            print(f"[слух] нет микрофона ({want}); жду устройство", flush=True)
        self._out = self._open_out(device_out)
        self._in_name = self.input_name
        self.out_name = sd.query_devices(self._out.device, "output")["name"]
        self._hostapi = sd.query_devices(self._out.device)["hostapi"]
        self._missing = ""               # выход, которого нет и после перечитывания списка

    def _open_in(self, device: int | None) -> sd.InputStream:
        return sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK,
                              device=device, callback=self._on_in)

    def _try_open_in(self, device: int | None) -> sd.InputStream | None:
        """Вход или None, если устройства нет (PortAudio: Error querying device -1)."""
        try:
            return self._open_in(device)
        except (sd.PortAudioError, ValueError):
            return None

    def _open_out(self, device: int | None) -> sd.OutputStream:
        return sd.OutputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK,
                               device=device, callback=self._on_out)

    def start(self) -> None:
        if self._in:
            self._in.start()
        self._out.start()
        if self.apm and self._in:  # без микрофона эха нет: задержку выставит переоткрытие входа
            self._set_delay()
            print(f"[звук] AEC включён, задержка {self.delay_ms} мс", flush=True)

    def watch(self, d: Path) -> None:
        """Следить за сменой системного выхода и флагом calibrate в папке шины d."""
        threading.Thread(target=self._watch, args=(d,), daemon=True, name="core-watch").start()
        threading.Thread(target=self._watch_mic, args=(d,), daemon=True, name="mic-watch").start()

    def close(self) -> None:
        """Сначала остановить потоки, потом отпускать AEC: иначе колбэк зовёт уже
        уничтоженный модуль (замер 8 окт: «handle not found» при выходе)."""
        self._closed = True
        for st in (self._in, self._out):
            if st is None:
                continue
            try:
                st.stop()
                st.close()
            except Exception:
                pass
        self.apm = None

    @property
    def input_name(self) -> str:
        """Имя открытого входа; пустое - микрофона нет."""
        return sd.query_devices(self._in.device, "input")["name"] if self._in else ""

    # --- калибровка колонок ----------------------------------------------------------------

    def _reported_delay(self) -> int:
        return int(((self._in.latency if self._in else 0.0) + self._out.latency) * 1000)

    def _set_delay(self, force: bool = False) -> None:
        """Задержка AEC для текущего выхода: из кэша, иначе (или по force) замером."""
        if not self.apm:
            return
        hit = _load_cache().get(self.out_name)
        if hit and not force:
            delay = int(hit["delay_ms"])
            print(f"[звук] колонки {self.out_name}: задержка {delay} мс из кэша", flush=True)
        else:
            delay = self.calibrate()
        self.apm.set_stream_delay_ms(delay)
        self.delay_ms = delay

    def calibrate(self) -> int:
        """Сыграть свип, записать сырой микрофон, найти сдвиг эха. Возвращает задержку в мс;
        удачный замер пишет в кэш. Микрофон на это время отдаётся слуху тишиной."""
        reported = self._reported_delay()
        rec: dict = {"out": [], "in": []}
        self._cal = rec
        time.sleep(0.1)                                   # набрать немного тишины до свипа
        self.play(cal_signal(), RATE)
        until = time.monotonic() + CAL_SECS + 2.0
        while self.busy() and time.monotonic() < until:
            time.sleep(0.02)
        time.sleep(CAL_MAX_MS / 1000 + 0.2)               # дождаться самого позднего эха
        self._cal = None
        out, t_out = _join(rec["out"])
        mic, t_in = _join(rec["in"])
        lag, corr = find_lag(out, mic, t_in - t_out)
        ok = corr >= CAL_MIN_CORR and CAL_MIN_MS <= lag <= CAL_MAX_MS
        delay = int(round(lag)) if ok else reported
        note = "" if ok else f", не верю замеру ({lag:.0f} мс), беру заявленную {reported} мс"
        print(f"[звук] калибровка колонок: {self.out_name} -> {delay} мс (корреляция {corr:.2f}){note}",
              flush=True)
        if ok:
            cache = _load_cache()
            cache[self.out_name] = {"delay_ms": delay, "corr": round(corr, 3),
                                    "reported_ms": reported, "at": now_iso()}
            try:
                CAL_FILE.parent.mkdir(parents=True, exist_ok=True)
                CAL_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                print(f"[звук] кэш калибровки не записан: {exc}", flush=True)
        return delay

    # --- смена выхода ----------------------------------------------------------------------

    def _idle(self, d: Path) -> bool:
        """Никто не говорит: ни я (очередь колонок, флаг speaking), ни он (hearing)."""
        return not (self.busy() or (d / SPEAKING_FLAG).exists() or (d / HEARING_FLAG).exists())

    def _watch(self, d: Path) -> None:
        flag = d / CAL_FLAG
        while not self._closed:
            time.sleep(DEVICE_POLL)
            if self._closed or not self._idle(d):
                continue
            try:
                name = default_output_name() if self._follow else None
                if name and not _same(name, self.out_name) and name != self._missing:
                    with self._dev_lock:
                        self._switch_output(name)
                elif flag.exists():
                    flag.unlink(missing_ok=True)
                    if self.apm:
                        self._set_delay(force=True)
                    else:
                        print("[звук] калибровка не нужна: AEC выключен", flush=True)
            except Exception as exc:  # наблюдатель не должен ронять звук
                print(f"[звук] наблюдатель колонок: {exc}", flush=True)

    def _find_out(self, name: str) -> int | None:
        for dev in sd.query_devices():
            if (dev["hostapi"] == self._hostapi and dev["max_output_channels"] > 0
                    and _same(dev["name"], name)):
                return dev["index"]
        return None

    def _switch_output(self, name: str) -> None:
        """Переоткрыть колонки на выходе name. Устройство из списка PortAudio - меняется только
        поток колонок; нового в списке нет, тогда PortAudio перезапускается целиком
        (sd._terminate закрывает все потоки), и микрофон переоткрывается тоже."""
        idx = self._find_out(name)
        if idx is None:
            print(f"[звук] выход {name} новый, перечитываю устройства (микрофон моргнёт)", flush=True)
            self._reinit()
            idx = self._find_out(name)
            if idx is None:
                self.out_name = sd.query_devices(self._out.device, "output")["name"]
                print(f"[звук] выход {name} не найден, играю в {self.out_name}", flush=True)
                self._missing = name
                self._set_delay()
                return
        old = self._out.device
        try:
            self._out.stop()
            self._out.close()
        except Exception:
            pass
        try:
            self._out = self._open_out(idx)
            self._out.start()
        except Exception as exc:  # устройство в списке, но не открылось: вернуться на прежнее
            print(f"[звук] выход {name} не открылся ({exc}), остаюсь на {self.out_name}", flush=True)
            self._missing = name
            self._out = self._open_out(old)
            self._out.start()
            return
        self.out_name, self._missing = sd.query_devices(idx)["name"], ""
        print(f"[звук] колонки переключены на {self.out_name}", flush=True)
        self._set_delay()

    def _reinit(self) -> None:
        """Перечитать устройства PortAudio: все потоки закрываются, вход открывается заново,
        колонки - временно на системный выход (вызывающий переоткроет на нужный)."""
        for st in (self._in, self._out):
            if st is None:
                continue
            try:
                st.stop()
                st.close()
            except Exception:
                pass
        sd._terminate()
        sd._initialize()
        self._in = self._try_open_in(self._input_index())
        if self._in:
            self._in.start()
            self._in_name = self.input_name
        self.mic_watch.restart()
        self._out = self._open_out(None)
        self._out.start()

    def _input_index(self) -> int | None:
        """Вход после перечитывания: выбранный по номеру ищу по прежнему имени (индексы могли
        сдвинуться), иначе выбираю заново (pick_in: MIC_DEVICE или системный)."""
        if self._device_in is not None and self._in_name:
            return next((d["index"] for d in sd.query_devices()
                         if d["max_input_channels"] > 0 and _same(d["name"], self._in_name)), None)
        return self._pick_in()

    # --- микрофон: сторож ------------------------------------------------------------------

    def _mic_event(self, d: Path, status: str, text: str) -> None:
        print(f"[слух] {text}", flush=True)
        append_jsonl(d / MIC_EVENTS, {"status": status, "device": self._in_name, "text": text})

    def _watch_mic(self, d: Path) -> None:
        """Поток замолчал - сказать; ждём - опрашивать список и переоткрыть, когда устройство есть.
        Старт без микрофона наблюдатель тоже узнаёт сразу: строка в лог уже была в __init__."""
        if self.mic_watch.waiting:
            append_jsonl(d / MIC_EVENTS, {"status": "missing", "device": "",
                                          "text": "нет микрофона; жду устройство"})
        while not self._closed:
            time.sleep(MIC_WATCH_TICK)
            try:
                self._mic_step(d)
            except Exception as exc:  # сторож не должен ронять звук
                print(f"[слух] сторож микрофона: {exc}", flush=True)

    def _mic_step(self, d: Path) -> None:
        w = self.mic_watch
        if w.stalled():
            self._acc = np.zeros(0, dtype=np.int16)
            self._mic_event(d, "lost", f"микрофон замолчал: {self._in_name or '?'}; жду устройство")
        elif w.resumed():
            self._mic_event(d, "back", f"микрофон вернулся: {self._in_name}")
        elif w.retry_due() and not self.busy():  # перечитывание закрывает колонки: не посреди речи
            if find_input(input_names(), self._in_name or None) is not None:
                self._reopen_mic()

    def _reopen_mic(self) -> None:
        """Нужный вход снова в списке системы: перечитать PortAudio, открыть вход и колонки на
        прежнем выходе, выставить задержку AEC. «Вернулся» скажет resumed() по первому блоку."""
        with self._dev_lock:
            self._reinit()
            idx = self._find_out(self.out_name)
            if idx is not None and idx != self._out.device:
                try:
                    self._out.stop()
                    self._out.close()
                except Exception:
                    pass
                self._out = self._open_out(idx)
                self._out.start()
            self.out_name = sd.query_devices(self._out.device, "output")["name"]
            if self._in:
                self._set_delay()

    # --- колонки ---------------------------------------------------------------------------

    def play(self, audio: np.ndarray, rate: int) -> None:
        """Поставить звук в очередь колонок (любая частота, int16 моно). Куски одной фразы можно
        дописывать по мере синтеза: они играют подряд. Ресемплинг здесь - для целого буфера;
        поток кусков приходит уже в RATE (audio_stream.Resampler), иначе стыки щёлкают."""
        if rate != RATE:
            g = gcd(RATE, rate)
            audio = resample_poly(audio.astype(np.float32), RATE // g, rate // g)
            audio = np.clip(audio, -32768, 32767).astype(np.int16)
        with self._lock:
            if self._wind and self._phrase_end and not self._play:
                self._wind = None   # фраза доиграна, колбэк ещё не снял её договаривание: новая звучит целиком (SAM-180)
            if self._wind:
                audio = audio[:self._wind.allowance(len(audio))]
            if len(audio):
                self._play.append(audio)
                self._phrase_end = False

    def end_phrase(self) -> None:
        """Последний кусок фразы уже в очереди: договаривание видит, сколько до точки."""
        with self._lock:
            self._phrase_end = True

    def duck(self, level: float) -> None:
        """Плавно перейти к громкости level (притихнуть или вернуть)."""
        with self._lock:
            if not self.fade_stop:
                self.target, self.ramp = level, 0.15

    def fade_out(self, seconds: float) -> None:
        """Погасить звук за seconds и остановить (договорить пару слов затихая)."""
        with self._lock:
            self.target, self.fade_stop = 0.0, True
            self.ramp = max(self.gain, 0.05) * BLOCK / RATE / max(seconds, 0.1)

    def wind_down(self, keep_secs: float, fade_secs: float, level: float = 0.4) -> None:
        """Договорить после перебивания: ещё keep_secs, затихая к level, и замолчать в ближайшей паузе
        между словами (WindDown); паузы нет - затухание за fade_secs. Уже готовый звук и куски,
        которые синтез допишет потом, звучат в пределах запаса; фраза кончилась раньше -
        договаривание кончается вместе с ней."""
        with self._lock:
            wind = WindDown(self.gain, level, int(keep_secs * RATE), int(fade_secs * RATE),
                            int(WIND_EXTRA_SECS * RATE))
            rest = np.concatenate(list(self._play)) if self._play else np.zeros(0, np.int16)
            rest = rest[:wind.allowance(len(rest))]
            self._play.clear()
            if len(rest):
                self._play.append(rest)
            self._wind = wind
            self.gain, self.target, self.fade_stop = 1.0, 1.0, False  # громкость ведёт огибающая

    @property
    def winding(self) -> bool:
        """Идёт договаривание после перебивания и запас ещё не исчерпан."""
        with self._lock:
            return self._wind is not None

    def busy(self) -> bool:
        with self._lock:
            return bool(self._play)

    def stop(self) -> None:
        with self._lock:
            self._play.clear()
            self._wind = None
            self._phrase_end = False
            self.gain, self.target, self.fade_stop = 1.0, 1.0, False

    def _peek(self, n: int) -> np.ndarray:
        """Первые n сэмплов очереди колонок, не забирая их (вызывать под замком)."""
        parts, got = [], 0
        for a in self._play:
            if got >= n:
                break
            parts.append(a[:n - got])
            got += len(parts[-1])
        return np.concatenate(parts) if parts else np.zeros(0, np.int16)

    def _take(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.int16)
        got = 0
        with self._lock:
            while got < n and self._play:
                head = self._play[0]
                k = min(n - got, len(head))
                out[got:got + k] = head[:k]
                got += k
                if k == len(head):
                    self._play.popleft()
                else:
                    self._play[0] = head[k:]
        return out

    def _on_out(self, outdata, frames, t, status) -> None:
        if status.output_underflow:
            self.underflows += 1
        chunk = self._take(frames)
        with self._lock:
            wind = self._wind
            if wind:
                if self._phrase_end and not wind.to_end and wind.stop_at is None:
                    rest = sum(len(a) for a in self._play)
                    if rest <= WIND_FINISH_SECS * RATE:
                        wind.finish_phrase(rest)
                chunk = wind.apply(chunk, self._peek(int(WIND_MARK_REACH_SECS * RATE) + WIND_MARK_FRAMES * FRAME))
                # запас договорён или фраза доиграна раньше запаса: договаривание кончается с ней, иначе следующая
                # реплика звучит под его огибающей и гаснет посреди слова (SAM-180)
                if wind.finished or (self._phrase_end and not self._play):
                    self._play.clear()
                    self._wind = None
            g0 = self.gain
            step = self.target - g0
            g1 = g0 + max(-self.ramp, min(self.ramp, step))
            self.gain = g1
            if self.fade_stop and g1 <= 0.001:
                self._play.clear()
                self.gain, self.target, self.fade_stop = 1.0, 1.0, False
        if g0 != 1.0 or g1 != 1.0:
            chunk = (chunk.astype(np.float32) * np.linspace(g0, g1, frames)).astype(np.int16)
        outdata[:, 0] = chunk
        if self.tap:
            self.tap(chunk)
        cal = self._cal
        if cal is not None:
            cal["out"].append((time.monotonic(), chunk.copy()))
        if self.apm:
            for i in range(0, frames - FRAME + 1, FRAME):
                part = chunk[i:i + FRAME]
                self.apm.process_reverse_stream(self._rtc.AudioFrame(part.tobytes(), RATE, 1, FRAME))

    # --- микрофон --------------------------------------------------------------------------

    def _on_in(self, indata, frames, t, status) -> None:
        self.mic_watch.block()
        if status.input_overflow:
            self.overflows += 1
        x = indata[:, 0].copy()
        cal = self._cal
        if cal is not None:  # калибровке нужен сырой микрофон, до AEC
            cal["in"].append((time.monotonic(), x.copy()))
        self.raw_rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2)))
        if self.apm:
            for i in range(0, frames - FRAME + 1, FRAME):
                frame = self._rtc.AudioFrame(x[i:i + FRAME].tobytes(), RATE, 1, FRAME)
                self.apm.process_stream(frame)
                x[i:i + FRAME] = np.frombuffer(frame.data, dtype=np.int16)
        if cal is not None:
            x[:] = 0  # свип слуху не отдаю: ни Whisper, ни перебивание его не услышат
        self.clean_rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2)))
        self.raw_peak = max(self.raw_peak, self.raw_rms)
        self.clean_peak = max(self.clean_peak, self.clean_rms)
        y, self._zi = lfilter(self._fir, 1.0, x.astype(np.float32), zi=self._zi)
        y = np.clip(y[::3], -32768, 32767).astype(np.int16)
        self._acc = np.concatenate([self._acc, y])
        while len(self._acc) >= OUT_BLOCK:
            block = self._acc[:OUT_BLOCK]
            self.mic_q.put(block)
            if self.mic_tap:
                self.mic_tap(block)
            self._acc = self._acc[OUT_BLOCK:]
