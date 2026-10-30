"""
SAM Gateway — тонкий FastAPI роутер.
Никакой агентной логики здесь — только маршрутизация.

Доступ: общий ключ SAM_API_KEY, без него - только с этой машины (gateway/access.py, SAM-79).
Сейчас работает только во внутренней сети (LAN-only).
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Optional

# Windows: stdout → UTF-8 чтобы print() с кириллицей не падал
# reconfigure вместо новой обёртки: обёртка над чужим буфером (захват pytest)
# закрывала его вместе с собой и роняла остальные тесты
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

import httpx
import uvicorn
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

load_dotenv()

from .command_auth import secret_scope
from .agent import _quiet_book
from .agent import _db, _mongo_ok, sam_agent, get_session_history, archive_repo, session_repo, mask_input, main_models, \
    sync_skills
from .agent import agent_answer_deps, agent_create_deps, compression_watch, get_rag_context, recall_at_input, _impl_remember
from . import recall
from .tools import agents as agents_tool
from .repos import TokenUsageRepo
from .event_bus import SUBSCRIBER_DROPPED, event_bus
from . import catalog as cat
from . import llm_client
from .llm_router import get_best_llm, lmstudio_error
from .stt_dedup import INPUT_HEADER, SPEECH_INPUT, deduplicate_stt, history_for_dedup
from .spoken_history import replay_fate
from .repos.session_repo import CHANNEL, COMMAND, IGNORE, TEXT_CHANNEL
from . import monitor_commands
from . import radar_client
from . import stand_report, stand_silence, stand_watch_keeper
from . import apps_api, household_api, soul_api
from .health import NO_LLM, check as health_check
from .env import env_bool, env_int
from .session_lock import (
    acquire_and_run, busy_sessions, clear_queue, is_busy, queue_size, get_queue, replace_queue,
)

# ─── Логирование ──────────────────────────────────────────────────────────────

log = logging.getLogger("sam.gateway")

# ─── Приложение ───────────────────────────────────────────────────────────────

app = FastAPI(title="SAM Gateway", version="1.0.0")
# Доступ по ключу SAM_API_KEY; без ключа - только с этой машины (SAM-79, gateway/access.py)
from .access import AccessMiddleware, startup_warning  # noqa: E402
# Прокси живых приложений (SAM-182): тело страницы приложения идёт в его контейнер, POST с любым типом
app.add_middleware(AccessMiddleware, any_body_prefixes=("/apps/",))
if _warn := startup_warning():
    log.warning(_warn)

_STATIC = Path(__file__).parent / "static"

# Лимит одновременных WebSocket-подключений к монитору
_MAX_WS_CONNECTIONS = 10
_ws_count           = 0

# ─── Pydantic модели ──────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    # content может быть строкой ИЛИ списком частей (multimodal: text/image_url)
    # для vision-моделей. Pydantic v2 принимает Union через | type.
    content: str | list[dict]


class ChatRequest(BaseModel):
    messages: list[ChatMessage]

    @field_validator("messages")
    @classmethod
    def must_have_user_message(cls, v: list[ChatMessage]) -> list[ChatMessage]:
        if not any(m.role == "user" for m in v):
            raise ValueError("Нет сообщений с role=user")
        return v


def _extract_text(content) -> str:
    """Извлечь только text часть из multimodal content. Для STT-dedup и UI."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return " ".join(parts).strip()
    return str(content or "")


def _extract_images(content) -> list[str]:
    """Извлечь image URL/data из multimodal content."""
    if not isinstance(content, list):
        return []
    imgs = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image_url":
            url = (item.get("image_url") or {}).get("url", "")
            if url:
                imgs.append(url)
    return imgs


class RegisterRequest(BaseModel):
    name: str           = "unknown"
    ip: str             = ""
    mac: str            = ""
    os: str             = ""
    cpu: str            = ""
    ram_gb: Optional[float] = None
    gpu: str            = ""
    mcp_url: str        = ""
    capabilities: list[str] = []
    auto_wake: bool     = True


class HeartbeatRequest(BaseModel):
    name: str             = ""
    presence_score: float = 0.0


class OfflineRequest(BaseModel):
    name: str = ""


class PushAckRequest(BaseModel):
    ids: list[str]   # id сообщений очереди push, по которым модель вне шлюза закончила ход


class EventRequest(BaseModel):
    event: str      = "unknown"
    context: str    = ""
    device_id: Optional[str] = None


class InjectRequest(BaseModel):
    session_id: str = "default"
    message: str    = ""


class AgentRequest(BaseModel):
    """Агент SAM от основной модели (SAM-91, gateway/tools/agents.py)."""
    description: str
    level: Optional[str] = None         # smart | medium | weak; пусто - medium
    tools: Optional[list[str]] = None   # права: подмножество инструментов воркера; пусто - все
    device: Optional[str] = None
    name: Optional[str] = None
    framework: Optional[str] = None     # sam | cortex (SAM-133); пусто - SAM_AGENT_FRAMEWORK
    peers: Optional[list[str]] = None   # кому агент Cortex может писать: id задач (ADR-022, «Кому писать»)
    depth: Optional[int] = None         # поколений детей агента Cortex, до SAM_AGENT_MAX_DEPTH; пусто - без детей
    steps: Optional[int] = None         # прикидка создателя, сколько шагов на задачу; пусто - умолчание пула
    kind: Optional[str] = None          # вид работы (task_class.KINDS): выбор модели и статистика
    env: Optional[str] = None           # inside - внутри экосистемы SAM, outside - снаружи


class LLMSwitchRequest(BaseModel):
    adapter: str            # llm_client.ADAPTERS: openai | custom | litellm | auto
    source: Optional[str] = None   # lmstudio | vllm | gemini | anthropic | openrouter


# ─── Chat ─────────────────────────────────────────────────────────────────────

def _turn_handler(run, session_id: str, user_id: str | None, images: list[str]):
    """Обработчик ходов одного запроса для acquire_and_run. Картинки запроса идут только
    с его первым ходом: следующий ход - склейка фраз из очереди, и прикреплять к нему
    те же картинки ещё раз нельзя (SAM-76). Картинки фраз, пришедших в очередь, не
    доходят: очередь session_lock хранит только текст, вопрос в SAM-76."""
    pending_images = list(images)

    async def _handler(msg: str) -> str:
        nonlocal pending_images
        turn_images, pending_images = pending_images, []
        return await run(msg, device_id=session_id, user_id=user_id, images=turn_images)
    return _handler


