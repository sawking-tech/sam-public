"""Отвечает ли быстрая модель (ADR-020, п. 11; SAM-168): workflow fast-voice решает воротами холодного старта и
отката по поправкам основной, код только спрашивает. Решатель - настоящий agent._voice_task и workflows/fast-voice.yml;
счётчик холодного старта и откат на поддельных часах - свежие на каждый тест."""
from pathlib import Path

import mongomock
import pytest

from gateway import fast_voice_gate as fvg
from gateway import workflows as wf
from gateway.fast_backoff import FastBackoff, duration
from gateway.workflow_spec import WorkflowError, parse_workflow
from tests.fakes_voice import Clock

ROOT = Path(__file__).resolve().parents[1]
CONSULTED = {"name": "fast-voice", "on": {"consult": "fast_voice"}, "gate": "fast_voice_ready"}
WINDOW_S = 300


def _length(n):
    return duration(n, base_s=60, factor=2, max_s=1800)


@pytest.fixture
def decider(monkeypatch):
    """Решатель шлюза с настоящим fast-voice.yml: свежий холодный старт, откат на часах теста; enabled - включены ли
    workflow (conftest гасит их, а решают ворота файла)."""
    from gateway import agent
    clock = Clock()
    monkeypatch.setattr(agent, "_cold_start", fvg.ColdStart())
    monkeypatch.setattr(agent, "fast_backoff", FastBackoff(window_s=WINDOW_S, clock=clock, length=_length))
    monkeypatch.setenv(wf.DIR_ENV, str(ROOT / "workflows"))   # файлы репозитория, не окружение разработчика
    monkeypatch.delenv(wf.OFF_ENV, raising=False)

    def make(enabled=True):
        monkeypatch.setattr(wf, "ENABLED", enabled)
        agent._decisions.cache_clear()
        return agent
    yield make, clock
    agent._decisions.cache_clear()


@pytest.mark.parametrize("warm_turns, corrected, enabled, task", [
    (0, False, True, "turn"),
    (1, False, True, "turn"),
    (2, False, True, "fast"),          # с третьего хода быстрая уже знает разговор
    (5, True, True, "turn"),           # откат после поправки основной
    (0, False, False, "fast"),         # workflow выключены - ворота ничего не решают
])
def test_first_two_voice_turns_main_then_fast(decider, warm_turns, corrected, enabled, task):
    make, _ = decider
    agent = make(enabled)
    for _ in range(warm_turns):
        agent._voice_task("voice")
    if corrected:
        agent.fast_backoff.note("voice")
    assert agent._voice_task("voice") == task


def test_correction_puts_session_into_backoff_main_answers_until_it_ends(decider, monkeypatch):
    """Поправка основной (amend_reply DROP с настоящей чисткой истории) ставит откат только этой сессии."""
    from gateway.repos.session_repo import SessionRepo
    from gateway.spoken_history import SpokenLedger
    make, clock = decider
    agent = make()
    repo = SessionRepo(mongomock.MongoClient().db)
    repo.col.insert_one({"session_id": "voice", "role": "assistant", "content": "бред", "msg_id": "a"})
    monkeypatch.setattr(agent, "session_repo", repo)
    monkeypatch.setattr(agent, "_spoken_ledger", SpokenLedger(lambda *a: None))
    for session in ("voice", "chat"):
        for _ in range(fvg.COLD_TURNS):
            agent._voice_task(session)
    assert agent._voice_task("voice") == "fast"
    assert agent._impl_amend_reply({"text": "DROP", "reply": "бред"}).startswith("OK")
    assert agent._voice_task("voice") == "turn" and agent._voice_task("chat") == "fast"
    clock.t += _length(1) + 1
    assert agent._voice_task("voice") == "fast"


@pytest.mark.parametrize("marks, check_at, session, remaining", [
    ([0], 0, "voice", 60),
    ([0, 10, 20], 20, "voice", 240),         # три поправки держат дольше одной
    ([0] * 10, 0, "voice", 1800),            # потолок
    ([0], 61, "voice", 0),                   # без новых поправок откат кончается
    ([0, 400], 400, "voice", 60),            # старая поправка вышла из окна
    ([0], 0, "chat", 0),                     # сессии раздельны
])
def test_backoff_length_grows_with_corrections_and_decays(marks, check_at, session, remaining):
    clock = Clock()
    backoff = FastBackoff(window_s=WINDOW_S, clock=clock, length=_length)
    for t in marks:
        clock.t = t
        backoff.note("voice")
    clock.t = check_at
    assert backoff.remaining(session) == remaining
    assert set(backoff.status()) == ({"voice"} if backoff.remaining("voice") else set())   # /health видит откат


# ─── к переносу в группу workflows (workflow_spec, Decisions.consult) ──────────────

def test_consult_workflow_parses_and_refuses_steps_and_command_gates():
    parsed = parse_workflow(CONSULTED, "fast-voice.yml")
    assert parsed.consult == "fast_voice" and parsed.steps == () and parsed.gate.fn == "fast_voice_ready"
    with pytest.raises(WorkflowError, match="шагов нет"):
        parse_workflow({**CONSULTED, "steps": [{"name": "x", "tool": "quiet"}]}, "x.yml")
    with pytest.raises(WorkflowError, match="функция"):
        parse_workflow({**CONSULTED, "gate": {"command": "echo 1"}}, "x.yml")
    with pytest.raises(WorkflowError, match="без schedule и event"):
        parse_workflow({**CONSULTED, "on": {"consult": "fast_voice", "event": "user_message"}}, "x.yml")


def test_decisions_noop_is_emitted_and_a_point_without_workflow_works():
    emitted, logged = [], []
    gates = {"fast_voice_ready": lambda ctx: "" if ctx.event["session"] == "voice" else "ок"}
    decisions = wf.Decisions([parse_workflow(CONSULTED, "fast-voice.yml")], gates,
                             lambda e, d: emitted.append((e, d)), log=logged.append)
    assert decisions.consult("fast_voice", {"session": "voice"}) is False
    assert emitted[0][1]["noop"] is True and emitted[0][1]["session"] == "voice"
    assert decisions.consult("fast_voice", {"session": "chat"}) is True
    assert decisions.consult("other_point", {}) is True
    broken = wf.Decisions([parse_workflow(CONSULTED, "fast-voice.yml")], {"fast_voice_ready": lambda ctx: 1 / 0},
                          lambda e, d: None, log=logged.append)
    assert broken.consult("fast_voice", {"session": "voice"}) is True and "упали" in logged[-1]


def test_rebuilding_line_hands_the_turn_to_the_main_model():
    """ADR-027: основная поставила «перестраиваю» - ворота пусты, отвечает она, пока не обновит заметки."""
    from gateway import fast_voice_gate as g
    cold = g.ColdStart()
    for _ in range(g.COLD_TURNS):
        cold.note("voice")
    assert g.fast_voice_ready("voice", cold, lambda s: 0.0) == g.READY
    assert g.fast_voice_ready("voice", cold, lambda s: 0.0, rebuilding=lambda s: True) == ""
