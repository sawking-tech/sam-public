"""Шина голосового слоя: папка с JSONL-потоками и флагами, через которые слух, озвучка и ведущий говорят друг с другом.

Папка - SAM_VOICE_BUS_DIR (config.BUS_DIR) или аргумент запуска. Что в ней:
  voice-in.jsonl    фразы человека после распознавания (hearing.py)
  say-out.jsonl     реплики на озвучку (say_out.py)
  said.jsonl        судьба каждой реплики: played, cut, dropped, dup, stale, retracted
  reply.to          ts последней фразы, отданной быстрому голосу
  ticks.jsonl       копия потока тиков ведущему (ticker.py)
  session.json      голос, скорость и движок синтеза (tts_client.py)
  speaking          флаг: идёт озвучка, микрофон глушится
  mute              флаг: микрофон выключен вручную
  hearing           флаг: человек говорит, озвучка ждёт конца фразы
  barge             флаг: человек перебил озвучку
  hold              флаг: перебить реплику можно только стоп-словом
  mic.jsonl         события микрофона: missing, lost, back (audio_core, mic_watch)
  mic.wav           весь звук с микрофона за запуск
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import BUS_DIR

VOICE_IN = "voice-in.jsonl"
SAY_OUT = "say-out.jsonl"
TICKS = "ticks.jsonl"
SAID = "said.jsonl"        # судьба каждой моей реплики: played, cut, dropped, dup, stale, retracted
REPLY_TO = "reply.to"      # ts последней фразы, отданной быстрому голосу: на неё его следующий ответ
SESSION_JSON = "session.json"
SPEAKING_FLAG = "speaking"
MUTE_FLAG = "mute"
HEARING_FLAG = "hearing"  # человек говорит: озвучка ждёт конца фразы
BARGE_FLAG = "barge"      # он перебил: озвучка обрывается, очередь сбрасывается
HOLD_FLAG = "hold"        # реплика с hold: перебивание только стоп-словом, не громкостью
MIC_WAV = "mic.wav"       # весь звук с микрофона за запуск
LEAD_INBOX = "lead-in.jsonl"  # входящие основной модели Claude Code не из голоса: чат монитора, inject (SAM-91)
LEAD_ALIVE = "lead.alive"  # тикер ведущего отмечается, пока жив: шлюз отдаёт ход ведущему, модель не зовёт (SAM-91)
MIC_EVENTS = "mic.jsonl"  # микрофона нет, замолчал, вернулся: тикер шлёт ведущему kind mic


def now_iso() -> str:
    """Метка записи шины до миллисекунд: по ней считается задержка от фразы до звука (15 окт, до секунды не видно)."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def bus_dir(arg: str | None = None) -> Path:
    """Папка шины: из аргумента или SAM_VOICE_BUS_DIR."""
    d = Path(arg) if arg else BUS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def append_jsonl(path: Path, record: dict) -> None:
    record = {"ts": now_iso(), **record}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def tail_jsonl(path: Path, poll: float = 0.2, backlog_secs: float = 0.0):
    """Генератор новых записей файла. backlog_secs: отдать и записи моложе стольких
    секунд, уже лежащие в файле (чтобы не терять написанное, пока скрипт стартовал)."""
    while not path.exists():
        time.sleep(poll)
    with path.open("r", encoding="utf-8") as f:
        if backlog_secs:
            cutoff = time.time() - backlog_secs
            for line in f:
                try:
                    rec = json.loads(line)
                    if datetime.fromisoformat(rec["ts"]).timestamp() >= cutoff:
                        yield rec
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        else:
            f.seek(0, os.SEEK_END)
        buf = ""
        while True:
            chunk = f.readline()
            if not chunk:
                time.sleep(poll)
                continue
            buf += chunk
            if not buf.endswith("\n"):
                continue
            line, buf = buf.strip(), ""
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
