"""Provider-specific transforms для tool schema и ошибок (SAM-27).

Портирован из opencode/packages/opencode/src/provider/transform.ts и error.ts
(SAM-27). Один источник истины для tool definitions —
just-in-time трансформации перед отправкой в конкретного провайдера.

Сейчас покрыто:
- Gemini schema sanitizer (enum→string, integer→string при enum, items default,
  cleanup non-object types)
- Error classifier (overflow / retryable / auth / other) для умного fallback
  в cooldown registry

Что ещё планируется (см. SAM-27 план):
- Message normalization (Claude toolCallId scrub, Mistral 9-char limit,
  пустой assistant между tool и user)
- Tool result format adapter (parsable JSON vs plain text по провайдеру)
"""

from __future__ import annotations

import re
from typing import Any

# ─── Gemini schema sanitizer (портирован из transform.ts:996-1043) ─────────────

_GEMINI_COMBINERS = {"anyOf", "oneOf", "allOf"}
_GEMINI_SCHEMA_KEYS = {
    "type", "properties", "items", "prefixItems", "enum", "const", "$ref",
    "additionalProperties", "patternProperties", "required", "not",
    "if", "then", "else",
}


def _is_plain_dict(node: Any) -> bool:
    return isinstance(node, dict)


def _has_combiner(node: Any) -> bool:
    return _is_plain_dict(node) and any(
        isinstance(node.get(k), list) for k in _GEMINI_COMBINERS
    )


def _has_schema_intent(node: Any) -> bool:
    if not _is_plain_dict(node):
        return False
    if _has_combiner(node):
        return True
    return any(k in node for k in _GEMINI_SCHEMA_KEYS)


def sanitize_for_gemini(schema: Any) -> Any:
    """Адаптировать JSON Schema под капризы Gemini API.

    Gemini требует:
    - enum значения всегда string (даже если type=integer)
    - если type=integer/number И есть enum → меняем type на string
    - items всегда присутствует в array-schema (иначе ошибка)
    - properties/required только у object — у остальных удалить
    - required содержит только реально существующие в properties поля
    """
    if schema is None or not isinstance(schema, (dict, list)):
        return schema

    if isinstance(schema, list):
        return [sanitize_for_gemini(x) for x in schema]

    result: dict = {}
    for key, value in schema.items():
        if key == "enum" and isinstance(value, list):
            result[key] = [str(v) for v in value]
            # integer/number + enum → меняем type на string
            if result.get("type") in ("integer", "number"):
                result["type"] = "string"
        elif isinstance(value, dict):
            result[key] = sanitize_for_gemini(value)
        elif isinstance(value, list):
            result[key] = [sanitize_for_gemini(v) for v in value]
        else:
            result[key] = value

    # required только из существующих в properties
    if result.get("type") == "object" and isinstance(result.get("properties"), dict) \
            and isinstance(result.get("required"), list):
        props = result["properties"]
        result["required"] = [f for f in result["required"] if f in props]

    # items обязателен для array
    if result.get("type") == "array" and not _has_combiner(result):
        if result.get("items") is None:
            result["items"] = {}
        items = result["items"]
        if _is_plain_dict(items) and not _has_schema_intent(items):
            items["type"] = "string"

    # properties/required только у object
    if result.get("type") and result["type"] != "object" and not _has_combiner(result):
        result.pop("properties", None)
        result.pop("required", None)

    return result


def _is_gemini(source: str, model: str) -> bool:
    return source == "gemini" or "gemini" in (model or "").lower()


# Штрафы повторов против залипания gemma/qwen на одном блоке; OpenAI-совместимый вход Gemini их не принимает
# (400 «Unknown name frequency_penalty»), повтор хода и воркер падали целиком
ANTI_REPEAT_PENALTIES = {"frequency_penalty": 0.4, "presence_penalty": 0.2}


def sampling_for_provider(source: str, model: str = "") -> dict:
    """Параметры выборки, которые поставщик принимает: штрафы повторов - всем, кроме Gemini."""
    return {} if _is_gemini(source, model) else dict(ANTI_REPEAT_PENALTIES)


def strip_unsupported_sampling(kwargs: dict, source: str, model: str = "") -> dict:
    """Параметры вызова без штрафов повторов, если поставщик их не принимает: при замене поставщика посреди хода
    (фолбэк по лимиту) параметры остались от прежнего, и Gemini отвечал 400."""
    if not _is_gemini(source, model):
        return kwargs
    return {k: v for k, v in kwargs.items() if k not in ANTI_REPEAT_PENALTIES}


