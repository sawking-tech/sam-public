"""Лёгкие workflow стенда (SAM-96, ADR-020): планировщик на настоящем шаге шлюза, очереди push и MongoStore на
mongomock; подменены часы, основная модель и процессы ворот. Разбор файла и выключение - чистые таблицы."""
import asyncio
import datetime as dt
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import mongomock
import pytest
import yaml

from gateway import agent, heartbeat, workflow_gates as wg, workflows as wf
from gateway.repos.memory_repo import MemoryRepo
from gateway.workflow_spec import ASK, MAIN_MODEL, Schedule, WorkflowError, load_dir, parse_workflow
from tests.fakes_core import app_client  # noqa: F401  (фикстура)
from tests.fakes_agents import T0, TZ, WORKFLOW_FILE, AgentsRig, WallClock, WorkflowRig, _flow, _polled, _resp, _sched

# Соседние группы собирают планировщик через эти имена
__all__ = ["T0", "_flow", "_sched"]

REPO_WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
HALF_HOUR = timedelta(minutes=30)
CTX = wf.GateContext(T0, None)


def _ask(**over):
    return _flow(gate=None, steps=[{"name": "ask", "main_model": "Сделай дело."}], **over)


def _event_flow(steps=None, **event):
    return _flow(gate=None, steps=steps or [{"name": "ask", "main_model": "Ответь на событие."}],
                 on={"event": {"name": "user_message", "where": {"session": "voice"}, **event}})


@pytest.fixture
def rigs(monkeypatch):
    made = []

    def make(flows, **kw):
        made.append(WorkflowRig(monkeypatch, flows, **kw))
        return made[-1]
    yield make
    for rig in made:
        rig.stop()


# ─── Расписание и доставка шага основной модели ───────────────────────────

@pytest.mark.parametrize("in_gateway", [False, True], ids=["outside", "in_gateway"])
async def test_scheduled_workflow_runs_once_per_slot_and_reaches_main_model(rigs, in_gateway):
    rig = rigs([_ask(category="care", tags=["proactive"])], in_gateway=in_gateway)
    assert await rig.scheduler.tick() == []                       # первый тик - только срок
    rig.wall.t += HALF_HOUR
    assert await rig.scheduler.tick() == ["demo"]
    [step] = await rig.ran(1)
    assert await rig.scheduler.tick() == []                       # тот же момент - сдвиг уже записан

    if in_gateway:
        assert rig.turn.turns == [("[workflow:demo/ask] Сделай дело.", "voice")] and step["reply"] == "Ложись."
    else:
        assert [(m["text"], m["source"], m["session"]) for m in rig.inbox] == [
            ("Сделай дело.", "workflow:demo/ask", "voice")] and rig.inbox[0]["push_ids"]
    assert (step["source"], step["target"], step["category"], step["tags"]) == (
        "workflow:demo/ask", "push", "care", ["proactive"])
    [status] = rig.scheduler.status()
    assert status["last_run"] == (T0 + HALF_HOUR).isoformat(timespec="seconds") and status["category"] == "care"
    assert rig.store.get("demo")["next_run"] == T0 + 2 * HALF_HOUR


async def test_quiet_session_skips_the_model_step(rigs):
    rig = rigs([_ask()])
    rig.quiet.on("voice", None, "tool")
    await rig.scheduler.run_once(rig.scheduler.workflows[0], CTX)
    assert rig.inbox == [] and rig.events()[0]["skipped"].startswith("тихий режим")


# ─── Хранилище: аренда и сдвиг ─────────────────────────────────────────────

class _BrokenAdvance(wf.MongoStore):
    def advance_many(self, items):
        raise ConnectionError("mongo down")


class _LeaseElsewhere(wf.MongoStore):
    def acquire_lease(self, *a):
        return False


@pytest.mark.parametrize("store", [_BrokenAdvance, _LeaseElsewhere], ids=["advance_not_written", "lease_elsewhere"])
async def test_scheduler_does_not_run_without_a_written_advance_or_the_lease(rigs, store):
    db = mongomock.MongoClient().db
    db[wf.COLLECTION].insert_one({"_id": "demo", "next_run": T0})
    rig = rigs([_ask()], store=store(db))
    rig.wall.t = T0 + timedelta(seconds=5)
    assert await rig.scheduler.tick() == [] and rig.inbox == []


async def test_lost_lease_reloads_state(rigs):
    class _Flaky(wf.MongoStore):
        held, loads = True, 0

        def acquire_lease(self, *a):
            return self.held

        def load_all(self):
            self.loads += 1
            return super().load_all()
    store = _Flaky(mongomock.MongoClient().db)
    rig = rigs([_ask(name="a"), _ask(name="b")], store=store)
    await rig.scheduler.tick()
    store.held = False
    await rig.scheduler.tick()
    store.held = True
    await rig.scheduler.tick()
    assert store.loads == 2


def test_mongo_lease_is_exclusive_and_state_round_trips():
    db = mongomock.MongoClient().db
    a, b = wf.MongoStore(db), wf.MongoStore(db)
    assert a.acquire_lease("one", T0, 90)
    assert not b.acquire_lease("two", T0 + timedelta(seconds=10), 90)
    assert a.acquire_lease("one", T0 + timedelta(seconds=30), 90)     # свой - продлевается
    assert b.acquire_lease("two", T0 + timedelta(seconds=200), 90)    # истекла - берёт другой
    a.advance("demo", T0 + HALF_HOUR, T0)
    a.set_gate("demo", "h")
    st = b.get("demo")
    assert st["next_run"] == T0 + HALF_HOUR and st["last_run"] == T0 and st["gate"] == "h"


EVERY_30 = Schedule(every_s=1800)


@pytest.mark.parametrize("next_run, now, fire, next_after", [
    (None, T0, False, T0 + HALF_HOUR),                                        # новый ждёт один период
    (T0, T0 + timedelta(seconds=20), True, T0 + timedelta(seconds=20) + HALF_HOUR),
    (T0, T0 + timedelta(minutes=14), True, T0 + timedelta(minutes=44)),       # шлюз лежал 14 мин - догоняет
    (T0, T0 + timedelta(minutes=16), False, T0 + timedelta(minutes=46)),      # больше половины - пропуск до слота
], ids=["new_waits", "due", "catch_up_within_half", "late_skips"])
def test_plan_catches_up_within_half_a_period_only(next_run, now, fire, next_after):
    assert wf.plan(EVERY_30, next_run, now) == wf.Plan(fire, next_after)


