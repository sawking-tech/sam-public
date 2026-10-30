"""Голосовой ход (ADR-010, SAM-53): фраза -> один вызов LLM потоком -> речь.

В отличие от `runner.run_turn`, здесь нет guard, критика ответа и повторов: каждый из них - ещё
один вызов LLM до звука. Действие уходит воркеру через `spawn_agent`, проверки остаются там.

Это не свой цикл модель-tool (цикл агента один на все роли): вызов один, его tools исполняет общий
`agent_loop.execute_tool`, текстовые вызовы разбирает тот же `parse_channel_format`, сборка
контекста - общая `runner.build_messages`.

Логика хода не знает про сокет: дельты уходят через колбэки, транспорт - в `main.py`.
"""
from __future__ import annotations

import asyncio
import os
import json
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from . import addressee, llm_gate, main_model, phrase_class, quiet_mode, recall, reply_class, spoken_history, token_usage
from .agent_loop import AgentLoopDeps, execute_tool
from . import recall_history
from .runner import PHRASE_SEP, SOURCE_FAST, SOURCE_MAIN, RunnerDeps, answer_source, _build_rag_query, archive_for, build_fast_messages, build_messages
from .env import env_int
from .llm_router import ROUTE_FAST, ROUTE_KEY
from .repos.session_repo import IGNORE

# Tools голосового пути: действие - воркеру, разговор - здесь; quiet - тихий режим сессии на срок голосом («помолчи час»)
VOICE_TOOL_NAMES = ("spawn_agent", "quiet")
# У быстрой вызовов нет, кроме минимума (SAM-169): действие делает основная отложенным вызовом, быстрая знает список
# возможностей SAM (VoiceTurnDeps.capabilities) и берёт паузу
FAST_TOOL_NAMES = ("quiet",)
# Срок поиска в памяти до ответа - общий для всех входов (gateway/recall.py): не успевшая память ответ не держит
RAG_TIMEOUT_S = recall.TIMEOUT_S
# Кто отвечает на ход - в сообщении source мосту (voice/layer/bridge.py): поправка отзывает только быструю
# Голосовая реплика короткая; отсечка против повтора блока, как max_tokens в agent_loop.
MAX_TOKENS = env_int("VOICE_MAX_TOKENS", 512)


# Профиль голоса -> имя для истории («профиль=Имя,...», у человека может быть два профиля: обычный и громкий); имена
# домашних - в .env, в коде их нет; не найден - id профиля как есть
SPEAKER_NAMES = dict(pair.split("=", 1) for pair in os.getenv("SAM_SPEAKER_NAMES", "").split(",") if "=" in pair)


def speaker_name(profile: str) -> str:
    return SPEAKER_NAMES.get(profile, profile)


