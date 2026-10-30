"""Per-session lock + message accumulation (SAM-01).

Сценарий: пользователь шлёт 3 голосовых сообщения пока первое ещё обрабатывается.
Раньше каждое порождало отдельный таск, агент захлёбывался. Теперь:

1. Первое сообщение захватывает лок сессии и идёт в обработку.
2. Последующие сообщения добавляются в очередь сессии.
3. Когда первая обработка завершилась — все накопленные сообщения
   склеиваются в один запрос и обрабатываются разом.

UI: monitor подписан на события queue_* и показывает pending-сообщения
с кнопками "✕ отмена" и "✎ редактировать". Через эндпоинты
/sam/session/{id}/cancel-queue и /replace-queue человек может вмешаться
до склейки.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from typing import Awaitable, Callable

log = logging.getLogger("sam.session_lock")

_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_queues: dict[str, list[str]] = defaultdict(list)
_busy: set[str] = set()
_active_tasks: dict[str, asyncio.Task] = {}  # session_id → текущий handler-таск
# Фоновые ходы сессии (пачки очереди push, ADR-020, п. 12): идут одновременно под одним захватом замка сессии
_background: dict[str, set[asyncio.Task]] = defaultdict(set)
_background_entry: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _emit(event_type: str, session_id: str, **extra) -> None:
    """Эмит события в event_bus с защитой от циклических импортов."""
    try:
        from .event_bus import event_bus
        event_bus.emit(event_type, {
            "session": session_id,
            "queue": list(_queues.get(session_id, [])),
            "queue_size": len(_queues.get(session_id, [])),
            "busy": session_id in _busy,
            **extra,
        })
    except Exception as e:
        log.warning("event_bus emit failed: %s", e)


def is_busy(session_id: str) -> bool:
    return session_id in _busy


def busy_sessions() -> list[str]:
    """Сессии, где сейчас идёт ход: монитор после переподключения узнаёт их и снова показывает «SAM думает»."""
    return sorted(_busy)


def queue_size(session_id: str) -> int:
    return len(_queues.get(session_id, []))


def get_queue(session_id: str) -> list[str]:
    """Снимок текущей очереди (копия для UI)."""
    return list(_queues.get(session_id, []))


async def acquire_and_run(
    session_id: str,
    message: str,
    handler: Callable[[str], Awaitable[str]],
) -> str:
    """Захватить сессию и обработать message. Если уже занята — добавить в очередь."""
    if session_id in _busy:
        _queues[session_id].append(message)
        log.info("[session=%s] busy, queued (q=%d)", session_id, queue_size(session_id))
        _emit("queue_added", session_id, message=message)
        return ""

    lock = _locks[session_id]
    async with lock:
        _busy.add(session_id)

        # session_busy эмитим с задержкой 0.5s — runner успевает выдать
        # user_message раньше. Если handler закончился до 0.5s — busy
        # вообще не показывается (быстрые turn'ы не нуждаются в indicator).
        _busy_emit_done = asyncio.Event()
        async def _delayed_busy():
            try:
                await asyncio.sleep(0.5)
                if not _busy_emit_done.is_set():
                    _emit("session_busy", session_id)
            except asyncio.CancelledError:
                pass
        busy_task = asyncio.create_task(_delayed_busy())

        try:
            result = ""
            error: Exception | None = None
            next_msg: str | None = message
            while next_msg is not None:
                task = asyncio.create_task(handler(next_msg))
                _active_tasks[session_id] = task
                try:
                    result, error = await task, None
                except asyncio.CancelledError:
                    _emit("turn_cancelled", session_id)
                    return "[отменено]"
                except Exception as e:
                    # Упавший ход не съедает накопленное: оно уходит следующим
                    # ходом сразу, а не после новой фразы (SAM-72)
                    log.warning("[session=%s] turn failed: %s", session_id, e)
                    error = e
                # Слили всё накопленное — обрабатываем одним проходом.
                next_msg = _take_queued(session_id)
            if error is not None:
                raise error
            return result
        finally:
            _busy_emit_done.set()
            busy_task.cancel()
            _active_tasks.pop(session_id, None)
            _busy.discard(session_id)
            _emit("session_idle", session_id)


@contextlib.asynccontextmanager
async def exclusive(session_id: str):
    """Ход не от фразы человека (шаг workflow, ответ основной после паузы быстрой): ждать замок сессии, пока идёт ход
    человека, и держать его на свой ход (ADR-023, шаг 1). Сессию не помечает занятой: фраза человека в это время
    не встаёт в очередь склейки, а ждёт замок и идёт своим ходом сразу после."""
    async with _locks[session_id]:
        task = asyncio.current_task()
        _active_tasks[session_id] = task   # «стоп» и отмена всех ходов достают и этот ход (ревью 12 окт)
        try:
            yield
        finally:
            if _active_tasks.get(session_id) is task:
                _active_tasks.pop(session_id, None)


@contextlib.asynccontextmanager
async def hold_if_idle(session_id: str):
    """Замок сессии на служебную работу над её историей (удаление): занята ходом или фоном - сразу False, без
    ожидания; свободна - True, и ход, пришедший за это время, ждёт конца работы."""
    lock = _locks[session_id]
    if session_id in _busy or lock.locked():
        yield False
        return
    async with lock:
        yield True


@contextlib.asynccontextmanager
async def background(session_id: str):
    """Фоновый ход (пачка workflow): как exclusive - не поверх хода человека, но фоновые ходы сессии идут вместе,
    одним захватом замка; человек ждёт, пока не кончится последний из них."""
    task = asyncio.current_task()
    async with _background_entry[session_id]:
        if not _background[session_id]:
            await _locks[session_id].acquire()
        _background[session_id].add(task)
    try:
        yield
    finally:
        _background[session_id].discard(task)
        if not _background[session_id]:
            _locks[session_id].release()


def _take_queued(session_id: str) -> str | None:
    """Забрать накопленные фразы одним сообщением; None - очередь пуста."""
    while _queues[session_id]:
        pending = _queues[session_id]
        _queues[session_id] = []
        merged = " ".join(pending).strip()
        if not merged:
            _emit("queue_flushed", session_id, merged="", count=0)
            continue
        log.info("[session=%s] flushing queue: %d msgs", session_id, len(pending))
        _emit("queue_flushed", session_id, merged=merged, count=len(pending))
        return merged
    return None


def cancel_current_turn(session_id: str) -> bool:
    """Отменить текущий handler-таск сессии. Возвращает True если что-то отменили."""
    tasks = [t for t in (_active_tasks.get(session_id), *_background[session_id]) if t and not t.done()]
    for task in tasks:
        task.cancel()
        log.info("[session=%s] turn cancelled", session_id)
    return bool(tasks)


def cancel_all_turns() -> int:
    """Отменить ВСЕ активные turn'ы во всех сессиях. Возвращает количество.
    Использовать как panic-button когда не знаешь в какой сессии завис LLM."""
    count = 0
    pairs = [*_active_tasks.items(), *((sid, t) for sid, ts in _background.items() for t in ts)]
    for sid, task in pairs:
        if task and not task.done():
            task.cancel()
            log.info("[session=%s] turn cancelled (panic)", sid)
            count += 1
    return count


def clear_queue(session_id: str) -> int:
    """Сбросить очередь полностью (для clear-all через REST).
    Возвращает сколько удалили."""
    pending = _queues.pop(session_id, [])
    if pending:
        _emit("queue_cleared", session_id, dropped=len(pending))
    return len(pending)


def pop_last_queued(session_id: str) -> str | None:
    """Удалить последнее сообщение из очереди (одно). Возвращает текст
    удалённого или None если очередь пуста."""
    q = _queues.get(session_id) or []
    if not q:
        return None
    last = q.pop()
    if not q:
        _queues.pop(session_id, None)
    _emit("queue_popped", session_id, removed=last)
    return last


def replace_queue(session_id: str, new_message: str) -> int:
    """Заменить всю накопленную очередь одним отредактированным сообщением.
    Возвращает сколько сообщений было заменено."""
    old = _queues.pop(session_id, [])
    text = (new_message or "").strip()
    if text:
        _queues[session_id] = [text]
    _emit("queue_replaced", session_id, dropped=len(old), new_message=text)
    return len(old)
