"""Сказать голосом: дописать реплику в say-out.jsonl шины SAM (ADR-012, SAM-59).

Так Claude Code ведёт разговор SAM: слушает тики `python -m voice.layer.ticker` через Monitor и отвечает этим
скриптом.

  python -m voice.layer.say "текст"
  python -m voice.layer.say --hold "текст"       громкость не перебивает, стоп-слово перебивает
  python -m voice.layer.say --interrupt "текст"  оборвать текущую реплику и сказать эту
  python -m voice.layer.say --back "текст"       короткий отклик посреди речи человека
  python -m voice.layer.say --fix "текст"        поправка: реплики быстрого голоса с последней фразы
                                                 человека отзываются (turns.py)
  python -m voice.layer.say --from haiku "текст" реплика быстрого голоса: несёт reply_to из reply.to
  python -m voice.layer.say --text-only "текст"  текст в монитор и историю без озвучки (схема, список); "-" - из stdin
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from gateway.access import auth_headers

from .bus import REPLY_TO, SAY_OUT, append_jsonl, bus_dir
from .turns import FAST, OBSERVER   # OBSERVER - ведущий; поправку от него ждёт turns.retracted_rows


def say_record(text: str, *, hold: bool = False, interrupt: bool = False, back: bool = False,
               fix: bool = False, who: str = OBSERVER, reply_to: str | None = None) -> dict:
    """Строка say-out без времени (его ставит append_jsonl)."""
    rec = {"text": text, "hold": hold, "interrupt": interrupt, "back": back, "from": who}
    if fix:
        rec["fix"] = True
    if who == FAST and reply_to:
        rec["reply_to"] = reply_to
    return rec


def say(d: Path, text: str, **flags) -> dict:
    reply_file = d / REPLY_TO
    reply_to = reply_file.read_text(encoding="utf-8").strip() if reply_file.exists() else None
    rec = say_record(text, reply_to=reply_to, **flags)
    append_jsonl(d / SAY_OUT, rec)
    return rec


# 127.0.0.1, не localhost: на Windows urllib сначала пробует ::1 и теряет ~2 с (замер 10 окт: 2,17 с против 0,39 с),
# при HTTP_TIMEOUT_S 3 с отчёт в шлюз и текст в монитор не успевали
SERVER_ENV, DEFAULT_SERVER = "SAM_SERVER", "http://127.0.0.1:8000"
SESSION = "voice"                 # голосовая сессия шлюза, та же, что у моста
FIX_REASON = "поправка ведущего"
HTTP_TIMEOUT_S = 3
STDIN_ARG = "-"                   # текст из stdin: многострочная схема без экранирования кавычек в shell


def _post(server: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(f"{server}{path}", data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **auth_headers()})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
        return json.load(r)


def report_reply(server: str, text: str, session: str = SESSION) -> str | None:
    """Реплика ведущего - в монитор и историю шлюза (SAM-91); шлюз недоступен - строка, реплика уже в шине."""
    try:
        _post(server, f"/sam/session/{session}/reply", {"text": text})
        return None
    except Exception as exc:
        return f"в монитор и историю не попало ({exc})"


def report_note(server: str, text: str, session: str = SESSION) -> str | None:
    """Текст без озвучки - в монитор и историю (POST /sam/session/{id}/note); шлюз недоступен - строка о причине:
    в шину текст не пишется, кроме шлюза ему деться некуда."""
    try:
        _post(server, f"/sam/session/{session}/note", {"text": text})
        return None
    except Exception as exc:
        return f"текст в монитор не попал ({exc}); шлюз - {server}/health"


def ignore_last_reply(server: str, session: str = SESSION, reason: str = FIX_REASON) -> str:
    """Поправка ведущего: последний ответ модели в истории шлюза помечается и не идёт в контекст (SAM-92).
    Шлюз недоступен - строка о том, что пометки нет; реплика поправки всё равно уже в шине."""
    try:
        return f"в истории помечено: {len(_post(server, f'/sam/session/{session}/ignore-last', {'reason': reason}).get('msg_ids', []))}"
    except Exception as exc:
        return f"историю не пометил ({exc}); ответ модели останется в контексте"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("text")
    p.add_argument("--bus", help="папка шины; по умолчанию SAM_VOICE_BUS_DIR")
    p.add_argument("--hold", action="store_true")
    p.add_argument("--interrupt", action="store_true")
    p.add_argument("--back", action="store_true")
    p.add_argument("--fix", action="store_true")
    p.add_argument("--from", dest="who", default=OBSERVER)
    p.add_argument("--text-only", action="store_true", help="в монитор и историю без озвучки")
    p.add_argument("--session", default=SESSION, help="сессия шлюза для --text-only")
    a = p.parse_args()
    if a.text == STDIN_ARG:
        sys.stdin.reconfigure(encoding="utf-8")
    text = sys.stdin.read() if a.text == STDIN_ARG else a.text
    if not text.strip():
        sys.exit("пустая реплика: нечего сказать")
    server = os.getenv(SERVER_ENV, DEFAULT_SERVER)
    if a.text_only:
        if problem := report_note(server, text, a.session):
            sys.exit(problem)
        return
    say(bus_dir(a.bus), text, hold=a.hold, interrupt=a.interrupt, back=a.back, fix=a.fix, who=a.who)
    if a.who == OBSERVER and (problem := report_reply(server, text)):
        print(problem)
    if a.fix:
        print(ignore_last_reply(server))


if __name__ == "__main__":
    main()
