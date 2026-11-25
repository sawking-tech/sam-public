"""Фальшивки тестов ядра: подменяют только внешнюю границу (LLM, Qdrant, эмбеддер, Mongo через mongomock, часы).

Хелперы из тестовых файлов других групп скопированы сюда, чтобы тесты ядра не зависели от чужих файлов.
"""

from __future__ import annotations

import contextvars
import hashlib
import inspect
import json
import math
from types import SimpleNamespace

import pytest


# ─── Эмбеддер и Qdrant ───────────────────────────────────────────────────────

class FakeEmbedder:
    """Детерминированный эмбеддер: вектор - сумма байтов sha256 слов, одинаковый в любом процессе (без hash())."""

    def __init__(self, dim: int = 8):
        self.dim = dim

    def get_sentence_embedding_dimension(self):
        return self.dim

    def encode(self, text: str, normalize_embeddings: bool = False):
        vec = [0.0] * self.dim
        for word in (text or "").lower().split():
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            for i in range(self.dim):
                vec[i] += digest[i % len(digest)] / 255.0
        if normalize_embeddings:
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            vec = [x / norm for x in vec]
        return vec


class FakePoint:
    def __init__(self, point_id, vector, payload, score=0.0):
        self.id = point_id
        self.vector = vector
        self.payload = payload
        self.score = score


class FakeCollections:
    def __init__(self, names):
        self.collections = [SimpleNamespace(name=n) for n in names]


class FakeCollectionInfo:
    def __init__(self, dim):
        self.config = SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=dim)))


class FakeQdrant:
    """Qdrant в памяти: коллекции, косинус, фильтр must по match.any / match.value."""

    def __init__(self, existing_collection_dim=None):
        self.collections = {}
        if existing_collection_dim is not None:
            self.collections["context"] = {"dim": existing_collection_dim, "points": {}}

    def get_collections(self):
        return FakeCollections(list(self.collections))

    def get_collection(self, name):
        col = self.collections.get(name)
        if col is None:
            raise RuntimeError("collection not found")
        return FakeCollectionInfo(col["dim"])

    def create_collection(self, collection_name, vectors_config):
        self.collections[collection_name] = {"dim": getattr(vectors_config, "size", 0), "points": {}}

    def delete_collection(self, collection_name):
        self.collections.pop(collection_name, None)

    def upsert(self, collection_name, points):
        col = self.collections.setdefault(collection_name, {"dim": 0, "points": {}})
        for p in points:
            col["points"][p.id] = {"id": p.id, "vector": p.vector, "payload": p.payload}

    def points(self, collection_name="context"):
        return list(self.collections.get(collection_name, {"points": {}})["points"].values())

    @staticmethod
    def _cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a)) or 1.0
        nb = math.sqrt(sum(x * x for x in b)) or 1.0
        return dot / (na * nb)

    @staticmethod
    def _match_filter(payload, query_filter):
        if query_filter is None:
            return True
        for cond in query_filter.must:
            val = payload.get(cond.key) if payload else None
            if hasattr(cond.match, "any"):
                wanted = cond.match.any
                if isinstance(val, list):
                    if not any(t in val for t in wanted):
                        return False
                elif val not in wanted:
                    return False
            elif hasattr(cond.match, "value") and val != cond.match.value:
                return False
        return True

    def query_points(self, collection_name, query, limit=10, score_threshold=None, query_filter=None):
        scored = []
        for p in self.points(collection_name):
            if not self._match_filter(p["payload"], query_filter):
                continue
            score = self._cosine(query, p["vector"])
            if score_threshold is not None and score < score_threshold:
                continue
            scored.append(FakePoint(p["id"], p["vector"], p["payload"], score))
        scored.sort(key=lambda x: x.score, reverse=True)
        return SimpleNamespace(points=scored[:limit])

    def delete(self, collection_name, points_selector):
        col = self.collections.get(collection_name, {"points": {}})
        if hasattr(points_selector, "points"):
            doomed = list(points_selector.points)
        else:
            doomed = [pid for pid, p in col["points"].items() if self._match_filter(p["payload"], points_selector)]
        for pid in doomed:
            col["points"].pop(pid, None)

    def scroll(self, collection_name, limit=100, offset=None, with_payload=True, with_vectors=False):
        pts = self.points(collection_name)
        start = offset or 0
        chunk = pts[start:start + limit]
        next_offset = start + limit if start + limit < len(pts) else None
        return ([FakePoint(p["id"], p["vector"] if with_vectors else None, p["payload"] if with_payload else None)
                 for p in chunk], next_offset)

    def set_payload(self, collection_name, payload, points):
        col = self.collections.get(collection_name, {"points": {}})
        for pid in points:
            if pid in col["points"]:
                col["points"][pid]["payload"].update(payload)


def make_rag_store(db, *, qdrant=None, embedder=None, **kw):
    """Настоящий RAGStore на mongomock, FakeQdrant и FakeEmbedder."""
    from gateway.rag import RAGStore
    from gateway.repos import MemoryRepo
    qdrant = qdrant if qdrant is not None else FakeQdrant()
    embedder = embedder if embedder is not None else FakeEmbedder()
    return RAGStore(memory_repo=MemoryRepo(db), qdrant_client_factory=lambda: qdrant,
                    embedder_factory=lambda: embedder, **kw)


