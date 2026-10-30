"""Фальшивки голосовых тестов: только внешние границы - поставщик LLM, хранилище истории, сокеты, часы.

Голосовой ход собирается из настоящих модулей шлюза (voice_turn, addressee, phrase_class, spoken_history, recall,
agent_loop, runner, tool_text_parse) и свежей шины EventBus; подменяется только то, что ходит наружу.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from gateway import agent_loop, llm_gate, provider_transform, runner, tool_text_parse
from gateway import voice_turn as vt
from gateway import workflows as wf
from gateway.event_bus import EventBus
from gateway.llm_router import ROUTE_FAST, ROUTE_KEY
from gateway.repos.session_repo import IGNORE
from gateway.spoken_history import EVENT as SPOKEN_EVENT, FATE, SpokenLedger
from gateway.text_utils import strip_thinking, transparent_speech

SESSION = "s"
# Маршруты llm_router: основная модель и быстрая (поле ROUTE_KEY)
MAIN_ROUTE = {"model": "glm", "source": "zai"}
FAST_ROUTE = {"model": "qwen", "source": "lmstudio", ROUTE_KEY: ROUTE_FAST}
# Реестр разбора вызовов из текста: инструменты голосового пути и один чужой ему
TEXT_TOOLS = {"spawn_agent": None, "quiet": None, "mcp_call": None}
# Предохранитель от зависшего теста, не замер: в зелёном прогоне ожидание кончается событием сразу
HANG_GUARD_S = 5.0


def reply(content: str | None = None, tool_calls: list | None = None) -> SimpleNamespace:
    """Ответ поставщика в форме OpenAI (choices[0].message)."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content,
                                                                             tool_calls=tool_calls or None))])


def tool_call(call_id: str, name: str, args: dict) -> SimpleNamespace:
    """Нативный вызов инструмента в ответе поставщика."""
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def text_call(name: str, args: dict) -> str:
    """Вызов инструмента текстом, тегом <tool_call> (формат Qwen)."""
    return f"<tool_call>{json.dumps({'name': name, 'arguments': args}, ensure_ascii=False)}</tool_call>"


class FakeLLM:
    """Поставщик LLM: очередь ответов (строка - ответ текстом). Перед каждым ответом проигрывает дельты потоком;
    reset - поставщик сменился после первой дельты. Запоминает аргументы вызовов и приоритет у ворот LLM."""

    def __init__(self, *responses, stream: tuple[str, ...] | list[str] = (), reset: bool = False) -> None:
        self._responses = [reply(r) if isinstance(r, str) else r for r in responses]
        self._stream, self._reset = list(stream), reset
        self.calls: list[dict] = []
        self.priorities: list[int] = []

    async def make_llm_call(self, llm_config, **kwargs):
        self.calls.append(kwargs)
        self.priorities.append(llm_gate.current_priority())
        for i, piece in enumerate(self._stream):
            kwargs["on_delta"](piece)
            if self._reset and i == 0:
                kwargs["on_reset"]()
        if not self._responses:
            raise RuntimeError("FakeLLM: ответов больше нет")
        return self._responses.pop(0)

    def _effective_adapter(self, llm_config):
        return "native"

    def inc_parse_fail(self):
        pass

    def offered(self, call: int = 0) -> list[str]:
        """Имена инструментов, предложенных модели в вызове."""
        return [t["function"]["name"] for t in self.calls[call]["tools"]]

    def phrase(self, call: int = 0) -> str:
        """Последняя реплика человека в вызове - с подсказками хода."""
        return [m for m in self.calls[call]["messages"] if m["role"] == "user"][-1]["content"]

    def system(self, call: int = 0) -> str:
        return self.calls[call]["messages"][0]["content"]

    def dialogue(self, call: int = 0) -> list[tuple[str, str]]:
        """Контекст вызова без системного сообщения: (роль, текст)."""
        return [(m["role"], m["content"]) for m in self.calls[call]["messages"][1:]]