@pytest.mark.parametrize("every_s, grace", [(60, wf.GRACE_MIN_S), (86400, wf.GRACE_MAX_S)], ids=["min", "max"])
def test_grace_is_clamped_like_hermes(every_s, grace):
    assert wf.grace_s(Schedule(every_s=every_s), T0) == grace


async def test_agent_step_creates_an_agent_task(rigs, monkeypatch, tmp_path):
    """Шаг-инструмент spawn_agent - настоящим путём шлюза (_workflow_tool): задача в Mongo, воркер до итога."""
    agents = AgentsRig(monkeypatch, tmp_path, {"проверь диск": [_resp(content="Свободно 124 ГБ.")]})
    monkeypatch.setattr(agent, "_current_llm", {"source": "gemini", "model": "gemini-2.0-flash"})
    flow = _flow(gate=None, steps=[{"name": "go", "tool": "spawn_agent", "with": {"description": "проверь диск"}}])
    rig = rigs([flow], tool=agent._workflow_tool)
    try:
        await rig.scheduler.run_once(rig.scheduler.workflows[0], CTX)
        [step] = rig.steps()
        short = step["reply"].split()[1]
        assert step["target"] == "spawn_agent" and step["reply"].endswith("выполняю в фоне.")
        task = await agents.finished(short)
        assert task["description"] == "проверь диск" and task["result"] == "Свободно 124 ГБ."
        await agents.until(lambda: rig.turn.turns)                   # итог - ходом основной модели в шлюзе
        assert f"[TASK_DONE:{short}]" in rig.turn.turns[0][0]
    finally:
        await agents.close()


# ─── Ворота без вызова модели ─────────────────────────────────────────────

async def test_gate_decides_whether_the_model_is_called(rigs):
    out = {"v": ""}
    rig = rigs([_flow()], gates={"some_gate": lambda ctx: out["v"]})
    flow = rig.scheduler.workflows[0]
    assert await rig.scheduler.run_once(flow, CTX) == [] and rig.inbox == []          # пусто - модель не зовётся
    out["v"] = "новое"
    await rig.scheduler.run_once(flow, CTX)
    assert rig.inbox[0]["text"] == f"Сделай дело.\n\n{wf.REASON_HEADER}\nновое"
    assert rig.tools == [("remember", {"fact": "x"}, "voice")]                        # шаг-инструмент без повода
    assert rig.store.get("demo")["gate"] == wf.gate_hash("новое")
    await rig.scheduler.run_once(flow, CTX, last_gate=wf.gate_hash("новое"))          # тот же вывод - не зовётся
    assert len(rig.inbox) == 1


async def test_command_gate_output_is_the_reason(rigs):
    async def command(cmd):
        return "diff"
    rig = rigs([_flow(gate={"command": "check"})], command_gate=command)
    await rig.scheduler.run_once(rig.scheduler.workflows[0], CTX)
    assert rig.inbox[0]["text"].endswith(f"{wf.REASON_HEADER}\ndiff")


async def test_real_command_gate():
    python = f'"{sys.executable}"'
    assert await wf.run_command_gate(f'{python} -c "print(42)"') == "42"
    with pytest.raises(RuntimeError) as e:
        await wf.run_command_gate(f'{python} -c "import sys; sys.exit(3)"')
    assert "кодом 3" in str(e.value) and "\n" not in str(e.value)


def test_unknown_gate_drops_only_that_workflow():
    logs = []
    s = wf.Scheduler([_flow(), _flow(name="other", gate=None)], wf.MemoryStore(), {}, None, lambda *a: None,
                     log=logs.append)
    assert [f.name for f in s.workflows] == ["other"] and "some_gate" in logs[0]


async def test_gates_of_one_tick_share_the_snapshot_and_commands_keep_the_pool(rigs):
    seen, active, peak = [], [0], [0]

    def gate(ctx):
        seen.append(id(ctx.shared))
        return ""

    async def command(cmd):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0)
        active[0] -= 1
        return ""
    flows = ([_flow(name=f"s{i}", gate={"fn": "g"}) for i in range(3)]
             + [_flow(name=f"c{i}", gate={"command": "check"}) for i in range(12)])
    rig = rigs(flows, gates={"g": gate}, command_gate=command, gate_concurrency=3)
    await rig.scheduler.tick()
    rig.wall.t += HALF_HOUR
    assert len(await rig.scheduler.tick()) == 15
    await rig.idle()
    assert len(set(seen)) == 1 and len(seen) == 3 and peak[0] == 3 and rig.inbox == []


# ─── Workflow репозитория: поздний час и память ───────────────────────────

PHRASES = [
    {"ts": "2026-10-10T01:00:00+06:00", "text": "старое", "who": "owner"},
    {"ts": "2026-10-10T01:40:00+06:00", "text": "У Ирины день рождения третьего", "who": "owner"},
    {"ts": "2026-10-10T01:41:00+06:00", "text": "черно", "who": "owner", "partial": True},
    {"ts": "2026-10-10T01:42:00+06:00", "text": "эхо SAM", "echo": True},
    {"ts": "2026-10-10T01:43:00+06:00", "text": "Ага", "who": None},
]


def _repo_flow(name):
    return next(f for f in load_dir(REPO_WORKFLOWS, warn=pytest.fail) if f.name == name)


@pytest.fixture
def stand_inputs(monkeypatch, tmp_path):
    """Входы ворот шлюза (agent._workflow_gates): журнал фраз в tmp_path, память на mongomock, часы голоса."""
    voice_in = tmp_path / "voice-in.jsonl"
    voice_in.write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in PHRASES) + "\n{битая\n", encoding="utf-8")
    monkeypatch.setattr(agent, "_VOICE_IN_FILE", voice_in)
    memory = MemoryRepo(mongomock.MongoClient().db)
    monkeypatch.setattr(agent, "memory_repo", memory)
    now = [1000.0]
    monkeypatch.setattr(agent, "human_activity", heartbeat.HumanActivity(clock=lambda: now[0]))
    return memory, now


