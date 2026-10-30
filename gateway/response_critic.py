"""Response Critic — проверяет ОРКЕСТРАТОРА (main SAM) до отдачи ответа.

Сценарий: SAM пишет «открыла YouTube» без вызова spawn_agent — это
враньё. Pattern-detector в runner ловит распространённые случаи через
ACTION_VERBS, но LLM-critic ловит шире и тоньше:
- утверждения сделанного без tool calls
- противоречия с core-rules в RAG
- стилистические провалы (поэтика на фактический вопрос)

Если нарушение — возвращает FIX-совет, runner делает retry с инжектом
hint'a в messages.

Управляется через self_config.response_critic_enabled.
"""
from __future__ import annotations

from typing import Awaitable, Callable


CRITIC_PROMPT = """Ты — критик ответа оркестратора SAM (главный агент).
Проверяешь готовый response ПЕРЕД отдачей пользователю.

ИСХОДНЫЙ ЗАПРОС ПОЛЬЗОВАТЕЛЯ:
{user_msg}

ОТВЕТ ОРКЕСТРАТОРА (raw: речь, рассуждение и мета-блоки в тегах, любые tool calls):
{raw_response}

ИСПОЛЬЗОВАННЫЕ TOOLS в этом turn'е:
{tools_used}

ПРАВИЛА которые могут быть нарушены (из памяти SAM):
{rules}

Проверь:
1. Если в ответе утверждение что SAM что-то сделала/проверила/нашла/
   открыла/запустила/увидела — был ли в этом turn'е реальный вызов любого
   инструмента, который это делает? Большинство дел SAM делает сама своими
   инструментами (запомнить, настроить себя, вспомнить); spawn_agent нужен
   только долгому делу и делу на устройстве. Вызов был - это не нарушение.
2. Нарушает ли ответ что-то из правил?

Если всё ОК — ответь одной строкой: OK

Если нет — ответь:
FIX: <одна короткая фраза в чём проблема>

Без рассуждений. OK или FIX:."""


def _format_rules(rules: list[str]) -> str:
    if not rules:
        return "(нет правил)"
    return "\n".join(f"- {r[:200]}" for r in rules[:6])


async def critique_response(
    user_msg: str,
    raw_response: str,
    tools_used: set[str],
    rules: list[str],
    llm_config: dict,
    *,
    llm_call_fn: Callable[..., Awaitable],
    strip_thinking_fn: Callable[[str], str],
) -> tuple[bool, str]:
    """Возвращает (ok, advice).

    ok=False → advice содержит FIX-инструкцию, runner делает retry
    с этим hint'ом.
    """
    if not raw_response.strip():
        return True, ""

    prompt = CRITIC_PROMPT.format(
        user_msg=user_msg[:400],
        raw_response=raw_response[:800],
        tools_used=", ".join(sorted(tools_used)) or "(никаких)",
        rules=_format_rules(rules),
    )

    try:
        resp = await llm_call_fn(
            llm_config,
            model=llm_config["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0,
        )
        raw = strip_thinking_fn(resp.choices[0].message.content or "")
    except Exception as e:
        print(f"[ResponseCritic] LLM error: {e} — allowing")
        return True, ""

    head = (raw.strip().split()[0] if raw.strip() else "").upper()
    if head.startswith("OK"):
        return True, ""

    print(f"[ResponseCritic] BLOCK: {raw[:120]}")
    return False, raw.strip()