@dataclass
class VoiceTurnDeps:
    """runner - история, память, шина и сборка контекста, общие с текстовым ходом;
    loop - поставщик, разбор вызовов и исполнение tools, общие с agent_loop."""
    runner: RunnerDeps
    loop: AgentLoopDeps
    rag_timeout_s: float = RAG_TIMEOUT_S
    max_tokens: int = MAX_TOKENS
    # Голос с отчётом о судьбе: ответ идёт в историю после отчёта моста.
    # None - канал без озвучки: ответ пишется в историю сразу.
    ledger: Any = None
    # Когда SAM последний раз звучала в сессии (monotonic): окно беседы
    last_spoken: dict = field(default_factory=dict)
    # Тишина сессий (gateway/quiet_mode.py): общий файл с мостом и инструментом quiet; без шины - в памяти.
    # Просили помолчать - до обращения по имени фразы не зовут модель. Раньше был словарь на ход и тишина не держалась
    quiet: quiet_mode.QuietBook = field(default_factory=lambda: quiet_mode.QuietBook(None))
    clock: Any = time.monotonic
    decide: Callable[[addressee.Heard], addressee.Verdict] = addressee.decide
    # Основная модель на этот ход (gateway/main_model.py, SAM-91): ответ рождается в шлюзе (LLM) или снаружи
    # (сессия Claude Code, no-op) - тогда ход пишется в историю, модель не зовётся. Выбор - на каждый ход
    main_model: Callable[[], main_model.ConcreteModel] = lambda: main_model.llm()
    # Своя реплика без модели (agent.say_own): подтверждение входа в тишину; None - канал без озвучки
    say: Callable[[str, str], bool] | None = None
    # Заметки основной модели о разговоре текстом (SAM-131): быстрая отвечает по линии разговора, а не по обрывку
    notes: Callable[[str], str] = lambda session: ""
    # Граница компетенции быстрой (SAM-169): кто отвечает на фразу по существу. Фраза не по силам быстрой - она
    # коротко берёт паузу («посмотрю», «вспомню»), ответ по существу ведёт основная (workflow fast-handoff).
    # По умолчанию граница выключена: всё отвечает маршрут fast
    responder_for: Callable[[str], str] = lambda phrase: phrase_class.FAST
    # Что умеет SAM текстом: сколько всего и короткий список (быстрая знает, что можно, а делает основная)
    capabilities: Callable[[], str] = lambda: ""
    # Заметки для быстрой (ADR-027): линия с выжимкой и заготовки раздельно
    notes_parts: Callable[[str], tuple[str, str]] = lambda session: ("", "")
    # Шаблон быстрой (ADR-027): зависимости хода с её системным промптом; корень композиции - gateway/agent.py
    fast_runner: Callable[[RunnerDeps], RunnerDeps] = lambda rd: rd
    # Пауза до ответа основной (SAM-169): фраза из набора конфига, разная каждый раз; None - пауза пишется моделью
    # с подсказкой. Код, а не модель: подсказку быстрая qwen держала 1 раз из 3 (прогон 12 окт, «сейчас сделаю» на
    # «вымой посуду», выдуманная погода)
    pause: Callable[[], str] | None = None
    # Быстрый голос при основной вне шлюза (ведущий Claude Code, SAM-173): быстрая отвечает сразу, ведущий получает
    # фразу своим путём (тикер) и ответ быстрой входом deliver и делает по существу; False - ведущий отвечает один
    fast_with_lead: bool = False


@dataclass
class VoiceTurnResult:
    raw: str                                  # ответ модели без размышлений
    voice: str                                # то, что звучит
    tools: list[str] = field(default_factory=list)
    rag_used: bool = False
    addressed: str = addressee.ME           # решение addressee: me, unsure; not_me и background - без хода
    source: str = SOURCE_MAIN               # кто ответил: SOURCE_FAST или SOURCE_MAIN


class _AnswerSource:
    """Кто отвечает на ход: по маршруту, после смены поставщика - основная модель (замену llm_client берёт
    маршрутом по умолчанию). Каждое значение сообщается on_source."""

    def __init__(self, llm_config: dict | None, on_source: Callable[[str], None] | None) -> None:
        self._on_source = on_source
        self.value = answer_source(llm_config)
        self._tell()

    def _tell(self) -> None:
        if self._on_source is not None:
            self._on_source(self.value)

    def reset_hook(self, on_reset: Callable[[], None] | None) -> Callable[[], None]:
        def _reset() -> None:
            if on_reset is not None:
                on_reset()
            self.value = SOURCE_MAIN
            self._tell()
        return _reset