async def test_repo_late_night_speaks_with_the_exact_time_only_when_someone_is_awake(rigs, stand_inputs):
    _, now = stand_inputs
    rig = rigs([_repo_flow("late-night")], gates=agent._workflow_gates())
    night = wf.GateContext(datetime(2026, 10, 10, 2, 13, tzinfo=TZ), None)
    await rig.scheduler.run_once(rig.scheduler.workflows[0], night)
    assert rig.inbox == []                                              # никого не слышно - модель не зовётся
    agent.human_activity.note()
    now[0] += 60
    await rig.scheduler.run_once(rig.scheduler.workflows[0], night)
    [nudge] = rig.inbox
    assert nudge["source"] == "workflow:late-night/nudge" and nudge["session"] == "voice"
    assert "ровно 02:13" in nudge["text"] and "1 мин назад" in nudge["text"]


async def test_repo_memory_review_fires_on_new_phrases_once(rigs, stand_inputs):
    memory, _ = stand_inputs
    rig = rigs([_repo_flow("memory-review")], gates=agent._workflow_gates())
    flow, at = rig.scheduler.workflows[0], datetime(2026, 10, 10, 1, 50, tzinfo=TZ)
    await rig.scheduler.run_once(flow, wf.GateContext(at, at - HALF_HOUR))
    [review] = rig.inbox
    assert review["source"] == "workflow:memory-review/review" and "list_memories" in review["text"]
    assert "01:40 owner: У Ирины день рождения третьего" in review["text"] and "01:43 неизвестный: Ага" in review["text"]
    assert "черно" not in review["text"] and "эхо SAM" not in review["text"] and "старое" not in review["text"]
    await rig.scheduler.run_once(flow, wf.GateContext(at + HALF_HOUR, at))        # нового не было
    memory.insert("день рождения Ирины - 3 марта")                             # записала сама - счёт с нуля
    await rig.scheduler.run_once(flow, wf.GateContext(at, at - HALF_HOUR))
    assert len(rig.inbox) == 1
    assert wg.read_memory_written(memory).tzinfo is not None


def test_read_phrases_tail_skips_cut_and_broken_lines(tmp_path):
    path = tmp_path / "voice-in.jsonl"
    path.write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in PHRASES) + "\n{битая\n", encoding="utf-8")
    assert [p["text"] for p in wg.read_phrases(path)] == [p["text"] for p in PHRASES]
    assert len(wg.read_phrases(path, tail_bytes=150)) < len(PHRASES)
    assert wg.read_phrases(tmp_path / "нет.jsonl") == []
    assert wg.heard_since(PHRASES, datetime(2026, 10, 10, 1, 30, tzinfo=TZ), max_chars=10).count("\n") == 0
    assert wg.cursor(T0, None, None, lookback_s=3600) == T0 - timedelta(hours=1)


# ─── Запуск по событию шины ───────────────────────────────────────────────

async def test_event_workflow_runs_once_within_cooldown(rigs):
    rig = await rigs([_event_flow(cooldown="1m")]).started()
    rig.bus.emit("user_message", {"session": "chat"})                   # поле не то
    rig.bus.emit("assistant_delta", {"session": "voice"})               # событие не то
    rig.bus.emit("user_message", {"session": "voice", "content": "привет"})
    await rig.ran(1)
    rig.mono[0] += 30
    rig.bus.emit("user_message", {"session": "voice"})                  # пауза ещё не прошла
    rig.mono[0] += 31
    rig.bus.emit("user_message", {"session": "voice", "content": "снова"})
    await rig.ran(2)
    assert [m["source"] for m in rig.inbox] == ["workflow:demo/ask"] * 2
    first = rig.inbox[0]["text"]
    assert "Событие стенда: user_message" in first and "привет" in first and "msg_id" not in first
    assert "снова" in rig.inbox[1]["text"]
    assert rig.scheduler.status()[0]["last_run"] == T0.isoformat(timespec="seconds")


async def test_event_before_the_scheduler_runs_is_ignored_and_tick_skips_event_only(rigs):
    rig = rigs([_event_flow()])
    assert rig.scheduler.on_event("user_message", {"session": "voice"}) == []      # цикла ещё нет
    assert await rig.scheduler.tick() == [] and rig.inbox == []                    # расписания нет - тик не трогает


async def test_event_finds_subscribers_by_name_only(rigs):
    flows = [_flow(name="hit", gate=None, steps=[{"name": "s", "main_model": "x"}], on={"event": {"name": "voice_heard"}}),
             _flow(name="miss", gate=None, steps=[{"name": "s", "main_model": "y"}], on={"event": {"name": "other"}})]
    rig = await rigs(flows).started()
    assert rig.scheduler.on_event("unknown", {}) == []
    rig.bus.emit("voice_heard", {})
    await rig.ran(1)
    assert [m["source"] for m in rig.inbox] == ["workflow:hit/s"]


def test_bus_listener_failure_does_not_break_emit():
    from gateway.event_bus import EventBus
    bus, seen = EventBus(), []
    bus.add_listener(lambda kind, data: (_ for _ in ()).throw(RuntimeError("сломан")))
    bus.add_listener(lambda kind, data: seen.append((kind, data.get("session"))))
    bus.emit("user_message", {"session": "voice"})
    assert seen == [("user_message", "voice")]


def _held_ask(release):
    async def reply(kwargs):
        await release.wait()
        return _resp(content="OK")
    return reply


@pytest.mark.parametrize("collect", [True, False], ids=["collect", "drop"])
async def test_events_during_a_run_go_as_one_batch_after_it_only_with_collect(rigs, collect):
    release = asyncio.Event()
    flow = _flow(gate=None, on={"event": {"name": "assistant_reply", "where": {"source": "fast"}, "cooldown": "5s",
                                          "collect": collect}},
                 steps=[{"name": "check", "ask": "Проверь: {{reason}}"}])
    rig = await rigs([flow], ask_replies=[_held_ask(release), _resp(content="OK")]).started()
    rig.bus.emit("assistant_reply", {"source": "fast", "voice": "первый"})
    await asyncio.wait_for(_polled(lambda: len(rig.ask_llm.calls) == 1), 5)    # первый запуск ждёт модель
    for voice in ("второй", "третий"):
        rig.bus.emit("assistant_reply", {"source": "fast", "voice": voice})
    rig.bus.emit("assistant_reply", {"source": "main", "voice": "чужой"})
    rig.mono[0] = 10.0                                                          # пауза прошла
    release.set()
    asked = [c["messages"][0]["content"] for c in rig.ask_llm.calls]
    if collect:
        await rig.ran(2)
        asked = [c["messages"][0]["content"] for c in rig.ask_llm.calls]
        assert "второй" in asked[1] and "третий" in asked[1] and "чужой" not in asked[1]
    else:
        await rig.ran(1)
        assert len(rig.ask_llm.calls) == 1
    assert "первый" in asked[0]


