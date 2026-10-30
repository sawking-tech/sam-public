"""Safety guard layer (SAM-31).

Три уровня проверки:
1. Pattern check (синхронный, по списку DANGEROUS_PATTERNS) — быстрый
   первый барьер. Не реализован отдельной функцией — `is_dangerous_text`
   просто проверяет вхождение.
2. LLM-проверка входящего сообщения пользователя — `guard_message`.
3. LLM-проверка tool call перед выполнением — `guard_tool_call`.

Plus `guard_response_syntax` — детектор tool calls в неправильном
формате (текстовая попытка вместо нативного API).

Все функции принимают `llm_call_fn` и `strip_thinking_fn` явно — для
тестов можно мокать.
"""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from .command_auth import CORE_SCOPED_TOOLS, JUDGED_TOOLS, PRIVILEGED_TOOLS
from .prompts import (
    DANGEROUS_PATTERNS,
    GUARD_MSG_PROMPT,
    GUARD_PRIVILEGE_PROMPT,
    GUARD_TOOL_PROMPT,
)

# Высокорисковые tools — проверяются LLM guard перед выполнением
# claude_delegate выполняет CLI команды — должен проходить через LLM-judge.
# mcp_call УБРАН: Guard слишком часто false-positive блокирует
# обычные mcp_call("run", {"program": "calc.exe"}). Pattern-проверка
# (Guard L1) ловит rm -rf / format c: / DROP TABLE в args — достаточно.
HIGH_RISK_TOOLS = frozenset({"claude_delegate"})


LLMCaller = Callable[..., Awaitable]
"""Сигнатура: llm_call_fn(llm_config, **kwargs) → response с .choices[0].message"""

StripThinkingFn = Callable[[str], str]


GUARD_MODE_ENV = "SAM_GUARD_MODE"
MODE_SEPARATE, MODE_INLINE = "separate", "inline"     # separate - отдельный вызов судьи перед ходом; inline - правила судьи в промпте хода, один вызов
DEFAULT_GUARD_MODE = MODE_SEPARATE


def guard_inline(environ=None) -> bool:
    """Судья сообщения встроен в ход (SAM_GUARD_MODE=inline); иначе - отдельный вызов, как раньше."""
    import os
    return ((environ if environ is not None else os.environ).get(GUARD_MODE_ENV) or DEFAULT_GUARD_MODE).strip().lower() == MODE_INLINE


def inline_blocked(raw: str) -> bool:
    """Встроенный судья отказал: в ответе модели метка блокировки."""
    return GUARD_BLOCK_MARK in (raw or "")


def is_dangerous_text(text: str) -> bool:
    """Быстрая sync-проверка по списку DANGEROUS_PATTERNS."""
    if not text:
        return False
    lower = text.lower()
    return any(p.lower() in lower for p in DANGEROUS_PATTERNS)


async def guard_message(
    message: str,
    llm_config: dict,
    *,
    llm_call_fn: LLMCaller,
    strip_thinking_fn: StripThinkingFn,
) -> bool:
    """LLM-проверка входящего user-сообщения. Возвращает True = SAFE."""
    try:
        resp = await llm_call_fn(
            llm_config,
            model=llm_config["model"],
            messages=[{"role": "user",
                       "content": GUARD_MSG_PROMPT.format(message=message)}],
            max_tokens=5,
            temperature=0,
        )
        raw = strip_thinking_fn(resp.choices[0].message.content or "")
        # Берём последнее слово — модели с <think> пишут вердикт в конце
        verdict = (raw.strip().split()[-1] if raw.strip() else "SAFE").upper()
        safe = "UNSAFE" not in verdict
        print(f"[Guard/msg] {verdict} -> {'allow' if safe else 'BLOCK'}")
        return safe
    except Exception as e:
        print(f"[Guard/msg] error: {e} — allowing")
        return True

NO_WORKSPACES = "none"   # подсказка судье, когда каталоги рабочих мест не переданы


async def guard_tool_call(
    tool_name: str,
    args: dict,
    llm_config: dict,
    *,
    llm_call_fn: LLMCaller,
    strip_thinking_fn: StripThinkingFn,
    scope: Callable[[str, dict], bool | None] | None = None,
    workspaces: str = NO_WORKSPACES,
) -> bool:
    """LLM-sub-agent проверка tool call перед выполнением.
    Вызывается для высокорисковых tools. scope - решение по пути без судьи (gateway/guard_scope.py): True пускает,
    False блокирует, None оставляет судье."""
    if scope is not None and (decided := scope(tool_name, args)) is not None:
        label = "рабочее место" if decided else "секретный путь"
        print(f"[Guard/tool] {tool_name} -> {label} -> {'allow' if decided else 'BLOCK'}")
        return decided
    try:
        resp = await llm_call_fn(
            llm_config,
            model=llm_config["model"],
            messages=[{"role": "user", "content": GUARD_TOOL_PROMPT.format(
                tool_name=tool_name,
                args=json.dumps(args, ensure_ascii=False)[:600],
                workspaces=workspaces,
            )}],
            max_tokens=5,
            temperature=0,
        )
        raw = strip_thinking_fn(resp.choices[0].message.content or "")
        verdict = (raw.strip().split()[-1] if raw.strip() else "SAFE").upper()
        safe = "UNSAFE" not in verdict
        print(f"[Guard/tool] {tool_name} -> {verdict} -> {'allow' if safe else 'BLOCK'}")
        return safe
    except Exception as e:
        print(f"[Guard/tool] error: {e} — allowing")
        return True


