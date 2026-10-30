"""Раскладка промпта быстрой модели (ADR-027, SAM-229 этап 1): неизменное первым, память и время у фразы."""
from datetime import datetime

from gateway import runner
from gateway.prompts import GUARD_SECRET_RULES
from tests.test_runner import _make_deps

LLM = {"source": "lmstudio", "model": "qwen", "prompt_format": "qwen"}
HISTORY = [
    {"role": "summary", "content": "раньше говорили о полке", "time_range": "10:00 – 10:20"},
    {"role": "user", "content": "test-do-not-use: привет"},
    {"role": "assistant", "content": "Привет."},
]


def _fast(deps, rag, notes="", caps="ВОЗМОЖНОСТИ", prepared=""):
    return runner.build_fast_messages(deps, "voice", "фраза", list(HISTORY), rag, LLM, line=notes, capabilities=caps,
                                      prepared=prepared)


def test_static_head_is_the_same_on_two_turns(monkeypatch):
    """Неизменная часть одинакова на двух ходах подряд: разные память и время меняют только последнее сообщение."""
    deps = _make_deps()
    first = _fast(deps, "факт A", notes="ЗАМЕТКИ")

    class _Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2030, 1, 1, 9, 41)
    monkeypatch.setattr(runner, "datetime", _Later)
    second = _fast(deps, "факт B", notes="ЗАМЕТКИ")
    assert first[:-1] == second[:-1]   # всё до блоков к фразе одинаково: кэш LM Studio берёт его целиком
    assert first[-1] != second[-1]


def test_order_static_then_notes_then_dialog_then_phrase():
    deps = _make_deps()
    deps.own_work = lambda: "СВОЯ РАБОТА"
    msgs = _fast(deps, "факт памяти", notes="ЗАМЕТКИ")
    system = msgs[0]["content"]
    order = [system.index(x) for x in ("ROOT SYS", "ВОЗМОЖНОСТИ", "ЗАМЕТКИ")]
    assert order == sorted(order)
    assert runner.SUMMARIES_HEADER not in system   # сводок у быстрой нет: в них бывает личное
    assert GUARD_SECRET_RULES not in system   # правило секретов у быстрой - в сжатых правилах шаблона
    assert "СВОЯ РАБОТА" not in system and "факт памяти" not in system and "локальное время" not in system
    assert [m["role"] for m in msgs[1:]] == ["user", "assistant", "user", "user"]
    assert msgs[-2]["content"] == "фраза"   # фраза - реплика диалога, без памяти и времени
    last = msgs[-1]["content"]
    assert last.index("факт памяти") < last.index("локальное время") < last.index(runner.TASK_LINE.format(phrase="фраза"))


def test_prepared_replies_follow_memory_and_empty_blocks_vanish():
    """Блок 8 - после памяти; пустые блоки после диалога не показываются (ADR-027, «Обязательные и условные»)."""
    with_all = _fast(_make_deps(), "факт", prepared="ЗАГОТОВКИ")[-1]["content"]
    assert with_all.index("факт") < with_all.index("ЗАГОТОВКИ") < with_all.index("Задание")
    bare = _fast(_make_deps(), "")[-1]["content"]
    assert runner.RAG_HEADER not in bare and "ЗАГОТОВКИ" not in bare


def test_empty_notes_keep_a_constant_line():
    assert runner.NO_NOTES_LINE in _fast(_make_deps(), "")[0]["content"]


def test_main_layout_keeps_rag_and_own_work_in_system():
    deps = _make_deps()
    deps.own_work = lambda: "СВОЯ РАБОТА"
    system = runner.build_messages(deps, "voice", "фраза", list(HISTORY), "факт памяти", LLM)[0]["content"]
    assert "СВОЯ РАБОТА" in system and "факт памяти" in system and "локальное время" in system


def test_fast_template_is_soul_and_compressed_rules():
    """ADR-027, блоки 1-2: шаблон быстрой - душа и сжатые правила (с правилом секретов), без полных разделов."""
    from gateway.prompts.sections import section
    from gateway.prompts.voice import voice_fast_template, voice_prompt_template
    fast = voice_fast_template()
    assert section("FAST_RULES") in fast and section("IDENTITY") in fast and section("TONE") in fast
    assert section("HEARING") not in fast and section("ORAL_SPEECH") not in fast
    assert "по секрету" in section("FAST_RULES") and "quiet" in section("FAST_RULES")
    assert len(fast) < len(voice_prompt_template())


def test_secret_history_record_reaches_fast_only_as_a_placeholder():
    """ADR-027, «Проверка»: запись истории с пометкой секрета не попадает в промпт быстрой; основная её видит."""
    from gateway import secret_mark
    history = [*HISTORY, {"role": "user", "content": "test-do-not-use: по секрету, хочу котёнка", secret_mark.FIELD: True},
               {"role": "assistant", "content": "Хорошо, про котёнка никому."}]
    deps = _make_deps()
    fast = runner.build_fast_messages(deps, "voice", "фраза", list(history), "", LLM)
    main = runner.build_messages(deps, "voice", "фраза", list(history), "", LLM)
    assert "котёнка" not in str(fast) and secret_mark.PLACEHOLDER in str(fast)
    assert "котёнка" in str(main)


def test_main_voice_sees_its_raw_replies_fast_only_speech():
    """ADR-027 (SAM-227): неозвученные блоки прошлых ответов вырезаются только для быстрой."""
    from gateway import agent
    voice = agent._voice_runner_deps(None, "voice")
    assert voice.history_speech_only is False
    assert agent._fast_runner(None)(voice).history_speech_only is True