@app.post("/v1/chat/completions")
async def chat(
    body: ChatRequest,
    x_device_id:  Optional[str] = Header(None, alias="X-Device-ID"),
    x_user_id:    Optional[str] = Header(None, alias="X-User-ID"),
    x_session_id: Optional[str] = Header(None, alias="X-Session-ID"),
    x_sam_input:  Optional[str] = Header(None, alias=INPUT_HEADER),
):
    user_content_raw = next(
        (m.content for m in reversed(body.messages) if m.role == "user"), ""
    )
    # Multimodal: extract text для STT dedup / lock / history-keying,
    # картинки прокинем отдельно через sam_agent.run.
    heard = mask_input(_extract_text(user_content_raw))   # SAM-88: секрет дальше входа не идёт
    user_message = heard.text
    user_images = _extract_images(user_content_raw)
    if user_images:
        print(f"[Chat] multimodal: text={user_message[:60]!r} images={len(user_images)}")

    # X-Session-ID позволяет голосовому клиенту иметь отдельную сессию
    # от веб-интерфейса; пустая строка приравнивается к отсутствию.
    effective_session = x_session_id or x_device_id or "default"
    radar.note_message("chat", x_device_id, x_user_id)      # чат даёт имя и устройство: функция радара note (SAM-105, SAM-198)

    # SAM-32: STT pre-trigger буфер захватывает предыдущие реплики. Удаляем
    # чанки, которые уже встречались в истории сессии — иначе агент думает
    # что пользователь повторяется. Если после дедупа пусто — тихо игнор.
    # Только для речи (заголовок INPUT_HEADER от голосового клиента): набранный повтор - осознанный, человек
    # переспрашивает, и молча выкинуть его нельзя (15 окт монитор терял повторный вопрос без ответа)
    original_message = user_message
    history_docs = get_session_history(effective_session, limit=20) if x_sam_input == SPEECH_INPUT else []
    history_texts = history_for_dedup(history_docs)
    if history_texts:
        cleaned = deduplicate_stt(user_message, history_texts)
        if cleaned != user_message:
            log.info("[stt-dedup] %s -> %s (session=%s)",
                     repr(original_message)[:80], repr(cleaned)[:80], effective_session)
        user_message = cleaned

    if not user_message.strip():
        return {
            "id":      f"sam-{uuid.uuid4().hex[:8]}",
            "object":  "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": ""},
                         "finish_reason": "stop"}],
        }

    # SAM-91: вход - в основную модель. Отвечает она вне шлюза (сессия Claude Code) - реплика в историю и ей во
    # входящие с фактами памяти (SAM-101), LLM шлюза не зовётся; ответ придёт голосом и событием, как у любого хода
    model = main_models.pick()
    if not model.in_gateway:
        event_bus.emit("user_message", {"content": user_message, "session": effective_session})
        await recall_at_input(model, user_message, effective_session, history_docs,
                              message={"text": user_message, "source": "monitor", "session": effective_session,
                                       **({"secret": sorted(heard.names)} if heard.names else {})})
        return {"id": f"sam-{uuid.uuid4().hex[:8]}", "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]}

    # SAM-01: per-session lock + message accumulation. Если сессия уже
    # обрабатывается — следующее сообщение копится в очереди и склеивается
    # с другими после завершения текущего прохода.
    handler = _turn_handler(sam_agent.run, effective_session, x_user_id, user_images)
    with secret_scope(heard.names):   # SAM-88: привилегированное в этом ходе - только с ключом во фразе
        response = await acquire_and_run(effective_session, user_message, handler)

    return {
        "id":      f"sam-{uuid.uuid4().hex[:8]}",
        "object":  "chat.completion",
        "choices": [
            {
                "message":       {"role": "assistant", "content": response},
                "finish_reason": "stop",
            }
        ],
    }


# ─── Архив компрессии (SAM-36) ────────────────────────────────────────────────

@app.get("/sam/archive/search")
async def archive_search(q: str = "", session: str = "", limit: int = 20):
    """Полнотекстовый поиск в архиве оригинальных сообщений до компрессии.
    Любое сжатое сообщение можно найти здесь и восстановить контекст."""
    if not _mongo_ok:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    docs = archive_repo.search(q, session, limit)
    items = []
    for doc in docs:
        items.append({
            "session_id": doc.get("session_id"),
            "role": doc.get("role"),
            "content": doc.get("content", "")[:500],
            "timestamp": doc.get("timestamp").isoformat() if doc.get("timestamp") else None,
            "archived_at": doc.get("archived_at").isoformat() if doc.get("archived_at") else None,
            "archive_reason": doc.get("archive_reason"),
            "tool_name": doc.get("tool_name"),
        })
    return {"query": q, "session": session, "count": len(items), "items": items}


# ─── Поиск по долгой памяти (SAM-101) ─────────────────────────────────────────

@app.get("/sam/memory/search")
async def memory_search(q: str = "", limit: int = 5):
    """Факты долгой памяти по запросу - тот же поиск, что у входа; строками, как в тике. Для основной модели вне
    шлюза и брифа сессии (.claude/hooks/lead_brief.py)."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="нужен запрос q")
    facts = await asyncio.to_thread(get_rag_context, q, limit)   # эмбеддер синхронный (SAM-74)
    return {"query": q, recall.FACTS: recall.fact_lines(facts or "")}


class MemoryBody(BaseModel):
    fact: str
    summary: str = ""
    tags: str = ""


@app.post("/sam/memory")
async def memory_add(body: MemoryBody):
    """Записать факт в долгую память: путь для основной модели вне шлюза, у которой нет tool remember (SAM-94).
    Секрет команд маскируется на входе, как у любого входа."""
    fact = mask_input(body.fact).text.strip()
    if not fact:
        raise HTTPException(status_code=400, detail="нужен fact: текст факта для памяти")
    result = await asyncio.to_thread(_impl_remember, {"fact": fact, "summary": mask_input(body.summary).text,
                                                      "tags": body.tags})
    return {"result": result}


# ─── Session queue control (SAM-01) ───────────────────────────────────────────

class ReplaceQueueRequest(BaseModel):
    message: str = ""


@app.get("/sam/session/{session_id}/queue")
async def get_session_queue(session_id: str):
    """Снимок очереди pending-сообщений (для monitor UI на старте)."""
    return {
        "session_id": session_id,
        "queue": get_queue(session_id),
        "queue_size": queue_size(session_id),
    }


@app.post("/sam/session/{session_id}/cancel-queue")
async def cancel_session_queue(session_id: str):
    """Сбросить ВСЮ очередь pending-сообщений (для clear-all)."""
    dropped = clear_queue(session_id)
    return {"session_id": session_id, "dropped": dropped,
            "queue_size": queue_size(session_id)}


@app.post("/sam/session/{session_id}/pop-last")
async def pop_last_queued_msg(session_id: str):
    """Удалить ОДНО последнее сообщение из очереди (ESC в мониторе).
    Текущий turn не трогается — он завершится нормально и SAM просто
    не съест отменённое накопленное сообщение."""
    from .session_lock import pop_last_queued
    removed = pop_last_queued(session_id)
    return {
        "session_id": session_id,
        "removed": removed,
        "queue_size": queue_size(session_id),
    }


@app.post("/sam/session/{session_id}/cancel-turn")
async def cancel_current_turn_endpoint(session_id: str):
    """Прервать ТЕКУЩИЙ turn агента (Ctrl+C-стиль). Запомнить ответ не успеет."""
    from .session_lock import cancel_current_turn
    cancelled = cancel_current_turn(session_id)
    return {"session_id": session_id, "turn_cancelled": cancelled}


@app.post("/sam/cancel-all")
async def cancel_all_endpoint():
    """Panic-button: прервать ВСЕ активные turn'ы во всех сессиях.
    Используется когда модель залипла в стриме и не понятно в какой сессии."""
    from .session_lock import cancel_all_turns
    n = cancel_all_turns()
    return {"cancelled": n}


@app.post("/sam/session/{session_id}/undo-last")
async def undo_last_exchange(session_id: str):
    """Удалить последний user+assistant обмен из истории сессии.
    Используется когда не успел отменить ошибочный запрос — `Ctrl+Z`."""
    if not _mongo_ok:
        return {"ok": False, "error": "Mongo unavailable"}
    deleted = session_repo.delete_last_exchange(session_id)
    return {"ok": True, "deleted": deleted, "session_id": session_id}


class ReplyRequest(BaseModel):
    text: str
    model: str = "claude-code"


@app.post("/sam/session/{session_id}/reply")
async def external_reply(session_id: str, body: ReplyRequest):
    """Ответ основной модели, которая отвечает вне шлюза (сессия Claude Code говорит через say): в монитор и
    историю, как ответ любого хода (SAM-91: иначе в мониторе нет её реплик)."""
    event_bus.emit("assistant_reply", {"content": body.text, "voice": body.text,
                                       "session": session_id, "source": "observer", "model": body.model})
    return {"ok": True}


class NoteRequest(BaseModel):
    text: str
    model: str = "claude-code"


@app.post("/sam/session/{session_id}/note")
async def external_note(session_id: str, body: NoteRequest):
    """Текст основной модели без озвучки (voice.layer.say --text-only): в монитор и историю репликой SAM с пометкой
    канала - текст, не голос. Монитор - основной терминал основной модели: схема псевдографикой
    или список показываются моноширинно как есть."""
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="пустой текст: нечего показать в мониторе")
    event_bus.emit("assistant_reply", {"content": body.text, "session": session_id, "source": "observer",
                                       "model": body.model, CHANNEL: TEXT_CHANNEL})
    return {"ok": True}


@app.get("/sam/commands")
async def commands_list():
    """Что монитор подсказывает по «/» (SAM-99): скиллы стенда и команды текущей основной модели."""
    from . import agent as _agent
    model = main_models.current
    return {"model": model.name, "commands": monitor_commands.available(_agent._skills_index, model)}


class CommandRequest(BaseModel):
    name: str
    args: str = ""


@app.post("/sam/session/{session_id}/command")
async def monitor_command(session_id: str, body: CommandRequest):
    """Команда из монитора - основной модели командой, а не фразой (SAM-99). Модели вне шлюза - во входящие с полем
    command (тикер несёт его в тике chat), модели в шлюзе - ходом со строкой COMMAND_MARK."""
    from . import agent as _agent
    model = main_models.pick()
    found = monitor_commands.find(monitor_commands.available(_agent._skills_index, model), body.name)
    if found is None:
        raise HTTPException(status_code=404, detail=monitor_commands.unknown(
            body.name, monitor_commands.available(_agent._skills_index, model)))
    text = monitor_commands.line(found["name"], mask_input(body.args).text)   # SAM-88: секрет дальше входа не идёт
    if not model.in_gateway:
        event_bus.emit("user_message", {"content": text, "session": session_id, COMMAND: found["name"]})
        model.deliver({"text": text, "source": "monitor", "session": session_id, COMMAND: found["name"]})
        return {"ok": True, "model": model.name, "command": found["name"]}
    handler = _turn_handler(sam_agent.run, session_id, None, [])
    await acquire_and_run(session_id, monitor_commands.for_gateway_model(text), handler)
    return {"ok": True, "model": model.name, "command": found["name"]}


class IgnoreRequest(BaseModel):
    reason: str = "поправка ведущего"
    with_phrase: bool = False   # и фразу, на которую был ответ (разговор не к SAM)


@app.post("/sam/session/{session_id}/ignore-last")
async def ignore_last(session_id: str, body: IgnoreRequest):
    """Последний ответ модели (и по желанию фразу перед ним) больше не идёт в контекст и сводку; в мониторе
    скрывается. Зовёт ведущий при поправке (voice.layer.say --fix): бредовый ответ не копится."""
    if not _mongo_ok:
        return {"ok": False, "error": "Mongo unavailable"}
    ids = session_repo.ignore_last(session_id, body.reason)
    if body.with_phrase:
        ids += session_repo.ignore_last(session_id, body.reason, roles=("user",))
    event_bus.emit("history_ignored", {"session": session_id, "msg_ids": ids, "reason": body.reason})
    return {"ok": True, "msg_ids": ids, "session_id": session_id}


@app.post("/sam/session/{session_id}/clear-summaries")
async def clear_session_summaries(session_id: str):
    """Удалить все compressed-summary этой сессии. Оригиналы остаются
    в архиве, компрессия пересчитается заново. Использовать когда
    SAM нагаллюцинировала фактов в summary."""
    if not _mongo_ok:
        return {"ok": False, "error": "Mongo unavailable"}
    deleted = session_repo.clear_summaries(session_id)
    return {"ok": True, "deleted": deleted, "session_id": session_id}


@app.post("/sam/session/{session_id}/replace-queue")
async def replace_session_queue(session_id: str, body: ReplaceQueueRequest):
    """Заменить накопленную очередь одним отредактированным сообщением.
    Используется когда человек через monitor правит то что SAM услышал."""
    dropped = replace_queue(session_id, body.message)
    return {
        "session_id": session_id,
        "dropped": dropped,
        "queue": get_queue(session_id),
    }


# ─── Events ───────────────────────────────────────────────────────────────────

@app.post("/sam/push/ack")
async def push_ack(body: PushAckRequest):
    """Подтверждение модели вне шлюза (ADR-020, п. 5): ход по этим сообщениям очереди push закончен."""
    from .agent import push_dispatcher
    return {"acked": push_dispatcher.ack(body.ids)}


class StandReportBody(BaseModel):
    states: dict[str, str] = {}


# Отчёты надзора стенда (SAM-195): время отбивки - в памяти и файле, смена - событием шины; прошлая отбивка - из файла
_stand_reports = stand_report.StandReports(event_bus.emit, partial(stand_report.save_beat, stand_report.beat_file()),
                                           last=stand_report.load_beat(stand_report.beat_file()))
# Старт шлюза - в окружение: ворота молчания надзора (процесс-наследник) считают молчание не раньше него
os.environ[stand_silence.STARTED_ENV] = str(time.time())


DEFERRED_EVERY_S = env_int("SAM_DEFERRED_EVERY_S", 60)   # шедулер отложенных задач: проход раз в минуту


@app.on_event("startup")
async def deferred_tasks_scheduler():
    """Фоновый шедулер (SAM-260): тяжёлые задачи, принятые при исчерпанном лимите, запускаются сами, когда он откроется."""
    async def loop():
        from . import agent as _agent
        while True:
            await asyncio.sleep(DEFERRED_EVERY_S)
            try:
                await _agent.run_deferred_tasks()
            except Exception as e:
                print(f"[Deferred] ERROR проход шедулера не удался ({type(e).__name__}): {e}")
    asyncio.get_running_loop().create_task(loop(), name="deferred-tasks")


@app.on_event("startup")
async def keep_stand_watch():
    """Надзор лежит - шлюз поднимает его (SAM-195, холодный старт): надзор поднимет шлюз, если упадёт он."""
    stand_watch_keeper.start("gateway")


@app.post(stand_report.REPORT_PATH)
async def stand_watch_report(body: StandReportBody):
    """Отбивка надзора scripts/stand_watch.py; смена состояния уходит событием stand_change (workflow stand-health)."""
    return {"event": _stand_reports.accept(body.states)}


class RestartBody(BaseModel):
    name: str   # компонент стенда: gateway, mcp, voice, bridge, tts, embedder, client, cosy, radar, cortex
    force: bool = False   # без health check: после правки кода или по прямой просьбе


@app.post("/sam/stand/restart")
async def stand_restart(body: RestartBody):
    """Перезапуск компонента для основной модели вне шлюза (MCP sam, SAM-195): без ключа привилегий, как у модели в
    шлюзе."""
    from .agent import restarter
    return {"result": await restarter.restart(body.name, body.force)}


SANDBOX_EXEC_BY = "mcp"     # кто внутри сессии в журнале тёплого контейнера


class SandboxExecBody(BaseModel):
    code: str
    lang: str = ""
    timeout: int = 0


@app.post("/sam/sandbox/exec")
async def sandbox_exec(body: SandboxExecBody):
    """Быстрая песочница для основной модели вне шлюза (MCP sam): тёплый контейнер сессии SAM_AGENT_PARENT_SESSION,
    как у её агентов - та же беседа. Ручка под ключом привилегий: состояние голосовой беседы не трогает любой процесс
    этой машины."""
    from . import sandbox, sandbox_warm
    from .agent import AGENT_PARENT_SESSION
    owner = sandbox.Owner(session=AGENT_PARENT_SESSION, by=SANDBOX_EXEC_BY)
    params = {"code": body.code, "lang": body.lang, "timeout": body.timeout}
    outcome = await asyncio.to_thread(sandbox_warm.pool().exec, owner, params)
    return {"result": outcome.text, "code": outcome.code, "ms": outcome.ms}


@app.on_event("startup")
async def _sweep_warm_sandboxes() -> None:
    """Уборка простоя быстрой песочницы: тёплый контейнер без вызовов дольше SAM_FAST_SANDBOX_IDLE_S гасится."""
    from . import sandbox_warm

    async def sweep() -> None:
        while True:
            await asyncio.sleep(sandbox_warm.sweep_every_s())
            try:
                await asyncio.to_thread(sandbox_warm.pool().sweep)
            except Exception as e:   # уборка не роняет шлюз: строка в лог, следующий круг
                print(f"[Sandbox] уборка тёплых контейнеров не прошла: {type(e).__name__}: {e}")
    asyncio.create_task(sweep(), name="warm-sandbox-sweep")


@app.on_event("startup")
async def _warm_cortex() -> None:
    """Cortex поднимается вместе со шлюзом (SAM-195): чинить стенд некому, пока он ждёт первого хода."""
    from .agent import warm_cortex
    asyncio.create_task(warm_cortex(), name="warm-cortex")


@app.on_event("shutdown")
async def _stop_warm_sandboxes() -> None:
    """Тёплые контейнеры уходят вместе со шлюзом: без этого контейнер живёт, пока исполнитель не дочитает stdin."""
    from . import sandbox_warm
    await asyncio.to_thread(sandbox_warm.pool().stop_all, "шлюз остановлен")


@app.on_event("shutdown")
async def _close_builtin_proxy() -> None:
    from . import builtin_proxy
    await builtin_proxy.close()


@app.post("/sam/event")
async def handle_event(body: EventRequest):
    """Endpoint для событий от n8n и HA."""
    response = await sam_agent.run(
        message=f"[EVENT:{body.event}] {body.context}",
        device_id=body.device_id,
    )
    return {"response": response}


# ─── Client registration ──────────────────────────────────────────────────────

@app.post("/sam/client/register")
async def register_client(body: RegisterRequest):
    """Вызывается client_agent.py при старте каждого ПК."""
    if not _mongo_ok:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")

    device = cat.register_device(_db, body.name, body.ip, {
        "mac":            body.mac,
        "os":             body.os,
        "cpu":            body.cpu,
        "ram_gb":         body.ram_gb,
        "gpu":            body.gpu,
        "mcp_url":        body.mcp_url,
        "capabilities":   body.capabilities,
        "auto_wake":      body.auto_wake,
        "presence_score": 0,
    })

    log.info("[register] '%s' @ %s | gpu=%s | caps=%s",
             body.name, body.ip, body.gpu or "none", body.capabilities)
    return {"status": "ok", "device": device}


@app.post("/sam/client/heartbeat")
async def client_heartbeat(body: HeartbeatRequest):
    """Периодический heartbeat — обновляет last_seen и presence_score."""
    if not _mongo_ok:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")

    cat.heartbeat(_db, body.name, body.presence_score)
    return {"status": "ok"}


@app.post("/sam/client/offline")
async def client_offline(body: OfflineRequest):
    """Вызывается при штатном завершении client_agent."""
    if not _mongo_ok:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")

    cat.mark_offline(_db, body.name)
    log.info("[offline] '%s' went offline", body.name)
    return {"status": "ok"}


# ─── Catalog API ──────────────────────────────────────────────────────────────

@app.get("/sam/catalog")
async def get_catalog(type: Optional[str] = None):
    """Список всех записей каталога (люди, питомцы, устройства)."""
    if not _mongo_ok:
        return []
    return cat.list_all(_db, type)


@app.get("/sam/catalog/devices/online")
async def get_online():
    """Только онлайн-устройства."""
    if not _mongo_ok:
        return []
    return cat.get_online_devices(_db)


# ─── Sessions API ─────────────────────────────────────────────────────────────

# Селектор сессии монитора: вид сессии (gateway/session_kinds.py), свежие сверху; проверка или агент без записей за
# столько часов - устаревшие, монитор их сворачивает
MONITOR_FRESH_H = env_int("SAM_MONITOR_FRESH_H", 24)
MONITOR_TASKS_LIMIT = env_int("SAM_MONITOR_TASKS_LIMIT", 30)
MONITOR_TASKS_CAP = 200   # больше за раз не отдаём: задачи живут сутки (gateway/tasks.py TTL_DONE_H)
LIVE_TASK_STATUSES = ("pending", "running", "waiting")   # gateway/tasks.py: задача ещё идёт
TASK_PREFIX = "task:"
TASK_KEY_LEN = 8   # короткий id задачи: сессия агента task:<первые 8 знаков id>


def _session_kinds_input() -> tuple[frozenset[str], frozenset[str]]:
    """Каналы людей (устройства каталога и этой машины) и сессии, которые удалять нельзя. Каталог не прочитан -
    каналы людей без устройств каталога, защита та же."""
    from . import session_kinds
    try:
        devices = [d.get("name", "") for d in cat.list_all(_db, "device")] if _mongo_ok else []
    except Exception as e:
        log.warning("[Sessions] каталог устройств не прочитан: %s: %s", type(e).__name__, e)
        devices = []
    return (session_kinds.person_sessions([*devices, cat.LOCAL_DEVICE]),
            session_kinds.protected_sessions(cat.LOCAL_DEVICE))


def _live_agent_keys() -> frozenset[str]:
    """Агенты, которые идут в памяти шлюза (фоновая задача не кончилась или ещё зарегистрирована для inject), даже
    если запись задачи уже ушла по TTL; воркеры Cortex идут теми же задачами (agent._launch_worker)."""
    from . import agent
    keys = {tid[:TASK_KEY_LEN] for tid, handle in list(agent._task_handles.items()) if not handle.done()}
    keys |= {name[len(TASK_PREFIX):] for name in list(agent._task_context_map) if name.startswith(TASK_PREFIX)}
    return frozenset(keys)


@app.get("/sam/sessions")
async def get_sessions():
    """Сессии для монитора с видом (канал человека, агент, проверка), свежие первыми; default - устройство этой
    машины, адресат реплики при ленте всех сессий. Mongo не отвечает - только устройство этой машины."""
    from . import session_kinds

    def build() -> list[dict]:
        people, protected = _session_kinds_input()
        try:
            rows = session_kinds.merge_summaries(session_repo.sessions_summary(),
                                                 archive_repo.sessions_summary()) if _mongo_ok else []
        except Exception as e:
            log.warning("[Sessions] список не прочитан: %s: %s", type(e).__name__, e)
            rows = []
        return session_kinds.describe(rows, people, protected, cat.LOCAL_DEVICE, datetime.utcnow(),
                                      timedelta(hours=MONITOR_FRESH_H))
    return await asyncio.to_thread(build)


SESSION_DELETE_NEEDS_KEY = "нужен ключ команд (заголовок X-SAM-Command-Secret) или ключ ведущего"
SESSION_DELETE_WRONG_KEY = "ключ не подошёл"
_session_delete_limiter = soul_api.SecretLimiter(log=log.warning, subject="удаление сессий", tag="[Sessions]")


def _session_delete_authorized(privilege: str | None, owner_secret: str | None) -> None:
    """Ключ ведущего или ключ команд; неверные ключи команд считаются, как у записи души."""
    from . import privilege_key
    from .secret_mask import load_secrets
    if soul_api.lead_key_ok(privilege, privilege_key.key()):
        return
    if _session_delete_limiter.blocked():
        raise HTTPException(status_code=429, detail=f"слишком много неверных ключей: удаление сессий закрыто на "
                                                    f"{round(soul_api.SECRET_BLOCK_S / 60)} мин")
    if soul_api.owner_secret_ok(owner_secret, load_secrets()):
        return
    if owner_secret:
        _session_delete_limiter.fail()
    raise HTTPException(status_code=403, detail=SESSION_DELETE_WRONG_KEY if owner_secret else SESSION_DELETE_NEEDS_KEY)


@app.delete("/sam/sessions/{session_id}")
async def delete_session(session_id: str, x_sam_privilege: Optional[str] = Header(None),
                         x_sam_command_secret: Optional[str] = Header(None)):
    """Удалить сессию со всеми следами (gateway/session_purge.py). Сессию задачи - без ключа, любую другую - с ключом
    человека или ведущего. Канал голоса, устройство этой машины, агент за работой и сессия посреди хода - отказ
    одной строкой."""
    from . import compression, session_kinds, session_purge
    from .session_lock import hold_if_idle
    if not _mongo_ok:
        raise HTTPException(status_code=503, detail="MongoDB недоступна: удалять нечего")
    _, protected = await asyncio.to_thread(_session_kinds_input)
    if session_kinds.is_protected(session_id, protected):
        raise HTTPException(status_code=403, detail=f"сессию {session_id} удалять нельзя: это канал человека")
    if session_kinds.delete_needs_key(session_id):
        _session_delete_authorized(x_sam_privilege, x_sam_command_secret)
    # замок сессии на всё удаление: ход, пришедший в это время, ждёт и пишет уже в пустую историю
    async with hold_if_idle(session_id) as held:
        if not held:
            raise HTTPException(status_code=409, detail=f"в сессии {session_id} идёт ход: дождись конца или прерви")
        if compression.in_progress(session_id):
            raise HTTPException(status_code=409, detail=f"в сессии {session_id} идёт сжатие: повтори через минуту")
        key = session_purge.task_key(session_id)
        if key:
            task = await asyncio.to_thread(_db.tasks.find_one, {"id": {"$regex": "^" + re.escape(key)}}, {"status": 1})
            if key in _live_agent_keys() or (task and task.get("status") in LIVE_TASK_STATUSES):
                raise HTTPException(status_code=409,
                                    detail=f"агент {key} ещё работает: отмени задачу и дождись, пока он свернётся")
        if not await asyncio.to_thread(session_purge.exists, _db, session_id):
            raise HTTPException(status_code=404, detail=f"сессии {session_id} нет")
        gone = await asyncio.to_thread(session_purge.purge, _db, session_id)
    log.info("[Sessions] удалена %s: %s", session_id, gone)
    event_bus.emit("session_deleted", {"session_id": session_id, "deleted": gone})
    return {"status": "ok", "session_id": session_id, "deleted": gone}


@app.get("/sam/tasks")
async def get_tasks_for_monitor(limit: int = 0):
    """Последние задачи агентов любого статуса (панель задач монитора и шапки секций агентов после F5)."""
    if not _mongo_ok:
        return []
    from .tasks import recent_for_monitor
    size = max(1, min(limit or MONITOR_TASKS_LIMIT, MONITOR_TASKS_CAP))
    try:
        return await asyncio.to_thread(recent_for_monitor, _db, size)
    except Exception as e:   # монитор без панели задач лучше, чем 500 на каждом снимке
        log.warning("[tasks] список для монитора не прочитан: %s: %s", type(e).__name__, e)
        return []


# ─── Compress API ─────────────────────────────────────────────────────────────

@app.post("/sam/sessions/{session_id}/compress")
async def compress_session_endpoint(session_id: str):
    """Явное сжатие истории сессии."""
    if not _mongo_ok:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")

    from .agent import compress_session_history
    from .llm_router import get_service_llm

    llm_config = await get_service_llm()   # сводку пишет служебная модель, как в фоне (SAM-226)
    result = await compress_session_history(session_id, llm_config)
    return {"status": "ok", "result": result}


# ─── Tasks API ────────────────────────────────────────────────────────────────

@app.post("/sam/tasks/{task_id}/cancel")
async def cancel_task_endpoint(task_id: str):
    """Отменить задачу. task_id может быть полный UUID или 8-char prefix
    (как показывается в мониторе)."""
    if not _mongo_ok:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")

    from .tasks import cancel_task
    from .agent import _task_handles

    # Резолвим prefix → полный id если нужно
    full_id = task_id
    handle = _task_handles.get(task_id)
    if handle is None:
        # Ищем по prefix среди живых handles
        candidates = [k for k in _task_handles.keys() if k.startswith(task_id)]
        if len(candidates) == 1:
            full_id = candidates[0]
            handle = _task_handles[full_id]
        elif len(candidates) > 1:
            raise HTTPException(
                status_code=409,
                detail=f"Ambiguous prefix: {len(candidates)} matches",
            )

    if handle is not None and not handle.done():
        handle.cancel()

    ok = cancel_task(_db, full_id)
    # Не падаем на 404 если уже не в DB — handle отменён, эмитим cancelled
    event_bus.emit("task_cancelled", {"task_id": full_id[:8]})
    if not ok and handle is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "ok", "task_id": full_id[:8], "cancelled_handle": handle is not None}


# ─── LLM Adapter ──────────────────────────────────────────────────────────────

@app.post("/sam/llm/switch")
async def llm_switch(body: LLMSwitchRequest):
    """Переключить LLM адаптер в рантайме (без рестарта)."""
    try:
        llm_client.set_adapter(body.adapter)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    payload: dict = {"adapter": body.adapter}
    if body.source:
        payload["source"] = body.source
    event_bus.emit("llm_switched", payload)
    from .agent import _current_llm
    return {"status": "ok", **llm_client.get_status(_current_llm)}


@app.get("/sam/llm/status")
async def llm_status():
    """Текущий статус LLM адаптера."""
    from .agent import _current_llm
    return llm_client.get_status(_current_llm)


@app.get("/sam/main-model/options")
async def main_model_options():
    """Для переключателя в мониторе: у каждого агрегатора с ключом активная модель и список для выбора."""
    from .llm_router import model_options
    return {"sources": model_options(), "current": os.getenv("LLM_SOURCE", "") or "auto"}


class MainModelRequest(BaseModel):
    source: str = ""
    model: str = ""
    main: str = ""   # claude-code | llm - кто основная (SAM-176)


@app.post("/sam/main-model")
async def switch_main_model(body: MainModelRequest):
    """SAM-129, SAM-176: сменить основную модель (main) или поставщика модели шлюза (source) без рестарта;
    недоступное не ставится (409)."""
    from .agent import _impl_switch_main_model
    result = await _impl_switch_main_model(body.model_dump())
    if not result.startswith("OK"):
        raise HTTPException(status_code=409, detail=result)
    llm = await get_best_llm(task="turn")   # то, чем пойдёт ход, а не задача default
    return {"status": "ok", "result": result, "main_model": main_models.current.name,
            "llm": llm["source"] if llm else None, "model": llm["model"] if llm else None}


@app.get("/sam/metrics/tokens/sessions/{session_id}")
async def token_usage_by_role(session_id: str):
    """SAM-48: токены сессии по ролям (main, worker, guard, summarize)."""
    if not _mongo_ok:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"session_id": session_id, "by_role": TokenUsageRepo(_db).totals_by_role(session_id)}


BALANCE_TIMEOUT_S = 10   # ответ API баланса и каталога поставщика; дольше - строка ошибки вместо ответа


async def _http_get_json(url: str, headers: dict) -> dict:
    async with httpx.AsyncClient(timeout=BALANCE_TIMEOUT_S) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


@app.get("/sam/usage/dev-budget")
async def usage_dev_budget():
    """Остатки лимитов тяжёлой работы SAM (SAM-260): расход за окно, сутки, неделю, месяц, ступень, можно ли агента."""
    if not _mongo_ok:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    from . import agent as _agent, dev_budget
    spent, stage, allowed, reason = _agent._dev_budget_state()
    limits = dev_budget.Limits()
    return {"stage": stage, "allowed": allowed, "reason": reason, "monthly_cap_rub": dev_budget.MONTHLY_CAP_RUB,
            "spent_rub": {"hour": round(spent.hour, 2), "window": round(spent.window, 2), "day": round(spent.day, 2),
                          "week": round(spent.week, 2), "month": round(spent.month, 2)},
            "limit_rub": {"hour": limits.hour_rub, "window": limits.window_rub, "day": limits.day_rub, "week": limits.week_rub,
                          "month": limits.month_rub}, "window_hours": limits.window_h}


@app.get("/sam/usage/cost")
async def usage_cost(days: int = 7, group_by: str = "role"):
    """Сколько стоили вызовы LLM за days дней (по таблице цен gateway/prices.json) и остаток у поставщиков с API баланса."""
    if not _mongo_ok:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    from . import balance, http_json, pricing, usage_cost as _usage_cost
    if group_by not in _usage_cost.GROUPS:
        raise HTTPException(status_code=400, detail=f"group_by: {', '.join(_usage_cost.GROUPS)}")
    rows = TokenUsageRepo(_db).rows_since(datetime.utcnow() - timedelta(days=days))
    return {"days": days, **_usage_cost.summarize(rows, pricing.load_price_book(), group_by),
            "balances": await balance.fetch_balances(_http_get_json, http_json.mcp_tool)}


@app.get("/sam/models/recommend")
async def models_recommend(steps: int | None = None, kind: str = "", env: str = "", deadline_s: float | None = None):
    """Какая модель лучше под задачу (SAM-258): выбор оркестратора по прикидке шагов, виду работы и среде, без запуска."""
    from . import model_picker
    return await model_picker.recommend(_db if _mongo_ok else None, _http_get_json, steps, llm_client.cooling_sources(),
                                        deadline_s, kind=kind, env=env)


@app.get("/sam/models/tierlist")
async def models_tierlist():
    """Тирлист моделей из списков переключения: рейтинг, уровень по факту, цена, скорость, вызовы (SAM-233)."""
    from . import model_picker
    return {"models": await model_picker.tier_list(_db if _mongo_ok else None, _http_get_json)}


# ─── Self-config (voice speed, pitch, volume, persona) ───────────────────────

@app.get("/sam/config")
async def get_self_config():
    """Текущий runtime self-config SAM."""
    from .self_config import get_instance
    return get_instance().all()


@app.get("/sam/config/schema")
async def self_config_schema():
    """Пределы, варианты и переключатели self-config: форма настроек монитора строит поля по ним, не по догадке."""
    from .self_config import BOOLEAN_KEYS, CHOICES, LIMITS
    return {"limits": {k: list(v) for k, v in LIMITS.items()}, "choices": {k: list(v) for k, v in CHOICES.items()},
            "booleans": sorted(BOOLEAN_KEYS)}


class ConfigTuneRequest(BaseModel):
    key: str
    value: float | bool | str


@app.post("/sam/config/tune")
async def tune_self_config(body: ConfigTuneRequest):
    """Изменить один ключ. Альтернатива tool tune_self для UI/curl."""
    from .self_config import get_instance
    ok, msg = get_instance().tune(body.key, body.value)
    if not ok:
        return {"ok": False, "error": msg}
    return {"ok": True, "message": msg, "config": get_instance().all()}


@app.post("/sam/config/reset")
async def reset_self_config():
    from .self_config import get_instance
    get_instance().reset()
    return {"ok": True, "config": get_instance().all()}


# ─── Душа SAM: разделы системного промпта, история и откат (gateway/soul_api.py) ──

def _soul_service():
    from . import agent
    return agent.soul_service


app.include_router(soul_api.make_router(_soul_service))


def _household_changed() -> None:
    from . import agent
    agent.household_block.invalidate()


app.include_router(household_api.make_router(lambda: _db if _mongo_ok else None, _household_changed))


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    llm    = await get_best_llm()
    online = cat.get_online_devices(_db) if _mongo_ok else []
    body = {
        "status":         "ok",
        "main_model":     main_models.pick().name,   # SAM-91: кто ведёт - ярлык монитора, доступная модель с первого запроса
        "llm":            llm["source"] if llm else None,
        "model":          llm["model"] if llm else None,
        "devices_online": [d["name"] for d in online],
    }
    if not llm:  # все поставщики лежат: /health это и говорит, а не падает с 500
        body["llm_error"] = NO_LLM
    if err := lmstudio_error():  # SAM-57: почему LM Studio пропущена
        body["lmstudio_error"] = err
    body["components"] = await health_check(_health_probes())  # SAM-80
    # SAM-97: несжатый хвост, последнее сжатие, ожидание паузы, ошибка; Mongo синхронна - в потоке
    body["compression"] = await asyncio.to_thread(compression_watch.status)
    from .agent import _decisions
    body["workflow_decisions"] = _decisions().status()   # ADR-020, п. 11: точки решения в коде и их workflow
    if _workflow_scheduler is not None:   # SAM-96: когда каждый workflow запустится и запускался
        try:
            body["workflows"] = await asyncio.to_thread(_workflow_scheduler.status)
            body["workflow_tick"] = _workflow_scheduler.tick_status()   # ADR-020, п. 10
        except Exception as e:
            body["workflows"] = {"error": str(e).splitlines()[0] if str(e) else type(e).__name__}
    body["quiet"] = await asyncio.to_thread(_quiet_book.active)   # тихие сессии (gateway/quiet_mode.py)
    from .agent import fast_backoff
    body["fast_backoff"] = fast_backoff.status()   # SAM-168: сессии, где быстрая молчит после поправок, и остаток в с
    body["stand_watch"] = _stand_reports.status()   # SAM-195: последняя отбивка надзора и её возраст
    return body


def _health_probes() -> dict:
    """Проверки компонентов: Mongo, Qdrant из RAG, MCP-агрегатор этой машины (MCP_PORT), процессы Cortex шлюза."""
    import httpx
    from .access import auth_headers
    from . import rag

    def _mongo():
        if not _mongo_ok:
            raise ConnectionError("MongoDB недоступна при старте gateway")
        _db.client.admin.command("ping")

    def _qdrant():
        store = rag._store()
        if store is None:
            raise ConnectionError("RAG не инициализирован")
        store._get_qdrant().get_collections()

    def _mcp():
        port = os.getenv("MCP_PORT", "3000")
        httpx.get(f"http://127.0.0.1:{port}/health", headers=auth_headers(), timeout=1).raise_for_status()

    def _cortex():   # SAM-195: процессы Cortex в шлюзе (поставщик acp, агенты в сессиях Cortex)
        from .agent import cortex_down
        if cortex_down():
            raise ConnectionError("процесс Cortex не жив; поднимет надзор (POST /sam/stand/restart cortex) "
                                  "или restart_component cortex")

    return {"mongo": _mongo, "qdrant": _qdrant, "mcp": _mcp, "cortex": _cortex}


# ─── Monitor UI ───────────────────────────────────────────────────────────────

@app.get("/sandbox/{job}/{name:path}")
async def sandbox_file(job: str, name: str):
    """Файл папки задачи песочницы. CSP sandbox без allow-same-origin: у страницы нет своего
    origin, до API gateway и cookie ключа она не дотягивается, даже открытая напрямую."""
    from . import sandbox
    # содержимое, а не путь: FileResponse открыл бы путь позже, когда контейнер задачи мог подменить его ссылкой
    content = sandbox.served_file(sandbox.SANDBOX_DIR, job, name)
    if content is None:
        raise HTTPException(status_code=404, detail="нет такого файла в песочнице")
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(content, media_type=media, headers={"Content-Security-Policy": SANDBOX_CSP,
                                                        "X-Content-Type-Options": "nosniff"})


SANDBOX_CSP = "sandbox allow-scripts"
APP_PROXY_TIMEOUT_S = 30           # весь ответ живого приложения странице; дольше - приложение зависло
APP_PASS_HEADERS = ("content-type", "accept", "accept-language")
# Потолки прокси: страница, график, небольшой JSON; больше - приложение льёт без конца, шлюз не держит это в памяти
APP_PROXY_MAX_BYTES = env_int("SAM_APPS_PROXY_MAX_BYTES", 20_000_000)
APP_PROXY_MAX_BODY = env_int("SAM_APPS_PROXY_MAX_BODY", 2_000_000)


def _apps_root() -> Path:
    """Корень приложений в момент запроса: тесты и настройка подменяют apps.APPS_DIR."""
    from . import apps
    return apps.APPS_DIR


# Страница «Приложения» и их API стенда (SAM-204, gateway/apps_api.py): встроенное приложение шлюза
app.include_router(apps_api.make_router(_apps_root))


@app.get("/apps-panel")
async def apps_panel_link():
    """Прежний адрес панели приложений (отдельный процесс до SAM-204): закладки и ссылки старых событий."""
    return RedirectResponse(apps_api.PAGE_PATH)


@app.get("/apps/{app_ref}")
async def app_view(app_ref: str, request: Request):
    """Приложение в каркасе SAM (gateway/app_view.py), страница - во фрейме /apps/<slug>/. По id - на slug,
    встроенное - на его настоящий адрес."""
    from . import app_view as view, apps, builtin_apps, builtin_proxy
    if app_ref in apps.RESERVED_SLUGS:
        b = await asyncio.to_thread(builtin_apps.find, apps.APPS_DIR, app_ref)
        if b is None:
            raise HTTPException(status_code=404, detail=f"встроенного приложения {app_ref} нет")
        if builtin_apps.is_proxied(b):
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"{builtin_apps.page_path(b)}{query}", status_code=BUILTIN_REDIRECT_STATUS)
        return RedirectResponse(builtin_apps.for_host(b.url, request.url.hostname),
                                status_code=BUILTIN_REDIRECT_STATUS)
    try:
        m = await asyncio.to_thread(apps.find, apps.APPS_DIR, app_ref)
    except apps.ManifestError:
        m = None
    if m is None:
        raise HTTPException(status_code=404, detail="нет такого приложения")
    if app_ref != m.slug:
        return RedirectResponse(apps.view_path(m), status_code=APP_MOVED_STATUS)
    return HTMLResponse(view.render(m))


APP_MOVED_STATUS = 308       # старый адрес /apps/<id>/ - на /apps/<slug>/ с тем же методом и телом (SAM-205)
# Монитор - на его страницу шлюза /monitor; радар отдельным процессом - на /apps/radar/ (gateway/builtin_proxy.py)
BUILTIN_REDIRECT_STATUS = 307
STUB_STATUS = 503            # заглушка вместо страницы: приложение есть, но сейчас не отдаёт её


def _stub(name: str, reason: str) -> HTMLResponse:
    """Общая заглушка приложения (gateway/app_stub.py), с той же CSP sandbox, что у его страницы."""
    from . import app_stub
    return HTMLResponse(app_stub.render(name, reason), status_code=STUB_STATUS,
                        headers={"Content-Security-Policy": SANDBOX_CSP, "Cache-Control": "no-store"})


@app.api_route("/apps/{app_ref}/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"])
async def app_proxy(app_ref: str, path: str, request: Request):
    """Страница живого приложения SAM (SAM-182): запрос идёт на его порт 127.0.0.1, ответ - с CSP sandbox, как у
    песочницы: у страницы нет своего origin, до API шлюза и cookie она не дотягивается. Зависло или упало
    приложение - одна строка, шлюз отвечает дальше. Адрес - slug; по id - перенаправление на slug, чтобы у
    приложения был один адрес, а ссылки из старых событий монитора открывались."""
    import httpx
    from . import app_settings, app_stub, apps, builtin_apps, builtin_proxy
    if app_ref in apps.RESERVED_SLUGS:
        b = await asyncio.to_thread(builtin_apps.find, apps.APPS_DIR, app_ref)
        if b is None:
            raise HTTPException(status_code=404, detail=f"встроенного приложения {app_ref} нет")
        if builtin_apps.is_proxied(b):
            return await builtin_proxy.forward(b, path, request)
        return RedirectResponse(builtin_apps.for_host(b.url, request.url.hostname),
                                status_code=BUILTIN_REDIRECT_STATUS)
    page = app_stub.wants_page(request.method, request.headers.get("accept", ""))
    m = await asyncio.to_thread(apps.upstream, apps.APPS_DIR, app_ref)
    if m is None:
        try:
            paused = await asyncio.to_thread(apps.find, apps.APPS_DIR, app_ref)
        except apps.ManifestError:
            paused = None
        if paused is not None and page:
            return _stub(paused.name, app_stub.STOPPED)
        raise HTTPException(status_code=404, detail="нет такого запущенного приложения")
    if app_ref != m.slug:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{apps.page_path(m)}{path}{query}", status_code=APP_MOVED_STATUS)
    base = apps.local_base(m)
    if path == app_settings.PAGE_PATH and request.method == "GET":     # настройки - из файла шлюза, не из контейнера
        values = await asyncio.to_thread(app_settings.load, apps.APPS_DIR, m)
        return Response(json.dumps(values, ensure_ascii=False), media_type="application/json",
                        headers={"Content-Security-Policy": SANDBOX_CSP, "X-Content-Type-Options": "nosniff",
                                 "Cache-Control": "no-store"})
    too_big = HTTPException(status_code=413, detail=f"тело запроса больше {APP_PROXY_MAX_BODY} байт")
    if int(request.headers.get("content-length") or 0) > APP_PROXY_MAX_BODY:
        raise too_big
    parts, size = [], 0
    async for part in request.stream():          # без content-length - считаем по ходу, в память не больше потолка
        size += len(part)
        if size > APP_PROXY_MAX_BODY:
            raise too_big
        parts.append(part)
    body = b"".join(parts)
    headers = {k: v for k, v in request.headers.items() if k.lower() in APP_PASS_HEADERS}

    async def fetch() -> tuple[int, dict, bytes]:
        async with httpx.AsyncClient(timeout=APP_PROXY_TIMEOUT_S) as client:
            async with client.stream(request.method, f"{base}/{path}", params=request.query_params, content=body,
                                     headers=headers) as res:
                chunks, size = [], 0
                async for chunk in res.aiter_bytes():
                    size += len(chunk)
                    if size > APP_PROXY_MAX_BYTES:
                        raise ValueError(f"ответ больше {APP_PROXY_MAX_BYTES} байт")
                    chunks.append(chunk)
                return res.status_code, dict(res.headers), b"".join(chunks)

    try:
        status, got, content = await asyncio.wait_for(fetch(), APP_PROXY_TIMEOUT_S)
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as e:
        if page:
            return _stub(m.name, app_stub.NOT_ANSWERING)
        raise HTTPException(status_code=502, detail=f"приложение {m.slug} не отвечает: {str(e) or type(e).__name__}")
    out = {"Content-Security-Policy": SANDBOX_CSP, "X-Content-Type-Options": "nosniff"}
    if location := apps.local_location(got.get("location"), m.slug, base):
        out["Location"] = location
    return Response(content, status_code=status, media_type=got.get("content-type"), headers=out)


@app.websocket("/apps/{app_ref}/{path:path}")
async def app_ws(app_ref: str, path: str, websocket: WebSocket):
    """WebSocket встроенного приложения отдельным процессом (радар) - через шлюз, как и его страница."""
    from . import apps, builtin_apps, builtin_proxy
    b = await asyncio.to_thread(builtin_apps.find, apps.APPS_DIR, app_ref) if app_ref in apps.RESERVED_SLUGS else None
    if b is None or not builtin_apps.is_proxied(b):
        await websocket.close(code=APP_WS_NOT_FOUND)
        return
    await builtin_proxy.relay(b, path, websocket)


APP_WS_NOT_FOUND = 4404      # WebSocket есть только у встроенного приложения отдельным процессом


SHELL_DIR = _STATIC / "shell"       # каркас страниц SAM: копия рамки и панели DotNetSolutionKit и API панелей
SHARED_ASSET_HEADERS = [(b"access-control-allow-origin", b"*")]


# Модули монитора (SAM-230) браузер перепроверяет по ETag на каждой загрузке: иначе после правки свежий main.js
# импортирует имя, которого нет в старом модуле из кеша, и монитор пуст до жёсткой перезагрузки
REVALIDATE_HEADERS = [(b"cache-control", b"no-cache")]


def revalidated(inner):
    """Статика с Cache-Control: no-cache - кеш браузера есть, но каждый раз сверяется с шлюзом."""
    async def asgi(scope, receive, send):
        async def send_revalidated(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers", [])) + REVALIDATE_HEADERS}
            await send(message)
        await inner(scope, receive, send_revalidated)
    return asgi


def shared_assets(inner):
    """Каркас - общая статика для страниц на других портах (радар :8010): шрифт с чужого origin браузер
    без CORS не грузит. Файлы публичные, поэтому разрешение - любому origin."""
    async def asgi(scope, receive, send):
        async def send_shared(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers", [])) + SHARED_ASSET_HEADERS}
            await send(message)
        await inner(scope, receive, send_shared)
    return asgi


app.mount("/static/shell", shared_assets(StaticFiles(directory=SHELL_DIR)), name="shell")
MONITOR_DIR = _STATIC / "monitor"   # модули страницы монитора (корень сборки main.js), их грузит monitor.html
app.mount("/static/monitor", revalidated(StaticFiles(directory=MONITOR_DIR)), name="monitor_static")


@app.get("/shell/config.js")
async def shell_config(request: Request):
    """Навигация каркаса для страниц SAM на любом порту (gateway/shell_config.py)."""
    from . import apps, shell_config as shell
    items = await asyncio.to_thread(shell.nav, apps.APPS_DIR, request.url.port)
    return Response(shell.config_js(items), media_type="text/javascript", headers={"Cache-Control": "no-store"})


@app.get("/monitor")
async def monitor_page():
    return FileResponse(_STATIC / "monitor.html")


# Радар присутствия (SAM-105) - отдельное приложение (radar/); шлюз зовёт только его функции (gateway/radar_client.py)
radar = radar_client.build()


# Снимок монитора при подключении: реплики всех сессий и скрытые (фон, поправки) за то же время (SAM-98)
MONITOR_SNAPSHOT_LIMIT = 300
MONITOR_HIDDEN_LIMIT = env_int("SAM_MONITOR_HIDDEN_LIMIT", 300)
# Первая порция при подключении: экран и немного сверху; остальное монитор берёт прокруткой вверх (/sam/monitor/history)
MONITOR_FIRST_PAGE = env_int("SAM_MONITOR_FIRST_PAGE", 60)


# Сводка пишется поверх записи первого сжатого сообщения и хранит его msg_id: в снимке у неё свой id, иначе монитор
# после обрыва принял бы новую сводку за уже показанное сообщение
SUMMARY_ID_PREFIX = "summary:"


def _record_id(doc: dict) -> str:
    """Постоянный id записи истории в снимке: msg_id живого события, иначе _id записи (сводки, старые записи).
    По нему монитор после обрыва отличает уже показанное от нового."""
    return doc.get("msg_id") or str(doc.get("_id", ""))


@dataclass
class MonitorPage:
    """Порция истории монитора: события, время и _id самой ранней записи (с них просят следующую) и сколько видимых
    записей взято - упёрлась ли порция в предел (часть записей событий не даёт: тело скилла, пустой ответ)."""
    events: list[str]
    oldest: str | None
    visible: int
    oldest_id: str | None = None


def _build_mongo_snapshot(before: datetime | None = None, limit: int | None = None,
                          session: str | None = None, before_id: ObjectId | None = None) -> MonitorPage:
    """
    Синхронный снимок текущего состояния из MongoDB; session - только эта сессия.

    Вызывать только через run_in_executor — не блокирует event loop.
    События - JSON-строки, готовые к отправке через WebSocket.
    """
    if not _mongo_ok:
        return MonitorPage([], None, 0)

    events: list[str] = []
    now_utc = datetime.utcnow()
    ts_now = now_utc.strftime("%H:%M:%S")
    ts_now_iso = now_utc.isoformat() + "Z"
    all_docs = []

    try:
        # Последние N сообщений из ВСЕХ сессий — единым Mongo запросом
        # сортированным по timestamp DESC, потом reverse для chronology.
        # Это и быстрее (один query), и корректнее: summary одной сессии
        # не вылезают между свежими сообщениями другой.
        cursor = session_repo.recent_for_monitor(limit or MONITOR_SNAPSHOT_LIMIT, MONITOR_HIDDEN_LIMIT, before,
                                                 session, before_id)
        for m in cursor:
            ts_doc = m.get("timestamp") or now_utc
            sid = m.get("session_id", "default")
            all_docs.append((ts_doc, sid, m))
        all_docs.sort(key=lambda x: (x[0], str(x[2].get("_id", ""))))   # порядок как у порций: время, затем _id

        for ts, sid, m in all_docs:
            ts_str = ts.strftime("%H:%M:%S") if ts else ts_now
            ts_iso = (ts.isoformat() + "Z") if ts else ts_now_iso
            role    = m.get("role", "user")
            content = m.get("content", "")

            if role == "summary":
                events.append(json.dumps({
                    "type":    "context_compressed",
                    "replay":  True,
                    "msg_id":  SUMMARY_ID_PREFIX + str(m.get("_id", "")),
                    "ts":      ts_str,
                    "ts_iso":  ts_iso,
                    "session": sid,
                    "total_msgs": 0,
                    "summaries": [{
                        # строка ленты - по msg_id сводки (живые слияния каскада находят её), скрытые - помечены
                        "agg_id":     m.get("msg_id", ""),
                        "ignore":     m.get("ignore"),
                        "time_range": m.get("time_range", ts_str[:5]),
                        "time_from":  m.get("time_from", ts_str),
                        "time_to":    m.get("time_to", ts_str),
                        "content":    content,
                    }],
                }, ensure_ascii=False))

            elif role == "user":
                events.append(json.dumps({
                    "type":    "user_message",
                    "ts":      ts_str,
                    "ts_iso":  ts_iso,
                    "session": sid,
                    "content": content,
                    **_replay_marks(m),
                    "msg_id":  _record_id(m),
                }, ensure_ascii=False))

            elif role == "assistant" and content:
                # Унифицированный путь: агент сохраняет tool_call
                # отдельной записью с role="tool_call".
                # Старый формат <tool_call>XML</tool_call> — legacy,
                # обрабатываем для обратной совместимости.
                if content.startswith("<tool_call>"):
                    for n, match in enumerate(re.finditer(
                        r"<tool_call>(.*?)</tool_call>",
                        content,
                        re.DOTALL,
                    )):
                        try:
                            tc = json.loads(match.group(1))
                        except Exception:
                            tc = {}
                        events.append(json.dumps({
                            "type":    "tool_call",
                            "ts":      ts_str,
                            "ts_iso":  ts_iso,
                            "session": sid,
                            "name":    tc.get("name", ""),
                            "args":    tc.get("arguments", {}),
                            "call_id": "",
                            "msg_id":  f"{_record_id(m)}:{n}",
                        }, ensure_ascii=False))
                else:
                    events.append(json.dumps({
                        "type":    "assistant_reply",
                        "ts":      ts_str,
                        "ts_iso":  ts_iso,
                        "session": sid,
                        **replay_fate(m),
                        **_replay_marks(m),
                        "msg_id":  _record_id(m),
                    }, ensure_ascii=False))

            elif role == "tool_call":
                events.append(json.dumps({
                    "type":    "tool_call",
                    "ts":      ts_str,
                    "ts_iso":  ts_iso,
                    "session": sid,
                    "name":    m.get("tool_name", content),
                    "args":    m.get("tool_args", m.get("args", {})),
                    "call_id": m.get("call_id", ""),
                    "msg_id":  _record_id(m),
                }, ensure_ascii=False))

    except Exception as exc:
        log.error("[snapshot] ошибка: %s", exc)

    # самая ранняя запись, а не событие: запись без события (тело скилла) не сдвигает следующую порцию вперёд
    oldest = (all_docs[0][0].isoformat() + "Z") if all_docs else None
    oldest_id = str(all_docs[0][2]["_id"]) if all_docs and all_docs[0][2].get("_id") else None
    return MonitorPage(events, oldest, sum(1 for *_, m in all_docs if not m.get(IGNORE)), oldest_id)


@app.on_event("startup")
async def _monitor_indexes() -> None:
    """Индексы, по которым монитор листает историю и задачи (порции по времени и _id, лента одной сессии); создание
    идемпотентно, сбой - строка в лог."""
    from . import tasks
    if not _mongo_ok:
        return

    def ensure() -> None:
        session_repo.ensure_indexes()
        try:
            tasks.ensure_indexes(_db)
        except Exception as e:
            print(f"[Tasks] index init: {e}")
    await asyncio.to_thread(ensure)


# Уборка сессий (gateway/session_purge.py): раз в столько секунд; агент, которого нельзя оживить, - через столько
# часов после последней записи (сутки, как живёт задача после done: упавший агент держит историю столько же),
# сессия с меткой проверки - после столько часов молчания. SAM_SESSION_SWEEP=0 выключает (тесты)
SESSION_SWEEP_S = env_int("SAM_SESSION_SWEEP_S", 3600)
DEAD_AGENT_GRACE_H = env_int("SAM_DEAD_AGENT_GRACE_H", 24)
CHECK_SESSION_TTL_H = env_int("SAM_CHECK_SESSION_TTL_H", 72)
MONGO_URI_DEFAULT = "mongodb://localhost:27017"   # как у подключения шлюза (gateway/agent.py)
MONGO_PING_TIMEOUT_MS = 2000
_sweep_client = None   # своё подключение уборки, если шлюз стартовал без Mongo


def _sweep_db():
    """База для уборки: шлюза, а если он стартовал без Mongo - своё подключение, как только Mongo ответит."""
    global _sweep_client
    if _mongo_ok:
        return _db
    from pymongo import MongoClient
    try:
        if _sweep_client is None:
            _sweep_client = MongoClient(os.getenv("MONGODB_URI", MONGO_URI_DEFAULT),
                                        serverSelectionTimeoutMS=MONGO_PING_TIMEOUT_MS)
        _sweep_client.admin.command("ping")
        return _sweep_client["sam"]
    except Exception:
        return None


@app.on_event("startup")
async def _sweep_sessions() -> None:
    """Сессии умерших агентов и старых проверок уходят сами: TTL Mongo снимает задачу и контекст агента, а не его
    историю. Mongo нет - попытка через период."""
    from . import session_purge
    if not env_bool("SAM_SESSION_SWEEP", True):
        return

    async def run() -> None:
        while True:
            try:
                db = await asyncio.to_thread(_sweep_db)
                if db is not None:
                    ids = await asyncio.to_thread(session_purge.sweep, db, datetime.utcnow(),
                                                  timedelta(hours=DEAD_AGENT_GRACE_H),
                                                  timedelta(hours=CHECK_SESSION_TTL_H), _live_agent_keys())
                    if ids:
                        print(f"[Sessions] убрано {len(ids)}: {', '.join(ids)}")
            except Exception as e:   # уборка не роняет шлюз: строка в лог, следующая попытка через период
                print(f"[Sessions] уборка не прошла: {type(e).__name__}: {e}")
            await asyncio.sleep(SESSION_SWEEP_S)
    asyncio.create_task(run(), name="session-sweep")


@app.on_event("startup")
async def _warm_embedder() -> None:
    """Эмбеддер памяти грузится в фоне на старте, а не на первом ходе.

    Замер 9 окт: import torch 3,3 с, import sentence_transformers 12,6 с, модель 3,2 с - первый ход
    после старта ждал 20-33 с до первого вызова LLM (rag.get_rag_context -> _get_embedder).
    SAM_WARM_EMBEDDER=0 выключает прогрев и сверку скиллов: так тесты с TestClient не ходят в Qdrant и память стенда.
    """
    from . import rag
    if not env_bool("SAM_WARM_EMBEDDER", True):
        return

    def warm() -> None:
        rag.ensure_collection()
        rag.embed("warmup")
        print(f"[Skills] сверка с памятью: {sync_skills()}")   # факты скиллов - после прогрева, без ожидания хода

    async def run() -> None:
        try:
            await asyncio.to_thread(warm)
            print("[RAG] эмбеддер прогрет")
        except Exception as exc:
            print(f"[RAG] прогрев эмбеддера не удался, загрузится на первом ходе: {exc}")

    asyncio.create_task(run(), name="warm-embedder")


_workflow_scheduler = None   # SAM-96: планировщик workflow стенда; его состояние - в /health


@app.on_event("startup")
async def _start_workflows() -> None:
    """Лёгкие workflow стенда (SAM-96), среди них своя инициатива SAM-49: SAM_WORKFLOWS=0 выключает."""
    global _workflow_scheduler
    from . import workflows
    from .agent import build_workflows
    if not workflows.ENABLED:
        return
    _workflow_scheduler = build_workflows()
    if _workflow_scheduler is None:
        print("[Workflow] выключены: нет MongoDB")
        return
    asyncio.create_task(_workflow_scheduler.run(), name="workflows")
    from .agent import push_dispatcher
    asyncio.create_task(push_dispatcher.run(), name="workflow-push")   # липкие шаги: доставка созревших пачек


# Старт шлюза поднимает живые приложения настоящим Docker; 0 - нет (тесты: фоновый подъём успевал поднять
# приложение из манифеста теста, и контейнеры оставались висеть, занимая подсеть порта)
APPS_REVIVE = env_bool("SAM_APPS_REVIVE", True)


@app.on_event("startup")
async def _revive_apps() -> None:
    """Живые приложения SAM (SAM-182), которые должны работать и не работают, - поднять; docker в потоке. Манифесты
    встроенных (монитор, радар) - записать, если их нет."""
    from . import apps, builtin_apps

    async def revive() -> None:
        try:
            lines = await asyncio.to_thread(builtin_apps.ensure, apps.APPS_DIR)
            if APPS_REVIVE:
                lines += await asyncio.to_thread(apps.revive, apps.APPS_DIR)
        except Exception as e:   # старт шлюза не зависит от приложений: одна строка вместо стека
            lines = [f"подъём приложений не прошёл: {type(e).__name__}: {e}"]
        for line in lines:
            print(f"[Apps] {line}")
    asyncio.create_task(revive(), name="apps-revive")


@app.on_event("startup")
async def _start_dream() -> None:
    """Ночная консолидация памяти (SAM-22): SAM_DREAM=1 включает; без Mongo или памяти - строка в лог."""
    from . import dream
    from .agent import build_dream
    if not dream.ENABLED:
        return
    job = build_dream()
    if job is None:
        print("[Dream] выключена: нет MongoDB или хранилища памяти")
        return
    asyncio.create_task(job.run(), name="dream")


@app.websocket("/voice/turn")
async def voice_turn_ws(ws: WebSocket):
    """Голосовой ход потоком (SAM-53); протокол - gateway/voice_socket.py."""
    from .agent import run_voice_turn, settle_spoken
    from .session_lock import cancel_current_turn
    from .voice_socket import serve_voice_socket
    await ws.accept()
    try:
        await serve_voice_socket(ws, run_voice_turn, acquire_and_run, cancel_current_turn,
                                 on_spoken=settle_spoken)
    except WebSocketDisconnect:
        pass


def _replay_marks(doc: dict) -> dict:
    """Поля записи, которые монитор показывает и после F5: id (скрыть по history_ignored), кто сказал, пометка,
    канал (текст без озвучки), команда монитора."""
    return {k: doc[k] for k in ("msg_id", "who", IGNORE, CHANNEL, COMMAND) if doc.get(k)}


SNAPSHOT_BEGIN, SNAPSHOT_END = "snapshot_begin", "snapshot_end"
MONITOR_PAGE_LIMIT = env_int("SAM_MONITOR_PAGE_LIMIT", 100)   # порция истории при прокрутке вверх


@app.get("/sam/monitor/history")
async def monitor_history(before: str, limit: int = 0, session: str = "", before_id: str = ""):
    """Порция истории монитора раньше `before` (ISO с Z) и `before_id` (_id самой ранней показанной записи, записи
    той же миллисекунды не теряются): листают вверх - старые подгружаются, последние уже на месте. session - лента
    одной сессии или агента (`task:<id>`)."""
    try:
        when = datetime.fromisoformat(before.removesuffix("Z"))
    except ValueError:
        raise HTTPException(status_code=400, detail="before - время ISO, как ts_iso события")
    if before_id and not ObjectId.is_valid(before_id):
        raise HTTPException(status_code=400, detail="before_id - _id записи, как oldest_id порции")
    size = max(1, min(limit or MONITOR_PAGE_LIMIT, MONITOR_SNAPSHOT_LIMIT))
    page = await asyncio.to_thread(_build_mongo_snapshot, when, size, session or None,
                                   ObjectId(before_id) if before_id else None)
    return {"events": [json.loads(e) for e in page.events], "oldest": page.oldest, "oldest_id": page.oldest_id}


@app.websocket("/ws/monitor")
async def monitor_ws(ws: WebSocket):
    global _ws_count
    if _ws_count >= _MAX_WS_CONNECTIONS:
        await ws.close(code=1008, reason="Too many connections")
        return

    await ws.accept()
    _ws_count += 1

    try:
        # подписка до снимка: события, пришедшие, пока он строится, ждут в очереди; повтор снимка монитор отсечёт
        # по msg_id
        q = event_bus.subscribe()
        # Снимок из MongoDB — в executor, не блокируем event loop
        loop     = asyncio.get_running_loop()
        page     = await loop.run_in_executor(None, _build_mongo_snapshot, None, MONITOR_FIRST_PAGE)
        # Снимок в рамке: монитор собирает его целиком и показывает разом, уже прокрученным к последнему; при
        # переподключении дописывает только то, чего ещё не показывал (по msg_id)
        try:
            await ws.send_text(json.dumps({"type": SNAPSHOT_BEGIN}))
            for ev in page.events:
                await ws.send_text(ev)
            # full - снимок упёрся в порцию: раньше него могло быть ещё, и после долгого обрыва монитор это покажет
            await ws.send_text(json.dumps({"type": SNAPSHOT_END, "oldest": page.oldest, "oldest_id": page.oldest_id,
                                           "full": page.visible >= MONITOR_FIRST_PAGE}))
            # ходы, которые идут прямо сейчас: «SAM думает» и «прервать» не теряются после обрыва связи
            for sid in busy_sessions():
                await ws.send_text(json.dumps({"type": "session_busy", "session": sid,
                                               "ts_iso": datetime.utcnow().isoformat() + "Z"}))
        except Exception:
            event_bus.unsubscribe(q)
            return

        async def _recv() -> None:
            """Принимаем inject-сообщения от клиента монитора."""
            try:
                while True:
                    raw = await ws.receive_text()
                    try:
                        data = json.loads(raw)
                        if data.get("type") == "inject":
                            event_bus.inject(
                                data.get("session_id", "default"),
                                mask_input(data.get("message", "")).text,   # SAM-88
                            )
                    except Exception:
                        pass
            except Exception:
                pass

        recv_task = asyncio.create_task(_recv())
        try:
            while True:
                # ждём событие или уход клиента: uvicorn сообщает об уходе только через receive, и без этого
                # закрытая вкладка держала бы слот до следующего события шины
                getter = asyncio.ensure_future(q.get())
                done, _ = await asyncio.wait({getter, recv_task}, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    break
                msg = getter.result()
                if msg == SUBSCRIBER_DROPPED:
                    # 1013 try again later: монитор переподключится и получит снимок
                    await ws.close(code=1013, reason="monitor fell behind")
                    break
                await ws.send_text(msg)
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            recv_task.cancel()
            event_bus.unsubscribe(q)

    finally:
        _ws_count -= 1


# ─── Inject ───────────────────────────────────────────────────────────────────

@app.post("/sam/voice/state")
async def voice_state(req: Request):
    """Voice client шлёт сюда события состояния mic'a — для индикатора в мониторе:
    state: 'wake_detected' | 'rec_start' | 'rec_end' | 'stt_done'
    + опц text/rms."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    event_bus.emit("voice_state", {
        "session": body.get("session", "voice"),
        "state":   body.get("state", ""),
        "text":    body.get("text", ""),
        "rms":     body.get("rms", 0),
    })
    return {"ok": True}