class BusLog:
    """Свежая шина EventBus и всё, что в неё ушло, - монитор теста."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.events: list[dict] = []
        self.bus.add_listener(lambda name, data: self.events.append({"event": name, **data}))

    def names(self) -> list[str]:
        return [e["event"] for e in self.events]

    def of(self, name: str) -> list[dict]:
        return [e for e in self.events if e["event"] == name]


class History:
    """История сессий вместо Mongo: пишет то же, что EventBus._persist (без system), читает как SessionRepo.get_recent
    (без скрытых записей). Порядок - порядок событий: у Mongo на Windows одинаковые метки времени путают его."""

    _ROLES = {"user_message": "user", "assistant_reply": "assistant", SPOKEN_EVENT: "assistant"}

    def __init__(self, bus: EventBus) -> None:
        self.docs: list[dict] = []
        bus.add_listener(self._on_event)

    def _on_event(self, name: str, data: dict) -> None:
        if name not in self._ROLES or data.get("system") or not data.get("session"):
            return
        doc = {"session_id": data["session"], "role": self._ROLES[name],
               "content": data.get("content") or data.get("voice") or ""}
        doc.update({k: data[k] for k in ("who", IGNORE, FATE) if data.get(k)})
        self.docs.append(doc)

    def recent(self, session: str, limit: int) -> list[dict]:
        visible = [d for d in self.docs if d["session_id"] == session and IGNORE not in d]
        return visible[-limit:]

    def said(self, session: str = SESSION) -> list[tuple[str, str]]:
        """Видимая история сессии: (роль, текст)."""
        return [(d["role"], d["content"]) for d in self.recent(session, len(self.docs))]


@dataclass
class VoiceStand:
    """Голосовой ход на настоящих модулях: deps - VoiceTurnDeps, llm - поставщик, log - шина, history - история."""
    deps: vt.VoiceTurnDeps
    llm: FakeLLM
    log: BusLog
    history: History

    async def turn(self, text: str, who: str | None = "owner", **kw) -> vt.VoiceTurnResult:
        return await vt.run_voice_turn(self.deps, text, SESSION, who=who, **kw)

    def replies(self) -> list[dict]:
        return self.log.of("assistant_reply")


def voice_stand(llm: FakeLLM | None = None, *, route: dict | None = None, rag="", tools: dict | None = None,
                ledger: bool = False, **fields) -> VoiceStand:
    """Голосовой ход как в gateway/agent.py run_voice_turn: речь прозрачная (transparent_speech), кому сказано -
    addressee.decide, разбор вызовов текстом - tool_text_parse. rag - строка памяти или функция поиска (q, limit);
    ledger - голос с отчётом о судьбе (SpokenLedger на той же шине); fields - поля VoiceTurnDeps поверх."""
    log, llm = BusLog(), llm or FakeLLM()
    history = History(log.bus)
    route = dict(route or MAIN_ROUTE)

    async def best_llm():
        return dict(route)

    async def allowed(*args, **kwargs):
        return True

    async def no_text_loop(*args, **kwargs):
        raise AssertionError("голосовой ход не зовёт цикл текстового хода")

    loop = agent_loop.AgentLoopDeps(
        llm_client=llm, event_bus=log.bus, tool_impl=dict(tools or {}),
        build_tools_schema=lambda names: [{"type": "function", "function": {"name": n}} for n in names],
        provider_transform=provider_transform, guard_syntax_check=allowed, guard_tool_check=allowed,
        dangerous_patterns=[], high_risk_tools=set(),
        parse_channel_format=lambda content: tool_text_parse.parse_channel_format(content, TEXT_TOOLS),
        strip_thinking=strip_thinking, worker_last_answer_var=contextvars.ContextVar("last_answer", default=None),
    )
    rd = runner.RunnerDeps(
        event_bus=log.bus, get_best_llm=best_llm, get_session_history=history.recent,
        get_rag_context=rag if callable(rag) else (lambda query, limit: rag),
        guard_check=allowed, agent_loop=no_text_loop, system_prompt="SYSTEM", main_tool_names=[],
        speech_of=transparent_speech, history_speech_only=True, history_speakers=True,
    )
    deps = vt.VoiceTurnDeps(runner=rd, loop=loop, ledger=SpokenLedger(log.bus.emit) if ledger else None, **fields)
    return VoiceStand(deps, llm, log, history)


class Clock:
    """Часы, которые двигает тест: монотонные секунды или datetime."""

    def __init__(self, t=0.0) -> None:
        self.t = t

    def __call__(self):
        return self.t


# ─── сокет /voice/turn со стороны моста (gateway/voice_socket.py) ─────────────────

class FakeVoiceSocket:
    """WebSocket моста к шлюзу: сообщения моста кладёт тест (send), ответы шлюза читает (recv); close - мост ушёл."""

    def __init__(self) -> None:
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._outbox: asyncio.Queue = asyncio.Queue()

    async def receive_text(self) -> str:
        raw = await self._inbox.get()
        if raw is None:
            raise ConnectionError("closed")
        return raw

    async def send_text(self, raw: str) -> None:
        self._outbox.put_nowait(json.loads(raw))

    def send(self, msg) -> None:
        self._inbox.put_nowait(msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False))

    def close(self) -> None:
        self._inbox.put_nowait(None)

    async def recv(self) -> dict:
        return await asyncio.wait_for(self._outbox.get(), HANG_GUARD_S)


# ─── сокет шлюза со стороны моста (voice/layer/bridge.py _serve) ────────────────────

class FakeGatewaySocket:
    """WebSocket шлюза для моста: ответы шлюза отдаёт, когда мост отправил answer_after сообщений (его turn), и
    закрывается, когда мост отправил close_after. Ждёт по событию отправки, без опроса по времени."""

    def __init__(self, replies: list[dict], answer_after: int = 0, close_after: int = 0) -> None:
        self.sent: list[dict] = []
        self._replies, self._answer_after, self._close_after = list(replies), answer_after, close_after
        self._sent_event = asyncio.Event()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))
        self._sent_event.set()

    async def _sent_at_least(self, n: int) -> None:
        while len(self.sent) < n:
            self._sent_event.clear()
            await self._sent_event.wait()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._replies:
            await self._sent_at_least(self._answer_after)
            return json.dumps(self._replies.pop(0), ensure_ascii=False)
        await self._sent_at_least(self._close_after)
        raise StopAsyncIteration


# ─── прежние помощники голосового хода для тестов других групп ──────────────────────
# test_background_visible, test_dialogue_notes, test_rough_edges, test_turn_trace берут их из tests/test_voice_turn.py
# под старыми именами; копии tests/test_agent_loop.py и tests/test_runner.py. Новым тестам - voice_stand.

class RecordingBus:
    """Шина-список: события по порядку, inject-очередь пуста."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, name, payload):
        self.events.append({"event": name, **payload})

    def pop_inject(self, session_id):
        return None

    def names(self):
        return [e["event"] for e in self.events]