def sanitize_tools_for_provider(tools: list[dict], source: str, model: str = "") -> list[dict]:
    """Адаптировать список tools под конкретного провайдера.

    Сейчас Gemini-only — для других провайдеров возвращается без изменений.

    Args:
        tools: OpenAI-формат tools schema (list[{type:function, function:{name, ...}}])
        source: "lmstudio" | "gemini" | "dashscope" | "anthropic" | ...
        model: model id (для проверки имени модели — например "gemini-..." через openai-compat)
    """
    if not _is_gemini(source, model):
        return tools

    out: list[dict] = []
    for t in tools:
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = dict(t["function"])
            params = fn.get("parameters")
            if isinstance(params, dict):
                fn["parameters"] = sanitize_for_gemini(params)
            out.append({**t, "function": fn})
        else:
            out.append(t)
    return out


# ─── Message normalization (портирован из transform.ts:49-190) ─────────────────
# Каждый провайдер имеет особые требования к tool_call_id и структуре messages.
# Эти функции приводят OpenAI-формат к тому что принимает конкретный провайдер.

_CLAUDE_TOOL_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")
_MISTRAL_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]")


def _scrub_claude_tool_id(tool_id: str) -> str:
    """Claude разрешает в toolCallId только [a-zA-Z0-9_-]. Всё остальное → '_'."""
    if not tool_id:
        return tool_id
    return _CLAUDE_TOOL_ID_RE.sub("_", tool_id)


def _scrub_mistral_tool_id(tool_id: str) -> str:
    """Mistral разрешает максимум 9 alphanumeric символов в toolCallId.
    Pad нулями если короче, обрезаем до 9."""
    if not tool_id:
        return tool_id
    cleaned = _MISTRAL_ALNUM_RE.sub("", tool_id)
    cleaned = cleaned[:9]
    if len(cleaned) < 9:
        cleaned = cleaned.ljust(9, "0")
    return cleaned


def _normalize_for_claude(messages: list[dict]) -> list[dict]:
    """Claude (anthropic) подгоняет messages: tool_call_id чистится."""
    out: list[dict] = []
    for m in messages:
        msg = dict(m)
        if "tool_calls" in msg and isinstance(msg["tool_calls"], list):
            new_tc = []
            for tc in msg["tool_calls"]:
                tc2 = dict(tc)
                if "id" in tc2:
                    tc2["id"] = _scrub_claude_tool_id(tc2["id"])
                new_tc.append(tc2)
            msg["tool_calls"] = new_tc
        if msg.get("role") == "tool" and "tool_call_id" in msg:
            msg["tool_call_id"] = _scrub_claude_tool_id(msg["tool_call_id"])
        # Claude не любит пустой content в assistant без tool_calls
        if msg.get("role") == "assistant" and not msg.get("tool_calls"):
            content = msg.get("content")
            if isinstance(content, str) and not content.strip():
                msg["content"] = " "  # минимальный non-empty
        out.append(msg)
    return out


def _normalize_for_mistral(messages: list[dict]) -> list[dict]:
    """Mistral специфика:
    - tool_call_id максимум 9 alphanumeric
    - tool message не может идти сразу после user (нужен пустой assistant)
    """
    out: list[dict] = []
    prev_role: str | None = None
    for m in messages:
        msg = dict(m)
        if "tool_calls" in msg and isinstance(msg["tool_calls"], list):
            new_tc = []
            for tc in msg["tool_calls"]:
                tc2 = dict(tc)
                if "id" in tc2:
                    tc2["id"] = _scrub_mistral_tool_id(tc2["id"])
                new_tc.append(tc2)
            msg["tool_calls"] = new_tc
        if msg.get("role") == "tool":
            if "tool_call_id" in msg:
                msg["tool_call_id"] = _scrub_mistral_tool_id(msg["tool_call_id"])
            # tool сразу после user → вставить пустой assistant
            if prev_role == "user":
                out.append({"role": "assistant", "content": " "})
        out.append(msg)
        prev_role = msg.get("role")
    return out