@app.post("/sam/inject")
async def inject_endpoint(body: InjectRequest):
    """Inject сообщения в сессию. Для task:xxx — если worker activeyy
    → обычный inject через event_bus, если idle → reanimate через
    _resume_worker_task (SAM-38: переиспользует историю воркера)."""
    sid = body.session_id
    msg = mask_input(body.message).text   # SAM-88

    # Не-task сессия — во основную модель тем же путём, что чат (SAM-91); модель в шлюзе - обычный inject
    if not sid.startswith("task:"):
        model = main_models.pick()
        if not model.in_gateway:
            await recall_at_input(model, msg, sid, message={"text": msg, "source": "inject", "session": sid})
            return {"message": msg, "status": "ok", "kind": "main_model"}
        if not is_busy(sid):   # хода нет: очередь читает только идущий ход, поэтому простаивающую модель будим ходом (SAM-259)
            handler = _turn_handler(sam_agent.run, sid, None, [])
            task = asyncio.create_task(acquire_and_run(sid, msg, handler), name=f"inject-wake:{sid}")

            def report_failure(done):
                if done.cancelled():
                    return
                exc = done.exception()
                if exc is not None:
                    error = f"ход inject-wake сессии {sid} упал: {type(exc).__name__}: {str(exc)[:160]}"
                    log.error("[Inject] ERROR %s", error)
                    event_bus.emit("turn_failed", {"session": sid, "error": error})

            task.add_done_callback(report_failure)
            return {"message": msg, "status": "ok", "kind": "woke_main_model"}
        event_bus.inject(sid, msg)
        return {"message": msg, "status": "ok", "kind": "live_inject"}

    # task: сессия - в агента: работающему в ход, завершившемуся - воскрешение (gateway/tools/agents.py)
    return agents_tool.answer_agent_impl({"session_id": sid, "message": msg}, agent_answer_deps())


