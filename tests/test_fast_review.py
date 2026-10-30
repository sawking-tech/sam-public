"""Проверка ответов быстрой модели (SAM-166): workflow fast-review.yml и инструмент amend_reply - поправка основной
отзывает реплику быстрой или звучит поздно, неверный ответ уходит из контекста. Голосовой слой подменён, история -
mongomock, чистка - настоящий agent._forget_reply."""
from pathlib import Path

import mongomock
import pytest
import yaml

from gateway import workflows as wf
from gateway.repos.session_repo import IGNORE, SessionRepo
from gateway.spoken_history import IGNORE_FIELD, SpokenLedger
from gateway.tools.amend_reply import LATE_FIX_TTL_S, amend_reply_impl
from gateway.workflow_spec import parse_workflow
from tests.fakes_voice import T0, scheduler

FILE = Path(__file__).resolve().parents[1] / "workflows" / "fast-review.yml"
FIX, LATE = "fix", "late"


class _Voice:
    """Голосовой слой: broadcast - сообщение fix мосту, say - своя реплика в паузе; connected - подключён ли."""

    def __init__(self, connected=True):
        self.connected, self.sent, self.said, self.fixes = connected, [], [], []

    def broadcast(self, msg):
        self.sent.append(msg)
        return self.connected

    def record(self, session, text):
        self.fixes.append((session, text))

    def say(self, session, text, ttl_s=None):
        self.said.append((session, text, ttl_s))
        return self.connected

    def heard(self):
        """Что услышит человек: fix - отзыв и поправка сразу, late - поздняя реплика, None - ничего."""
        return FIX if self.sent and self.connected else LATE if self.said and self.connected else None


def _flow():
    data = yaml.safe_load(FILE.read_text(encoding="utf-8"))
    data.pop("enabled", None)
    return parse_workflow(data, FILE.name)


@pytest.mark.parametrize("verdict, events, expected", [
    ("OK", 1, None),
    ("ой, поправлюсь: свет в спальне я не включала", 1, FIX),
    ("кстати, про погоду я выдумала", 2, LATE),            # проверялась пачка ответов
])
async def test_fast_review_workflow_ok_fix_or_late(verdict, events, expected):
    """Настоящий fast-review.yml, LLM-шаг подменён: OK - тишина, поправка одного ответа - отзыв, по пачке - поздняя."""
    v, asked = _Voice(), []

    async def step(st, src, text, session):
        if st.tool == "dialogue_context":
            return wf.StepOutcome(st.tool, "Разговор, последние реплики:\nАнтон: который час")
        if st.kind == "ask":
            asked.append(st.text)
            return wf.StepOutcome("ask", verdict)
        return wf.StepOutcome(st.tool, amend_reply_impl(st.args, "voice", v.broadcast, v.record, v.say))
    s = scheduler([_flow()], step)
    trigger = "\n".join(f'assistant_reply {{"source": "fast", "phrase": "фраза {i}", "voice": "ответ {i}"}}'
                        for i in range(events))
    await s.run_once(s.workflows[0], wf.GateContext(T0, None), trigger=trigger)
    assert "ответ 0" in asked[0] and v.heard() == expected


@pytest.mark.parametrize("params, last_phrase, connected, out, heard", [
    ({"text": "ой, поправлюсь: сейчас вечер", "count": "1"}, None, True, "OK", FIX),
    ({"text": "кстати, про будильник я ошиблась", "count": "3"}, None, True, "OK", LATE),
    ({"text": "ой, не так", "count": "1", "phrase": "который час"}, "а погода какая", True, "OK", LATE),
    ({"text": "ой, не так", "phrase": "который час"}, "который час", True, "OK", FIX),   # своя фраза последняя
    ({"text": "DROP", "count": "2"}, None, True, "Убирать нечего", None),   # DROP в пачке - не поправка
    ({"text": ""}, None, True, "text", None),
    ({"text": "x", "count": "два"}, None, True, "count", None),
    ({"text": "x"}, None, False, "не подключён", None),
    ({"text": "x", "count": "2"}, None, False, "не подключён", None),
])
def test_amend_retracts_last_answer_or_speaks_late(params, last_phrase, connected, out, heard):
    v = _Voice(connected)
    result = amend_reply_impl(params, "voice", v.broadcast, v.record, v.say, lambda session: last_phrase)
    assert out in result and "\n" not in result and v.heard() == heard
    if heard == FIX:
        assert v.sent == [{"type": "fix", "text": params["text"]}] and v.fixes == [("voice", params["text"])]
    if heard == LATE:
        assert v.said == [("voice", params["text"], LATE_FIX_TTL_S)]


