"""Мост голосового слоя к gateway (ADR-012, SAM-09): voice-in.jsonl -> /voice/turn -> say-out.jsonl.

  python -m voice.layer.bridge [папка-шины] [--url ws://127.0.0.1:8000/voice/turn] [--session voice]

Законченная фраза человека (не эхо, не кусок по ходу речи) уходит в gateway; дельты ответа
собираются в предложения, каждое пишется строкой say-out сразу, и озвучка (say_out) говорит первое,
пока модель пишет следующее. Поставщик сменился посреди ответа (reset) - следующее предложение
идёт с interrupt: начатое старым поставщиком обрывается, очередь сбрасывается.
Строка say-out несёт источник ответа (from): быстрая модель или основная, по сообщению source хода;
поправка в say_out отзывает только реплики быстрой.

После хода мост ждёт судьбу каждого своего предложения в said.jsonl (played, cut, dropped, stale ...)
и отчитывается gateway сообщением spoken: в историю идёт только прозвучавшее. Судьба не
пришла за FATE_TIMEOUT_S - предложение в отчёте с судьбой unknown.

Своя реплика SAM (say с id: итог воркера, heartbeat; SAM-85) звучит из очереди своих реплик в паузе,
не по порядку ответов. OwnReplies ждёт её судьбу отдельно и отчитывается spoken с тем же id; не взятая
в озвучку до срока очереди - stale, в историю не идёт.

Логика решения отделена от сети и файлов: TurnSpeech получает сообщения протокола и отдаёт строки
say-out, FateBook собирает судьбы; run_bridge только читает, шлёт и пишет.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from gateway.access import auth_headers  # ключ SAM_API_KEY (SAM-79)
from gateway.env import env_float
from gateway import quiet_mode, stand_watch_keeper

from . import agenda, health, radar_feed
from .bus import SAID, SAY_OUT, VOICE_IN, append_jsonl, bus_dir, tail_jsonl
from .stress import plain
from .turns import FAST, MAIN, STALE
from .speech_text import Sentences, VoiceText

GATEWAY_URL = os.getenv("SAM_VOICE_TURN_URL", "ws://127.0.0.1:8000/voice/turn")
SESSION = os.getenv("SAM_VOICE_SESSION", "voice")
# Кто ответил на ход, gateway говорит сообщением source до дельт (gateway/voice_turn.py SOURCE_*). Поправка
# отзывает только реплики быстрой; без сообщения (старый gateway) ответ - основной модели, чтобы поправка
# не отозвала его зря.
SOURCES = {"fast": FAST, "main": MAIN}
RECONNECT_S = env_float("SAM_BRIDGE_RECONNECT_S", 2.0)  # gateway перезапускается - ждём и снова
# Сколько ждать судьбу предложения после конца хода: озвучка длинного ответа идёт минуту. Меньше срока
# gateway (SAM_SPOKEN_REPORT_TIMEOUT_S, 120), чтобы отчёт успел раньше его метки. Не замерено.
FATE_TIMEOUT_S = env_float("SAM_BRIDGE_FATE_TIMEOUT_S", 90.0)
UNKNOWN = "unknown"
OWN_CHECK_S = 1.0         # как часто проверять срок своих реплик, пока судеб нет


def is_phrase(rec: dict) -> bool:
    """В gateway уходит законченная фраза человека: без эха своей озвучки и кусков по ходу речи."""
    return bool(str(rec.get("text") or "").strip()) and not rec.get("echo") and not rec.get("partial")


class FateBook:
    """Судьбы предложений одного ответа из said.jsonl. Сверка по тексту без ударений, как пишет say_out."""

    def __init__(self, sentences: list[str]) -> None:
        self.sentences = list(sentences)
        self.status: dict[int, str] = {}

    def record(self, text: str, status: str) -> bool:
        """Судьба пришла; True - она про предложение этого ответа, ещё без судьбы."""
        key = plain(text).strip()
        for i, s in enumerate(self.sentences):
            if i not in self.status and plain(s).strip() == key:
                self.status[i] = status
                return True
        return False

    @property
    def complete(self) -> bool:
        return len(self.status) == len(self.sentences)

    def report(self) -> list[dict]:
        return [{"text": s, "status": self.status.get(i, UNKNOWN)} for i, s in enumerate(self.sentences)]


class TurnSpeech:
    """Сообщения /voice/turn одного хода -> строки say-out."""

    def __init__(self, reply_to: str | None = None) -> None:
        self.reply_to = reply_to
        self.source = MAIN                # чей ответ: сообщение source хода, до него - основная модель
        self.written: list[str] = []      # всё, что ушло в say-out за ход, и до reset тоже
        self._reset_state()
        self._interrupt_next = False

    def _reset_state(self) -> None:
        self.voice, self.sentences = VoiceText(), Sentences()

    def on_message(self, msg: dict) -> list[dict]:
        kind = msg.get("type")
        if kind == "delta":
            return self._lines(self.sentences.push(self.voice.feed(msg.get("text", ""))))
        if kind == "source":
            self.source = SOURCES.get(str(msg.get("source") or ""), MAIN)
            return []
        if kind == "reset":
            self._reset_state()
            self._interrupt_next = True
            return []
        if kind == "done":
            tail = self.sentences.push(self.voice.finish(str(msg.get("voice") or "")))
            return self._lines(tail + self.sentences.flush())
        return []

    def _lines(self, sentences: list[str]) -> list[dict]:
        out = []
        for s in sentences:
            line = {"text": s, "from": self.source, "reply_to": self.reply_to}
            if self._interrupt_next:
                line["interrupt"], self._interrupt_next = True, False
            out.append(line)
            self.written.append(s)
        return out


def fix_lines(msg: dict, reply_to: str | None) -> list[dict]:
    """Поправка основной модели: по предложению, с fix - say_out отзывает реплики быстрой."""
    s = Sentences()
    sentences = s.push(str(msg.get("text") or "") + " ") + s.flush()
    return [{"text": t, "from": MAIN, "reply_to": reply_to, "fix": True} for t in sentences]


def say_ttl(msg: dict) -> float | None:
    """Свой срок реплики say в секундах (поздняя поправка проверки пачкой); нет или мусор - срок очереди."""
    try:
        ttl = float(msg["ttl"])
    except (KeyError, TypeError, ValueError):
        return None
    return ttl if ttl > 0 else None


class OwnReplies:
    """Свои реплики say, ждущие судьбы (SAM-85). Судьба в said.jsonl приходит под текстом реплики или
    под путём к её готовому аудио: так её пишет озвучка очереди своих реплик."""

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def add(self, key: str, text: str, aliases: list[str], intent_id: str, deadline: float) -> None:
        names = {plain(a).strip() for a in [text, *aliases]}
        self._open[key] = {"text": text, "names": names, "intent": intent_id, "deadline": deadline}

    def record(self, text: str, status: str) -> dict | None:
        """Судьба пришла; отчёт, если она про одну из ждущих своих реплик."""
        name = plain(text).strip()
        for key, own in self._open.items():
            if name in own["names"]:
                del self._open[key]
                return self._report(key, own["text"], status)
        return None

    def due(self, now: float, taken: Any) -> list[dict]:
        """Отчёты по истёкшим: не взятая в озвучку - stale, взятая без судьбы - unknown."""
        out = []
        for key in [k for k, own in self._open.items() if own["deadline"] <= now]:
            own = self._open.pop(key)
            out.append(self._report(key, own["text"], UNKNOWN if taken(own["intent"]) else STALE))
        return out

    @staticmethod
    def _report(key: str, text: str, status: str) -> dict:
        return {"type": "spoken", "id": key, "sentences": [{"text": text, "status": status}]}


async def run_bridge(d: Path, url: str = GATEWAY_URL, session: str = SESSION) -> None:
    """Переподключаться к gateway, пока процесс жив: мост во вкладке с автоподъёмом."""
    import websockets
    phrases: asyncio.Queue = asyncio.Queue()
    _tail_in_thread(d / VOICE_IN, phrases, asyncio.get_running_loop())
    link: dict = {"up": False}
    health.clear(d / health.BRIDGE_HEALTH)        # health check моста: bridge.health (SAM-195), старый - долой
    health.tick_loop(asyncio.get_running_loop(), link)
    health.start_bridge_beat(d, link, lambda: link["up"])
    while True:
        try:
            async with websockets.connect(url, additional_headers=auth_headers()) as ws:
                print(f"[мост] gateway {url}, сессия {session}", flush=True)
                link["up"] = True
                try:
                    await _serve(ws, d, phrases, session)
                finally:
                    link["up"] = False
        except Exception as exc:  # сеть или gateway: строка и повтор
            print(f"[мост] нет связи с gateway ({exc}); повтор через {RECONNECT_S:g} с", flush=True)
            await asyncio.sleep(RECONNECT_S)


async def _serve(ws: Any, d: Path, phrases: asyncio.Queue, session: str,
                 fates: asyncio.Queue | None = None, fate_timeout_s: float = FATE_TIMEOUT_S,
                 own_ttl_s: float = agenda.DEFAULT_TTL) -> None:
    turn = TurnSpeech()
    if fates is None:
        fates = asyncio.Queue()
        _tail_in_thread(d / SAID, fates, asyncio.get_running_loop(), lambda rec: True)
    books: asyncio.Queue = asyncio.Queue()
    own = OwnReplies()
    turn_fates: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    quiet = quiet_mode.QuietBook(d / quiet_mode.QUIET_FILE)
    radar = radar_feed.build()

    async def _send_phrases() -> None:
        nonlocal turn
        while True:
            rec = await phrases.get()
            radar.heard(rec)                   # голос у узла - свидетельство радару и в тишине: слух работает, молчит только модель
            send, lifted = quiet.gate(session, rec["text"])
            if lifted:
                agenda.add(d, quiet_mode.EXIT_SAY)   # тишина снята: готовая фраза без модели, ответ хода - следом
            if not send:
                print(f"[мост] тихо в {session}: фраза не отправлена ходом", flush=True)
                continue
            turn.reply_to = rec.get("ts")  # ответ относится к последней сказанной фразе
            turn_msg = {"type": "turn", "session": session, "text": rec["text"]}
            if rec.get("ts"):
                turn_msg["at"] = rec["ts"]     # SAM-101: к этому времени фразы шлюз относит найденные факты
            if rec.get("who"):
                turn_msg["who"] = rec["who"]   # опознание голоса: кому сказано
            if rec.get("secret"):
                turn_msg["secret"] = rec["secret"]   # SAM-88: имена названных ключей (слух сами ключи замаскировал)
            await ws.send(json.dumps(turn_msg, ensure_ascii=False))

    async def _send_report(report: dict) -> None:
        await ws.send(json.dumps({**report, "session": session}, ensure_ascii=False))

    sender = asyncio.create_task(_send_phrases())
    reporter = asyncio.create_task(_report_fates(ws, session, books, turn_fates, fate_timeout_s))
    sorter = asyncio.create_task(_sort_fates(fates, own, turn_fates, _send_report,
                                             lambda iid: agenda.is_done(d, iid)))
    try:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "error":
                print(f"[мост] gateway: {msg.get('error')}", flush=True)
            if msg.get("type") == "say" and str(msg.get("text") or "").strip():
                text = msg["text"].strip()
                ttl = say_ttl(msg)          # свой срок: поздняя поправка протухает раньше очереди
                iid = agenda.add(d, text, **({"ttl": ttl} if ttl is not None else {}))   # звучит в паузе (SAM-49)
                if msg.get("id"):           # gateway ждёт её судьбу для истории (SAM-85)
                    own.add(str(msg["id"]), text, [str(agenda.wav_path(d, iid))], iid,
                            loop.time() + (own_ttl_s if ttl is None else ttl) + fate_timeout_s)
                continue
            lines = fix_lines(msg, turn.reply_to) if msg.get("type") == "fix" else turn.on_message(msg)
            for line in lines:
                append_jsonl(d / SAY_OUT, line)
            if msg.get("type") == "done" and turn.written:
                books.put_nowait(FateBook(turn.written))
            if msg.get("type") in ("done", "cancelled", "error"):
                turn = TurnSpeech(turn.reply_to)
    finally:
        # Сокет закрылся: отчёты по уже сказанному слать некуда, их ответы gateway допишет
        # с меткой «судьба неизвестна» по своему сроку. Ждать их здесь - висеть.
        sender.cancel()
        reporter.cancel()
        sorter.cancel()


async def _sort_fates(fates: asyncio.Queue, own: OwnReplies, turn_fates: asyncio.Queue,
                      send: Any, taken: Any) -> None:
    """Судьбы своих реплик - их отчётом сразу, остальные - ответам хода по порядку; истёкшие свои - stale."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            rec = await asyncio.wait_for(fates.get(), OWN_CHECK_S)
        except asyncio.TimeoutError:
            rec = None
        if rec is not None:
            report = own.record(str(rec.get("text") or ""), str(rec.get("status") or UNKNOWN))
            if report:
                await send(report)
            else:
                turn_fates.put_nowait(rec)
        for report in own.due(loop.time(), taken):
            await send(report)