# ─── Агенты основной модели (SAM-91) ──────────────────────────────────────────

# Причина отказа ядра -> код HTTP
_AGENT_ERROR_STATUS = {agents_tool.BAD_REQUEST: 400, agents_tool.BUSY: 429,
                       agents_tool.UNAVAILABLE: 503, agents_tool.NOT_FOUND: 404}


@app.post("/sam/agents")
async def create_agent_endpoint(body: AgentRequest):
    """Основная модель вне шлюза создаёт агента SAM: тот же воркер, что spawn_agent. Вопросы агента приходят ей
    во входящие (тик kind chat, source agent_question), ответ - POST /sam/inject в сессию task:<id>."""
    result = await agents_tool.create_agent_impl(body.model_dump(exclude_none=True), agent_create_deps())
    if result["status"] != "ok":
        raise HTTPException(status_code=_AGENT_ERROR_STATUS.get(result["reason"], 400), detail=result["error"])
    return result


class AgentQuestion(BaseModel):
    question: str


class AgentStatus(BaseModel):
    message: str


@app.post("/sam/agents/{task_id}/ask")
async def agent_ask(task_id: str, body: AgentQuestion):
    """Канал агента разработки (SAM-164, mcp/agent_channel_server.py): вопрос создателю, как ask_creator воркера."""
    from .agent import agent_channel_ask
    result = await asyncio.to_thread(agent_channel_ask, task_id, mask_input(body.question).text)
    if result["status"] != "ok":
        raise HTTPException(status_code=404, detail=result["error"])
    return result


