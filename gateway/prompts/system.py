"""Системный промпт основной модели SAM: сборка из разделов души и инструкций (gateway/prompts/sections.py).

Характер (IDENTITY, CHARACTER, TONE) живёт в хранилище SAM, инструкции - в SAM.md; здесь - только какие разделы и в
каком порядке. Сборка читает снимок души при каждом вызове: правка характера доходит до следующего хода без рестарта.
Шаблон с метками {женская|мужская} форма: род речи - по голосу (SAM-78, gateway/prompts/identity.py); подстановки
{ключ} заполняет личный слой (gateway/prompts/personal.py). system_prompt() - сборка в роде по умолчанию с
подстановками из шапки SAM.md, для кода, которому род и имена не важны.

Прежние имена модуля (IDENTITY, SPEECH, SYSTEM_PROMPT_TEMPLATE, SYSTEM_PROMPT и другие разделы) отдаёт __getattr__:
текущий текст на момент обращения, а не снимок импорта.

Голосовой ход (gateway/prompts/voice.py) и ведущий (gateway/prompts/lead.py) берут часть тех же разделов. Список
tools и порядок дел (DOING, WHEN_WHAT) годятся только циклу agent_loop, где речь звучит на следующем шаге.
"""

from .identity import DEFAULT_GENDER, render
from .instructions import meta
from .prompt_md import fill
from .sections import section

# Ответ при принудительном финише, пока фоновая задача хода не вернула итог (SAM-127, agent_loop.pending_reply)
PENDING_REPLY = "Ещё проверяю - скажу, как только будет результат."

SECTION_BREAK = "\n\n"
SPEECH_PART = "SPEECH"   # речь и тон раздельно: промпт ведущего (prompts/lead.py) берёт тон, а говорит командой say

MAIN_ORDER = ("IDENTITY", "DOING", "CHARACTER", SPEECH_PART, "HEARING", "TIME_RULES", "NO_LIES", "NO_INVENTED_CONTENT",
              "TOOLS", "WHEN_WHAT", "TASK_NAMING", "ORAL_SPEECH")
# Разделы, которые модуль отдавал константами
_SECTIONS = frozenset({"IDENTITY", "DOING", "CHARACTER", "HEARD", "TONE", "HEARING", "TIME_RULES", "NO_LIES",
                       "NO_INVENTED_CONTENT", "TOOLS", "WHEN_WHAT", "TASK_NAMING", "ORAL_SPEECH"})


def speech() -> str:
    return section("HEARD") + "\n" + section("TONE")


def part(name: str) -> str:
    """Текст части промпта: раздел души или инструкций, или речь (HEARD и TONE)."""
    return speech() if name == SPEECH_PART else section(name)


def join(names) -> str:
    return SECTION_BREAK.join(part(name) for name in names)


def system_prompt_template() -> str:
    return join(MAIN_ORDER)


def system_prompt() -> str:
    return fill(render(system_prompt_template(), DEFAULT_GENDER), meta())


_LAZY = {"SPEECH": speech, "SYSTEM_PROMPT_TEMPLATE": system_prompt_template, "SYSTEM_PROMPT": system_prompt}


def __getattr__(name: str):
    if name in _LAZY:
        return _LAZY[name]()
    if name in _SECTIONS:
        return section(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