# ─── Результат шага следующему ────────────────────────────────────────────

CHECK_AND_FIX = [{"name": "check", "ask": "Верен ли ответ: {{reason}}? Верен - ровно OK."},
                 {"name": "fix", "tool": "say_fix", "with": {"text": "{{check}}", "day": dt.date(2026, 10, 12),
                                                            "nested": {"b": ["{{check}}", 1]}},
                  "if": "check != OK"}]


@pytest.mark.parametrize("reply, model, fixed", [
    ("ой, поправлюсь: сейчас вечер", True, True),      # не OK - fix получает ответ проверки
    ("<think>рассуждаю</think> OK. ", True, False),    # OK без рассуждения и знаков - fix пропущен
    ("", True, False),                                 # пустой ответ - шаг на нём пропущен
    (None, False, False),                              # модели нет - результат пустой
], ids=["wrong_answer", "fine_answer", "empty_answer", "no_model"])
async def test_step_results_feed_the_next_step(rigs, reply, model, fixed):
    rig = rigs([_flow(gate=None, steps=CHECK_AND_FIX)], ask_replies=[_resp(content=reply)], ask_model=model)
    outcomes = await rig.scheduler.run_once(rig.scheduler.workflows[0], CTX, trigger="assistant_reply fast")
    if model:
        [call] = rig.ask_llm.calls
        assert call["messages"] == [{"role": "user", "content":
                                     "Верен ли ответ: Событие стенда: assistant_reply fast? Верен - ровно OK."}]
        assert "tools" not in call                      # разовый вопрос: без инструментов и истории
    assert len(outcomes) == 1 + fixed
    if fixed:
        assert rig.tools == [("say_fix", {"text": reply, "day": dt.date(2026, 10, 12), "nested": {"b": [reply, 1]}},
                              "voice")]
    else:
        assert rig.tools == [] and rig.events()[-1]["skipped"]


@pytest.mark.parametrize("reply, wrong", [("OK", False), ("ok.", False), (" OK! ", False),
                                          ("ой, поправлюсь: не так", True), ("", True)])
def test_result_condition_ignores_case_and_trailing_marks(reply, wrong):
    assert wf.condition_met("check != OK", "", {"check": reply}) is wrong
    assert wf.condition_met("check == OK", "", {"check": reply}) is (not wrong)


FATE_STEPS = [{"name": "say", "main_model": "скажи", "wait_ack": 0.01},
              {"name": "thanks", "main_model": "ок", "if": "acked"},
              {"name": "again", "main_model": "скажи иначе", "if": "not_acked"}]


@pytest.mark.parametrize("in_gateway, turn_reply, ran", [
    (True, "Сказала.", ["say", "thanks"]),                         # модель в шлюзе ответила - acked
    (False, None, ["say", "again"]),                               # вне шлюза подтверждения нет за срок
    (True, ConnectionError("модель упала"), ["say", "again"]),     # сбой доставки - error
], ids=["acked", "not_acked", "error"])
async def test_conditional_steps_follow_the_fate_of_the_previous(rigs, in_gateway, turn_reply, ran):
    rig = rigs([_flow(gate=None, steps=FATE_STEPS)], in_gateway=in_gateway)
    rig.turn.reply = turn_reply
    await rig.scheduler.run_once(rig.scheduler.workflows[0], CTX)
    assert [e["step"] for e in rig.steps()] == ran
    assert any(e.get("skipped") for e in rig.events())


# ─── Файл workflow ────────────────────────────────────────────────────────

FULL = {"name": "full", "on": {"schedule": {"cron": "*/15 2-5 * * *"}}, "gate": {"command": "python check.py"},
        "category": "care", "tags": ["proactive", "speaks", "proactive"], "session": "kitchen",
        "steps": [{"name": "say", "main_model": "скажи", "wait_ack": 60, "importance": 0.5, "delivery": "merge"},
                  {"name": "again", "main_model": "скажи иначе", "if": "not acked"},
                  {"name": "check", "ask": "Верно? {{reason}}"},
                  {"name": "fix", "tool": "say_fix", "with": {"text": "{{check}}"}, "if": "check != OK"}]}


def test_workflow_file_is_valid():
    flow = parse_workflow(FULL, "full.yml")
    assert flow.schedule.cron == "*/15 2-5 * * *" and flow.gate.command == "python check.py"
    assert flow.schedule.next_after(T0) == T0 + timedelta(minutes=15) and flow.schedule.period_s(T0) == 900
    assert (flow.category, flow.tags, flow.session) == ("care", ("proactive", "speaks"), "kitchen")
    say, again, check, fix = flow.steps
    assert (say.kind, say.wait_ack_s, say.importance, say.delivery) == (MAIN_MODEL, 60, 0.5, "merge")
    assert (again.condition, check.kind, fix.condition, fix.args) == ("not_acked", ASK, "check != OK",
                                                                     {"text": "{{check}}"})
    demo = _flow()                                        # on без кавычек, every, ворота-функция, сессия по умолчанию
    assert demo.schedule == Schedule(every_s=1800.0) and demo.gate.fn == "some_gate" and demo.session == "voice"
    assert demo.steps[0].importance == 1 and demo.tags == () and _flow(category=None).category == ""
    event = _event_flow(cooldown="2m", collect=True)
    assert event.schedule is None and event.event.cooldown_s == 120.0 and event.event.collect
    assert event.event.matches("user_message", {"session": "voice", "content": "привет"})
    assert not event.event.matches("user_message", {"session": "chat"})
    assert _flow(enabled=False) is None


