"""Тики разговора SAM для Claude Code-ведущего (ADR-012, SAM-59): одна строка stdout - одно событие.

Claude Code слушает поток через Monitor и отвечает `voice.layer.say`.

  python -m voice.layer.ticker [папка-шины]

События (JSON в строке, копия в ticks.jsonl):
  ready    тикер запущен
  voice    законченная фраза человека из voice-in.jsonl; эхо своей озвучки и куски по ходу речи не идут;
           at - время фразы, по нему тик rag находит свою фразу; secret - имена ключей, названных во фразе (SAM-88, SAM-104)
  unheard  реплика SAM оборвана, сброшена или устарела (said.jsonl: cut, dropped, stale) - важное повторить;
           speaker - подпись «SAM · быстрая», from - источник реплики (turns.speaker_label)
  chat     вход основной модели не из голоса (lead-in.jsonl): чат монитора, inject, вопрос и итог агента SAM;
           session и task_id - куда отвечать; at - время входа; facts - факты памяти, найденные к нему (SAM-101);
           command - имя команды монитора: строка «/имя ...» - команда, а не фраза (SAM-99);
           secret - имена ключей, названных во входе (SAM-88, SAM-104)
  rag      факты памяти, не успевшие ко входу или к голосовой фразе (lead-in.jsonl, source rag): at и phrase -
           к какому входу, facts - строки фактов
  mic      микрофона нет, он замолчал или вернулся (mic.jsonl: missing, lost, back) - слух не работает

Решение «что тикнуть» (voice_ticks, said_ticks, mic_ticks) отделено от чтения файлов и печати.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .bus import LEAD_INBOX, MIC_EVENTS, SAID, TICKS, VOICE_IN, append_jsonl, bus_dir, now_iso
from gateway.env import env_float
from .turns import speaker_label

POLL_SECS = env_float("SAM_TICKER_POLL_S", 0.5)   # опрос файлов шины; 0,5 с - задержка тика не больше полсекунды
UNHEARD = ("cut", "dropped", "stale")  # dup - не пропуск: на ту же фразу уже ответили


def voice_ticks(records: list[dict]) -> list[dict]:
    out = []
    for rec in records:
        # Куски по ходу речи не тикают: законченная фраза повторяет их целиком
        if rec.get("echo") or rec.get("partial") or not str(rec.get("text") or "").strip():
            continue
        out.append({"kind": "voice", "text": rec["text"], "who": rec.get("who"), **_at(rec),
                    **({"secret": rec["secret"]} if rec.get("secret") else {})})
    return out


def said_ticks(records: list[dict]) -> list[dict]:
    return [{"kind": "unheard", "status": r["status"], "text": r.get("text", ""),
             "speaker": speaker_label(r.get("from")), **({"from": r["from"]} if r.get("from") else {})}
            for r in records if r.get("status") in UNHEARD]


def _at(rec: dict) -> dict:
    """Время входа в тике: ts записи шины; ts самого тика - время тика."""
    return {"at": rec["ts"]} if rec.get("ts") else {}


# Поля входящего, которые идут в тик как есть: кто сказал; сессия и задача - куда отвечать (вопрос агента, SAM-91);
# факты памяти к входу строками (SAM-101, gateway/recall.py); secret - во фразе был ключ команд (SAM-88),
# сам ключ слух и вход уже замаскировали
# push_ids - сообщения очереди push в этом входе (шаг workflow): ведущий подтверждает их POST /sam/push/ack (SAM-174)
INBOX_PASSTHROUGH = ("who", "session", "task_id", "facts", "command", "secret", "push_ids")
RAG_SOURCE = "rag"                     # gateway/recall.py LATE_SOURCE: факты к уже доставленному входу
RAG_FIELDS = ("at", "phrase", "session", "facts")


def inbox_ticks(records: list[dict]) -> list[dict]:
    """Входящие основной модели: вход не из голоса (чат монитора, inject, вопрос и итог агента) - тик kind chat;
    факты памяти к уже доставленному входу - тик kind rag."""
    out = []
    for r in records:
        if r.get("source") == RAG_SOURCE:
            if r.get("facts"):
                out.append({"kind": RAG_SOURCE, **{k: r[k] for k in RAG_FIELDS if r.get(k)}})
        elif r.get("text"):
            out.append({"kind": "chat", "text": r["text"], "source": r.get("source", ""), **_at(r),
                        **{k: r[k] for k in INBOX_PASSTHROUGH if r.get(k)}})
    return out


def mic_ticks(records: list[dict]) -> list[dict]:
    return [{"kind": "mic", "status": r.get("status"), "text": r.get("text", "")} for r in records]


def new_records(path: Path, pos: int) -> tuple[list[dict], int]:
    """Записи, дописанные после позиции pos, и новая позиция - по целым строкам; битая строка пропускается."""
    if not path.exists() or path.stat().st_size <= pos:
        return [], pos
    with path.open("rb") as f:
        f.seek(pos)
        chunk = f.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], pos
    recs = []
    for line in chunk[:end].decode("utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            recs.append(rec)
    return recs, pos + end + 1


class Ticker:
    def __init__(self, d: Path, out=print) -> None:
        self.d, self._out = d, out
        self.voice_path, self.said_path, self.mic_path = d / VOICE_IN, d / SAID, d / MIC_EVENTS
        # Только новое: старые фразы ведущему не будить
        self.voice_pos = self.voice_path.stat().st_size if self.voice_path.exists() else 0
        self.said_pos = self.said_path.stat().st_size if self.said_path.exists() else 0
        self.mic_pos = self.mic_path.stat().st_size if self.mic_path.exists() else 0
        self.inbox_path = d / LEAD_INBOX
        self.inbox_pos = self.inbox_path.stat().st_size if self.inbox_path.exists() else 0

    def emit(self, tick: dict) -> None:
        self._out(json.dumps({"ts": now_iso(), **tick}, ensure_ascii=False))
        append_jsonl(self.d / TICKS, tick)

    def poll(self) -> None:
        recs, self.voice_pos = new_records(self.voice_path, self.voice_pos)
        for tick in voice_ticks(recs):
            self.emit(tick)
        recs, self.said_pos = new_records(self.said_path, self.said_pos)
        for tick in said_ticks(recs):
            self.emit(tick)
        recs, self.mic_pos = new_records(self.mic_path, self.mic_pos)
        for tick in mic_ticks(recs):
            self.emit(tick)
        recs, self.inbox_pos = new_records(self.inbox_path, self.inbox_pos)
        for tick in inbox_ticks(recs):
            self.emit(tick)

    def run(self) -> None:
        self.emit({"kind": "ready", "bus": str(self.d)})
        while True:
            self.poll()
            time.sleep(POLL_SECS)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    Ticker(bus_dir(sys.argv[1] if len(sys.argv) > 1 else None), out=lambda s: print(s, flush=True)).run()


if __name__ == "__main__":
    main()
