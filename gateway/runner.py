"""SAMAgentRunner: один user turn от прихода сообщения до ответа.

Логика:
1. Подобрать LLM (роутер) + проинициализировать runtime
2. Подтянуть историю (сжатие ставит запись в историю, gateway/compression_watch.py, SAM-97)
3. RAG context из памяти по последним user-фразам
4. Детектор разрыва сессии (>30 мин) — system note
5. Собрать system + chat history + текущий user msg
6. Guard входящего сообщения (если block → отказ)
7. _agent_loop — основной цикл
8. Если create_task вызывался → voice глушим (придёт через TASK_DONE)
9. Иначе речь - весь ответ без мета-блоков + детект action-verbs без create_task
   (форсированный второй прогон если глаголы есть но таски нет)
10. Emit assistant_reply

Все зависимости через RunnerDeps. agent.py создаёт deps один раз.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from .agent_loop import STEP_LIMIT_REPLY
from .prompts import GUARD_BLOCK_MARK, GUARD_INLINE_RULES, GUARD_SECRET_RULES
from .repos.session_repo import CHANNEL, IGNORE, SUMMARY_CHANNEL, TEXT_CHANNEL, THOUGHTS_CHANNEL
from .spoken_history import strip_fate_mark
from . import recall_history, runaway, secret_mark, turn_taint
from .env import env_int
from .llm_router import ROUTE_FAST, ROUTE_KEY

RAG_HEADER = "Из памяти (используй активно):"
THOUGHTS_SUMMARY_MARK = "Мои решения и планы: "   # сводка канала мыслей среди сводок разговора (SAM-227)
SUMMARIES_HEADER = "[Сжатая история предыдущего диалога]:"
SUMMARY_SEP = "\n---\n"
# Разделитель частей последнего сообщения быстрой: память, время, фраза; фраза - после последнего разделителя
PHRASE_SEP = "\n\n"
# Задание быстрой после диалога (ADR-027, блок 9): фраза повторно - она не тонет за памятью и заготовками
TASK_LINE = "Задание: продолжи разговор от имени SAM - ответь на последнюю фразу: «{phrase}»"
# Заметок основной ещё нет: постоянная строка на их месте, пропажа блока сдвинула бы всё ниже и порвала кэш (ADR-027)
NO_NOTES_LINE = "Заметок основной модели о разговоре пока нет."

# Сколько реплик человека тело скилла (итог с пометкой linger) остаётся в контексте целиком; потом убирается, нужно
# снова - модель зовёт load_skill. Шесть - разговор о деле обычно укладывается, а каждый ход не платит за тело вечно
LINGER_TURNS = env_int("SAM_SKILL_LINGER_TURNS", 6)
LINGER_HEADER = "[итог {name}]"

# Реплика SAM текстом в монитор (note основной модели): модель видит, что это не звучало. Метка в скобках
# короче 40 знаков - если модель повторит её в речи, voice_turn.drop_marks её срежет
TEXT_REPLY_MARK = "[текстом в монитор, не голосом]"


# Глаголы действия и намерения — если они в ответе но create_task не
# вызвался → форсируем retry. Лучше переспросить чем соврать про
# "я сделаю" не сделав.
ACTION_VERBS = (
    # настоящее время
    "включаю", "открываю", "запускаю", "открываем", "запускаем", "включаем",
    "нашла, открываю", "нашёл, открываю",
    # намерения — тоже должны быть через spawn_agent
    "сейчас найду", "сейчас открою", "сейчас запущу", "сейчас включу",
    "уже ищу", "уже открываю", "сейчас сделаю",
    "найду тебе", "найду для тебя", "открою сейчас",
    "сейчас проверю", "проверю что", "сейчас посмотрю", "посмотрю что",
    "уже проверяю", "уже смотрю", "сейчас узнаю",
    "сейчас гляну", "гляну что", "сразу проверю",
    # запись в состав дома: слабая модель докладывает о ней без set_household_member
    "занесла", "занёс", "занесены", "занесён", "занесена", "записаны", "внесла", "внёс", "внесены",
    # ПРОШЕДШЕЕ время — главная категория для lying detection.
    # SAM говорит «открыла X», «запустила Y», «нашла Z» БЕЗ tool call =
    # враньё. Forced retry с обязательным spawn_agent.
    "открыла", "запустила", "включила", "нашла", "проверила", "посмотрела",
    "сделала", "выполнила", "обновила", "увидела", "загрузила",
    " открыл ", " запустил ", " включил ", " нашёл ", " проверил ",
    " посмотрел ", " сделал ", " выполнил ", " обновил ", " увидел ",
    # «покажу/расскажу что умею» = нужен list_capabilities
    "сейчас покажу", "сейчас расскажу", "покажу, что умею",
    "расскажу, что умею", "вот что умею", "вот мои функции",
)


# Кто ответил на ход - в событии assistant_reply (подпись в мониторе) и в сообщении source мосту (voice/layer/bridge.py)
SOURCE_FAST = "fast"
SOURCE_MAIN = "main"


def answer_source(llm_config: dict | None) -> str:
    """Источник ответа по конфигу маршрута: fast - быстрая модель, иначе основная."""
    return SOURCE_FAST if (llm_config or {}).get(ROUTE_KEY) == ROUTE_FAST else SOURCE_MAIN

BLOCKED_REPLY = "Не могу выполнить этот запрос — он нарушает правила безопасности."


def _usable_retry(raw: str) -> bool:
    """Ответ повтора хода годится на замену первого: непустой и не служебный «лимит шагов» короткого повтора."""
    return bool(raw.strip()) and STEP_LIMIT_REPLY not in raw


@dataclass
class RunnerDeps:
    """Зависимости SAMAgentRunner — никакого global state."""
    event_bus: Any
    get_best_llm: Callable[[], Awaitable[dict]]
    get_session_history: Callable[[str, int], list]
    get_rag_context: Callable[[str, int], str]
    guard_check: Callable[[str, dict], Awaitable[bool]]
    agent_loop: Callable[..., Awaitable[tuple[str, set]]]
    system_prompt: str
    main_tool_names: list[str]
    speech_of: Callable[[str], str]   # что звучит из ответа (text_utils.transparent_speech)
    on_session_change: Callable[[str, dict], None] = field(
        default=lambda sid, cfg: None,
    )
    session_break_threshold_sec: int = 1800
    rag_limit: int = 7
    history_limit: int = 30
    # Голосовой ход: в истории только сказанный текст, без мета-тегов и прочей разметки,
    # и у фразы человека - кто её сказал, иначе модель не отличает одного домашнего от другого. Текстовому чату не нужно
    history_speech_only: bool = False
    history_speakers: bool = False
    # Судья сообщения встроен в ход (SAM_GUARD_MODE=inline): правила в системном промпте, отказ меткой GUARD_BLOCK_MARK, отдельного вызова нет
    guard_inline: bool = False
    # Часы паузы между сессиями (UTC, как timestamp истории); тест подставляет свои
    utcnow: Callable[[], datetime] = datetime.utcnow
    # Своя недавняя работа из всех сессий (gateway/own_work.py): истории сессий раздельные, а SAM одна
    own_work: Callable[[], str] = lambda: ""
    # Давнее из архива сессии к фразе - только основной (SAM-228): (сессия, фраза) -> блок
    get_archive_context: Callable[[str, str], str] = lambda session, phrase: ""
    # Один поиск - выдача основной и быстрой (ADR-027); None - обе из get_rag_context
    get_rag_pair: Callable[[str, int], tuple[str, str]] | None = None
    # Прошлые вызовы в истории нативными tool_calls (SAM-217); текстом - модели без нативных tools (Harmony, custom)
    native_tool_history: Callable[[dict], bool] = lambda cfg: cfg.get("prompt_format") not in ("harmony",)


# Старые tool-имена → новые agent-имена. Если в session history (Mongo)
# остались записи с устаревшими именами — переименовываем при показе LLM,
# чтобы она не училась на старом API.
_TOOL_NAME_MIGRATIONS = {
    "create_task":  "spawn_agent",
    "get_tasks":    "list_agents",
    "send_to_task": "send_to_agent",
    "cancel_task":  "cancel_agent",
}


def _history_text(deps: RunnerDeps, doc: dict, content: str) -> str:
    """Текст записи истории для модели: в голосе - речь без разметки и с подписью говорящего; текст в монитор -
    с пометкой, что не звучал."""
    if doc.get(CHANNEL) == TEXT_CHANNEL:
        return f"{TEXT_REPLY_MARK} {content}"
    if doc.get("role") == "assistant" and deps.history_speech_only:
        return deps.speech_of(content) or content
    who = doc.get("who")
    if doc.get("role") == "user" and deps.history_speakers and who:
        return f"{who}: {content}"
    return content


# Прошлый вызов в истории - нативно, как внутри хода (SAM-217): текстом <tool_call>{json}</tool_call> в репликах
# ассистента модель училась писать вызовы текстом. Итоги прошлых вызовов история не хранит - ответ инструмента честно
# говорит об этом, а не выдумывает итог
HISTORY_RESULT = "итог этого прошлого вызова в истории не сохранён; что вышло - в ответе после него"
HISTORY_CALL_ID = "h{:08d}"   # 9 букв и цифр: такой id принимают и строгие поставщики (Mistral)


def native_tool_turns(calls: list[dict], start: int) -> list[dict]:
    """Подряд идущие записи вызовов -> сообщение ассистента с tool_calls и по ответу инструмента на каждый."""
    ids = [HISTORY_CALL_ID.format(start + n) for n in range(len(calls))]
    assistant = {"role": "assistant", "tool_calls": [   # та же форма, что у вызова внутри хода (agent_loop)
        {"id": cid, "type": "function", "function": {
            "name": _TOOL_NAME_MIGRATIONS.get(c.get("tool_name", ""), c.get("tool_name", "")),
            "arguments": json.dumps(c.get("tool_args") or {}, ensure_ascii=False)}}
        for cid, c in zip(ids, calls)]}
    return [assistant, *({"role": "tool", "tool_call_id": cid, "content": HISTORY_RESULT} for cid in ids)]


def _format_tool_call_for_history(name: str, args: dict, prompt_format: str) -> str:
    name = _TOOL_NAME_MIGRATIONS.get(name, name)
    if prompt_format == "harmony":
        args_str = json.dumps(args, ensure_ascii=False)
        return (
            f"<|channel|>commentary to=functions.{name} "
            f"<|constrain|>json<|message|>{args_str}<|end|>"
        )
    tc_json = json.dumps({"name": name, "arguments": args}, ensure_ascii=False)
    return f"<tool_call>{tc_json}</tool_call>"


def _detect_session_break(history: list, threshold_sec: int, now_utc: datetime | None = None) -> str:
    """>30 мин с последнего сообщения → возвращаем системную пометку."""
    if not history:
        return ""
    last_ts = history[-1].get("timestamp")
    if not last_ts:
        return ""

    now_utc = now_utc or datetime.utcnow()
    ts_naive = (
        last_ts.replace(tzinfo=None)
        if hasattr(last_ts, "tzinfo") and last_ts.tzinfo
        else last_ts
    )
    try:
        gap_sec = int((now_utc - ts_naive).total_seconds())
    except TypeError:
        return ""

    if gap_sec <= threshold_sec:
        return ""

    h, m_ = divmod(gap_sec // 60, 60)
    gap_str = f"{h}ч {m_}мин" if h else f"{gap_sec // 60}мин"
    return f"⚠️ НОВАЯ СЕССИЯ: прошло {gap_str} с последнего сообщения."


@dataclass
class PromptParts:
    """Части промпта хода до раскладки: основная и быстрая модели раскладывают их по-разному (ADR-027)."""
    system_prompt: str
    rules: list[str]
    device_line: str
    session_break: str
    time_line: str
    own_work: str
    rag: str
    summaries: list[str]
    chat_turns: list[dict]
    message: str


def build_messages(
    deps: RunnerDeps,
    session_id: str,
    message: str,
    history: list,
    rag: str,
    llm_config: dict,
    archive: str = "",
) -> list[dict]:
    """Собирает messages = system + chat_turns + current user (раскладка основной модели). archive - блок давнего из
    архива к фразе (SAM-228), посчитанный вызывающим вне цикла событий (archive_for)."""
    p = prompt_parts(deps, session_id, message, history, rag, llm_config, own_work=True)
    system_parts = [p.system_prompt, p.time_line, *p.rules]
    system_parts += [x for x in (p.device_line, p.session_break, p.own_work) if x]
    if p.rag:
        system_parts.append(f"{RAG_HEADER}\n{p.rag}")
    if archive:
        system_parts.append(archive)
    system = "\n\n".join(system_parts)
    if p.summaries:
        system += f"\n\n{SUMMARIES_HEADER}\n" + SUMMARY_SEP.join(p.summaries)
    return [{"role": "system", "content": system}, *p.chat_turns, {"role": "user", "content": p.message}]


async def archive_for(deps: RunnerDeps, session_id: str, phrase: str, timeout_s: float) -> str:
    """Блок архива к фразе в потоке с пределом времени (SAM-228): запрос к Mongo не держит цикл событий; не успел -
    ход без архива, строка в лог."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(deps.get_archive_context, session_id, phrase), timeout_s)
    except Exception as e:
        print(f"[Runner] ERROR архив к фразе не получен: {type(e).__name__}: {e}")
        return ""


