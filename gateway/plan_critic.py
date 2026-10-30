"""Plan Critic — третий actor после Guard L1/L2.

Guard смотрит только на vandalism (паттерны). Critic смотрит на
СЕМАНТИКУ плана: соответствует ли набор вызовов воркера фактической
задаче, нет ли лишних/странных шагов, правильные ли параметры.

Если план OK — пропускает.
Если нет — возвращает СОВЕТ с готовым исправленным планом,
который инжектится в контекст воркера. Worker LLM получает hint
+ примеры правильных вызовов и переделывает план.

Включается через self_config.plan_critic_enabled.
"""
from __future__ import annotations

import json
from typing import Awaitable, Callable


CRITIC_PROMPT = """Ты — минималистичный критик плана воркера SAM.

ЗАДАЧА ВОРКЕРА:
{task}

ПЛАН (последовательность tool-вызовов которую воркер собрался выполнить):
{plan}

⛔ ТВОЯ ЗАДАЧА — НЕ УЛУЧШАТЬ план. Только убирать ОЧЕВИДНЫЕ глупости.

OK по умолчанию. Блокируй ТОЛЬКО если:
1. **Дублирующие шаги** — один и тот же вызов идёт 2+ раз подряд с теми же
   аргументами.
2. **Очевидная ошибка инструмента** — например задача "открыть калькулятор",
   а воркер запускает msconfig.exe или regedit.

⛔ НЕ ВМЕШИВАЙСЯ в эти вещи:
- Какой именно URL/программу воркер выбрал — это его выбор, ты не знаешь лучше.
- Какой язык поиска (русский/английский) — воркер сам решает.
- Параметры запроса — он сам подберёт.
- Глобальная "стратегия" — твоё дело только удалить лишнее.
- Если воркер делает search_web + потом mcp_call — это нормальный flow,
  НЕ блокируй "не надо искать если уже знаешь URL".

Если план содержит дубли или ошибку — ответь:
FIX: <одна фраза что лишнее или ошибочно>
PLAN:
<план БЕЗ лишних шагов — те же что воркер собирался, минус дубли/ошибки>

В любом другом случае — ответь ОДНОЙ строкой ровно:
OK"""


def _format_plan(calls: list[tuple[str, dict]]) -> str:
    """Форматирует план в читаемый вид для промпта критика."""
    lines = []
    for name, args in calls:
        args_str = json.dumps(args, ensure_ascii=False)
        if len(args_str) > 200:
            args_str = args_str[:200] + "...}"
        lines.append(f"  {name}({args_str})")
    return "\n".join(lines)


async def critique_plan(
    task: str,
    planned_calls: list[tuple[str, dict]],
    llm_config: dict,
    *,
    llm_call_fn: Callable[..., Awaitable],
    strip_thinking_fn: Callable[[str], str],
) -> tuple[bool, str]:
    """Вернёт (ok, advice).

    ok=True  → план хороший, advice=""
    ok=False → план плохой, advice — short human-readable hint
                с готовым исправлением для inject в worker context.
    """
    if not planned_calls or not task.strip():
        return True, ""

    prompt = CRITIC_PROMPT.format(
        task=task[:300],
        plan=_format_plan(planned_calls),
    )

    try:
        resp = await llm_call_fn(
            llm_config,
            model=llm_config["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0,
        )
        raw = strip_thinking_fn(resp.choices[0].message.content or "")
    except Exception as e:
        print(f"[Critic] LLM error: {e} — allowing plan")
        return True, ""

    text = raw.strip()
    head = text.split()[0].upper() if text else ""
    if head.startswith("OK"):
        return True, ""
    # Иначе считаем как FIX
    print(f"[Critic] BLOCK with advice ({len(text)} chars)")
    return False, text