@pytest.mark.parametrize("over, words", [
    ({"on": {"schedule": {"every": "30m", "cron": "* * * * *"}}}, "ровно одно из cron и every"),
    ({"on": {"schedule": {"every": "полчаса"}}}, "every="),
    ({"on": {"schedule": {"cron": "каждый час"}}}, "cron="),
    ({"on": {"push": {}}}, "нет on.schedule, on.event и on.consult"),
    ({"on": {"event": "workflow"}}, "зациклится"),
    ({"on": {"event": "workflow_push"}}, "зациклится"),
    ({"on": {"event": {"name": "user_message", "cooldown": "4s"}}}, "не меньше"),
    ({"steps": []}, "steps"),
    ({"steps": [{"main_model": "a", "tool": "b"}]}, "steps[0]"),
    ({"steps": [{"name": "a", "ask": "q", "tool": "t"}]}, "ровно одно"),
    ({"name": "Мой workflow"}, "name="),
    ({"tags": ["Ночь"]}, "метка"),
    ({"steps": [{"name": "a", "main_model": "x"}, {"name": "a", "main_model": "y"}]}, "повторяются"),
    ({"steps": [{"name": "say", "main_model": "x", "if": "acked"}]}, "первого шага"),
    ({"steps": [{"name": "a", "main_model": "x"}, {"name": "b", "main_model": "y", "if": "maybe"}]}, "if="),
    ({"steps": [{"name": "t", "tool": "remember", "wait_ack": 5}]}, "wait_ack"),
    ({"steps": [{"name": "a", "tool": "t", "with": {"x": "{{later}}"}}, {"name": "later", "ask": "q"}]}, "later"),
    ({"steps": [{"name": "a", "ask": "q"}, {"name": "b", "tool": "t", "if": "ghost == OK"}]}, "ghost"),
    ({"steps": [{"name": "s", "main_model": "hi", "importance": 2}]}, "importance"),
    ({"steps": [{"name": "s", "main_model": "hi", "importance": "липко"}]}, "importance"),
    ({"steps": [{"name": "s", "main_model": "hi", "delivery": "сразу"}]}, "delivery"),
])
def test_bad_workflow_file_is_one_line_error(over, words):
    with pytest.raises(WorkflowError) as e:
        _flow(**({"gate": None} if "steps" in over else {}), **over)
    assert words in str(e.value) and "\n" not in str(e.value) and "demo.yml" in str(e.value)


