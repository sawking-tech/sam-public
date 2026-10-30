"""SAM EventBus — session queue: любой актор → MongoDB + WS монитор + inject-queue."""
import asyncio
import json
import uuid
from datetime import datetime
from typing import Any, Callable
from . import secret_mark
from .env import env_int
from .repos.session_repo import CHANNEL, COMMAND, IGNORE

# assistant_spoken - реплика голосового хода в той части, что прозвучала (gateway/spoken_history.py)
_PERSIST = {"user_message", "assistant_reply", "assistant_spoken", "tool_call", "tool_result"}
# Пометки события, которые ложатся полем в запись реплики: монитор показывает их и после F5, сборка контекста читает;
# ignore - фраза-фон сразу скрыта от модели (SAM-98)
_MARKS = (CHANNEL, IGNORE, COMMAND)
LINGER = "linger"   # итог инструмента, который живёт в истории несколько ходов (тело скилла)

# Очередь событий на одного подписчика (монитор); переполнилась - подписчик отстал
SUBSCRIBER_QUEUE_SIZE = env_int("SAM_MONITOR_QUEUE_SIZE", 1000)
# Последний элемент очереди отписанного подписчика: WS-обработчик закрывает сокет,
# монитор переподключается и получает снимок заново. Без маркера обработчик вечно
# ждал q.get(), монитор висел «connected» без событий (SAM-72).
SUBSCRIBER_DROPPED = "__subscriber_dropped__"


def _as_is(value: Any) -> Any:
    return value


class EventBus:
    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []
        self._inject_queues: dict[str, asyncio.Queue] = {}
        self._db = None
        self._scrub: Callable[[Any], Any] = _as_is
        self._on_history: Callable[[str], Any] | None = None
        self._listeners: list[Callable[[str, dict], Any]] = []   # SAM-96: workflow по событию

    def set_scrubber(self, scrub: Callable[[Any], Any]) -> None:
        """Чем чистить данные события до монитора и истории (SAM-88: секрет не утекает); корень композиции."""
        self._scrub = scrub

    def set_db(self, db):
        self._db = db

    def set_history_listener(self, listener: Callable[[str], Any]) -> None:
        """Кого звать с id сессии после записи реплики в историю (SAM-97: постановка сжатия); корень композиции."""
        self._on_history = listener

    def add_listener(self, listener: Callable[[str, dict], Any]) -> None:
        """Слушатель каждого события (тип, данные после маскировки); его сбой - строка в лог, шина работает."""
        self._listeners.append(listener)

    # ── Subscribe / Unsubscribe (WS monitor) ─────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.append(q)
        print(f"[EventBus] +subscriber total={len(self._subscribers)}")
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._subscribers.remove(q)
            print(f"[EventBus] -subscriber total={len(self._subscribers)}")
        except ValueError:
            pass

    def _drop(self, q: asyncio.Queue):
        """Отписать отставшего подписчика: очистить очередь и оставить маркер."""
        while not q.empty():
            q.get_nowait()
        q.put_nowait(SUBSCRIBER_DROPPED)
        self.unsubscribe(q)

    # ── Emit ─────────────────────────────────────────────────────────────────

    def emit(self, event_type: str, data: dict):
        msg_id = str(uuid.uuid4())
        data = {**self._scrub(data), "msg_id": msg_id}
        # ts — ISO с UTC suffix; monitor рендерит в local через new Date(iso).
        # Запасной короткий ts оставляем для тех клиентов что не используют ts_iso.
        now_utc = datetime.utcnow()
        payload = json.dumps(
            {
                "type": event_type,
                "ts_iso": now_utc.isoformat() + "Z",
                "ts": now_utc.strftime("%H:%M:%S"),
                **data,
            },
            ensure_ascii=False,
        )
        print(f"[Bus] {event_type} subs={len(self._subscribers)}")

        # 1. Broadcast to WS monitor
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._drop(q)

        for listener in self._listeners:
            try:
                listener(event_type, data)
            except Exception as e:
                print(f"[EventBus] слушатель {event_type}: {e}")

        # 2. Persist to MongoDB (session events only, skip system=True)
        # Debug-режим из self_config → НЕ сохраняем chat history,
        # remember() в RAG всё равно работает (отдельная коллекция).
        session_id = data.get("session")
        if event_type in _PERSIST and self._db is not None and session_id and not data.get("system"):
            try:
                from .self_config import get_instance as _get_self_cfg
                if _get_self_cfg().get("debug_mode", False):
                    return  # debug — не персистим
            except Exception:
                pass
            self._persist(event_type, session_id, data)

    def _persist(self, event_type: str, session_id: str, data: dict):
        doc = {"session_id": session_id, "msg_id": data.get("msg_id", ""), "timestamp": datetime.utcnow()}
        if event_type == "user_message":
            doc.update({"role": "user", "content": data.get("content", ""),
                        **({"who": data["who"]} if data.get("who") else {})})   # кто сказал - для голосового хода
            if secret_mark.is_secret_phrase(doc["content"]):   # ADR-027: секрет помечается кодом при записи
                doc[secret_mark.FIELD] = True
        elif event_type in ("assistant_reply", "assistant_spoken"):
            # Сохраняем RAW content (со всеми <think> и мета-тегами): после F5 renderRaw в мониторе
            # разберёт его на речь и мета-блоки.
            doc.update({
                "role": "assistant",
                "content": data.get("content") or data.get("voice") or "",
            })
            if data.get("fate"):
                doc["fate"] = data["fate"]   # судьба реплики - поле записи, не текст
        elif event_type == "tool_call":
            # нейтральный формат — форматируется при чтении под текущую модель
            doc.update({
                "role": "tool_call",
                "tool_name": data.get("name", ""),
                "tool_args": data.get("args", {}),
                "content": "",
            })
        elif event_type == "tool_result" and data.get(LINGER):
            # тело скилла: живёт в контексте несколько ходов целиком (runner), сжатие его не пересказывает
            doc.update({"role": "tool", "tool_name": data.get("name", ""), "content": data.get("content", ""),
                        LINGER: True})
        elif event_type == "tool_result":
            return  # не сохраняем — шум в истории, воркер пишет через report_status
        doc.update({k: data[k] for k in _MARKS if data.get(k)})
        try:
            self._db.sessions.insert_one(doc)
        except Exception as e:
            # Реплика не попала в историю: следующий ход её не увидит (SAM-76)
            print(f"[EventBus] history write failed: {event_type} session={session_id}: {e}")
            return
        if not doc.get(IGNORE):   # скрытая запись (фон, SAM-98) контекст модели не меняет - сжатию нечего ставить
            self._notify_history(session_id)

    def _notify_history(self, session_id: str) -> None:
        """Сбой слушателя не роняет emit: реплика уже в истории и в мониторе."""
        if self._on_history is None:
            return
        try:
            self._on_history(session_id)
        except Exception as e:
            print(f"[EventBus] history listener failed: session={session_id}: {e}")

    # ── Inject queue (agent loop consumes) ───────────────────────────────────

    def inject(self, session_id: str, message: str):
        if session_id not in self._inject_queues:
            self._inject_queues[session_id] = asyncio.Queue()
        self._inject_queues[session_id].put_nowait(message)
        self.emit("injected", {"content": message, "session": session_id})

    def pop_inject(self, session_id: str) -> str | None:
        q = self._inject_queues.get(session_id)
        if q and not q.empty():
            try:
                return q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        return None


event_bus = EventBus()
