"""Протокол WebSocket /voice/turn (SAM-53): транспорт голосового хода к мосту голосового слоя.

Клиент -> gateway:
    {"type": "turn", "session": "...", "text": "...", "who": "person"}   фраза; who - опознание голоса
    {"type": "cancel", "session": "..."}                 отменить текущий ход сессии
    {"type": "spoken", "session": "...", "sentences": [{"text": "...", "status": "played"}, ...]}
                                                         судьба предложений ответа
    {"type": "spoken", "session": "...", "id": "...", "sentences": [...]}
                                                         судьба своей реплики say с этим id (SAM-85)
Gateway -> клиент:
    {"type": "source", "source": "fast"}                 кто отвечает: fast - быстрая модель, main - основная;
                                                         до первой дельты и после reset (voice_turn.SOURCE_*)
    {"type": "delta", "text": "..."}                     кусок ответа модели
    {"type": "reset"}                                    поставщик сменился, показанное не продолжится
    {"type": "done", "voice": "...", "tools": [...]}     ход закончен, voice - что звучит
    {"type": "queued"}                                   сессия занята, фраза уйдёт следующим ходом
    {"type": "say", "text": "...", "id": "..."}          своя реплика SAM в очередь голосового слоя (SAM-49);
                                                         id - ключ отчёта о её судьбе (SAM-85)
    {"type": "say", ..., "ttl": 20}                      свой срок в секундах: непрозвучавшая протухает
                                                         (поздняя поправка проверки пачкой, SAM-166)
    {"type": "fix", "text": "..."}                       поправка основной модели: голосовой слой отзывает
                                                         реплики быстрой после последней фразы (amend_reply, SAM-166)
    {"type": "cancelled"} / {"type": "error", "error": "..."}

Ход идёт через acquire_and_run: в сессии один ход, фразы во время хода копятся. Проверка ответа быстрой модели -
workflow fast-review по событию ответа (SAM-166), поправку он шлёт через broadcast.
Ход и приём сообщений идут параллельно, чтобы cancel доходил посреди хода. В сокет пишет одна
задача из очереди: дельты приходят синхронными колбэками, и порядок отправки держит очередь.
Модуль не знает ни FastAPI, ни агента: сокет и функции хода приходят параметрами.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from .command_auth import secret_scope

DEFAULT_SESSION = "voice"
# Очереди отправки подключённых голосовых слоёв: своя реплика SAM уходит всем (SAM-49)
_listeners: set = set()
# Сессии, фразы которых пришли через этот сокет: итог воркера такой сессии звучит репликой say (SAM-60)
_voice_sessions: set[str] = set()


def is_voice_session(session: str) -> bool:
    return session in _voice_sessions


def broadcast(payload: dict) -> int:
    """Сообщение всем подключённым голосовым слоям; сколько получили."""
    for outbox in list(_listeners):
        outbox.put_nowait(payload)
    return len(_listeners)
CANCELLED_REPLY = "[отменено]"  # то, что acquire_and_run возвращает отменённому ходу


async def serve_voice_socket(
    ws: Any,
    run_turn: Callable[..., Awaitable[Any]],
    acquire_and_run: Callable[[str, str, Callable[[str], Awaitable[str]]], Awaitable[str]],
    cancel_turn: Callable[[str], bool],
    on_spoken: Callable[..., Any] | None = None,
) -> None:
    """Читать сообщения сокета до закрытия. ws - receive_text / send_text."""
    outbox: asyncio.Queue = asyncio.Queue()
    _listeners.add(outbox)
    writer = asyncio.create_task(_write(ws, outbox))
    running: set[asyncio.Task] = set()
    try:
        while True:
            msg = _parse(await ws.receive_text())
            if msg is None:
                outbox.put_nowait({"type": "error", "error": "message is not a JSON object with type"})
                continue
            session = str(msg.get("session") or DEFAULT_SESSION)
            if msg["type"] == "cancel":
                cancel_turn(session)
            elif msg["type"] == "spoken":
                sentences = msg.get("sentences")
                if on_spoken is None or not isinstance(sentences, list):
                    outbox.put_nowait({"type": "error", "error": "spoken needs a list of sentences"})
                    continue
                key = msg.get("id")
                on_spoken(session, sentences, *([str(key)] if key else []))
            elif msg["type"] == "turn":
                text = str(msg.get("text") or "").strip()
                if not text:
                    outbox.put_nowait({"type": "error", "error": "empty text"})
                    continue
                _voice_sessions.add(session)
                # SAM-88: признак ключа уходит в контекст задачи хода (create_task копирует ContextVar)
                with secret_scope(msg.get("secret")):
                    task = asyncio.create_task(_turn(outbox, session, text, run_turn, acquire_and_run,
                                                     msg.get("who"), msg.get("at")))
                running.add(task)
                task.add_done_callback(running.discard)
            else:
                outbox.put_nowait({"type": "error", "error": f"unknown type {msg['type']!r}"})
    finally:
        _listeners.discard(outbox)
        for task in running:
            task.cancel()
        writer.cancel()


async def _turn(outbox: asyncio.Queue, session, text, run_turn, acquire_and_run, who=None,
                heard_at: str | None = None) -> None:
    """heard_at - время фразы в шине голоса (мост шлёт его полем at): к нему относятся факты памяти (SAM-101)."""
    result: dict = {}

    async def _handler(phrase: str) -> str:
        out = await run_turn(
            phrase, session,
            on_delta=lambda piece: outbox.put_nowait({"type": "delta", "text": piece}),
            on_reset=lambda: outbox.put_nowait({"type": "reset"}),
            on_source=lambda source: outbox.put_nowait({"type": "source", "source": source}),
            **({"who": who} if who else {}),
            **({"heard_at": heard_at} if heard_at else {}),
        )
        result["voice"], result["tools"] = out.voice, list(out.tools)
        # done на каждый ответ: накопленная фраза идёт вторым ходом того же захвата сессии,
        # и у её ответа свой отчёт о судьбе
        outbox.put_nowait({"type": "done", **result})
        return out.voice

    try:
        reply = await acquire_and_run(session, text, _handler)
    except Exception as e:
        outbox.put_nowait({"type": "error", "error": str(e)})
        return
    if reply == CANCELLED_REPLY:
        outbox.put_nowait({"type": "cancelled"})
    elif not result:
        outbox.put_nowait({"type": "queued"})


async def _write(ws, outbox: asyncio.Queue) -> None:
    """Единственный писатель в сокет: сообщения уходят в порядке постановки."""
    while True:
        payload = await outbox.get()
        await ws.send_text(json.dumps(payload, ensure_ascii=False))


def _parse(raw: str) -> dict | None:
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    return msg if isinstance(msg, dict) and isinstance(msg.get("type"), str) else None