def test_load_dir_skips_broken_and_duplicate_files_and_repo_workflows_load(tmp_path):
    (tmp_path / "a.yml").write_text(WORKFLOW_FILE, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(WORKFLOW_FILE, encoding="utf-8")            # то же имя demo
    (tmp_path / "c.yml").write_text("name: c\nsteps: []", encoding="utf-8")
    warns = []
    assert [f.name for f in load_dir(tmp_path, warn=warns.append)] == ["demo"]
    assert len(warns) == 2 and all("\n" not in w for w in warns)
    names = {f.name: f for f in load_dir(REPO_WORKFLOWS, warn=pytest.fail)}
    assert names["memory-review"].schedule.every_s == 1800 and names["memory-review"].gate.fn == "received_since_memory"
    assert names["late-night"].gate.fn == "late_night"


# ─── Выключение ───────────────────────────────────────────────────────────

CARE_NIGHT = {"category": "care", "tags": ["night"]}


@pytest.mark.parametrize("labels, off, is_off", [
    (CARE_NIGHT, {"demo"}, True), (CARE_NIGHT, {"t:night"}, True), (CARE_NIGHT, {"tag:night"}, True),
    (CARE_NIGHT, {"c:care"}, True), (CARE_NIGHT, {"category:care"}, True),
    (CARE_NIGHT, {"t:disk", "c:memory", "de", "c:"}, False), (CARE_NIGHT, set(), False),
    ({"category": None, "tags": ["night"]}, {"c:", "category:care"}, False),    # без категории - только имя и метки
    ({"category": "memory", "tags": ["memory"]}, {"t:memory"}, True),           # метка общая для категорий
    ({"category": "maintenance", "tags": ["memory", "skills"]}, {"t:memory"}, True),
    ({"category": "memory", "tags": ["memory"]}, {"c:maintenance"}, False),
])
def test_workflow_is_turned_off_by_name_tag_or_category(labels, off, is_off):
    assert wf.is_off(_flow(**labels), off) is is_off


@pytest.mark.parametrize("env, off, warned", [
    ({"SAM_WORKFLOWS_OFF": "memory-review, x"}, {"memory-review", "x"}, False),
    ({"SAM_HEARTBEAT": "0"}, {"late-night"}, True),                     # .env стенда 10 окт ночью
    ({"SAM_HEARTBEAT": "1"}, set(), False),
])
def test_workflows_off_by_env_and_legacy_heartbeat_switch(env, off, warned):
    warns = []
    assert wf.disabled(env, warns.append) == off
    assert bool(warns) is warned and all(wf.OFF_ENV in w for w in warns)


# ─── Тысячи workflow (ADR-020, п. 7, 9, 10) ───────────────────────────────

class _CountingStore(wf.MongoStore):
    """Считает обращения: состояние читается один раз, сдвиги пишутся пачкой."""

    def __init__(self, db):
        super().__init__(db)
        self.gets, self.loads, self.batches = 0, 0, []

    def get(self, name):
        self.gets += 1
        return super().get(name)

    def load_all(self):
        self.loads += 1
        return super().load_all()

    def advance_many(self, items):
        self.batches.append(len(items))
        super().advance_many(items)


def _many(n, schedule=None):
    return [_flow(name=f"wf{i}", gate=None, steps=[{"name": "s", "main_model": "x", "importance": 0}],
                  on={"schedule": schedule or {"every": "30m"}}) for i in range(n)]


async def test_many_workflows_read_once_write_in_batches(rigs):
    store = _CountingStore(mongomock.MongoClient().db)
    rig = rigs(_many(50), store=store, hold_s=30)
    assert await rig.scheduler.tick() == []                     # первая загрузка: первые сроки одной пачкой
    assert store.loads == 1 and store.batches == [50]
    for _ in range(3):                                          # тики до срока: ни чтений, ни записей
        rig.wall.t += timedelta(minutes=5)
        assert await rig.scheduler.tick() == []
    rig.wall.t = T0 + HALF_HOUR
    assert len(await rig.scheduler.tick()) == 50
    await rig.idle()
    assert store.loads == 1 and store.gets == 0 and store.batches[-1] == 50
    assert rig.scheduler.tick_status()["fired"] == 50 and rig.scheduler.tick_status()["queued"] == 50


@pytest.mark.parametrize("schedule, distinct", [({"cron": "*/5 * * * *"}, 5), ({"every": "1m"}, 15)],
                         ids=["cron_inside_the_minute", "interval_phases"])
async def test_many_workflows_are_spread_by_jitter(rigs, schedule, distinct):
    rig = rigs(_many(30, schedule), jitter_s=50.0)
    await rig.scheduler.tick()
    assert len({st["next_run"] for st in rig.scheduler.status()}) > distinct


def test_jitter_is_stable_within_the_range_and_keeps_the_period():
    offsets = {wf.jitter_for(f"wf{i}", 50.0) for i in range(100)}
    assert all(0 <= o < 50 for o in offsets) and len(offsets) > 50
    assert wf.jitter_for("disk-watch", 50.0) == wf.jitter_for("disk-watch", 50.0) and wf.jitter_for("x", 0) == 0
    cron = wf.Jittered(Schedule(cron="*/5 * * * *"), 17.0)
    first = cron.next_after(T0)
    assert first == T0 + timedelta(seconds=17) and cron.next_after(first) == first + timedelta(minutes=5)
    every = wf.Jittered(Schedule(every_s=60.0), 7.0)
    assert every.first_after(T0) == T0 + timedelta(seconds=67) and every.next_after(T0) == T0 + timedelta(seconds=60)


async def test_fast_action_hands_the_unbacked_promise_to_the_main_model(rigs):
    """fast-voice 19, workflows 94: настоящий fast-action.yml - ответ быстрой «включила» без spawn_agent доходит
    до основной модели своим ходом с фразой человека и обещанием; ответ другого класса workflow не запускает."""
    rig = await rigs([_repo_flow("fast-action")], in_gateway=True).started()
    reply = {"source": "fast", "main": "llm", "spoken": "1", "handoff": "0", "session": "voice",
             "phrase": "Сэм, включи свет", "voice": "Включила свет в зале."}
    rig.bus.emit("assistant_reply", {**reply, "reply_class": "chatter"})
    rig.bus.emit("assistant_reply", {**reply, "reply_class": "action_unbacked"})
    await rig.ran(1)

    [(text, session)] = rig.turn.turns
    assert session == "voice" and text.startswith("[workflow:fast-action/act]")
    assert "spawn_agent" in text and "Сэм, включи свет" in text and "Включила свет в зале." in text
    assert "action_unbacked" in text and "chatter" not in text                   # повод - обещание, не болтовня


# ─── фаза 4: повтор поверх идущего, ворота события, ход llm вне истории ──────

SOON_S = 5   # страховка ожидания запуска workflow в тесте


async def _soon(condition):
    await asyncio.wait_for(_polled(condition), SOON_S)

async def test_scheduled_run_does_not_start_over_a_running_one():
    """workflows 74: пока workflow идёт, следующий запуск по расписанию не начинается; срок ждёт конца и тогда
    идёт один раз - не две реплики подряд."""
    release, calls = asyncio.Event(), []

    async def step(st, src, text, session):
        calls.append(src)
        if len(calls) == 1:
            await release.wait()
        return wf.StepOutcome("fake", "ok")
    clock = WallClock()
    s, _, _ = _sched([_ask()], run_step=step, clock=clock)
    assert await s.tick() == []
    clock.t += HALF_HOUR
    assert await s.tick() == ["demo"]
    await asyncio.sleep(0)
    clock.t += HALF_HOUR
    assert await s.tick() == [] and len(calls) == 1          # идущий не запускается второй раз
    release.set()
    await _soon(lambda: not s.status()[0]["running"])
    assert await s.tick() == ["demo"]
    await _soon(lambda: len(calls) == 2)


async def test_event_gate_is_checked_after_the_event_and_schedule_still_runs():
    """workflows 19: у workflow и событие, и расписание; ворота запуска по событию проверяются после события: пустые -
    модель не зовётся, непустые - повод «Событие стенда» и вывод ворот; плановый запуск идёт своим сроком."""
    gate_out, checked = {"v": ""}, []

    def gate(ctx):
        checked.append(ctx.now)
        return gate_out["v"]
    flow = _flow(on={"schedule": {"every": "30m"}, "event": {"name": "user_message", "where": {"session": "voice"}}},
                 steps=[{"name": "ask", "main_model": "Ответь."}])
    clock, mono = WallClock(), [0.0]
    s, calls, _ = _sched([flow], gates={"some_gate": gate}, clock=clock)
    s._clock = lambda: mono[0]
    s._loop = asyncio.get_running_loop()
    assert await s.tick() == []

    clock.t += timedelta(minutes=5)
    assert s.on_event("user_message", {"session": "voice", "content": "привет"}) == ["demo"]
    await _soon(lambda: checked and not s.status()[0]["running"])
    assert calls == [] and checked == [clock.t]               # ворота - в момент события, пустые: модель молчит
    gate_out["v"] = "дверь открыта"
    mono[0] += 120
    s.on_event("user_message", {"session": "voice", "content": "кто пришёл"})
    await _soon(lambda: len(calls) == 1)
    assert "Событие стенда: user_message" in calls[0][1] and "кто пришёл" in calls[0][1]
    assert calls[0][1].index("кто пришёл") < calls[0][1].index("дверь открыта")

    gate_out["v"] = "по расписанию"
    clock.t = T0 + HALF_HOUR
    assert await s.tick() == ["demo"]
    await _soon(lambda: len(calls) == 2)
    assert "по расписанию" in calls[1][1] and "Событие стенда" not in calls[1][1]


@pytest.mark.parametrize("session, voice", [("voice", True), ("workpc", False)], ids=["voice", "chat"])
async def test_llm_step_input_stays_out_of_history_and_only_speech_is_spoken(monkeypatch, session, voice):
    """workflows 83, 50: шаг основной llm - ход шлюза системным входом (вход в историю не пишется); в голосовой
    сессии ответ в историю сразу не идёт - только прозвучав (say в паузе), строка «ОТРАБОТАНО:» вслух не звучит.
    Подменены ход модели (её ответ) и голосовой слой."""
    from gateway import voice_socket
    turns, said = [], []

    class _Main:
        async def run(self, text, **kw):
            turns.append((text, kw))
            return "Свет выключила.\nОТРАБОТАНО: 1, 2"
    monkeypatch.setattr(agent, "sam_agent", _Main())
    monkeypatch.setattr(agent, "say_own", lambda s, text, ttl_s=None: said.append((s, text)) or True)
    monkeypatch.setattr(voice_socket, "_voice_sessions", {"voice"})

    reply = await agent._workflow_turn("[workflow:late-night/say] Третий час.", session)

    [(text, kw)] = turns
    assert text == "[workflow:late-night/say] Третий час." and kw["_system_call"] is True
    assert kw["persist_reply"] is (not voice) and kw["device_id"] == session
    assert said == ([(session, "Свет выключила.")] if voice else [])
    assert reply.endswith("ОТРАБОТАНО: 1, 2")                # строка подтверждения - разбору очереди, не голосу


async def test_system_turn_writes_neither_its_input_nor_an_unspoken_reply(mongo_db):
    """workflows 83: ход шлюза с _system_call и persist_reply=False не пишет в историю ни вход шага, ни ответ -
    в историю ответ попадёт только по отчёту моста о прозвучавшем. Настоящие run_turn, EventBus и mongomock."""
    import dataclasses

    from gateway import runner
    from gateway.event_bus import EventBus
    from gateway.repos import SessionRepo
    from tests.fakes_core import runner_deps, said
    bus = EventBus()
    bus.set_db(mongo_db)
    deps = dataclasses.replace(runner_deps([*said("Свет выключила.", tool=None), *said("Выключила.", tool=None)]),
                               event_bus=bus)

    await runner.run_turn(deps, "[workflow:late-night/say] Третий час.", device_id="voice", _system_call=True,
                          persist_reply=False)
    assert SessionRepo(mongo_db).get_recent("voice", 10) == []
    await runner.run_turn(deps, "Сэм, выключи свет", device_id="voice")
    # метки времени двух записей на Windows могут совпасть: порядок не проверяется, только что записано
    assert sorted(m["role"] for m in SessionRepo(mongo_db).get_recent("voice", 10)) == ["assistant", "user"]


def test_fast_voice_corrections_speak_as_one_and_never_name_models():
    """fast-voice 27: поправка ответа быстрой звучит от первого лица одним голосом - проверка основной просит
    поправку без упоминания моделей и проверки; доводка обещанного и ответ по существу обращаются к модели как к той,
    кто только что сказал («Ты только что»)."""
    [check] = [s for s in _repo_flow("fast-review").steps if s.name == "check"]
    assert "одной поправкой от первого лица" in check.text and "без\nупоминания моделей и проверки" in check.text
    for name in ("fast-action", "fast-handoff"):
        [step] = _repo_flow(name).steps
        assert step.text.startswith("Ты только что"), name


# ─── фаза 5: P3 ───────────────────────────────────────────────────────────────

async def test_collected_reasons_keep_only_the_latest(rigs, monkeypatch):
    """workflows 15: накопленных поводов не больше SAM_WORKFLOW_COLLECT_MAX - пачка несёт последние."""
    monkeypatch.setattr(wf, "COLLECT_MAX", 2)
    release = asyncio.Event()
    flow = _flow(gate=None, on={"event": {"name": "assistant_reply", "where": {"source": "fast"}, "cooldown": "5s",
                                          "collect": True}},
                 steps=[{"name": "check", "ask": "Проверь: {{reason}}"}])
    rig = await rigs([flow], ask_replies=[_held_ask(release), _resp(content="OK")]).started()
    rig.bus.emit("assistant_reply", {"source": "fast", "voice": "первый"})
    await asyncio.wait_for(_polled(lambda: len(rig.ask_llm.calls) == 1), 5)
    for voice in ("второй", "третий", "четвёртый"):
        rig.bus.emit("assistant_reply", {"source": "fast", "voice": voice})
    rig.mono[0] = 10.0
    release.set()
    await rig.ran(2)
    batch = rig.ask_llm.calls[1]["messages"][0]["content"]
    assert "второй" not in batch and "третий" in batch and "четвёртый" in batch


async def test_count_is_zero_for_a_scheduled_run(rigs):
    """workflows 61: {{count}} - 0 у запуска по расписанию (1 - одиночное событие, больше - пачка)."""
    rig = rigs([_flow(gate=None, steps=[{"name": "check", "ask": "Поводов: {{count}}"}])],
               ask_replies=[_resp(content="OK")])
    await rig.scheduler.run_once(rig.scheduler.workflows[0], CTX)
    assert rig.ask_llm.calls[0]["messages"][0]["content"] == "Поводов: 0"


@pytest.mark.parametrize("name", ["reason", "count"])
def test_reason_and_count_are_not_step_names(name):
    """workflows 62: имена шагов reason и count заняты подстановкой - файл с ними отклоняется одной строкой."""
    with pytest.raises(WorkflowError) as e:
        _flow(gate=None, steps=[{"name": name, "ask": "x"}, {"name": "tell", "main_model": "{{" + name + "}}"}])
    assert name in str(e.value) and "\n" not in str(e.value)


async def test_ask_step_leaves_no_trace_in_the_conversation(rigs):
    """workflows 63: шаг ask - разовый вопрос: без записи в беседу, без хода основной модели и очереди push."""
    rig = rigs([_flow(gate=None, steps=[{"name": "check", "ask": "Верно ли?"}])], ask_replies=[_resp(content="OK")])
    await rig.scheduler.run_once(rig.scheduler.workflows[0], CTX)
    assert rig.inbox == [] and rig.turn.turns == [] and rig.bus.of("workflow_push") == []
    assert [e["target"] for e in rig.steps()] == ["ask"]
    assert rig.bus.of("user_message") == [] and rig.bus.of("assistant_reply") == []


GATE_TIMEOUT_S = 1.0


async def test_gate_command_that_hangs_is_a_failure_shown_to_the_monitor(rigs):
    """workflows 77, 86: ворота-команда дольше срока - сбой ворот; монитор получает событие workflow с ошибкой одной
    строкой, модель не зовётся."""
    import functools
    hang = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    rig = rigs([_flow(gate={"command": hang})],
               command_gate=functools.partial(wf.run_command_gate, timeout_s=GATE_TIMEOUT_S))
    await rig.scheduler.tick()
    rig.wall.t += HALF_HOUR
    assert await rig.scheduler.tick() == ["demo"]
    await rig.idle()
    [failed] = [e for e in rig.events() if "error" in e]
    assert failed["workflow"] == "demo" and "не ответили за 1 с" in failed["error"] and "\n" not in failed["error"]
    assert rig.inbox == [] and rig.turn.turns == []


async def test_step_reaches_claude_code_as_a_line_of_lead_in_and_noop_gets_nothing(tmp_path, monkeypatch):
    """workflows 82, 84: основная claude-code получает шаг строкой во входящих lead-in.jsonl с источником; основная
    noop шаг не получает и ход не идёт."""
    from gateway import main_model
    inbox = tmp_path / "lead-in.jsonl"
    monkeypatch.setattr(agent, "_LEAD_INBOX_FILE", inbox)
    turns = []

    async def turn(text, session):
        turns.append(text)
        return "ответ"
    lead = main_model.claude_code(lambda: True, agent._deliver_to_lead)
    await wf.deliver_main_model(lead, "workflow:demo/ask", "Сделай дело.", "voice", turn)
    [line] = [json.loads(x) for x in inbox.read_text(encoding="utf-8").splitlines()]
    assert (line["text"], line["source"], line["session"]) == ("Сделай дело.", "workflow:demo/ask", "voice")
    outcome = await wf.deliver_main_model(main_model.noop(), "workflow:demo/ask", "Сделай дело.", "voice", turn)
    assert outcome.target == main_model.NOOP and turns == []
    assert len(inbox.read_text(encoding="utf-8").splitlines()) == 1


def test_skills_sync_runs_without_the_model():
    """workflows 91: skills-sync.yml сверяет скиллы с памятью инструментом sync_skills, модель не зовётся."""
    flow = _repo_flow("skills-sync")
    assert [(s.kind, s.tool) for s in flow.steps] == [("tool", "sync_skills")] and flow.gate is None


async def test_empty_tick_writes_only_the_lease(rigs):
    """workflows 66: тик, когда ничему не пора, - одна запись аренды, без сдвигов и чтений состояния."""
    class _Leases(_CountingStore):
        leases = 0

        def acquire_lease(self, *a):
            self.leases += 1
            return super().acquire_lease(*a)
    store = _Leases(mongomock.MongoClient().db)
    rig = rigs(_many(3), store=store)
    await rig.scheduler.tick()
    before = (store.leases, list(store.batches), store.loads, store.gets)
    for _ in range(3):
        rig.wall.t += timedelta(minutes=1)
        assert await rig.scheduler.tick() == []
    assert (store.leases, store.batches, store.loads, store.gets) == (before[0] + 3, *before[1:])


async def test_gateway_without_workflows_or_mongo_starts_no_scheduler(monkeypatch, capsys):
    """workflows 10, 73: SAM_WORKFLOWS=0 - планировщик не стартует; без Mongo - не стартует, строка в лог."""
    from gateway import main
    built = []
    monkeypatch.setattr(main, "_workflow_scheduler", None)
    monkeypatch.setattr(agent, "build_workflows", lambda: built.append("built") or None)
    monkeypatch.setattr(wf, "ENABLED", False)
    await main._start_workflows()
    assert built == [] and main._workflow_scheduler is None
    monkeypatch.setattr(wf, "ENABLED", True)
    await main._start_workflows()
    assert built == ["built"] and main._workflow_scheduler is None and "нет MongoDB" in capsys.readouterr().out


def _health(client, monkeypatch):
    from gateway import main

    async def _llm(*a, **kw):
        return {"source": "test", "model": "m"}
    monkeypatch.setattr(main, "get_best_llm", _llm)
    monkeypatch.setattr(main, "_health_probes", lambda: {})
    return client.get("/health").json()


def test_health_shows_workflows_with_labels_and_the_last_tick(rigs, app_client, monkeypatch):  # noqa: F811
    """workflows 8, 75: /health -> workflows (next_run, last_run, running, категория и метки) и workflow_tick."""
    from gateway import main
    client, _ = app_client
    rig = rigs([_ask(category="care", tags=["proactive"])])
    asyncio.run(rig.scheduler.tick())
    monkeypatch.setattr(main, "_workflow_scheduler", rig.scheduler)
    body = _health(client, monkeypatch)
    [demo] = body["workflows"]
    assert demo["name"] == "demo" and demo["next_run"] and not demo["running"]
    assert (demo["category"], demo["tags"]) == ("care", ["proactive"])
    assert set(body["workflow_tick"]) >= {"fired", "queued"}


def test_decision_point_file_broken_is_one_log_line_and_points_show_in_health(tmp_path, monkeypatch, capsys,
                                                                            app_client):  # noqa: F811
    """workflows 25: пропавший или битый файл точки решения - строка в лог на старте; точки видны в /health
    (workflow_decisions)."""
    from gateway import fast_voice_gate
    client, _ = app_client
    (tmp_path / "fast-voice.yml").write_text("{битый", encoding="utf-8")
    monkeypatch.setenv(wf.DIR_ENV, str(tmp_path))
    monkeypatch.setattr(wf, "ENABLED", True)
    agent._decisions.cache_clear()
    try:
        assert agent._decisions().status() == {}
        assert f"точки {fast_voice_gate.POINT} нет" in capsys.readouterr().out
        monkeypatch.setenv(wf.DIR_ENV, str(REPO_WORKFLOWS))
        agent._decisions.cache_clear()
        assert fast_voice_gate.POINT in _health(client, monkeypatch)["workflow_decisions"]
    finally:
        agent._decisions.cache_clear()


CHILD_GONE_S = 2   # убитая команда ворот исчезает за доли секунды


async def test_gate_command_past_its_deadline_does_not_outlive_the_gate(tmp_path):
    """workflows 77: ворота дольше срока - сбой, и команда ворот не остаётся работать после него."""
    psutil = pytest.importorskip("psutil")
    pid_file = tmp_path / "pid"
    script = f"import os, time; open(r'{pid_file}', 'w').write(str(os.getpid())); time.sleep(30)"
    with pytest.raises(RuntimeError, match="не ответили"):
        await wf.run_command_gate(f'"{sys.executable}" -c "{script}"', timeout_s=GATE_TIMEOUT_S)
    pid = int(pid_file.read_text())
    try:
        gone = psutil.wait_procs([psutil.Process(pid)], timeout=CHILD_GONE_S)[0] if psutil.pid_exists(pid) else [pid]
        assert gone
    finally:
        if psutil.pid_exists(pid):
            psutil.Process(pid).kill()