@pytest.mark.parametrize("count, heard", [("1", FIX), ("2", LATE)], ids=["urgent", "late"])
def test_amend_does_not_speak_meta_blocks(count, heard):
    """Мета-блоки поправки не звучат (ADR-003): ни срочной, ни поздней; из одного блока поправки нет."""
    v = _Voice(True)
    params = {"text": "ой, поправлюсь: <think>проверю часы</think>сейчас вечер<note>monitor</note>", "count": count}
    assert "OK" in amend_reply_impl(params, "voice", v.broadcast, v.record, v.say) and v.heard() == heard
    spoken = (v.sent[0]["text"] if heard == FIX else v.said[0][1])
    assert spoken == "ой, поправлюсь: сейчас вечер"
    assert "text" in amend_reply_impl({"text": "<silent></silent>"}, "voice", v.broadcast, v.record, v.say)


def test_amend_keeps_every_line_of_a_multiline_fix():
    """Готовая поправка - речь целиком: строка «Сначала ...» не принимается за план рассуждения."""
    v = _Voice(True)
    amend_reply_impl({"text": "Сначала проверила лампы.\nВсё работает."}, "voice", v.broadcast, v.record, v.say)
    assert v.sent[0]["text"] == "Сначала проверила лампы.\nВсё работает."


REPLY = "Погода дома - тихо, вечерний свет"


@pytest.mark.parametrize("text, reply, where, hidden, heard", [
    ("ой, поправлюсь: дома тихо", REPLY, "history", True, FIX),
    ("DROP", REPLY, "history", True, None),              # бред убирается без поправки вслух
    ("ой, поправлюсь: дома тихо", "вечерний свет", "history", False, FIX),   # подстрокой не ищет (ревью 12 окт)
    ("ой, поправлюсь: дома тихо", REPLY, "ledger", True, FIX),   # длинная реплика ещё звучит
], ids=["history-fix", "history-drop", "substring", "still-sounding"])
async def test_wrong_reply_leaves_context_with_or_without_spoken_fix(monkeypatch, text, reply, where, hidden, heard):
    from gateway import agent
    written = []
    repo, ledger = SessionRepo(mongomock.MongoClient().db), SpokenLedger(lambda name, data: written.append(data))
    repo.col.insert_many([{"session_id": "voice", "role": "assistant", "content": "Антон, я тут", "msg_id": "b"}])
    if where == "history":
        repo.col.insert_one({"session_id": "voice", "role": "assistant", "content": REPLY, "msg_id": "a"})
    else:
        ledger.expect("voice", REPLY, phrase="какая погода")
    monkeypatch.setattr(agent, "session_repo", repo)
    monkeypatch.setattr(agent, "_spoken_ledger", ledger)
    v = _Voice()
    assert amend_reply_impl({"text": text, "reply": reply}, "voice", v.broadcast, v.record, v.say,
                            forget=agent._forget_reply).startswith("OK") is (hidden or heard is not None)
    assert v.heard() == heard
    assert IGNORE not in repo.col.find_one({"msg_id": "b"})
    if where == "history":
        assert (IGNORE in repo.col.find_one({"msg_id": "a"})) is hidden
    else:
        ledger.settle("voice", [{"text": REPLY, "status": "played"}])
        assert IGNORE_FIELD == IGNORE and written[0][IGNORE_FIELD] == agent.CORRECTION_REASON


# ─── к переносу в группу workflows (подстановка {{event.*}}) ───────────────────────

async def test_single_event_fields_fill_event_placeholders():
    seen = []

    async def step(st, src, text, session):
        seen.append(dict(st.args or {}))
        return wf.StepOutcome("t", "ok")
    flow = parse_workflow({"name": "e", "on": {"event": "assistant_reply"},
                           "steps": [{"name": "a", "tool": "t", "with": {"p": "{{event.phrase}}", "n": "{{count}}"}}]}, "e.yml")
    s = scheduler([flow], step)
    await s.run_once(s.workflows[0], wf.GateContext(T0, None), trigger="x", event={"phrase": "который час"})
    await s.run_once(s.workflows[0], wf.GateContext(T0, None), trigger="x\ny")
    assert seen == [{"p": "который час", "n": "1"}, {"p": "", "n": "2"}]