def build_fast_messages(
    deps: RunnerDeps,
    session_id: str,
    message: str,
    history: list,
    rag: str,
    llm_config: dict,
    line: str = "",
    capabilities: str = "",
    prepared: str = "",
) -> list[dict]:
    """Раскладка быстрой модели (ADR-027, «Утверждённая структура»), от неизменного к меняющемуся, чтобы LM Studio
    брал начало из кэша. Системная часть - статика (шаблон с правилами, возможности, устройство), линия и выжимка
    основной (постоянная строка, когда их нет), сводки. Диалог - реплики и фраза одной лентой. Последнее сообщение -
    блоки к этой фразе: память, заготовки, метка сессии, время и задание с фразой повторно. Своей работы у быстрой нет:
    нужное из неё основная кладёт в выжимку."""
    p = prompt_parts(deps, session_id, message, history, rag, llm_config, own_work=False, hide_secrets=True)
    rules = [r for r in p.rules if r != GUARD_SECRET_RULES]   # правило секретов - в сжатых правилах шаблона быстрой
    system_parts = [p.system_prompt, *rules, *(x for x in (capabilities, p.device_line) if x), line or NO_NOTES_LINE]
    # Сводок у быстрой нет (compression.md, «Какую историю получает каждая модель»): в них бывает личное, нужное из
    # раннего основная кладёт в линию и выжимку
    system = "\n\n".join(system_parts)
    blocks = [f"{RAG_HEADER}\n{p.rag}" if p.rag else "", prepared, p.session_break, p.time_line,
              TASK_LINE.format(phrase=p.message)]
    return [{"role": "system", "content": system}, *p.chat_turns, {"role": "user", "content": p.message},
            {"role": "user", "content": PHRASE_SEP.join(x for x in blocks if x)}]