async def run_voice_turn(
    deps: VoiceTurnDeps,
    text: str,
    session_id: str,
    on_delta: Callable[[str], None] | None = None,
    on_reset: Callable[[], None] | None = None,
    who: str | None = None,
    echo: bool = False,
    on_source: Callable[[str], None] | None = None,
    heard_at: str | None = None,
) -> VoiceTurnResult:
    """Один голосовой ход. Ровно один вызов LLM (тест считает вызовы), если фраза ей или
    код не уверен; фон и «не мне» - без вызова, в монитор и историю скрытой записью с решением (SAM-98), модели
    не идут. on_source(SOURCE_*) - кто отвечает,
    до первой дельты и снова после смены поставщика. heard_at - время фразы в voice-in.jsonl: к нему
    модель вне шлюза относит найденные факты (SAM-101); нет - время прихода хода."""
    verdict = _addressed(deps, text, session_id, who, echo)
    if verdict.to in (addressee.BACKGROUND, addressee.NOT_ME):
        _show_unaddressed(deps, text, session_id, who, verdict)
        return VoiceTurnResult(raw="", voice="", addressed=verdict.to)
    with token_usage.usage_scope(session_id, token_usage.role_for_session(session_id)),             token_usage.trace_turn():
        rd = deps.runner
        rd.event_bus.emit("user_message", {"content": text, "session": session_id, ACCEPTED_FIELD: ACCEPTED_ON,
                                           **({"who": speaker_name(who)} if who else {})})
        responder = deps.main_model()
        history = await asyncio.to_thread(rd.get_session_history, session_id, rd.history_limit)
        token_usage.mark_stage("history")
        context = _with_in_flight(deps, session_id, history)
        # Память до развилки основной модели (SAM-101): модели вне шлюза факты уходят в её deliver
        found: dict[str, str] = {}

        def _search(query: str, limit: int) -> str:
            """Один поиск: основной и ведущему - вся выдача, быстрой - своя (ADR-027) в found."""
            if rd.get_rag_pair is None:
                return rd.get_rag_context(query, limit)
            main_facts, found["fast"] = rd.get_rag_pair(query, limit)
            return main_facts
        rag = await recall.at_input(responder, _search, _build_rag_query(text, context),
                                    limit=rd.rag_limit, timeout_s=deps.rag_timeout_s,
                                    at=heard_at or recall.now_iso(), phrase=text, session=session_id)
        token_usage.mark_stage("rag")
        lead = not responder.in_gateway
        if lead:
            rd.event_bus.emit("main_model_turn", {"session": session_id, "model": responder.name, "text": text[:120]})
        llm_config = await rd.get_best_llm() if not lead or deps.fast_with_lead else None
        if lead and answer_source(llm_config) != SOURCE_FAST:   # нет быстрой - отвечает ведущий, модель шлюза не зовём
            return VoiceTurnResult(raw="", voice="", rag_used=bool(rag), addressed=verdict.to)
        rd.on_session_change(session_id, llm_config)
        source = _AnswerSource(llm_config, on_source)
        token_usage.mark_stage("route")
        fast = answer_source(llm_config) == SOURCE_FAST
        if fast:   # ADR-027: раскладка для кэша LM Studio - неизменное первым, память и время у фразы
            line, prepared = deps.notes_parts(session_id)
            fast_rag = found.get("fast", rag) if rd.get_rag_pair is not None else rag
            messages = build_fast_messages(deps.fast_runner(rd), session_id, text, context, fast_rag, llm_config,
                                           line=line, capabilities=deps.capabilities(), prepared=prepared)
        else:
            archive = await archive_for(rd, session_id, text, recall_history.ARCHIVE_TIMEOUT_S)
            messages = build_messages(rd, session_id, text, context, rag, llm_config, archive=archive)
            _add_notes(messages, deps.notes(session_id))
        handoff = fast and deps.responder_for(text) == phrase_class.MAIN
        if handoff and verdict.to == addressee.ME and deps.pause is not None:
            result = _say_pause(deps, session_id, text, deps.pause(), on_delta, source, rag, responder.name)
            _tell_lead(responder, session_id, text, result.voice, handoff=True)
            return result
        if handoff:
            _add_to_phrase(messages, HANDOFF_HINT)
        if verdict.to == addressee.UNSURE:
            _add_listener_hint(messages, who, deps.last_spoken.get(session_id), deps.clock())
        token_usage.mark_stage("context")

        # только сам вызов: задачи, созданные выше (сжатие), унесли контекст без приоритета (SAM-84)
        with llm_gate.priority_scope(llm_gate.PRIORITY_VOICE):
            msg = await _one_call(deps, messages, llm_config, session_id, on_delta, source.reset_hook(on_reset))
        tools = await _run_voice_tools(deps, msg, llm_config, session_id)

        raw = deps.loop.strip_thinking(msg.content or "")
        voice = _voice_of(rd, raw)
        _publish_reply(deps, session_id, raw, voice, text, source.value, llm_config.get("model", ""),
                       reply_class.classify(voice, tools), handoff, responder.name)
        _tell_lead(responder, session_id, text, voice, handoff)
        if voice.strip():
            deps.last_spoken[session_id] = deps.clock()
        return VoiceTurnResult(raw=raw, voice=voice, tools=tools, rag_used=bool(rag), addressed=verdict.to,
                               source=source.value)