# POST: чтение снимает ответы из очереди, а GET зовёт любая страница картинкой (SAM-188)
@app.post("/sam/agents/{task_id}/inbox")
async def agent_inbox(task_id: str):
    """Реплики создателя в сессию task:<id> с прошлого раза: ответ на вопрос агента разработки, вводная."""
    from .agent import agent_channel_inbox
    messages = agent_channel_inbox(task_id)
    if messages is None:
        raise HTTPException(status_code=404, detail=f"задача {task_id} не идёт")
    return {"messages": messages}


class AgentGrant(BaseModel):
    tool: str


@app.post("/sam/agents/{task_id}/grant")
async def agent_grant(task_id: str, body: AgentGrant):
    """Создатель выдаёт агенту Cortex инструмент по его запросу права (SAM-133 этап 4)."""
    from .agent import _cortex_workers
    result = await _cortex_workers().grant(task_id, body.tool)
    if not result.get("granted"):
        raise HTTPException(status_code=409, detail=result.get("reason", "не выдано"))
    return result


class AgentEffort(BaseModel):
    direction: str = "more"     # more | less
    reason: str = ""


@app.post("/sam/agents/{task_id}/effort")
async def agent_effort(task_id: str, body: AgentEffort):
    """Создатель извне шлюза даёт агенту больше или меньше сил (SAM-258): подборщик берёт соседнюю модель по цене."""
    from .agent import set_agent_effort
    return {"result": await set_agent_effort(task_id, body.direction, body.reason)}


@app.get("/sam/agents/{task_id}/info")
async def agent_info_endpoint(task_id: str):
    """Что за агент: модель, шаги, время работы, смены сил (только чтение)."""
    from .agent import agent_info
    return agent_info(task_id)


class AgentSteps(BaseModel):
    steps: int
    reason: str = ""


@app.post("/sam/agents/{task_id}/steps")
async def agent_steps(task_id: str, body: AgentSteps):
    """Создатель извне шлюза выдаёт агенту шаги по его request_steps (SAM-212); отказ - ответом агенту в сессию."""
    from .agent import grant_steps_external
    ok, text = grant_steps_external(task_id, body.steps, body.reason)
    if not ok:
        raise HTTPException(status_code=409, detail=text)
    return {"granted": True, "result": text}


@app.post("/sam/agents/{task_id}/status")
async def agent_status(task_id: str, body: AgentStatus):
    from .agent import agent_channel_status
    result = agent_channel_status(task_id, mask_input(body.message).text)
    if result["status"] != "ok":
        raise HTTPException(status_code=404, detail=result["error"])
    return result


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run("gateway.main:app", host="0.0.0.0", port=8000, reload=False)