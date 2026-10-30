"""Универсальный async agent loop с DI.

Поддерживает любую модель с tool_calling (native OpenAI tools API).
Особые случаи:
- Harmony (gpt-oss): special-channel format + stop tokens
- Custom adapter (LM Studio): текстовый парсинг tool_call тегов как fallback
- litellm adapter: всегда нативный tool_calls

Защиты:
- DANGEROUS_PATTERNS (паттерны типа rm -rf)
- LLM guard sub-agent для high-risk tools (mcp_call, claude_delegate)
- Loop guard: 3+ одинаковых вызова подряд → hint, 5+ → force-finish
- Idempotency: одинаковый action-вызов внутри одного loop → дедуп

Зависимости — через AgentLoopDeps. Никакого global state, легко тестировать.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from . import guard_scope, runaway, token_usage
from .worker import INJECT_PREFIX, get_last_answer
from .env import env_int
from .repos.session_repo import IGNORE

HIDDEN_CALL = "workflow"   # причина скрытой записи вызова: шаг workflow, а не реплика разговора


# Tools только для чтения — для них дедуп не применяем (нужны свежие данные)
READONLY_TOOLS = frozenset({
    "tools/list", "screenshot", "get_windows", "get_audio_state",
    "list_allowed_directories", "read_file", "search_web",
})
# Отчёт воркера повторяется дословно законно: «ОТВЕТ:», отклонённый до первого действия ([REJECT]), после работы
# приходит тем же текстом и должен закончить задачу
REPEATABLE_TOOLS = READONLY_TOOLS | {"report_status"}

# Шлюзы к инструментам: имя одно, дела разные (mcp_call - сотни инструментов устройства). Повтор подряд считается по вызову
# целиком, иначе пять разных команд подряд закрывают ход агента как цикл (28 окт: 7 из 30 агентов ушли с итогом «Готово.»)
GATEWAY_TOOLS = frozenset({"mcp_call"})

STOP_WORKER_MARKER = "[STOP_WORKER]"
# Подсказка loop guard: годится любому инструменту, итоги вызовов уже в истории выше
LOOP_HINT = "Ты вызываешь {tool} {n}-й раз подряд; результаты выше - используй их или ответь."
# Опрос без прогресса (SAM-80, SAM-69): тот же вызов с тем же результатом столько раз - цикл
# заканчивается ответом. Повтор подряд ловит loop guard, этот - и вперемешку с другими вызовами.
NO_PROGRESS_LIMIT = env_int("SAM_NO_PROGRESS_LIMIT", 3)

# Предел ответа модели в ходе, где она может вызвать инструмент (SAM-210). Вызов app_make со страницей 5 КБ (код с
# кириллицей) - 1,6-1,9 тыс. токенов аргументов (o200k и cl100k по data/apps/*/src/index.html 14 окт), страница 8 КБ -
# около 3 тыс.; GLM с thinking (zai) тратит тот же предел и на рассуждение. При 2048 такой вызов обрезался в 12 из 14
# попыток 14 окт. 8192 держат страницу 8 КБ вместе с рассуждением с запасом. Залипание на повторе гасят штрафы
# повторов (provider_transform.sampling_for_provider), предел только ограничивает цену такого сбоя. Выше 8192 не
# поднимать без проверки поставщика: у gemini-2.0-flash потолок вывода 8192, больший max_tokens - ответ 400.
MAX_TOKENS_TOOLS = env_int("SAM_LOOP_MAX_TOKENS", 8192)
# Ход только с ответом (tool_choice="none"): аргументов в нём нет, остаётся прежняя отсечка против repetition
# collapse у gemma и qwen
MAX_TOKENS_TEXT = env_int("SAM_LOOP_MAX_TOKENS_TEXT", 2048)
TRUNCATED_FINISH = "length"   # finish_reason OpenAI-совместимого ответа, упёршегося в max_tokens
TRUNCATED_CALL = "Вызов {name} обрезан по длине ответа (предел {limit} токенов): {advice}."
TRUNCATED_REPLY = "Ответ обрезан по длине (предел {limit} токенов): {advice}."   # вызов из текста, имени не видно
SPLIT_ADVICE = "раздели аргументы на несколько вызовов или пиши короче"


def truncated_message(name: str | None, limit: int, hints: Mapping[str, str]) -> str:
    """Причина для модели, чей вызов обрезан пределом: совет инструмента из hints или общий."""
    advice = hints.get(name or "", SPLIT_ADVICE)
    if name is None:
        return TRUNCATED_REPLY.format(limit=limit, advice=advice)
    return TRUNCATED_CALL.format(name=name, limit=limit, advice=advice)


def first_named_tool(text: str, names: frozenset[str]) -> str | None:
    """Инструмент, имя которого встречается в тексте раньше других (с одного места - длинное: app_make, не app);
    None - ни одного."""
    found = [(text.find(n), -len(n), n) for n in names if n and n in text]
    return min(found)[2] if found else None


# Инструмент, которого модели не показали, не исполняется: выдача держит исполнение, а не только схему (опыт 12 окт)
NOT_OFFERED = "Инструмента {name} нет"


def _offered(name: str, offered: frozenset[str] | None) -> bool:
    return offered is None or name in offered


async def _always_allowed(name: str, args: dict) -> bool:
    return True


def _secret_block(deps: "AgentLoopDeps", name: str, args: dict, session_id: str, call_id: str) -> dict:
    deps.event_bus.emit("guard_block", {"level": "secret", "tool": name, "args": args,
                                        "reason": "нет ключа в ходе", "session": session_id, "call_id": call_id})
    agent = token_usage.role_for_session(session_id) == token_usage.ROLE_WORKER   # агент идёт вне хода человека
    if agent and deps.refused.repeated(session_id, name, args):
        return {"role": "tool", "tool_call_id": call_id, "content": guard_scope.REPEAT_REFUSED}
    if agent:
        deps.refused.remember(session_id, name, args)
    text = deps.agent_secret_message if agent and deps.agent_secret_message else deps.need_secret_message
    reason = deps.refusal_reason(name, args) if agent else ""
    if reason:
        text = f"{text} Причина: {reason}."
    return {"role": "tool", "tool_call_id": call_id, "content": text}


# итог инструмента в событии монитора: начало, не весь (скриншот - мегабайты); 400 знаков резали смысл итогов
# поиска и чтения файлов, 4000 - пара экранов текста, монитор показывает начало и разворачивает остальное
RESULT_PREVIEW = env_int("SAM_MONITOR_RESULT_CHARS", 4000)


def _tool_result_event(deps: "AgentLoopDeps", name: str, result: str, session_id: str, call_id: str) -> dict:
    """Событие итога: начало для монитора; у живущего в истории инструмента - весь текст и пометка linger."""
    event = {"name": name, "result": result[:RESULT_PREVIEW], "session": session_id, "call_id": call_id}
    if len(result) > RESULT_PREVIEW:
        event["result_len"] = len(result)   # монитор пишет, сколько итога не пришло
    if name in deps.linger_tools:
        event.update(content=result, linger=True)
    return event


RUNAWAY_HINT = ("[SYSTEM] Твой ответ оборван: разнос генерации ({detail}). Продолжи по-русски, коротко и по делу, "
                "без повторов.")
# Длинный текст агента - не разнос, а работа не в том месте: подсказка отправляет её в файлы
TEXT_TOO_LONG_HINT = ("[SYSTEM] Твой ответ оборван и выброшен: {detail}. Код и длинные выкладки "
                      "не пиши текстом - сразу вызови инструмент и запиши нужное в файл; текстом - шаги и план коротко.")
RUNAWAY_REPLY = "[SAM] Генерация агента ушла в разнос: {detail}. Ответ оборван."   # второй разнос подряд - итог без модели
STEP_LIMIT_REPLY = "[SAM] Превышен лимит шагов агента."   # служебный ответ цикла, не реплика: повтор хода с ним не заменяет готовый ответ

def _no_budget(session_id: str) -> tuple[bool, str | None]:
    return True, None


def _no_turn_done(session_id: str, tools: list[str]) -> None:
    return None


def loop_signature(tool_call) -> str:
    """Подпись вызова для loop guard: имя инструмента; у шлюза (GATEWAY_TOOLS) ещё и разобранные аргументы."""
    name = tool_call.function.name
    if name not in GATEWAY_TOOLS:
        return name
    raw = tool_call.function.arguments or ""
    try:
        raw = json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        pass
    return f"{name}:{raw}"


@dataclass
class AgentLoopDeps:
    """Все зависимости agent_loop — никакого global state."""
    llm_client: Any                              # gateway.llm_client (модуль)
    event_bus: Any                               # gateway.event_bus
    tool_impl: dict[str, Callable[..., Awaitable[Any]]]
    build_tools_schema: Callable[[list[str]], list[dict]]
    provider_transform: Any                      # gateway.provider_transform
    guard_syntax_check: Callable[[str, dict], Awaitable[bool]]
    guard_tool_check: Callable[[str, dict, dict], Awaitable[bool]]
    dangerous_patterns: list[str]
    high_risk_tools: set[str]
    parse_channel_format: Callable[[str], list[tuple[str, dict]]]
    strip_thinking: Callable[[str], str]
    worker_last_answer_var: contextvars.ContextVar
    # Слой политик (SAM-79): allow / ask / deny по tool и аргументам, решает код до вызова
    tool_policy: Callable[[str, dict], str] = lambda name, args: "allow"
    # SAM-88: привилегированный вызов только в ходе с секретом; False - отказ с просьбой назвать ключ
    privilege_check: Callable[[str, dict], Awaitable[bool]] = _always_allowed
    need_secret_message: str = "[GUARD] Нужен ключ."
    refused: "guard_scope.RefusalMemory" = field(default_factory=lambda: guard_scope.RefusalMemory())   # SAM-236 п.4
    refusal_reason: Callable[[str, dict], str] = lambda name, args: ""   # почему отказ, если видно по аргументам (SAM-236)
    agent_secret_message: str = ""   # отказ агенту (сессия task:): ключ ему не назвать; пусто - как основной модели
    # SAM-127: инструменты, которые запускают фоновую работу с итогом в следующем ходе (spawn_agent). Принудительный
    # финиш после такого вызова отвечает pending_reply без модели: итога ещё нет, модель отвечала по догадке
    pending_tools: frozenset[str] = frozenset()
    pending_reply: str = ""
    # Инструменты, чей итог живёт в истории несколько ходов целиком (тело скилла): событие несёт весь текст и
    # пометку linger, шина пишет его в историю, сборка хода показывает N ходов, сжатие не пересказывает
    linger_tools: frozenset[str] = frozenset()
    step_budget: Callable[[str], tuple[bool, str | None]] = _no_budget   # шаги агента по сессии (agent_budget)
    turn_done: Callable[[str, list[str]], None] = _no_turn_done   # оборот кончился этими вызовами (шаг каналов - назад)
    # Что показано модели сейчас: по умолчанию список хода; Cortex отдаёт текущий набор сессии (выдача посреди хода)
    offered: Callable[[list[str]], frozenset[str]] = frozenset
    # Совет инструмента, чей вызов обрезан пределом ответа (SAM-210): как разделить его аргументы; нет совета - общий
    split_hints: Mapping[str, str] = field(default_factory=dict)


async def run_agent_loop(
    deps: AgentLoopDeps,
    messages: list,
    llm_config: dict,
    tool_names: list[str],
    max_turns: int = 20,
    session_id: str = "default",
) -> tuple[str, set[str]]:
    """Главный цикл агента. Возвращает (final_text, set_of_tool_names_used).
    Вызовы LLM внутри записываются на session_id с ролью main или worker (SAM-48)."""
    with token_usage.usage_scope(session_id, token_usage.role_for_session(session_id)):
        return await _run_agent_loop(
            deps, messages, llm_config, tool_names, max_turns, session_id,
        )


async def _run_agent_loop(
    deps: AgentLoopDeps,
    messages: list,
    llm_config: dict,
    tool_names: list[str],
    max_turns: int,
    session_id: str,
) -> tuple[str, set[str]]:
    model = llm_config["model"]

    def _tools_schema() -> list[dict]:
        # На каждом шаге: список может вырасти посреди хода (найденные tool_search инструменты Cortex)
        return deps.provider_transform.sanitize_tools_for_provider(
            deps.build_tools_schema(tool_names), llm_config.get("source", ""), model,
        )

    provider_src = llm_config.get("source", "")
    provider_model = model
    last_tool: str | None = None
    last_signature: str | None = None
    same_tool_streak = 0
    extra = (
        {"extra_body": llm_config["extra_body"]}
        if llm_config.get("extra_body") else {}
    )
    seen_calls: set[str] = set()
    tools_used: set[str] = set()
    outcomes: dict[str, int] = {}   # вызов вместе с результатом -> сколько раз (SAM-80)
    no_progress = False
    runaways = 0   # оборванных стражем ответов агента подряд: первый - замечание и повтор, второй - итог без модели
    voice = token_usage.role_for_session(session_id) == token_usage.ROLE_MAIN   # голосовые пороги длины - только основной
    is_harmony = llm_config.get("prompt_format") == "harmony"

    # Harmony: добавляем каналы в system
    if is_harmony and messages and messages[0].get("role") == "system":
        sys_content = messages[0]["content"]
        if "Valid channels" not in sys_content:
            harmony_suffix = (
                "\n\n# Valid channels: analysis, commentary, final."
                "\n# All function tool calls must go to the commentary "
                "channel: 'functions'."
            )
            messages = [
                {"role": "system", "content": sys_content + harmony_suffix},
            ] + messages[1:]

    for turn in range(max_turns):
        # Шаги агента (gateway/agent_budget.py): пул кончился - итог того, что успел; на доле - подсказка
        allowed, note = deps.step_budget(session_id)
        if not allowed:
            return STEP_LIMIT_REPLY, tools_used
        if note:
            messages.append({"role": "user", "content": f"{INJECT_PREFIX} {note}"})

        # Inject из монитора
        inject_msg = deps.event_bus.pop_inject(session_id)
        if inject_msg:
            print(f"[Inject/{session_id}] {inject_msg[:60]}")
            messages.append({
                "role": "user", "content": f"{INJECT_PREFIX} {inject_msg}",
            })

        # Loop guard: 3+ одинаковых вызова — подсказка
        if same_tool_streak == 3:
            print(
                f"[Loop] LOOP DETECTED: '{last_tool}' x{same_tool_streak}",
            )
            deps.event_bus.emit("loop_guard", {
                "tool": last_tool, "streak": same_tool_streak,
                "session": session_id,
            })
            messages.append({"role": "user",
                             "content": LOOP_HINT.format(tool=last_tool, n=same_tool_streak)})

        force_finish = same_tool_streak >= 5 or no_progress
        if force_finish:
            print(f"[Loop] FORCE FINISH: '{last_tool}' x{same_tool_streak}")
            if tools_used & deps.pending_tools and deps.pending_reply:
                print(f"[Loop] фоновая работа хода ещё идёт ({sorted(tools_used & deps.pending_tools)}) - статус без модели")
                return deps.pending_reply, tools_used

        normalized_messages = deps.provider_transform.normalize_messages_for_provider(
            messages, provider_src, provider_model,
        )
        max_tokens = MAX_TOKENS_TEXT if force_finish else MAX_TOKENS_TOOLS
        call_kwargs = dict(
            model=model,
            messages=normalized_messages,
            tools=_tools_schema(),
            tool_choice="none" if force_finish else "auto",
            max_tokens=max_tokens,
            # штрафы повторов - там, где поставщик их принимает (не Gemini)
            **deps.provider_transform.sampling_for_provider(provider_src, provider_model),
            **extra,
        )
        if is_harmony:
            call_kwargs["stop"] = ["<|call|>", "<|return|>"]

        # SAM-09 streaming: emit assistant_delta для монитора (живая печать).
        # Только на основном turn, не на retry/finish — там короткие fallback'и.
        def _on_delta(piece: str, _sid=session_id, _bus=deps.event_bus):
            try:
                _bus.emit("assistant_delta", {"text": piece, "session": _sid})
            except Exception:
                pass

        # SAM-73: поставщик сменился посреди стрима - показанное начало стирается,
        # запасной стримит ответ заново.
        def _on_reset(_sid=session_id, _bus=deps.event_bus):
            try:
                _bus.emit("assistant_stream_reset", {"session": _sid})
            except Exception:
                pass

        watch = runaway.Watch(voice_limits=voice)
        resp = await deps.llm_client.make_llm_call(
            llm_config, on_delta=_on_delta, on_reset=_on_reset, stop_when=watch.feed, **call_kwargs,
        )
        # Диагностика стрима — что собралось.
        try:
            _m = resp.choices[0].message
            _content_len = len(_m.content or "")
            _tcs_len = len(_m.tool_calls or [])
            _fr = resp.choices[0].finish_reason
            print(
                f"[Stream/done] turn={turn} content_len={_content_len} "
                f"tcs={_tcs_len} finish={_fr}"
            )
        except Exception:
            pass
        # Маркер конца стрима — UI может зафиксировать строку.
        try:
            deps.event_bus.emit("assistant_stream_end", {"session": session_id})
        except Exception:
            pass
        choice = resp.choices[0]
        msg = choice.message
        truncated = getattr(choice, "finish_reason", None) == TRUNCATED_FINISH
        if (sign := getattr(resp, "runaway", None)) is not None:
            deps.event_bus.emit("generation_runaway", {"session": session_id, "model": model, "sign": sign.sign,
                                                       "detail": sign.detail, "chars": len(msg.content or ""),
                                                       "stage": "stream"})
            if not voice:   # основной ответ дальше обрезает runner до здорового начала; агенту - замечание и повтор
                runaways += 1
                if runaways > 1:
                    return RUNAWAY_REPLY.format(detail=sign.detail), tools_used
                hint = TEXT_TOO_LONG_HINT if sign.sign == runaway.AGENT_TOO_LONG else RUNAWAY_HINT
                messages.append({"role": "user", "content": hint.format(detail=sign.detail)})
                continue
        else:
            runaways = 0   # счёт подряд: оборот без разноса его сбрасывает

        # Сериализуем assistant: если есть tool_calls — content исключаем
        if msg.tool_calls:
            messages.append({
                "role": "assistant",
                "tool_calls": [{
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                } for tc in msg.tool_calls],
            })
        else:
            messages.append({
                "role": "assistant", "content": msg.content or "",
            })

        if not msg.tool_calls:
            # Парсер text-формата tool-вызовов работает ВСЕГДА (не только custom):
            # Qwen/LM Studio часто возвращает '[tool_call: foo(...)]' как text
            # content вместо native tool_calls API. Если LLM использовала native
            # API правильно — content пустой/без [tool_call:] → парсер ничего
            # не вытащит, безопасно.
            is_custom = deps.llm_client._effective_adapter(llm_config) == "custom"
            raw_content = msg.content or ""
            # text_tools=False (SAM-57, LLM_TEXT_TOOLS=0): вызовы только нативные,
            # текст ответа остаётся текстом
            text_calls = (
                deps.parse_channel_format(raw_content)
                if llm_config.get("text_tools", True) else []
            )
            print(
                f"[Loop/text-parse] turn={turn} session={session_id} "
                f"content_len={len(raw_content)} parsed={len(text_calls)}"
            )
            if not text_calls and raw_content.strip():
                # Покажем первые 200 символов чтоб видно что Qwen пишет
                print(f"[Loop/text-parse] sample: {raw_content[:200]!r}")

            if not text_calls:
                cleaned = deps.strip_thinking(msg.content or "")

                # Ответ упёрся в предел и вызова в нём не разобрано: обрезка уходила пользователю ответом. Модель
                # получает причину; в ходе только с ответом (force_finish) обрезанный текст - последнее, что есть
                if truncated and not force_finish:
                    name = first_named_tool(raw_content, deps.offered(tool_names))
                    print(f"[Loop] ответ обрезан по пределу {max_tokens} токенов, вызов: {name}")
                    messages.pop()
                    messages.append({"role": "user", "content":
                                     f"[SYSTEM] {truncated_message(name, max_tokens, deps.split_hints)}"})
                    continue

                # Custom + text → syntax-guard проверяет правильность tool-вызовов
                if (cleaned.strip() and not force_finish
                        and deps.llm_client._effective_adapter(llm_config) == "custom"):
                    syntax_ok = await deps.guard_syntax_check(
                        msg.content or "", llm_config,
                    )
                    if not syntax_ok:
                        deps.llm_client.inc_parse_fail()
                        messages.pop()
                        messages.append({
                            "role": "user", "content":
                                "[SYSTEM] Обнаружен неверный синтаксис вызова "
                                "инструмента. Используй только правильный "
                                "формат tool_call тегов. Повтори ответ с "
                                "правильным синтаксисом.",
                        })
                        deps.event_bus.emit("guard_block", {
                            "reason": "wrong tool call syntax",
                            "session": session_id,
                        })
                        continue

                # Пустой ответ после strip — запрашиваем короткий ответ
                if not cleaned.strip():
                    messages.pop()
                    messages.append({
                        "role": "user",
                        "content": "Ответь коротко — 1 предложение по-русски.",
                    })
                    resp2 = await deps.llm_client.make_llm_call(
                        llm_config, model=model, messages=messages,
                        tool_choice="none", **extra,
                    )
                    return (
                        deps.strip_thinking(resp2.choices[0].message.content or "")
                        or "Готово."
                    ), tools_used
                return cleaned, tools_used

            if force_finish:
                # Force finish: даже если есть text_calls — принудительно ответ
                messages.pop()
                messages.append({
                    "role": "user", "content":
                        "Дай краткий текстовый ответ пользователю на русском. "
                        "Без вызовов инструментов.",
                })
                resp2 = await deps.llm_client.make_llm_call(
                    llm_config, model=model, messages=messages,
                    tool_choice="none", **extra,
                )
                return (
                    deps.strip_thinking(resp2.choices[0].message.content or "")
                ), tools_used

            # Текстовые tool calls — обрабатываем как fake tool_calls
            print(
                f"[Loop/text] turn={turn} "
                f"text_calls={[n for n, _ in text_calls]}",
            )

            # Эмитим план в монитор — видно ЧТО воркер собрался делать.
            # Полезно даже без Plan Critic'а.
            deps.event_bus.emit("worker_plan", {
                "turn": turn,
                "calls": [
                    {"name": n, "args": a} for n, a in text_calls
                ],
                "session": session_id,
            })

            # Plan Critic (опциональный): LLM-смотр семантики плана.
            # Если воркер и self_config.plan_critic_enabled — критик
            # смотрит весь набор text_calls. При FIX:-вердикте инжектит
            # совет в messages и пропускает выполнение этого turn'а.
            try:
                from .worker import worker_task_var
                from .self_config import get_instance as _get_self_cfg
                from . import plan_critic as _critic
                cfg_obj = _get_self_cfg()
                critic_on = bool(cfg_obj.get("plan_critic_enabled", False))
                worker_task = worker_task_var.get("")
            except Exception:
                critic_on = False
                worker_task = ""

            # Лимит: critic блокирует не более CRITIC_MAX_BLOCKS раз на task.
            # После — выполняем план как есть (даже если critic считает иначе).
            # Иначе critic vs worker уходит в бесконечный loop "лучших" версий.
            CRITIC_MAX_BLOCKS = 2
            try:
                from .worker import worker_state_var
                state = worker_state_var.get(None) or {}
                critic_blocks_done = state.get("critic_blocks", 0)
            except Exception:
                critic_blocks_done = 0

            if critic_on and worker_task and session_id.startswith("task:") \
                    and critic_blocks_done < CRITIC_MAX_BLOCKS:
                deps.event_bus.emit("critic_check", {
                    "tool": "plan", "tools_count": len(text_calls),
                    "session": session_id,
                })
                ok, advice = await _critic.critique_plan(
                    worker_task, text_calls, llm_config,
                    llm_call_fn=deps.llm_client.make_llm_call,
                    strip_thinking_fn=deps.strip_thinking,
                )
                if not ok:
                    deps.event_bus.emit("critic_block", {
                        "tool": "plan",
                        "reason": advice[:200],
                        "session": session_id,
                    })
                    if state is not None:
                        state["critic_blocks"] = critic_blocks_done + 1
                    messages.pop()  # убираем raw text план
                    messages.append({
                        "role": "user", "content":
                            "[CRITIC] Твой план мне не нравится:\n" + advice +
                            "\n\nПеределай по совету выше — выполни ИСПРАВЛЕННЫЙ план."
                    })
                    continue  # пропускаем выполнение, новая итерация
                deps.event_bus.emit("critic_pass", {
                    "tool": "plan", "session": session_id,
                })

            messages.pop()
            fake_tcs = [
                {
                    "id": f"text_{turn}_{i}",
                    "type": "function",
                    "function": {
                        "name": tname,
                        "arguments": json.dumps(targs, ensure_ascii=False),
                    },
                }
                for i, (tname, targs) in enumerate(text_calls)
            ]
            messages.append({"role": "assistant", "tool_calls": fake_tcs})

            tool_results = []
            for j, (tname, targs) in enumerate(text_calls):
                tc_id = fake_tcs[j]["id"]
                args_str = json.dumps(targs).lower()
                blocked = False
                for pattern in deps.dangerous_patterns:
                    if pattern.lower() in args_str:
                        deps.event_bus.emit("guard_block", {
                            "level": "pattern",
                            "tool": tname, "pattern": pattern,
                            "reason": f"Pattern '{pattern}' в args",
                            "session": session_id, "call_id": tc_id,
                        })
                        tool_results.append({
                            "role": "tool", "tool_call_id": tc_id,
                            "content": f"[GUARD] Заблокировано: '{pattern}'",
                        })
                        blocked = True
                        break
                if blocked:
                    continue
                verdict = deps.tool_policy(tname, targs)
                if verdict == "deny":
                    tool_results.append(_policy_block(deps, tname, session_id, tc_id))
                    continue

                if not await deps.privilege_check(tname, targs):
                    tool_results.append(_secret_block(deps, tname, targs, session_id, tc_id))
                    continue

                if tname in deps.high_risk_tools or verdict == "ask":
                    deps.event_bus.emit("guard_check", {
                        "level": "llm",
                        "tool": tname, "args": targs,
                        "session": session_id, "call_id": tc_id,
                    })
                    safe = await deps.guard_tool_check(tname, targs, llm_config)
                    if not safe:
                        deps.event_bus.emit("guard_block", {
                            "level": "llm",
                            "tool": tname, "args": targs,
                            "reason": "LLM judge: UNSAFE",
                            "session": session_id, "call_id": tc_id,
                        })
                        tool_results.append({
                            "role": "tool", "tool_call_id": tc_id,
                            "content": "[GUARD] Команда отклонена.",
                        })
                        continue
                    deps.event_bus.emit("guard_pass", {
                        "level": "llm",
                        "tool": tname, "session": session_id, "call_id": tc_id,
                    })

                deps.event_bus.emit("tool_call", {
                    "name": tname, "args": targs,
                    "session": session_id, "call_id": tc_id,
                })

                impl = deps.tool_impl.get(tname) if _offered(tname, deps.offered(tool_names)) else None
                if not impl:
                    result_str = NOT_OFFERED.format(name=tname)
                else:
                    # Считаем реальные действия (всё кроме отчётов и проверок).
                    # Это критично для blocking premature 'ОТВЕТ:' в воркере.
                    if tname not in ("report_status", "get_tasks"):
                        try:
                            from .worker import worker_state_var
                            state = worker_state_var.get(None)
                            if state is not None:
                                state["real_calls"] = state.get("real_calls", 0) + 1
                        except Exception:
                            pass
                    try:
                        result_str = str(await impl(targs))
                        tools_used.add(tname)
                        print(
                            f"[Tool/text] {tname}({list(targs.keys())}) -> "
                            f"{result_str[:80]}",
                        )
                    except Exception as e:
                        result_str = f"Ошибка {tname}: {e}"

                deps.event_bus.emit("tool_result", _tool_result_event(deps, tname, result_str, session_id, tc_id))
                tool_results.append({
                    "role": "tool", "tool_call_id": tc_id,
                    "content": result_str,
                })

            messages.extend(tool_results)
            if any(r["content"] == STOP_WORKER_MARKER for r in tool_results):
                return get_last_answer(deps.worker_last_answer_var) or "", tools_used
            continue

        print(
            f"[Loop] turn={turn} "
            f"tool_calls={[tc.function.name for tc in msg.tool_calls]}",
        )

        first_tool = msg.tool_calls[0].function.name if msg.tool_calls else None
        signature = loop_signature(msg.tool_calls[0]) if msg.tool_calls else None
        if signature and signature == last_signature:
            same_tool_streak += 1
        else:
            same_tool_streak = 1
            last_tool, last_signature = first_tool, signature

        deps.turn_done(session_id, [tc.function.name for tc in msg.tool_calls])
        # Ответ упёрся в предел: неразобранные аргументы - обрезка, модель узнаёт причину и предел (SAM-210)
        cut_at = max_tokens if truncated else None
        # Параллельное выполнение всех tool_calls
        raw_results = await asyncio.gather(*[
            execute_tool(deps, tc, llm_config, session_id, seen_calls, offered=deps.offered(tool_names),
                         truncated_at=cut_at)
            for tc in msg.tool_calls
        ])
        # execute_tool может вернуть dict (обычный) или list[dict] (vision:
        # tool_msg + follow-up user_msg с картинками). Ответы на все tool_calls
        # идут подряд сразу за assistant, follow-up - после них: user между
        # tool-ответами OpenAI-совместимый API отвергает (SAM-72).
        tool_results: list = []
        follow_ups: list = []
        for r in raw_results:
            if isinstance(r, list):
                tool_results.append(r[0])
                follow_ups.extend(r[1:])
            else:
                tool_results.append(r)
        tool_results.extend(follow_ups)
        for tc in msg.tool_calls:
            tools_used.add(tc.function.name)
        messages.extend(tool_results)
        stuck = _count_outcomes(outcomes, msg.tool_calls, tool_results)
        if stuck and not no_progress:
            no_progress = True
            deps.event_bus.emit("no_progress", {"tool": stuck, "limit": NO_PROGRESS_LIMIT,
                                                "session": session_id})
            messages.append({"role": "user", "content":
                             f"[СИСТЕМА] {stuck} {NO_PROGRESS_LIMIT} раз вернул одно и то же. "
                             "Ответь тем, что уже известно, без новых вызовов."})
        if any(
            r.get("role") == "tool" and r.get("content") == STOP_WORKER_MARKER
            for r in tool_results
        ):
            return get_last_answer(deps.worker_last_answer_var) or "", tools_used

    return STEP_LIMIT_REPLY, tools_used


def _count_outcomes(outcomes: dict[str, int], tool_calls: list, tool_results: list) -> str | None:
    """Учесть вызовы шага вместе с результатами; имя tool, если какой-то дошёл до NO_PROGRESS_LIMIT."""
    by_id = {r.get("tool_call_id"): r.get("content", "") for r in tool_results if r.get("role") == "tool"}
    stuck = None
    for tc in tool_calls:
        key = json.dumps([tc.function.name, tc.function.arguments, by_id.get(tc.id, "")], ensure_ascii=False)
        outcomes[key] = outcomes.get(key, 0) + 1
        if outcomes[key] >= NO_PROGRESS_LIMIT:
            stuck = tc.function.name
    return stuck


def _policy_block(deps: AgentLoopDeps, name: str, session_id: str, call_id: str) -> dict:
    """Вызов запрещён политикой (SAM-79): tool не исполняется, модель и монитор видят причину."""
    deps.event_bus.emit("guard_block", {
        "level": "policy", "tool": name, "reason": "запрещено политикой tools",
        "session": session_id, "call_id": call_id,
    })
    return {"role": "tool", "tool_call_id": call_id,
            "content": f"[POLICY] {name} с такими аргументами запрещён настройками SAM."}


def parse_tool_args(raw: Any) -> tuple[dict, str | None]:
    """Аргументы tool call -> (dict, None) или ({}, причина). Пустые аргументы - вызов без них."""
    if isinstance(raw, dict):
        return raw, None
    if raw is None or not str(raw).strip():
        return {}, None
    try:
        args = json.loads(raw)
    except (TypeError, ValueError) as e:
        return {}, f"не JSON ({e})"
    if not isinstance(args, dict):
        return {}, f"ожидался объект, пришёл {type(args).__name__}"
    return args, None


async def execute_tool(
    deps: AgentLoopDeps,
    tc: Any,
    llm_config: dict,
    session_id: str = "default",
    seen_calls: set | None = None,
    offered: frozenset[str] | None = None,
    truncated_at: int | None = None,
    hidden: bool = False,
) -> dict:
    """Один tool_call: guard + dedup + dispatch. offered - инструменты, показанные модели; None - без ограничения
    (шаг workflow зовёт инструмент сам, не модель). truncated_at - предел токенов, в который упёрся ответ модели:
    неразобранные аргументы тогда обрезаны, и модель получает эту причину. hidden - вызов не из разговора (шаг
    workflow): в историю он ложится скрытой записью, монитор его видит, а контекст модели и сжатие - нет (SAM-219)."""
    name = tc.function.name
    if not _offered(name, offered):
        content = NOT_OFFERED.format(name=name)
        deps.event_bus.emit("tool_result", {"name": name, "result": content, "session": session_id, "call_id": tc.id})
        return {"role": "tool", "tool_call_id": tc.id, "content": content}
    args, parse_error = parse_tool_args(tc.function.arguments)
    if parse_error:
        # Вызов с {} падал внутри tool на KeyError, и модель не понимала,
        # что сломаны её аргументы (SAM-76)
        if truncated_at is not None:
            content = truncated_message(name, truncated_at, deps.split_hints)
            print(f"[Loop] вызов {name} обрезан по пределу {truncated_at} токенов")
        else:
            content = f"Аргументы {name} не разобраны: {parse_error}. Повтори вызов, аргументы - JSON-объект."
        deps.event_bus.emit("tool_result", {
            "name": name, "result": content,
            "session": session_id, "call_id": tc.id,
        })
        return {"role": "tool", "tool_call_id": tc.id, "content": content}

    # Дедуп action-вызовов внутри loop
    if seen_calls is not None and name not in REPEATABLE_TOOLS:
        tool_key = args.get("tool", name)
        call_key = f"{name}:{tool_key}:{json.dumps(args, sort_keys=True)}"
        if call_key in seen_calls:
            print(f"[Dedup] Skipped duplicate: {name}({tool_key})")
            return {
                "role": "tool", "tool_call_id": tc.id,
                "content": "[уже выполнено — дублирующий вызов проигнорирован]",
            }
        seen_calls.add(call_key)

    deps.event_bus.emit("tool_call", {
        "name": name, "args": args,
        "session": session_id, "call_id": tc.id,
        **({IGNORE: HIDDEN_CALL} if hidden else {}),
    })

    verdict = deps.tool_policy(name, args)
    if verdict == "deny":
        return _policy_block(deps, name, session_id, tc.id)

    args_str = json.dumps(args).lower()
    for pattern in deps.dangerous_patterns:
        if pattern.lower() in args_str:
            print(f"[Guard/pattern BLOCK] {name}: {pattern}")
            deps.event_bus.emit("guard_block", {
                "level": "pattern",
                "tool": name, "pattern": pattern,
                "reason": f"Pattern '{pattern}' в args",
                "session": session_id, "call_id": tc.id,
            })
            return {
                "role": "tool", "tool_call_id": tc.id,
                "content": f"[GUARD] Заблокировано: '{pattern}'",
            }

    if not await deps.privilege_check(name, args):
        return _secret_block(deps, name, args, session_id, tc.id)

    if name in deps.high_risk_tools or verdict == "ask":
        deps.event_bus.emit("guard_check", {
            "level": "llm",
            "tool": name, "args": args,
            "session": session_id, "call_id": tc.id,
        })
        safe = await deps.guard_tool_check(name, args, llm_config)
        if not safe:
            deps.event_bus.emit("guard_block", {
                "level": "llm",
                "tool": name, "args": args,
                "reason": "LLM judge: UNSAFE",
                "session": session_id, "call_id": tc.id,
            })
            return {
                "role": "tool", "tool_call_id": tc.id,
                "content": "[GUARD] Команда отклонена системой безопасности.",
            }
        deps.event_bus.emit("guard_pass", {
            "level": "llm",
            "tool": name, "session": session_id, "call_id": tc.id,
        })

    impl = deps.tool_impl.get(name)
    if not impl:
        result = f"Неизвестный инструмент: {name}"
    else:
        # Считаем реальные действия (всё кроме отчётов и проверок).
        # Используем мутабельный dict в ContextVar — child Tasks в asyncio.
        # gather видят shared state, в отличие от примитива через .set().
        if name not in ("report_status", "get_tasks"):
            try:
                from .worker import worker_state_var
                state = worker_state_var.get(None)
                if state is not None:
                    state["real_calls"] = state.get("real_calls", 0) + 1
            except Exception:
                pass
        try:
            result = await impl(args)
            print(f"[Tool] {name}({list(args.keys())}) -> {str(result)[:80]}")
        except Exception as e:
            result = f"Ошибка {name}: {e}"

    result_str = str(result)
    deps.event_bus.emit("tool_result", _tool_result_event(deps, name, result_str, session_id, tc.id))

    # Vision: если результат содержит data:image/...;base64,... — НЕ кладём
    # картинку в tool message (OpenAI API запрещает list content для tool
    # role). Возвращаем list: [tool_msg, user_msg_with_image]. Tool message
    # содержит только текст-подтверждение, картинка в user message следом.
    if "data:image/" in result_str and "base64," in result_str:
        from . import image_utils as _imgu
        model_id = (llm_config.get("model") or "").lower()
        # Vision-капабельность определяем по семейству. Если конкретный
        # квант в LM Studio загружен без vision-projection — safety net в
        # llm_client._openai_call ловит 400 "Invalid 'content'" и ретраит
        # без image_url. Так что False-positive здесь не критичен.
        is_vision = any(tag in model_id for tag in (
            "v-flash", "vision", "llava", "qwen-vl",
            "gpt-4o", "claude-3", "claude-haiku",
            "gemma-4",  # gemma 4 family — multimodal по архитектуре
        )) or "-vl-" in model_id or model_id.endswith("-vl")
        urls = _imgu.extract_data_urls(result_str)
        text_part = result_str
        for u in urls:
            text_part = text_part.replace(u, "")
        text_part = text_part.strip() or "image"

        if urls and is_vision:
            tool_msg = {
                "role": "tool", "tool_call_id": tc.id,
                "content": f"{text_part[:200]} [{len(urls)} image(s) attached below]",
            }
            user_parts: list = [{
                "type": "text",
                "text": f"Это результат tool {name}. Картинки выше — что на них?",
            }]
            for u in urls:
                user_parts.append({"type": "image_url", "image_url": {"url": u}})
            user_msg = {"role": "user", "content": user_parts}
            print(f"[Vision] {len(urls)} image(s) attached via follow-up user msg")
            return [tool_msg, user_msg]
        # Не vision — текст без огромного base64
        if urls:
            short = text_part + f" [{len(urls)} image(s) omitted]"
            return {
                "role": "tool", "tool_call_id": tc.id, "content": short[:1200],
            }

    return {
        "role": "tool", "tool_call_id": tc.id, "content": result_str,
    }