def prompt_parts(
    deps: RunnerDeps,
    session_id: str,
    message: str,
    history: list,
    rag: str,
    llm_config: dict,
    own_work: bool = True,
    hide_secrets: bool = False,
) -> PromptParts:
    """Части промпта хода: правила, строки сессии, история репликами и сводками, фраза. hide_secrets - запись с
    пометкой секрета идёт обобщением без содержания (быстрая, ADR-027)."""
    session_break = _detect_session_break(
        history, deps.session_break_threshold_sec, deps.utcnow(),
    )
    device_line = (
        f"Устройство сессии (инициатор, дефолт для create_task): {session_id}"
        if session_id != "default" else ""
    )

    # Локальное время — чтобы агент мог корректно отвечать на "который час",
    # "сегодня", "вчера", "через 2 часа" итд.
    now_local = datetime.now()
    time_line = (
        f"Сейчас локальное время: {now_local.strftime('%Y-%m-%d %H:%M, %A')} "
        f"(TZ: {now_local.astimezone().tzinfo})"
    )

    rules = [GUARD_SECRET_RULES, *([GUARD_INLINE_RULES] if deps.guard_inline else [])]

    prompt_format = llm_config.get("prompt_format", "standard")
    history, message = _take_current_phrase(deps, history, message)
    summary_parts: list[str] = []
    chat_turns: list[dict] = []
    alive = lingering(history, LINGER_TURNS)
    native = deps.native_tool_history(llm_config)
    pending: list[dict] = []   # подряд идущие прошлые вызовы: одно сообщение ассистента с tool_calls

    def flush() -> None:
        if pending:
            chat_turns.extend(native_tool_turns(pending, len(chat_turns)))
            pending.clear()

    for i, h in enumerate(history):
        role = h.get("role", "user")
        content = h.get("content", "")
        if role != "tool_call":
            flush()
        if role == "summary" and content:
            tr = h.get("time_range", "")
            prefix = (f"[{tr}] " if tr else "") + (THOUGHTS_SUMMARY_MARK if h.get(SUMMARY_CHANNEL) == THOUGHTS_CHANNEL else "")
            summary_parts.append(f"{prefix}{content}")
        elif role == "tool_call" and native:
            pending.append(h)
        elif role == "tool_call":
            name = h.get("tool_name", "")
            args = h.get("tool_args", {})
            tc_text = _format_tool_call_for_history(name, args, prompt_format)
            chat_turns.append({"role": "assistant", "content": tc_text})
        elif role == "tool" and i in alive:
            chat_turns.append({"role": "user", "content": f"{LINGER_HEADER.format(name=h.get('tool_name', ''))}\n{content}"})
        elif hide_secrets and (h.get(recall_history.RECALLED) or role == recall_history.RECALL_ROLE):
            continue   # поднятое из архива и сама ссылка - только основной: секреты там не помечены (SAM-228)
        elif role == recall_history.RECALL_ROLE and content:
            chat_turns.append({"role": "user", "content": f"[{content}]"})
        elif role in ("user", "assistant") and content and hide_secrets and secret_mark.hidden(history, i):
            chat_turns.append({"role": role, "content": _history_text(deps, h, secret_mark.PLACEHOLDER)})
        elif role in ("user", "assistant") and content:
            chat_turns.append({"role": role, "content": _history_text(deps, h, strip_fate_mark(content))})
    flush()

    return PromptParts(
        system_prompt=deps.system_prompt, rules=rules, device_line=device_line, session_break=session_break or "",
        time_line=time_line, own_work=(deps.own_work() or "") if own_work else "", rag=rag or "",
        summaries=summary_parts, chat_turns=chat_turns, message=message,
    )