def _tell_lead(responder, session_id: str, phrase: str, voice: str, handoff: bool) -> None:
    """Ведущему (основная вне шлюза) - что сказала быстрая на его фразу: он делает по существу или поправляет."""
    if responder.in_gateway or not voice.strip():
        return
    note = FAST_REPLY_NOTE_HANDOFF if handoff else FAST_REPLY_NOTE
    responder.deliver({"text": note.format(phrase=phrase, voice=voice), "source": FAST_REPLY_SOURCE,
                       "session": session_id})


FAST_REPLY_SOURCE = "fast_reply"
FAST_REPLY_NOTE = ("[быстрая уже ответила на «{phrase}»: «{voice}». Верно - молчи; неверно или выдумано - поправь "
                   "(say --fix); обещано действие - сделай]")
FAST_REPLY_NOTE_HANDOFF = ("[быстрая взяла паузу на «{phrase}»: «{voice}». Ответ по существу и действие - за тобой]")


def _say_pause(deps: VoiceTurnDeps, session_id: str, phrase: str, pause: str, on_delta, source: "_AnswerSource",
               rag: str, main: str = "") -> "VoiceTurnResult":
    """Пауза без вызова модели: звучит сразу, ответ помечен handoff - по существу ответит основная (fast-handoff)."""
    _delta_sink(deps.loop, session_id, on_delta)(pause)
    deps.loop.event_bus.emit("assistant_stream_end", {"session": session_id})
    _publish_reply(deps, session_id, pause, pause, phrase, source.value, PAUSE_MODEL, handoff=True, main=main)
    deps.last_spoken[session_id] = deps.clock()
    return VoiceTurnResult(raw=pause, voice=pause, rag_used=bool(rag), addressed=addressee.ME, source=source.value)


PAUSE_MODEL = "pause"   # подпись модели у паузы в мониторе и событии: ответила не модель, а код


def _voice_of(rd, raw: str) -> str:
    """Что звучит: весь ответ без мета-блоков (rd.speech_of - text_utils.transparent_speech, ADR-003) и меток;
    пусто после <silent> - молчание."""
    return drop_marks(rd.speech_of(raw)).strip()


# Служебные метки в речи - как у моста (voice/layer/speech_text.py MARK_MAX_CHARS, _MARK):
# модель копирует «[прервано]», «[вызов: x]» из контекста, а в речи скобок нет
MARK_MAX_CHARS = 40
_MARK = re.compile(rf"\[[^\[\]\n]{{1,{MARK_MAX_CHARS}}}\]\s*")


def drop_marks(text: str) -> str:
    """Речь без служебных меток в квадратных скобках."""
    return _MARK.sub("", text)


# Подсказка модели, когда код не решил, SAM ли сказано: кто сказал, сколько прошло, право молчать
HANDOFF_HINT = ("[Это действие или факт, которого может не быть в разговоре. Действие - коротко скажи своими словами, "
                "что сделаешь; не уверена, что SAM это умеет, - скажи, что не уверена и посмотришь, что можно сделать, "
                "не отказывай. Факт - знаешь точно из разговора, заметок или памяти - ответь; не уверена - ничего не "
                "выдумывай, скажи, что посмотришь или вспомнишь.]")
MAIN_FIELD = "main"
# поле события ответа: по нему workflow fast-handoff зовёт основную
HANDOFF_FIELD, HANDOFF_ON, HANDOFF_OFF = "handoff", "1", "0"
SPOKEN_FIELD, SPOKEN_ON, SPOKEN_OFF = "spoken", "1", "0"
ACCEPTED_FIELD, ACCEPTED_ON = "accepted", "1"   # фраза принята в разговор (мне или «не уверена»), не фон


LISTENER_HINT = ("[Слышу: {who}; {since}. Если сказано не тебе (люди говорят между собой, телевизор), "
                 "можешь промолчать: <silent></silent> - молчание тоже ответ.]")


def _addressed(deps: VoiceTurnDeps, text: str, session_id: str, who: str | None, echo: bool):
    last = deps.last_spoken.get(session_id)
    since = None if last is None else deps.clock() - last
    quiet = deps.quiet.is_quiet(session_id)
    verdict = deps.decide(addressee.Heard(text, who, echo, since, quiet))
    after = addressee.next_quiet(quiet, text, verdict)
    if after and not quiet:
        deps.quiet.on(session_id, None, quiet_mode.BY_PHRASE)
        if deps.say is not None:
            deps.say(session_id, quiet_mode.ENTER_SAY)   # готовой фразой, модель не зовётся
    elif quiet and not after:
        deps.quiet.off(session_id, quiet_mode.BY_NAME)
        if verdict.to != addressee.ME and deps.say is not None:
            deps.say(session_id, quiet_mode.EXIT_SAY)   # сняли разрешением без имени: ответа модели не будет, подтверждение готовой фразой
    deps.runner.event_bus.emit("addressee", {"to": verdict.to, "reason": verdict.reason, "who": who,
                                             "text": text[:120], "session": session_id})
    return verdict