def _normalize_for_gemini(messages: list[dict]) -> list[dict]:
    """Gemini (SAM-261): вызов функции допустим только сразу после user-хода или ответа функции, иначе 400
    «function call turn comes immediately after a user turn or after a function response turn».
    - системные сообщения посреди истории (сводка сжатия) становятся user-ходами;
    - подряд идущие assistant-ходы склеиваются (текст перед вызовом уходит в тот же ход);
    - ответ инструмента без вызова перед ним выбрасывается."""
    out: list[dict] = []
    for index, m in enumerate(messages):
        msg = dict(m)
        role = msg.get("role")
        if role == "system" and index > 0:
            msg["role"], role = "user", "user"
        if role == "tool":
            last = out[-1] if out else {}
            known = last.get("role") == "tool" or bool(last.get("tool_calls"))
            if not known:
                continue
        if role == "assistant" and out and out[-1].get("role") == "assistant" and not out[-1].get("tool_calls"):
            prev = out.pop()
            joined = "\n".join(t for t in (prev.get("content") or "", msg.get("content") or "") if isinstance(t, str) and t)
            msg["content"] = joined
        out.append(msg)
    return out


def normalize_messages_for_provider(
    messages: list[dict], source: str, model: str = "",
) -> list[dict]:
    """Адаптировать messages под формат конкретного провайдера.
    Сейчас покрыто: Claude (anthropic), Mistral, Gemini.
    Для LM Studio / OpenAI / DashScope — возвращается без изменений.
    """
    src_l = (source or "").lower()
    mdl_l = (model or "").lower()
    if src_l == "anthropic" or "claude" in mdl_l:
        return _normalize_for_claude(messages)
    if src_l == "mistral" or "mistral" in mdl_l:
        return _normalize_for_mistral(messages)
    if _is_gemini(src_l, mdl_l):
        return _normalize_for_gemini(messages)
    return messages


# ─── Error classifier (портирован из error.ts:9-29, 173-196) ───────────────────

_OVERFLOW_PATTERNS = (
    re.compile(r"context.{0,20}length", re.IGNORECASE),
    re.compile(r"maximum.{0,20}context", re.IGNORECASE),
    re.compile(r"exceeded.{0,20}token", re.IGNORECASE),
    re.compile(r"too.{0,5}long", re.IGNORECASE),
    re.compile(r"input.{0,20}too.{0,10}large", re.IGNORECASE),
    re.compile(r"prompt.{0,10}token.{0,10}exceed", re.IGNORECASE),
)

_AUTH_PATTERNS = (
    re.compile(r"\binvalid.{0,10}api.{0,5}key\b", re.IGNORECASE),
    re.compile(r"\bunauthor", re.IGNORECASE),
    re.compile(r"\bauthentication\b", re.IGNORECASE),
    re.compile(r"\b401\b"),
    re.compile(r"\b403\b"),
)


def classify_error(exc: BaseException) -> dict:
    """Классифицировать ошибку API для умного fallback.

    Returns:
        {
          "kind": "overflow" | "rate_limit" | "auth" | "server" | "unknown",
          "retryable": bool,   # есть смысл просто повторить?
          "cooldown_s": int,   # как долго класть провайдера в cooldown
          "switch_provider": bool,  # переключаться на следующий?
        }

    Сейчас классификация основана на тексте ошибки (HTTPError, openai.APIError,
    google exceptions); status_code достаётся если присутствует на объекте.
    """
    text = str(exc) or exc.__class__.__name__
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)

    # 429 → rate limit (cooldown короткий, fallback мгновенный)
    if status == 429 or "429" in text or "rate" in text.lower() and "limit" in text.lower():
        return {"kind": "rate_limit", "retryable": True, "cooldown_s": 30, "switch_provider": True}

    # 401/403/auth ключ → длинный cooldown, переключение
    if status in (401, 403) or any(p.search(text) for p in _AUTH_PATTERNS):
        return {"kind": "auth", "retryable": False, "cooldown_s": 3600, "switch_provider": True}

    # context overflow → не retryable, переключения не помогут (надо урезать контекст)
    if any(p.search(text) for p in _OVERFLOW_PATTERNS):
        return {"kind": "overflow", "retryable": False, "cooldown_s": 0, "switch_provider": False}

    # 5xx → серверная, retry с коротким бэкоффом, при повторе переключение
    if status and 500 <= int(status) < 600:
        return {"kind": "server", "retryable": True, "cooldown_s": 10, "switch_provider": True}

    # Всё остальное — неизвестно, лучше переключиться
    return {"kind": "unknown", "retryable": False, "cooldown_s": 60, "switch_provider": True}