def lingering(history: list, turns: int) -> set[int]:
    """Индексы итогов с пометкой linger, после которых реплик человека меньше turns: они ещё в контексте."""
    alive, users_after = set(), 0
    for i in range(len(history) - 1, -1, -1):
        h = history[i]
        if h.get("role") == "user":
            users_after += 1
        elif h.get("role") == "tool" and h.get("linger") and users_after < turns:
            alive.add(i)
    return alive


def _take_current_phrase(deps: RunnerDeps, history: list, message: str) -> tuple[list, str]:
    """Фраза хода уже в истории: user_message пишется до чтения истории, а сборка ставит её последней сама.
    Запись снимается с хвоста истории, а её подпись говорящего переходит на фразу хода. Сравнение раньше шло
    по подписанному тексту («Имя: ...» против фразы без подписи), и модель видела фразу дважды (voice-leaks,
    9 окт; SAM-95, 10 окт)."""
    last = history[-1] if history else None
    if not last or last.get("role") != "user" or strip_fate_mark(last.get("content") or "") != message:
        return history, message
    return history[:-1], _history_text(deps, last, message)


def _build_rag_query(message: str, history: list) -> str:
    """Конкат последних 2 user сообщений + текущее — для лучшего RAG-recall."""
    if not history:
        return message
    recent_user = [
        h["content"] for h in history[-6:] if h.get("role") == "user"
    ][-2:]
    return " ".join(recent_user + [message]) if recent_user else message