# Решение адресата в мониторе (SAM-98): та же разметка, что у модели - что это и кто сказал
VERDICT_LABELS = {addressee.BACKGROUND: "фон", addressee.NOT_ME: "не мне"}
UNKNOWN_VOICE = "голос не узнан"


def heard_mark(verdict: addressee.Verdict, who: str | None) -> str:
    """«фон - обрывок вне беседы; голос не узнан»: решение, причина, кто сказал."""
    label = VERDICT_LABELS.get(verdict.to, verdict.to)
    return f"{label} - {verdict.reason}; {speaker_name(who) if who else UNKNOWN_VOICE}"


def _show_unaddressed(deps: VoiceTurnDeps, text: str, session_id: str, who: str | None,
                      verdict: addressee.Verdict) -> None:
    """Фраза, решённая как фон или «не мне»: в монитор и историю с пометкой ignore - монитор прячет её во
    второстепенные (как поправку SAM-92), контекст и сводка её не берут, после F5 она там же."""
    deps.runner.event_bus.emit("user_message", {"content": text, "session": session_id,
                                                **({"who": speaker_name(who)} if who else {}),
                                                IGNORE: heard_mark(verdict, who)})


def _add_to_phrase(messages: list, hint: str) -> None:
    """Подсказка хода - прямо перед фразой в последней реплике человека; у быстрой перед фразой стоят память и время
    (runner.build_fast_messages), подсказка идёт после них, вплотную к фразе."""
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            head, sep, phrase = m["content"].rpartition(PHRASE_SEP)
            m["content"] = f"{head}{sep}{hint}\n{phrase}"
            return


def _add_listener_hint(messages: list, who: str | None, last: float | None, now: float) -> None:
    since = "реплик от тебя ещё не было" if last is None else f"{int(now - last)} с после твоей реплики"
    _add_to_phrase(messages, LISTENER_HINT.format(who=who or UNKNOWN_VOICE, since=since))


def _add_notes(messages: list[dict], notes: str) -> None:
    """Заметки о разговоре - в конец системного сообщения: рядом с памятью, до истории."""
    if notes and messages and messages[0].get("role") == "system":
        messages[0] = {**messages[0], "content": f"{messages[0]['content']}\n\n{notes}"}


def _with_in_flight(deps: VoiceTurnDeps, session_id: str, history: list) -> list:
    """Контекст хода: история и ответы прошлых ходов, которые ещё звучат (spoken_history.with_in_flight).
    Сжатие получает историю без них: в историю они пойдут по отчёту моста."""
    if deps.ledger is None:
        return history
    return spoken_history.with_in_flight(history, deps.ledger.in_flight(session_id))


def _publish_reply(deps: VoiceTurnDeps, session_id: str, raw: str, voice: str, phrase: str,
                   source: str = SOURCE_MAIN, model: str = "", kind: str = "", handoff: bool = False,
                   main: str = "") -> None:
    """Монитор видит ответ сразу. В историю он идёт сразу только без отчёта о судьбе; с отчётом
    assistant_reply помечен system и не пишется, историю пишет ledger прозвучавшим.
    source - кто ответил: монитор ставит пометку рядом с подписью SAM."""
    # model - какая модель ответила именно этот ход: у каждого сообщения своя, основная меняется
    # phrase - на что ответ: workflow проверки ответа быстрой (on.event assistant_reply, source fast) видит пару
    # reply_class - класс ответа (reply_class.classify): workflow ловит действие, обещанное без spawn_agent
    reply = {"content": raw, "voice": voice, "session": session_id, "source": source, "phrase": phrase,
             # класс ответа не нужен при передаче: обещанное «сделаю» доводит fast-handoff, а не fast-action
             **({"model": model} if model else {}), **({"reply_class": kind} if kind and not handoff else {}),
             # handoff - быстрая взяла паузу, ответ по существу за основной (workflow fast-handoff, SAM-169)
             # handoff - только если что-то звучит; spoken - звучит ли ответ: workflow быстрой молчание не проверяют
             # (ревью 12 окт: фраза не к SAM с «открой» запускала ход основной в чужой разговор)
             HANDOFF_FIELD: HANDOFF_ON if handoff and voice.strip() else HANDOFF_OFF,
             SPOKEN_FIELD: SPOKEN_ON if voice.strip() else SPOKEN_OFF,
             # main - кто основная: workflow проверки и передачи быстрой работают при модели шлюза (llm), ведущий
             # получает ответ быстрой сам (SAM-173)
             **({MAIN_FIELD: main} if main else {})}
    if deps.ledger is None:
        deps.runner.event_bus.emit("assistant_reply", reply)
        return
    deps.runner.event_bus.emit("assistant_reply", {**reply, "system": True})
    deps.ledger.expect(session_id, voice, phrase=phrase)


