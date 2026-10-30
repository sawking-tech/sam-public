"""Worker runtime: фоновое выполнение задач от create_task / send_to_task.

Изоляция:
- ContextVars (worker_session_var, worker_last_answer_var) — глобальные,
  чтобы report_status и agent_loop их видели независимо от вложенности
  asyncio.gather (ContextVar наследуется дочерними Tasks).
- WorkerDeps — все зависимости как параметры, ничего не хардкодим
  из agent.py. agent.py создаёт deps при инициализации.
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .env import env_float, env_int
from .tools.agents import ASK_TOOL, REQUEST_STEPS_TOOL, RESULT_PREVIEW
from .worker_partial import partial_result

# Сообщение создателя в ходе воркера (agent_loop) и при воскрешении (run_worker с prior_messages)
INJECT_PREFIX = "[INJECT]"
CONTINUE_PREFIX = "[CONTINUE]"
# Сколько агент, спросивший создателя (ask_creator), ждёт ответ перед итогом (SAM-91). Основная модель отвечает
# своим ходом: тик, ход, POST /sam/inject - в смоуке 10 окт ответ пришёл через секунды после вопроса, а агент к тому
# времени уже закончил. Дольше слот воркера не держим: поздний ответ воскресит агента с историей.
ANSWER_WAIT_S = env_float("SAM_AGENT_ANSWER_WAIT_S", 90.0)
# Шаг опроса очереди в ожидании: ответ подхватывается не позже чем через секунду
ANSWER_POLL_S = 1.0
# Сколько раз за прогон продолжить с ответом создателя: вопрос-ответ без конца не держит воркер-слот;
# ответы сверх лимита остаются в очереди и воскрешают агента после итога
MAX_ANSWER_ROUNDS = env_int("SAM_AGENT_ANSWER_ROUNDS", 5)
# Страховка от зависшей задачи по часам: работу ограничивает пул шагов (agent_budget), а за прежние 300 с задача кода
# не успевала прочитать код (28 окт: stage_switch и monitor_scenarios оборваны посреди работы)
AGENT_TIMEOUT_S = env_int("SAM_AGENT_TIMEOUT_S", 1800)


# ContextVars живут здесь — agent.py их импортирует
worker_session_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "worker_session", default=None,
)
# Финальный ответ воркера - изменяемый dict {"text": ...}, его создаёт run_worker.
# report_status вызывается внутри asyncio.gather, и .set() строки в дочерней Task
# до agent_loop не доходил: на нативных tool calls ответ воркера был пустым (SAM-72).
worker_last_answer_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "worker_last_answer", default=None,
)


def set_last_answer(text: str | None, var: contextvars.ContextVar = worker_last_answer_var) -> None:
    """Записать финальный ответ в holder, общий для дочерних Task."""
    holder = var.get(None)
    if holder is None:
        holder = {}
        var.set(holder)
    holder["text"] = text


def get_last_answer(var: contextvars.ContextVar = worker_last_answer_var) -> str | None:
    holder = var.get(None)
    return holder.get("text") if holder else None
# Счётчик «реальных» вызовов tools — мутабельный dict в ContextVar,
# чтобы asyncio.gather дочерние Tasks могли модифицировать общий state.
# ContextVar.set() в gather копирует контекст и НЕ propagate'ит в родителя,
# поэтому через простой int не работает. Через dict — дочки изменяют поле.
worker_state_var: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "worker_state", default=None,
)
# Описание текущей задачи воркера — для Plan Critic, чтобы он мог
# судить о соответствии плана и задачи.
worker_task_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "worker_task", default="",
)


@dataclass
class WorkerDeps:
    """Все зависимости worker runtime — инъекция, без global state.

    agent.py создаёт deps один раз и переиспользует.
    """
    db: Any
    event_bus: Any
    tasks_module: Any                 # gateway.tasks
    worker_session_module: Any        # gateway.worker_session
    agent_loop: Callable[..., Awaitable[tuple[str, set]]]
    worker_tool_names: list[str]
    worker_system: str
    rag_fetcher: Callable[[str, int], str] | None = None
    auto_respond: Callable[[str, str, str, dict], None] | None = None
    task_handles: dict | None = None
    task_context_map: dict | None = None
    fallback_session_id: Callable[[], str] | None = None
    timeout_seconds: int = AGENT_TIMEOUT_S
    max_turns: int = 12
    # Ответ создателя после итога (SAM-91): (полный id задачи, сообщение) -> воскресить; None - ответ теряется
    reanimate: Callable[[str, str], None] | None = None
    # Контракт завершения (SAM-237): (описание, итог) -> причина, по которой итог не принят; None - принят; None-функция - без проверки
    verify_result: Callable[[str, str], str | None] | None = None
    # Итог запуска в статистику выбора модели (SAM-258): (полный id, итог после проверки, конфиг модели, секунды)
    record_run: Callable[[str, str, dict, float], None] | None = None
    answer_wait_s: float = ANSWER_WAIT_S
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic


UNVERIFIED = "unverified"


RUN_FAILED, RUN_CANCELLED = "failed", "cancelled"   # итоги запуска, кроме статуса принятого итога (_accept)


def _record_run(deps: "WorkerDeps", task_id: str, outcome: str, llm_config: dict, seconds: float) -> None:
    """Итог запуска - в статистику выбора модели; сбой записи не роняет задачу, строка в лог."""
    if deps.record_run is None:
        return
    try:
        deps.record_run(task_id, outcome, llm_config, seconds)
    except Exception as e:
        print(f"[Worker] ERROR итог запуска {task_id[:8]} не записан ({type(e).__name__}): {e}")


def _accept(deps: "WorkerDeps", description: str, result: str) -> tuple[str, str]:
    """(итог, статус): итог без артефакта получает статус unverified и причину первой строкой (SAM-237)."""
    reason = deps.verify_result(description, result) if deps.verify_result else None
    if reason:
        return f"[НЕ ПОДТВЕРЖДЕНО] {reason}\n{result}", UNVERIFIED
    return result, "done"


def _calls_tool(message: dict, name: str) -> bool:
    return any((tc.get("function") or {}).get("name") == name for tc in message.get("tool_calls") or [])


def question_pending(messages: list, ask_tool: str = ASK_TOOL) -> bool:
    """Последний вопрос создателю позже последнего его ответа ([INJECT] в ходе или [CONTINUE] воскрешения)."""
    for m in reversed(messages):
        content = m.get("content")
        if m.get("role") == "user" and isinstance(content, str) and content.startswith((INJECT_PREFIX,
                                                                                        CONTINUE_PREFIX)):
            return False
        if m.get("role") == "assistant" and _calls_tool(m, ask_tool):
            return True
    return False


def drain_injects(event_bus: Any, session_id: str) -> list[str]:
    """Все непрочитанные сообщения в сессию воркера."""
    found = []
    while (msg := event_bus.pop_inject(session_id)) is not None:
        found.append(msg)
    return found


async def wait_answer(deps: WorkerDeps, session_id: str) -> list[str]:
    """Ждать сообщения в сессию до deps.answer_wait_s; не дождались - пустой список."""
    deadline = deps.clock() + deps.answer_wait_s
    while True:
        found = drain_injects(deps.event_bus, session_id)
        if found or deps.clock() >= deadline:
            return found
        await deps.sleep(ANSWER_POLL_S)


async def _loop_until_answered(deps: WorkerDeps, messages: list, llm_config: dict, session_id: str) -> str:
    """Цикл агента до итога. Сообщение создателя, легшее в очередь за последний шаг или пришедшее в ожидании после
    его вопроса, не теряется: ещё раунд цикла с ним вместо итога (SAM-91, смоук 10 окт). Шаги кончились, а просьба о
    шагах ждёт ответа - агент ждёт его так же: выдача продолжает работу. Без итога - частичный итог (SAM-212)."""
    from .agent_loop import STEP_LIMIT_REPLY   # agent_loop импортирует этот модуль: импорт при вызове
    final, out_of_steps = "", False
    for round_no in range(MAX_ANSWER_ROUNDS + 1):
        result, _ = await deps.agent_loop(
            messages, llm_config, deps.worker_tool_names,
            max_turns=deps.max_turns, session_id=session_id,
        )
        answer = get_last_answer()
        out_of_steps = not answer and result == STEP_LIMIT_REPLY
        final = answer or result
        if round_no == MAX_ANSWER_ROUNDS:
            break
        answers = drain_injects(deps.event_bus, session_id)
        if not answers and (question_pending(messages)
                            or out_of_steps and question_pending(messages, REQUEST_STEPS_TOOL)):
            print(f"[Worker] {session_id}: жду ответ создателя до {deps.answer_wait_s:.0f} с")
            answers = await wait_answer(deps, session_id)
        if not answers:
            break
        print(f"[Worker] {session_id}: ответ создателя до итога - продолжаю")
        messages.extend({"role": "user", "content": f"{INJECT_PREFIX} {a}"} for a in answers)
        set_last_answer(None)   # итог «жду ответа» снят: следующий раунд даст свой
    if out_of_steps:
        return partial_result(messages, unanswered=question_pending(messages, REQUEST_STEPS_TOOL),
                              limit=RESULT_PREVIEW)
    return final


def hand_over_late_answers(deps: WorkerDeps, task_id: str, worker_session: str, saved: bool) -> None:
    """Сообщение, принятое как running_inject между итогом и снятием задачи, воскрешает агента (SAM-91).
    Вызывается сразу после снятия сессии из task_context_map, без await между ними: позже /sam/inject уже
    воскрешает сам."""
    late = drain_injects(deps.event_bus, worker_session)
    if not late:
        return
    if saved and deps.reanimate:
        print(f"[Worker] {worker_session}: ответ после итога - воскрешаю")
        deps.reanimate(task_id, "\n".join(late))
        return
    print(f"[Worker] ERROR {worker_session}: сообщение после сбоя задачи не доставлено: {late[0][:80]!r}")


async def run_worker(
    deps: WorkerDeps,
    task: str,
    device: str = "",
    context: str = "",
    llm_config: dict | None = None,
    session_id: str = "default",
    prior_messages: list | None = None,
) -> tuple[str, list]:
    """Воркер с изолированным LLM loop. Возвращает (final, messages).

    prior_messages — для resume idle-воркера (SAM-38)."""
    if not llm_config:
        return "LLM не инициализирован", []

    worker_session_var.set(session_id)
    worker_state_var.set({"real_calls": 0, "critic_blocks": 0})
    worker_task_var.set(task)

    if prior_messages:
        messages = list(prior_messages)
        messages.append({"role": "user", "content": f"[CONTINUE] {task}"})
    else:
        system = deps.worker_system
        if device:
            system += f"\nЦелевое устройство: {device}"
        if context:
            system += f"\nКонтекст: {context}"
        user_msg = f"[Устройство: {device}] {task}" if device else task
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]

    worker_last_answer_var.set({"text": None})
    final = await _loop_until_answered(deps, messages, llm_config, session_id)
    print(f"[Worker] done: {final[:100]}")
    return final, messages


def _emit_stage(deps: WorkerDeps, worker_session: str, short_id: str,
                name: str, idx: int, status: str, message: str = "") -> None:
    deps.event_bus.emit("stage_update", {
        "task_id": short_id, "name": name, "idx": idx, "total": 2,
        "status": status, "message": message, "session": worker_session,
    })


async def run_worker_task(
    deps: WorkerDeps,
    task_id: str,
    description: str,
    device: str,
    llm_config: dict,
    parent_session: str,
) -> None:
    """Фоновая задача: plan (RAG) → execute (run_worker) → save session."""
    deps.tasks_module.mark_task_running(deps.db, task_id)
    short_id = task_id[:8]
    worker_session = f"task:{short_id}"
    saved = False
    started, outcome = deps.clock(), RUN_FAILED

    try:
        _emit_stage(deps, worker_session, short_id, "plan", 0, "running",
                    "анализ задачи")
        context = f"task_id={short_id} parent_session={parent_session}"
        if deps.rag_fetcher:
            try:
                # синхронный эмбеддер и Qdrant - в потоке, не в event loop (SAM-74)
                rag = await asyncio.to_thread(deps.rag_fetcher, description, 5)
                if rag:
                    context += f"\n\nИЗ ПАМЯТИ (факты по теме задачи):\n{rag}"
                    print(f"[Worker] RAG для {short_id}: {len(rag)} символов")
            except Exception as e:
                print(f"[Worker] ERROR RAG ошибка для {short_id}: {e}")
        _emit_stage(deps, worker_session, short_id, "plan", 0, "done")

        _emit_stage(deps, worker_session, short_id, "execute", 1, "running",
                    description[:80])
        result, worker_messages = await asyncio.wait_for(
            run_worker(deps, description, device, context=context,
                       llm_config=llm_config, session_id=worker_session),
            timeout=deps.timeout_seconds,
        )
        _emit_stage(deps, worker_session, short_id, "execute", 1, "done")

        result, status = _accept(deps, description, result)
        outcome = status
        deps.tasks_module.update_task_status(
            deps.db, task_id, status, result=result[:RESULT_PREVIEW],
        )
        deps.worker_session_module.save_session(
            deps.db, task_id, device=device, description=description,
            messages=worker_messages, parent_session=parent_session,
            status="idle",
        )
        saved = True
        deps.event_bus.emit("task_done", {
            "task_id": short_id, "description": description,
        })
        if deps.auto_respond:
            deps.auto_respond(short_id, result[:RESULT_PREVIEW], parent_session, llm_config)

    except asyncio.CancelledError:
        _emit_stage(deps, worker_session, short_id, "execute", 1, "failed",
                    "отменено")
        deps.tasks_module.update_task_status(
            deps.db, task_id, "cancelled", error="Отменено",
        )
        deps.event_bus.emit("task_cancelled", {"task_id": short_id})
        outcome = RUN_CANCELLED

    except asyncio.TimeoutError:
        _emit_stage(deps, worker_session, short_id, "execute", 1, "failed",
                    f"таймаут {deps.timeout_seconds // 60} мин")
        deps.tasks_module.update_task_status(
            deps.db, task_id, "failed",
            error=f"Timeout {deps.timeout_seconds}s",
        )
        deps.event_bus.emit("task_failed", {
            "task_id": short_id, "reason": f"Timeout {deps.timeout_seconds}s",
        })
        # Главный агент должен отреагировать на провал, не молчать
        if deps.auto_respond:
            deps.auto_respond(
                short_id,
                f"[TASK_FAILED] Таймаут {deps.timeout_seconds // 60} мин — не справилась за лимит времени",
                parent_session, llm_config,
            )

    except Exception as e:
        _emit_stage(deps, worker_session, short_id, "execute", 1, "failed",
                    str(e)[:60])
        deps.tasks_module.update_task_status(
            deps.db, task_id, "failed", error=str(e)[:200],
        )
        deps.event_bus.emit("task_failed", {
            "task_id": short_id, "reason": str(e)[:80],
        })
        if deps.auto_respond:
            deps.auto_respond(
                short_id,
                f"[TASK_FAILED] {str(e)[:120]}",
                parent_session, llm_config,
            )

    finally:
        if deps.task_handles is not None:
            deps.task_handles.pop(task_id, None)
        if deps.task_context_map is not None:
            deps.task_context_map.pop(worker_session, None)
        hand_over_late_answers(deps, task_id, worker_session, saved)
        _record_run(deps, task_id, outcome, llm_config, deps.clock() - started)


async def resume_worker_task(
    deps: WorkerDeps,
    task_id: str,
    message: str,
    llm_config: dict,
) -> None:
    """SAM-38: воскрешает idle-воркера c сохранённой историей + новым msg."""
    session = deps.worker_session_module.load_session(deps.db, task_id)
    if not session:
        return

    short_id = task_id[:8]
    worker_session = f"task:{short_id}"
    device = session.get("device", "")
    description = session.get("description", "")
    prior_messages = session.get("messages", [])
    fallback_sid = (
        deps.fallback_session_id() if deps.fallback_session_id else "default"
    )
    parent_session = session.get("parent_session", fallback_sid)

    deps.tasks_module.mark_task_running(deps.db, task_id)
    deps.worker_session_module.mark_running(deps.db, task_id)
    started, outcome = deps.clock(), RUN_FAILED
    if deps.task_context_map is not None:
        deps.task_context_map[worker_session] = (parent_session, short_id)
    saved = False

    try:
        _emit_stage(deps, worker_session, short_id, "resume", 0, "running",
                    "оживляю воркера")
        _emit_stage(deps, worker_session, short_id, "resume", 0, "done")
        _emit_stage(deps, worker_session, short_id, "execute", 1, "running",
                    message[:80])

        result, new_messages = await asyncio.wait_for(
            run_worker(deps, message, device, llm_config=llm_config,
                       session_id=worker_session, prior_messages=prior_messages),
            timeout=deps.timeout_seconds,
        )
        _emit_stage(deps, worker_session, short_id, "execute", 1, "done")

        result, status = _accept(deps, description, result)
        outcome = status
        deps.tasks_module.update_task_status(
            deps.db, task_id, status, result=result[:RESULT_PREVIEW],
        )
        deps.worker_session_module.save_session(
            deps.db, task_id, device=device, description=description,
            messages=new_messages, parent_session=parent_session,
            status="idle",
        )
        saved = True
        deps.event_bus.emit("task_done", {
            "task_id": short_id, "description": description,
        })
        if deps.auto_respond:
            deps.auto_respond(short_id, result[:RESULT_PREVIEW], parent_session, llm_config)

    except asyncio.TimeoutError:
        _emit_stage(deps, worker_session, short_id, "execute", 1, "failed",
                    "таймаут resume")
        deps.tasks_module.update_task_status(
            deps.db, task_id, "failed",
            error=f"Timeout resume {deps.timeout_seconds}s",
        )
        deps.worker_session_module.mark_dead(deps.db, task_id)

    except Exception as e:
        _emit_stage(deps, worker_session, short_id, "execute", 1, "failed",
                    str(e)[:60])
        deps.tasks_module.update_task_status(
            deps.db, task_id, "failed", error=str(e)[:200],
        )
        deps.worker_session_module.mark_dead(deps.db, task_id)

    finally:
        if deps.task_handles is not None:
            deps.task_handles.pop(task_id, None)
        if deps.task_context_map is not None:
            deps.task_context_map.pop(worker_session, None)
        hand_over_late_answers(deps, task_id, worker_session, saved)
        _record_run(deps, task_id, outcome, llm_config, deps.clock() - started)