async def _maybe_response_critic(
    deps,
    user_msg: str,
    raw: str,
    tools_used: set,
    messages: list,
    llm_config: dict,
    session_id: str,
    *,
    _system_call: bool = False,
    max_blocks: int = 2,
) -> tuple[str, set]:
    """Внутренний диалог: LLM-критик смотрит ответ оркестратора. Если
    нарушение — инжектит FIX-hint, переспрашивает агента. Возвращает
    финальный (raw, tools_used) после фиксов или original если ok."""
    if _system_call:
        return raw, tools_used
    # llm_client может отсутствовать в тестах (FakeRunnerDeps)
    llm_client = getattr(deps, "llm_client", None)
    if llm_client is None:
        return raw, tools_used
    try:
        from .self_config import get_instance
        if not get_instance().get("response_critic_enabled", False):
            return raw, tools_used
        from . import response_critic as _rc
    except Exception:
        return raw, tools_used

    # core-rules из RAG (релевантные), тем же поиском памяти, что и ход
    rules_text = await asyncio.to_thread(deps.get_rag_context, "правило поведения SAM", 6) or ""
    rules_list = [
        line.lstrip("- ").strip()
        for line in rules_text.split("\n") if line.strip()
    ]

    blocks_done = 0
    while blocks_done < max_blocks:
        ok, advice = await _rc.critique_response(
            user_msg, raw, tools_used, rules_list, llm_config,
            llm_call_fn=llm_client.make_llm_call,
            strip_thinking_fn=deps.strip_thinking,
        )
        if ok:
            if blocks_done > 0:
                deps.event_bus.emit("critic_pass", {
                    "tool": "response", "session": session_id,
                })
            return raw, tools_used

        # Block — инжектим hint, retry
        deps.event_bus.emit("critic_block", {
            "tool": "response", "reason": advice[:200],
            "session": session_id,
        })
        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user", "content":
                "[INNER CRITIC] Твой ответ не годится:\n" + advice +
                "\n\nПеределай — следуй совету. Соблюдай правила.",
        })
        try:
            raw2, tools2 = await deps.agent_loop(
                messages, llm_config, deps.main_tool_names,
                session_id=session_id, max_turns=3,
            )
        except Exception as e:
            print(f"[Runner] ERROR critic retry failed, keeping answer: {e}")
            return raw, tools_used
        if _usable_retry(raw2):
            raw = raw2
            tools_used = tools_used | tools2
        blocks_done += 1
    return raw, tools_used