# ─── LLM: ответы поставщика и зависимости agent_loop (копия из tests/test_agent_loop.py) ──────────

class FakeEventBus:
    def __init__(self, inject_queue=None):
        self.events = []
        self.inject_queue = inject_queue or {}

    def emit(self, name, payload):
        self.events.append({"event": name, **payload})

    def pop_inject(self, session_id):
        q = self.inject_queue.get(session_id, [])
        return q.pop(0) if q else None

    def names(self):
        return [e["event"] for e in self.events]

    def by_name(self, name):
        return [e for e in self.events if e["event"] == name]


class FakeProviderTransform:
    """Тождественное преобразование поставщика."""

    def sanitize_tools_for_provider(self, schema, src, model):
        return schema

    def normalize_messages_for_provider(self, messages, src, model):
        return messages

    def sampling_for_provider(self, src, model=""):
        return {}


class FakeLLMClient:
    """Очередь ответов поставщика: каждый make_llm_call берёт следующий."""

    def __init__(self, responses, adapter="native"):
        self._responses = list(responses)
        self._adapter = adapter
        self.calls = []
        self.parse_fails = 0

    async def make_llm_call(self, llm_config, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("Нет больше моков ответов")
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    def _effective_adapter(self, llm_config):
        return self._adapter

    def inc_parse_fail(self):
        self.parse_fails += 1


class CountingLLM(FakeLLMClient):
    """Проигрывает дельты в on_delta перед ответом, как стрим поставщика (копия из tests/test_voice_turn.py)."""

    def __init__(self, responses, deltas=()):
        super().__init__(responses)
        self._deltas = list(deltas)

    async def make_llm_call(self, llm_config, **kwargs):
        for piece in self._deltas:
            kwargs["on_delta"](piece)
        return await super().make_llm_call(llm_config, **kwargs)


def resp(content=None, tool_calls=None):
    """Ответ в форме OpenAI (choices[0].message)."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content,
                                                                           tool_calls=tool_calls or None))])


def tc(call_id, name, args):
    """Вызов инструмента в форме OpenAI."""
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def loop_deps(*, llm_client=None, tool_impl=None, high_risk_tools=None, dangerous_patterns=None,
              parse_channel_format=None, strip_thinking=None, event_bus=None):
    """AgentLoopDeps с настоящим циклом: подменены поставщик и шина, guard пропускает всё."""
    from gateway import agent_loop as al

    async def _guard_ok(*a, **kw):
        return True

    return al.AgentLoopDeps(
        llm_client=llm_client or FakeLLMClient([resp(content="default")]),
        event_bus=event_bus or FakeEventBus(),
        tool_impl=tool_impl or {},
        build_tools_schema=lambda names: [{"type": "function", "function": {"name": n}} for n in names],
        provider_transform=FakeProviderTransform(),
        guard_syntax_check=_guard_ok,
        guard_tool_check=_guard_ok,
        dangerous_patterns=dangerous_patterns or [],
        high_risk_tools=high_risk_tools or set(),
        parse_channel_format=parse_channel_format or (lambda s: []),
        strip_thinking=strip_thinking or (lambda s: s),
        worker_last_answer_var=contextvars.ContextVar("last_answer", default=None),
    )


# ─── Шлюз через TestClient ───────────────────────────────────────────────────

@pytest.fixture
def app_client(monkeypatch, mongo_db, tmp_path):
    """TestClient(main.app) без стенда: startup не греет эмбеддер и не сверяет скиллы с живой памятью,
    история и снимок монитора - на mongomock, приложения - в tmp_path, а не в data/apps стенда.
    Возвращает (client, db)."""
    from fastapi.testclient import TestClient

    from gateway import agent, apps, main
    from gateway.event_bus import event_bus
    from gateway.repos import ArchiveRepo, SessionRepo

    monkeypatch.setenv("SAM_WARM_EMBEDDER", "0")
    sessions = SessionRepo(mongo_db, scrub=agent._scrub_secret)
    archive = ArchiveRepo(mongo_db)
    for mod in (agent, main):
        monkeypatch.setattr(mod, "_db", mongo_db)
        monkeypatch.setattr(mod, "session_repo", sessions)
        monkeypatch.setattr(mod, "archive_repo", archive)
    monkeypatch.setattr(event_bus, "_db", mongo_db)
    monkeypatch.setattr(apps, "APPS_DIR", tmp_path)
    with TestClient(main.app) as client:
        yield client, mongo_db


# ─── Поставщик LLM за OpenAI SDK (граница llm_client) ────────────────────────

class FakeOpenAI:
    """Клиент OpenAI SDK: chat.completions.create отдаёт ответы из очереди (корутина-фабрика или готовый объект)
    и пишет параметры запросов. Подключение: monkeypatch.setattr(llm_client, "_openai_client", fake.client)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def client(self, base_url, api_key):
        return self

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.answers:
            raise RuntimeError("Нет больше ответов поставщика")
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer()
        return await answer if inspect.isawaitable(answer) else answer


def chunk(text):
    """Чанк стрима OpenAI с текстовой дельтой."""
    delta = SimpleNamespace(content=text, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None)


class FakeStream:
    """Стрим OpenAI из готовых чанков."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


# ─── Ход модели шлюза: runner с настоящим agent_loop ─────────────────────────

TURN_TOOLS = ("noop", "spawn_agent", "create_task", "remember")   # инструменты хода, которые отвечают «ок»


class RecordingLLM(FakeLLMClient):
    """Очередь ответов, которая запоминает копию messages каждого вызова: цикл дописывает в тот же список."""

    def __init__(self, responses, adapter="native"):
        super().__init__(responses, adapter)
        self.sent = []

    async def make_llm_call(self, llm_config, **kwargs):
        self.sent.append([dict(m) for m in kwargs.get("messages") or []])
        return await super().make_llm_call(llm_config, **kwargs)


def said(text, tool="noop"):
    """Ответы поставщика на один прогон цикла: вызов инструмента, потом реплика; tool=None - без вызова."""
    voice = resp(content=text)
    return [voice] if tool is None else [resp(tool_calls=[tc(f"c-{tool}", tool, {})]), voice]


def runner_deps(replies=(), *, history=(), rag="", guard_ok=True, prompt_format="standard", **fields):
    """RunnerDeps с настоящим agent_loop и разбором речи из text_utils; поставщик - RecordingLLM (deps.llm)."""
    from gateway import agent_loop, runner, text_utils

    llm = RecordingLLM(list(replies))
    bus = FakeEventBus()

    async def _ok(args):
        return "ок"

    loop = loop_deps(llm_client=llm, tool_impl={name: _ok for name in TURN_TOOLS}, event_bus=bus)

    async def _agent_loop(messages, cfg, names, max_turns=20, session_id="default"):
        return await agent_loop.run_agent_loop(loop, messages, cfg, names, max_turns=max_turns, session_id=session_id)

    async def _best_llm():
        return {"source": "test", "model": "tm", "prompt_format": prompt_format}

    async def _guard(msg, cfg):
        return guard_ok

    deps = runner.RunnerDeps(
        event_bus=bus,
        get_best_llm=_best_llm,
        get_session_history=lambda sid, limit: list(history),
        get_rag_context=lambda q, limit: rag,
        guard_check=_guard,
        agent_loop=_agent_loop,
        system_prompt="ROOT SYS",
        main_tool_names=list(TURN_TOOLS),
        speech_of=text_utils.transparent_speech,
        **fields,
    )
    deps.llm = llm
    return deps


def off_loop_probe(result):
    """Синхронная функция-зонд: пишет, где её вызвали - в потоке ("thread") или в event loop ("loop").
    Вызов в потоке и есть то, что не держит цикл; без времени и снов."""
    import asyncio

    def probe(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            probe.where.append("loop")
        except RuntimeError:
            probe.where.append("thread")
        return result

    probe.where = []
    return probe


# ─── Планировщик workflow на поддельных часах (копия из tests/test_workflows.py) ──────────────────

from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone  # noqa: E402

WORKFLOW_T0 = _datetime(2026, 10, 10, 2, 0, tzinfo=_timezone(_timedelta(hours=6)))   # часовой пояс фиксирован: тесты не зависят от машины


def workflow_scheduler(flows, run_step, now=WORKFLOW_T0):
    """Настоящий Scheduler с хранилищем в памяти, часами на месте и без лога; события - в список."""
    from gateway import workflows as wf
    events = []
    scheduler = wf.Scheduler(flows, wf.MemoryStore(), {}, run_step, lambda kind, ev: events.append((kind, ev)),
                             now=lambda: now, log=lambda m: None)
    return scheduler, events


# ─── Голосовой ход (копия _deps из tests/test_voice_turn.py) ─────────────────

def voice_turn_deps(llm, *, tool_impl=None, rag=""):
    """VoiceTurnDeps с настоящим циклом на поставщике llm; адресат - каждая фраза к SAM (правила адресата не здесь)."""
    from gateway import voice_turn as vt
    loop = loop_deps(llm_client=llm, tool_impl=tool_impl or {})
    runner = runner_deps(rag=rag)
    runner.event_bus = loop.event_bus
    return vt.VoiceTurnDeps(runner=runner, loop=loop,
                            decide=lambda heard: vt.addressee.Verdict(vt.addressee.ME, "тест"))


def patch_module_sleep(monkeypatch, module, sleep):
    """Подменить sleep только внутри модуля: module.time - копия модуля time с этим sleep.

    monkeypatch.setattr(module.time, "sleep", ...) меняет time.sleep на весь процесс, и чужой живой поток (оставленный
    соседним тестом) попадает в подменённый sleep: сбивает счётчик тиков теста или ловит его StopIteration."""
    import time as real_time
    import types
    local = types.SimpleNamespace(**{n: getattr(real_time, n) for n in dir(real_time) if not n.startswith("__")})
    local.sleep = sleep
    monkeypatch.setattr(module, "time", local)