PRIVILEGED_VERDICT = "PRIVILEGED"
ARGS_CHARS = 600   # аргументы в промпт гарда: путь и команда видны, длинный текст не раздувает вызов


async def guard_privileged(
    tool_name: str,
    args: dict,
    secret_in_turn: bool,
    main_config: dict,
    *,
    llm_call_fn: LLMCaller,
    strip_thinking_fn: StripThinkingFn,
    touches_core: Callable[[str, dict], bool] | None = None,
    scope: Callable[[str, dict], bool | None] | None = None,
    workspaces: str = NO_WORKSPACES,
) -> bool:
    """SAM-88: можно ли выполнить вызов. Привилегированный - только в ходе с секретом.

    Список PRIVILEGED_TOOLS решает без модели; mcp_call (JUDGED_TOOLS) судит основная модель, не модель хода.
    Сбой суда - вызов считается привилегированным: без секрета он не пройдёт. Инструмент из CORE_SCOPED_TOOLS
    требует ключ, только если touches_core говорит, что вызов трогает ядро (SAM-194); без touches_core - всегда."""
    if secret_in_turn or (tool_name not in PRIVILEGED_TOOLS and tool_name not in JUDGED_TOOLS):
        return True
    if scope is not None and (decided := scope(tool_name, args)) is not None:   # путь решает код, не судья (SAM-236)
        label = "рабочее место, ключ не нужен" if decided else "секретный путь, нужен ключ"
        print(f"[Guard/secret] {tool_name} -> {label}")
        return decided
    if tool_name in CORE_SCOPED_TOOLS and touches_core is not None and not touches_core(tool_name, args):
        print(f"[Guard/secret] {tool_name} -> периферия, ключ не нужен")
        return True
    if tool_name in PRIVILEGED_TOOLS:
        print(f"[Guard/secret] {tool_name} -> нет ключа в ходе -> BLOCK")
        return False
    try:
        resp = await llm_call_fn(
            main_config,
            model=main_config["model"],
            messages=[{"role": "user", "content": GUARD_PRIVILEGE_PROMPT.format(
                tool_name=tool_name,
                args=json.dumps(args, ensure_ascii=False)[:ARGS_CHARS],
                workspaces=workspaces,
            )}],
            max_tokens=5,
            temperature=0,
        )
        raw = strip_thinking_fn(resp.choices[0].message.content or "")
        privileged = PRIVILEGED_VERDICT in raw.upper()
    except Exception as e:
        print(f"[Guard/secret] error: {e} - считаю привилегированным")
        privileged = True
    print(f"[Guard/secret] {tool_name} -> {'привилегированный, нет ключа -> BLOCK' if privileged else 'обычный'}")
    return not privileged


async def guard_response_syntax(
    response: str,
    llm_config: dict,
    *,
    llm_call_fn: LLMCaller,
    strip_thinking_fn: StripThinkingFn,
) -> bool:
    """Проверить ответ агента на наличие tool calls в неправильном формате.
    True = синтаксис чистый (пропустить), False = заблокировать."""
    fmt = llm_config.get("prompt_format", "standard")
    if fmt == "harmony":
        correct = "только <|channel|>commentary to=functions.NAME <|constrain|>json<|message|>{...}<|end|> или чистый текст"
    elif fmt == "qwen":
        correct = "только <tool_call>{\"name\": ..., \"arguments\": {...}}</tool_call> или чистый текст"
    else:
        correct = "только чистый текст (tool calls идут через API, не через текст)"
    try:
        prompt = (
            "Проверь этот ответ ИИ-ассистента.\n"
            "Есть ли в нём попытка вызвать инструмент/функцию в НЕПРАВИЛЬНОМ формате?\n"
            f"Правильный формат для этой модели ({fmt}): {correct}.\n"
            "Неправильный — любые другие теги, квадратные скобки с именем функции, псевдокод вызова.\n"
            "Ответь одним словом: BAD или CLEAN.\n\n"
            f"Ответ ассистента:\n{response[:800]}"
        )
        resp = await llm_call_fn(
            llm_config,
            model=llm_config["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            temperature=0,
        )
        raw = strip_thinking_fn(resp.choices[0].message.content or "")
        # Также проверяем reasoning field (thinking models)
        if not raw.strip() and hasattr(resp.choices[0].message, "reasoning"):
            raw = resp.choices[0].message.reasoning or ""
        verdict = (raw.strip().split()[-1] if raw.strip() else "CLEAN").upper()
        safe = "BAD" not in verdict
        print(f"[Guard/syntax] {verdict} -> {'allow' if safe else 'BLOCK'}")
        return safe
    except Exception as e:
        print(f"[Guard/syntax] error: {e} — allowing")
        return True
