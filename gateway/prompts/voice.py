"""Системный промпт голосового хода (ADR-010): разделы общего промпта и правила одного вызова.

Общий промпт перечисляет tools и порядок дел для цикла agent_loop, где речь звучит на следующем шаге. На
голосовом пути шаг один и из tools только spawn_agent и quiet: с разделами общего промпта про tools модель отвечала
одним spawn_agent даже на «привет» (glm-5.3-flash, 9 окт: 3 из 3 фраз), поэтому они в голосовой промпт не входят. Характер, речь, слух и время (SAM-95), запрет вранья и выдуманного контента, имена задач и устная речь -
общие разделы. Сборка читает снимок души при каждом вызове; прежние имена модуля отдаёт __getattr__.
"""

import os

from .sections import section
from .system import SECTION_BREAK, SPEECH_PART, join

# Быстрая модель - не отдельный голос, а SAM (SAM-169): без «я только голос» и отказов от неуверенности (ONE_SAM).
# него и говорит на каждый обрывок (прогон реплик 12 окт 14:40-14:45, SAM-130); раздел идёт в промпт, только когда
# говорит друг дома
VOICE_ORDER = ("IDENTITY", "CHARACTER", "ONE_SAM", SPEECH_PART, "HEARING", "TIME_RULES", "NO_LIES", "VOICE_TURN_RULES",
               "NO_INVENTED_CONTENT", "TASK_NAMING", "ORAL_SPEECH")
# Быстрая модель (ADR-027, блоки 1-2): душа и сжатые правила одним разделом, правило секретов в нём же
VOICE_FAST_ORDER = ("IDENTITY", "CHARACTER", "TONE", "FAST_RULES")
_SECTIONS = frozenset({"VOICE_TURN_RULES", "ONE_SAM", "FAST_RULES"})


def voice_prompt_base() -> str:
    return join(VOICE_ORDER)


def voice_fast_template() -> str:
    """Шаблон быстрой модели голоса."""
    return join(VOICE_FAST_ORDER)


def voice_prompt_template() -> str:
    return voice_prompt_base()


_LAZY = {"VOICE_PROMPT_BASE": voice_prompt_base, "VOICE_PROMPT_TEMPLATE": voice_prompt_template}


def __getattr__(name: str):
    if name in _LAZY:
        return _LAZY[name]()
    if name in _SECTIONS:
        return section(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