class SameProviderTransform:
    """Поставщик без причуд: схемы и сообщения как есть."""

    def sanitize_tools_for_provider(self, schema, src, model):
        return schema

    def normalize_messages_for_provider(self, messages, src, model):
        return messages


class CountingLLM:
    """Очередь ответов (как FakeLLMClient tests/test_agent_loop.py); перед ответом проигрывает дельты."""

    def __init__(self, responses, deltas=()):
        self._responses, self._deltas = list(responses), list(deltas)
        self.calls = []

    async def make_llm_call(self, llm_config, **kwargs):
        for piece in self._deltas:
            kwargs["on_delta"](piece)
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("Нет больше моков ответов")
        return self._responses.pop(0)

    def _effective_adapter(self, llm_config):
        return "native"

    def inc_parse_fail(self):
        pass


def legacy_reply(content=None, tool_calls=None):
    return reply(content, tool_calls)


def legacy_voice_deps(llm, *, tool_impl=None, parse=None, rag="", rag_timeout_s=vt.RAG_TIMEOUT_S):
    """VoiceTurnDeps прежних тестов: шина-список, речь - весь ответ, каждая фраза - ей."""
    async def allowed(*args, **kwargs):
        return True

    async def best_llm():
        return {"source": "test", "model": "tm", "prompt_format": "standard"}

    async def no_text_loop(*args, **kwargs):
        raise RuntimeError("Нет больше моков agent_loop")
    bus = RecordingBus()
    loop = agent_loop.AgentLoopDeps(
        llm_client=llm, event_bus=bus, tool_impl=tool_impl or {},
        build_tools_schema=lambda names: [{"type": "function", "function": {"name": n}} for n in names],
        provider_transform=SameProviderTransform(), guard_syntax_check=allowed, guard_tool_check=allowed,
        dangerous_patterns=[], high_risk_tools=set(), parse_channel_format=parse or (lambda s: []),
        strip_thinking=lambda s: s, worker_last_answer_var=contextvars.ContextVar("last_answer", default=None),
    )
    rd = runner.RunnerDeps(
        event_bus=bus, get_best_llm=best_llm, get_session_history=lambda sid, limit: [],
        get_rag_context=lambda q, limit: rag, guard_check=allowed, agent_loop=no_text_loop,
        system_prompt="ROOT SYS", main_tool_names=["create_task", "remember"],
        speech_of=transparent_speech,
    )
    return vt.VoiceTurnDeps(runner=rd, loop=loop, rag_timeout_s=rag_timeout_s,
                            decide=lambda heard: vt.addressee.Verdict(vt.addressee.ME, "тест"))


# ─── планировщик workflow (копия помощника tests/test_workflows.py) ─────────────────

T0 = datetime(2026, 10, 10, 2, 0, tzinfo=timezone(timedelta(hours=6)))   # Омск, как стенд


def scheduler(flows, run_step):
    """Планировщик workflow на поддельных часах T0 и памяти вместо Mongo."""
    return wf.Scheduler(flows, wf.MemoryStore(), {}, run_step, lambda kind, ev: None, now=Clock(T0),
                        log=lambda message: None)