async def _report_fates(ws: Any, session: str, books: asyncio.Queue, fates: asyncio.Queue,
                        timeout_s: float) -> None:
    """Ответы по очереди: ждать судьбы предложений, затем отчёт spoken в gateway."""
    loop = asyncio.get_running_loop()
    while True:
        book: FateBook = await books.get()
        deadline = loop.time() + timeout_s
        while not book.complete and (left := deadline - loop.time()) > 0:
            try:
                rec = await asyncio.wait_for(fates.get(), left)
            except asyncio.TimeoutError:
                break
            book.record(str(rec.get("text") or ""), str(rec.get("status") or UNKNOWN))
        await ws.send(json.dumps({"type": "spoken", "session": session, "sentences": book.report()},
                                 ensure_ascii=False))
        books.task_done()


def _tail_in_thread(path: Path, out: asyncio.Queue, loop: asyncio.AbstractEventLoop,
                    keep=is_phrase) -> None:
    """tail_jsonl блокирующий: читает в потоке, записи отдаёт в event loop."""
    def _run() -> None:
        for rec in tail_jsonl(path):
            if keep(rec):
                loop.call_soon_threadsafe(out.put_nowait, rec)
    threading.Thread(target=_run, daemon=True, name=f"bridge-tail-{path.name}").start()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    p = argparse.ArgumentParser()
    p.add_argument("bus", nargs="?", help="папка шины; по умолчанию SAM_VOICE_BUS_DIR")
    p.add_argument("--url", default=GATEWAY_URL)
    p.add_argument("--session", default=SESSION)
    a = p.parse_args()
    stand_watch_keeper.start("bridge")   # надзор лежит - поднять (SAM-195, холодный старт)
    asyncio.run(run_bridge(bus_dir(a.bus), a.url, a.session))


if __name__ == "__main__":
    main()
