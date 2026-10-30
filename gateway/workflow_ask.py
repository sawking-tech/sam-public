"""Шаг ask workflow (ADR-020): разовый вопрос основной модели без хода - без истории, без tools, без записи в беседу.

Ответ - результат шага: следующие шаги читают его через {{имя_шага}} и if «шаг == текст». Модель и вызов приходят
параметрами: логика проверяется без сети.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from . import token_usage
from .env import env_int
from .text_utils import strip_thinking

ASK_MAX_TOKENS = env_int("SAM_WORKFLOW_ASK_MAX_TOKENS", 1024)   # у GLM рассуждение входит в выход: при 256 ответ упирался в потолок (9 окт)

GetLlm = Callable[[], Awaitable[dict | None]]
Call = Callable[..., Awaitable[Any]]


async def ask(question: str, session: str, get_llm: GetLlm, call: Call, normalize: Callable[[list, str, str], list]) -> str:
    """Один вызов основной модели; недоступна - пустая строка (шаг не падает, if по пустому результату решает файл)."""
    llm_config = await get_llm()
    if not llm_config:
        return ""
    src, model = llm_config.get("source", ""), llm_config["model"]
    messages = normalize([{"role": "user", "content": question}], src, model)
    with token_usage.usage_scope(session, token_usage.ROLE_WORKFLOW):
        resp = await call(llm_config, model=model, max_tokens=ASK_MAX_TOKENS, messages=messages)
    return strip_thinking(resp.choices[0].message.content or "").strip()