def offered_tools(llm_config: dict) -> tuple[str, ...]:
    """Инструменты, показанные модели голосового хода: у быстрой - минимум, у основной - голосовой набор."""
    return FAST_TOOL_NAMES if answer_source(llm_config) == SOURCE_FAST else VOICE_TOOL_NAMES


def voice_tools_schema(lp: AgentLoopDeps, llm_config: dict) -> list:
    """Схема tools голосового пути под поставщика (offered_tools)."""
    src, model = llm_config.get("source", ""), llm_config["model"]
    names = offered_tools(llm_config)
    return lp.provider_transform.sanitize_tools_for_provider(lp.build_tools_schema(list(names)), src, model)


async def _one_call(deps, messages, llm_config, session_id, on_delta, on_reset) -> Any:
    lp = deps.loop
    src, model = llm_config.get("source", ""), llm_config["model"]
    sent = lp.provider_transform.normalize_messages_for_provider(messages, src, model)
    kwargs: dict = dict(
        model=model,
        messages=sent,
        tools=voice_tools_schema(lp, llm_config), tool_choice="auto", max_tokens=deps.max_tokens,
    )
    if llm_config.get("extra_body"):
        kwargs["extra_body"] = llm_config["extra_body"]
    sink = _delta_sink(lp, session_id, on_delta)
    resp = await lp.llm_client.make_llm_call(
        llm_config, on_delta=sink, on_reset=_reset_sink(lp, session_id, on_reset), **kwargs,
    )
    lp.event_bus.emit("assistant_stream_end", {"session": session_id})
    return resp.choices[0].message


def _delta_sink(lp: AgentLoopDeps, session_id: str, on_delta):
    """Дельта - монитору (как в agent_loop) и транспорту голоса."""
    def _sink(piece: str) -> None:
        lp.event_bus.emit("assistant_delta", {"text": piece, "session": session_id})
        if on_delta is not None:
            on_delta(piece)
    return _sink


def _reset_sink(lp: AgentLoopDeps, session_id: str, on_reset):
    def _sink() -> None:
        lp.event_bus.emit("assistant_stream_reset", {"session": session_id})
        if on_reset is not None:
            on_reset()
    return _sink


async def _run_voice_tools(deps, msg, llm_config, session_id) -> list[str]:
    return await run_voice_tools(deps.loop, msg, llm_config, session_id)


async def run_voice_tools(lp: AgentLoopDeps, msg, llm_config: dict, session_id: str) -> list[str]:
    """Вызовы из ответа - нативные или текстом; исполняются только tools голосового пути."""
    calls = list(msg.tool_calls or [])
    if not calls and llm_config.get("text_tools", True):
        calls = [
            SimpleNamespace(id=f"voice_text_{i}", function=SimpleNamespace(
                name=name, arguments=json.dumps(args, ensure_ascii=False)))
            for i, (name, args) in enumerate(lp.parse_channel_format(msg.content or ""))
        ]
    used: list[str] = []
    for tc in calls:
        name = tc.function.name
        if name not in offered_tools(llm_config):   # быстрой показан только quiet: spawn_agent ей не исполняется
            print(f"[VoiceTurn] tool {name} модели не показан, пропущен")
            continue
        await execute_tool(lp, tc, llm_config, session_id)
        used.append(name)
    return used
