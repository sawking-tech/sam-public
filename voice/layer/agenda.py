"""Воля SAM: очередь того, что она хочет сказать сама.


  python -m voice.layer.agenda add "текст" [--high] [--ttl 300]   добавить намерение, озвучка готовится сразу
  python -m voice.layer.agenda                                    показать очередь

Запись agenda.jsonl: {"ts", "id", "text", "priority": "normal"|"high", "expires"}; выполненные и
снятые - в agenda-done.jsonl по id. Аудио готовится при добавлении (agenda/<id>.wav): в паузе
реплика звучит без задержки на синтез.

Кто берёт слово решает pick(): обычное намерение - только когда пауза (никто не говорит, я молчу,
с последней реплики человека прошло FLOOR_GAP секунд); важное - сразу, поверх шума.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .bus import append_jsonl, bus_dir

AGENDA = "agenda.jsonl"
DONE = "agenda-done.jsonl"
FLOOR_GAP = 2.5          # пауза после человека, после которой обычное намерение берёт слово
DEFAULT_TTL = 300.0      # заготовка устаревает: тема уходит


@dataclass
class Intent:
    id: str
    text: str
    priority: str
    expires: float
    wav: Path | None


def load(d: Path, now: float | None = None) -> list[Intent]:
    """Живые намерения: не выполненные и не просроченные, важные первыми, затем по времени."""
    now = time.time() if now is None else now
    done = set()
    if (d / DONE).exists():
        for line in (d / DONE).read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                continue
    out = []
    if (d / AGENDA).exists():
        for line in (d / AGENDA).read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("id") in done or r.get("expires", 0) < now:
                continue
            wav = wav_path(d, r["id"])
            out.append(Intent(r["id"], r["text"], r.get("priority", "normal"), r["expires"],
                              wav if wav.exists() else None))
    out.sort(key=lambda i: (i.priority != "high",))  # стабильная: внутри группы порядок записи
    return out


def pick(intents: list[Intent], *, speaking: bool, hearing: bool, since_human: float,
         queue_busy: bool) -> Intent | None:
    """Кому брать слово сейчас. Важное - всегда, кроме когда я уже говорю; обычное - в паузе."""
    if not intents or speaking or queue_busy:
        return None
    top = intents[0]
    if top.priority == "high":
        return top
    if hearing or since_human < FLOOR_GAP:
        return None
    return top


def mark_done(d: Path, intent_id: str, how: str = "сказано") -> None:
    append_jsonl(d / DONE, {"id": intent_id, "how": how})


def is_done(d: Path, intent_id: str) -> bool:
    """Намерение взято в озвучку или снято (agenda-done.jsonl)."""
    if not (d / DONE).exists():
        return False
    for line in (d / DONE).read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("id") == intent_id:
                return True
        except json.JSONDecodeError:
            continue
    return False


def wav_path(d: Path, intent_id: str) -> Path:
    """Готовое аудио намерения; озвучка пишет судьбу такой реплики под этим путём (say_out.floor_step)."""
    return d / "agenda" / f"{intent_id}.wav"


def add(d: Path, text: str, high: bool = False, ttl: float = DEFAULT_TTL) -> str:
    """Добавить намерение и сразу подготовить аудио (без устройства - звук не играет)."""
    iid = uuid.uuid4().hex[:8]
    try:
        from .tts_client import session_voice, synthesize
        audio, rate = synthesize(text, *session_voice(d))
        wav_path(d, iid).parent.mkdir(exist_ok=True)
        with wave.open(str(wav_path(d, iid)), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(audio.tobytes())
    except Exception as exc:  # без готового аудио реплика синтезируется в момент взятия слова
        print(f"[очередь] аудио не подготовлено: {exc}", file=sys.stderr)
    append_jsonl(d / AGENDA, {"id": iid, "text": text, "priority": "high" if high else "normal",
                              "expires": time.time() + ttl})
    return iid


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    d = bus_dir()
    args = sys.argv[1:]
    if args and args[0] == "add":
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd")
        p.add_argument("text")
        p.add_argument("--high", action="store_true")
        p.add_argument("--ttl", type=float, default=DEFAULT_TTL)
        a = p.parse_args()
        print(add(d, a.text, a.high, a.ttl))
        return
    for i in load(d):
        left = int(i.expires - time.time())
        print(f"{i.id} [{i.priority}] ещё {left} с{' (аудио готово)' if i.wav else ''}: {i.text}")


if __name__ == "__main__":
    main()