async def run_turn(
    deps: RunnerDeps,
    message: str,
    device_id: str | None = None,
    user_id: str | None = None,
    _system_call: bool = False,
    images: list[str] | None = None,
    persist_reply: bool = True,
) -> str:
    """Один ход в рамке учёта внешнего текста (gateway/turn_taint.py): итог агента или системный ход - внешний
    вход, правка души в таком ходе не пройдёт."""
    with turn_taint.turn_scope(turn_taint.external_input(message, _system_call)):
        return await _run_turn(deps, message, device_id, user_id, _system_call, images, persist_reply)


async def _run_turn(
    deps: RunnerDeps,
    message: str,
    device_id: str | None = None,
    user_id: str | None = None,
    _system_call: bool = False,
    images: list[str] | None = None,
    persist_reply: bool = True,
) -> str:
    """Полный pipe одного user-turn'а. Возвращает voice-string (или ""
    если ответ ушёл через TASK_DONE callback).
    persist_reply=False - ответ только в монитор: в историю его пишет учёт прозвучавшего (SAM-85)."""
    session_id = device_id or "default"
    tag = "[sys]" if _system_call else "[usr]"
    print(
        f"[Runner] {tag} session={session_id} msg="
        f"{message[:100]!r}",
    )

    # Эмитим user_message СРАЗУ — до тяжёлых операций (RAG, embedder,
    # build_messages). Иначе пользователь видит "SAM думает..." раньше
    # своего сообщения в монитор-логе (хронологически путано).
    display_msg = (
        message.split("\n")[0]
        if message.startswith("[TASK_DONE:") else message
    )
    deps.event_bus.emit("user_message", {
        "content": display_msg, "session": session_id, "system": _system_call,
    })

    llm_config = await deps.get_best_llm()
    deps.on_session_change(session_id, llm_config)
    print(f"[Runner] LLM: {llm_config['source']} / {llm_config['model']}")

    history = deps.get_session_history(session_id, deps.history_limit)
    print(f"[Runner] history len={len(history)}")

    rag_query = _build_rag_query(message, history)
    # Эмбеддер на CPU, Qdrant и Mongo синхронны: в потоке, иначе стоят все сессии (SAM-74)
    rag = await asyncio.to_thread(deps.get_rag_context, rag_query, deps.rag_limit) if rag_query else ""
    if rag:
        print(f"[Runner] RAG: {len(rag)} chars")

    archive = await archive_for(deps, session_id, message, recall_history.ARCHIVE_TIMEOUT_S)
    messages = build_messages(deps, session_id, message, history, rag, llm_config, archive=archive)
    print(f"[Runner] messages built: {len(messages)} (system+history+user)")

    # Multimodal: если в этом turn пришли картинки (drag/paste/attach в
    # мониторе), переписываем последнее user message как content list с
    # image_url parts. Vision-модели увидят, не-vision получат текст +
    # safety net в llm_client стрипнет картинки на 400.
    if images and messages:
        print(f"[Runner] got {len(images)} image(s), looking for user msg in {len(messages)} messages")
        attached = False
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                txt = messages[i].get("content")
                txt_str = txt if isinstance(txt, str) else (
                    " ".join(p.get("text", "") for p in txt
                             if isinstance(p, dict) and p.get("type") == "text")
                    if isinstance(txt, list) else str(txt)
                )
                parts: list = [{"type": "text", "text": txt_str or "опиши"}]
                for url in images:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
                messages[i] = {**messages[i], "content": parts}
                print(f"[Runner] attached {len(images)} image(s) to user msg #{i}")
                attached = True
                break
        if not attached:
            print(f"[Runner] WARN: no user msg found, prepending image as new msg")
            parts = [{"type": "text", "text": message or "опиши"}]
            for url in images:
                parts.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": "user", "content": parts})

    guard_ok = deps.guard_inline or await deps.guard_check(message, llm_config)
    if not guard_ok:
        print("[Runner] GUARD BLOCK — request rejected")
        deps.event_bus.emit("assistant_reply", {
            "content": "[BLOCKED]", "voice": "[BLOCKED]",
            "session": session_id,
        })
        deps.event_bus.emit("guard_block", {
            "reason": "request blocked by safety guard",
            "session": session_id,
        })
        return BLOCKED_REPLY

    raw, tools_used = await deps.agent_loop(
        messages, llm_config, deps.main_tool_names, session_id=session_id,
    )
    if deps.guard_inline and GUARD_BLOCK_MARK in raw:
        print("[Runner] GUARD BLOCK (inline) — request rejected")
        deps.event_bus.emit("assistant_reply", {"content": "[BLOCKED]", "voice": "[BLOCKED]", "session": session_id})
        deps.event_bus.emit("guard_block", {"reason": "request blocked by inline safety guard", "session": session_id})
        return BLOCKED_REPLY
    print(
        f"[Runner] agent_loop done: tools={sorted(tools_used)} "
        f"raw_len={len(raw)}",
    )

    # Response Critic — внутренний диалог. Проверяет ответ оркестратора
    # (raw) на соответствие правилам ДО отдачи пользователю. Если нарушение
    # — retry агент с hint'ом критика. Макс 2 итерации.
    raw, tools_used = await _maybe_response_critic(
        deps, message, raw, tools_used, messages, llm_config, session_id,
        _system_call=_system_call,
    )

    # Раньше при spawn_agent глушили voice (ждали TASK_DONE). Но это
    # давало неприятную тишину — пользователь не понимает что SAM начала
    # делать. Теперь intro озвучиваем коротко («сейчас попробую»), потом
    # TASK_DONE даст второй voice с результатом. Промпт просит intro
    # делать кратким (1 фраза).
    # _system_call=True (TASK_DONE inject) — это уже второй проход, не
    # intro, его voice идёт обычным путём ниже.

    # весь ответ без мета-блоков (ADR-003); пусто после <silent> - молчание
    final = voice = deps.speech_of(raw)

    # Ответ без tool - обычный ответ: повтора «вызови хотя бы noop» нет, простая реплика - один вызов модели (15 окт)

    # Глагол действия без единого вызова инструмента → форс второго прогона (SAM-07). Любой реальный вызов считается:
    # большинство дел SAM делает сама (tune_self, remember), агент - только долгому делу (душа SAM, раздел DOING)
    if final and any(v in final.lower() for v in ACTION_VERBS):
        if not tools_used:
            print(
                f"[Runner] ACTION-VERB без tool → форс retry: "
                f"voice={final[:60]!r}",
            )
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user", "content":
                    "[СИСТЕМА] Ты сказала действие но НЕ вызвала tool! "
                    "Если записываешь людей или питомцев дома → set_household_member на каждого. "
                    "Если 'покажу что умею' → list_capabilities. "
                    "Если 'открою/найду X' → spawn_agent. "
                    "Вызови нужный tool СЕЙЧАС.",
            })
            # Сбой повтора не должен выбрасывать готовый первый ответ (SAM-72)
            try:
                raw2, tools2 = await deps.agent_loop(
                    messages, llm_config, deps.main_tool_names,
                    session_id=session_id, max_turns=3,
                )
                voice2 = deps.speech_of(raw2) if _usable_retry(raw2) else ""
                if voice2:
                    raw, final = raw2, voice2
                tools_used |= tools2
            except Exception as e:
                print(f"[Runner] action-verb retry failed, keeping first answer: {e}")
            # Не глушим — intro озвучивается (см. комментарий выше),
            # TASK_DONE даст второй voice с результатом.

    sign = runaway.detect(raw)
    ignore = ""
    if sign:   # SAM-244, SAM-246: здравое начало остаётся ответом, хвост отбрасывается; начало негодное - как бред (SAM-92)
        head = runaway.trim(final)
        deps.event_bus.emit("generation_runaway", {"session": session_id, "model": llm_config.get("model", ""),
                                                   "sign": sign.sign, "detail": sign.detail, "chars": len(raw),
                                                   "kept": len(head)})
        if head and runaway.detect(head) is None:
            raw = final = head
        else:
            raw = final = ""
            ignore = f"runaway:{sign.sign}"   # модели реплику в истории не видят, монитор показывает с причиной
    deps.event_bus.emit("assistant_reply", {
        "content": raw, "voice": final,
        "session": session_id, "source": answer_source(llm_config),
        **({IGNORE: ignore} if ignore else {}),
        "system": (_system_call and not final) or not persist_reply,
    })
    return final
