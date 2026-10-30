"""Заметки основной модели о разговоре (SAM-131): разбор, вид в контексте, хранилище и workflow dialogue-notes."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from gateway import workflows as wf
from gateway.repos import SessionRepo
from gateway.repos.dialogue_notes_repo import DialogueNotesRepo
from gateway.tools import dialogue_notes as dn
from gateway.workflow_spec import parse_workflow
from tests.fakes_core import WORKFLOW_T0, workflow_scheduler

NOTES_JSON = ('{"topic": "агенты разработки", "line": "Антон описывает миньонов", "forecast": "спросит про Hermes", '
              '"branches": [{"who": "Антон", "says": "а ты так умеешь?", "reply": "Умею, запускаю агента под задачу"}, '
              '{"says": "что такое Hermes", "reply": "Hermes умеет так же"}, {"who": "Ирина", "says": "", "reply": "x"}]}')
BRANCHES = [{"who": "Антон", "says": "а ты так умеешь?", "reply": "Умею, запускаю агента под задачу"},
            {"who": "", "says": "что такое Hermes", "reply": "Hermes умеет так же"}]
NOTES_TURNS = 30
NOW = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw, notes", [
    (NOTES_JSON, True), (f"```json\n{NOTES_JSON}\n```", True), (f"Вот заметки: {NOTES_JSON}", True),
    ("", False), ("OK", False), ("{не json}", False), ('{"other": 1}', False),
], ids=["json", "fenced", "in-prose", "empty", "ok", "broken-json", "other-fields"])
def test_model_answer_becomes_notes_or_nothing(raw, notes):
    got = dn.parse_notes(raw)
    if not notes:
        assert got is None
        return
    assert got["topic"] == "агенты разработки" and got["branches"] == BRANCHES   # ветка без says отброшена


# SAM-218: два ответа шага ask из живого разговора 15 окт - обрезан пределом посреди ветки и с битым ключом ветки
CUT_ANSWER = ('```json\n{\n  "topic": "Статус трекера калорий",\n  "line": "Агент осматривает папки",\n'
              '  "forecast": "Агент доложит",\n  "branches": [\n    {"who": "Человек", "says": "Докопался?", '
              '"reply": "Копает малыми шагами"},\n    {\n      "who": "В')
BAD_KEY_ANSWER = ('{"topic": "Статус трекера калорий", "line": "Человек считает это багом", "forecast": "Отчёт агента", '
                  '"branches": [{"who": "Человек", "says": "Что нашёл агент?", "reply": "Копаю ещё"}, '
                  '{"who": "В lost": "Код был в другой сессии", "reply": "Соберу с нуля"}]}')


@pytest.mark.parametrize("raw", [CUT_ANSWER, BAD_KEY_ANSWER], ids=["cut_by_limit", "bad_branch_key"])
def test_cut_or_broken_model_json_still_gives_the_topic_and_whole_branches(raw):
    got = dn.parse_notes(raw)
    assert got["topic"] == "Статус трекера калорий" and got["line"] and got["forecast"]
    assert {"who": "Человек", "says": got["branches"][0]["says"], "reply": got["branches"][0]["reply"]} == got["branches"][0]
    assert all(b["says"] and b["reply"] for b in got["branches"])
    assert dn.update_impl({"notes": raw}, "voice", lambda s, n: 1).startswith("OK")


def test_notes_render_for_context():
    text = dn.render_notes(dn.parse_notes(NOTES_JSON))
    assert text.startswith(dn.NOTES_HEADER) and "отвечай, как считаешь лучше" in text and "Тема: агенты разработки" in text
    assert "Если Антон скажет «а ты так умеешь?» - можно ответить: «Умею, запускаю агента под задачу»" in text
    assert "Если скажет «что такое Hermes»" in text
    assert dn.render_notes(None) == ""


def test_notes_repo_keeps_versions_and_drops_stale(mongo_db):
    repo = DialogueNotesRepo(mongo_db, max_age_s=3600)
    assert repo.put("voice", {"topic": "a"}, NOW) == 1 and repo.put("voice", {"topic": "b"}, NOW) == 2
    assert repo.get("voice", NOW)["topic"] == "b"
    assert repo.get("voice", NOW + timedelta(hours=2)) is None
    assert DialogueNotesRepo(None, 1).get("voice") is None and DialogueNotesRepo(None, 1).put("voice", {}) == 0


def _flow():
    path = Path(__file__).resolve().parents[1] / "workflows" / "dialogue-notes.yml"
    return parse_workflow(yaml.safe_load(path.read_text(encoding="utf-8")), path.name)


@pytest.mark.parametrize("dialogue", [True, False], ids=["with-dialogue", "without-dialogue"])
async def test_notes_workflow_reads_asks_saves(mongo_db, dialogue):
    """Workflow из репозитория на настоящем планировщике; шаги-инструменты - настоящие context_impl и update_impl
    на истории и заметках в mongomock, шаг ask - ответ модели (граница)."""
    sessions, notes = SessionRepo(mongo_db), DialogueNotesRepo(mongo_db, max_age_s=3600)
    if dialogue:
        mongo_db.sessions.insert_many([
            {"session_id": "voice", "role": "user", "content": "есть же агенты", "who": "Антон", "timestamp": NOW},
            {"session_id": "voice", "role": "assistant", "content": "Да, есть.", "timestamp": NOW}])
    asked, ran = [], []

    async def step(st, src, text, session):
        ran.append(st.name)
        if st.kind == "ask":
            asked.append(st.text)
            return wf.StepOutcome("ask", NOTES_JSON)
        if st.tool == "dialogue_context":
            return wf.StepOutcome(st.tool, dn.context_impl(st.args, "voice", sessions.get_recent,
                                                           lambda s: notes.get(s, NOW), NOTES_TURNS))
        return wf.StepOutcome(st.tool, dn.update_impl(st.args, "voice", lambda s, n: notes.put(s, n, NOW)))

    scheduler, _ = workflow_scheduler([_flow()], step)
    await scheduler.run_once(scheduler.workflows[0], wf.GateContext(WORKFLOW_T0, None), trigger="user_message {}")
    if not dialogue:
        assert ran == ["context"] and notes.get("voice", NOW) is None   # нет разговора - модель не спрашивают
        return
    assert "Антон: есть же агенты" in asked[0] and "SAM: Да, есть." in asked[0] and "Прежних заметок нет" in asked[0]
    saved = notes.get("voice", NOW)
    assert saved["topic"] == "агенты разработки" and saved["version"] == 1
    # Мусор от модели не перезаписывает заметки и говорит, почему
    assert "не разобраны" in dn.update_impl({"notes": "OK"}, "voice", lambda s, n: notes.put(s, n, NOW))
    assert notes.get("voice", NOW)["version"] == 1


def test_compression_summary_is_labelled_as_a_summary_not_a_human_line():
    """SAM-219: сводка сжатия в контексте заметок - «[сводка] время», а не «Человек:»."""
    history = [{"role": "summary", "content": "обсудили трекер", "time_range": "10:24 – 10:25"},
               {"role": "user", "content": "как трекер?", "who": "Человек"}, {"role": "assistant", "content": "готов"}]
    text = dn.context_impl({}, "voice", lambda s, n: history, lambda s: None, 10)
    assert "[сводка] 10:24 – 10:25: обсудили трекер" in text and "Человек: обсудили" not in text
    assert "Человек: как трекер?" in text and "SAM: готов" in text


def test_digest_and_rebuilding_split_for_the_fast_model():
    """ADR-027: линия и выжимка - выше диалога быстрой, заготовки - после него; признак «перестраиваю» - флаг."""
    notes = dn.parse_notes('{"topic": "ремонт", "line": "считаем плитку", "digest": "агент нашёл цены", '
                           '"rebuilding": true, "branches": [{"who": "Человек", "says": "сколько?", "reply": "посчитаю"}]}')
    assert notes["rebuilding"] is True and dn.is_rebuilding(notes)
    line, prepared = dn.render_line(notes), dn.render_prepared(notes)
    assert line.startswith(dn.LINE_HEADER) and "Выжимка: агент нашёл цены" in line and "посчитаю" not in line
    assert prepared.startswith(dn.PREPARED_HEADER) and "«посчитаю»" in prepared and "ремонт" not in prepared
    assert dn.parse_notes('{"topic": "ремонт", "rebuilding": "false"}')["rebuilding"] is False
    assert dn.render_line(None) == "" and dn.render_prepared({"topic": "x"}) == ""


def test_context_for_notes_carries_own_work_for_the_digest():
    text = dn.context_impl({}, "voice", lambda s, n: [{"role": "user", "content": "привет"}], lambda s: None, 5,
                           own_work=lambda: "СВОЯ РАБОТА")
    assert text.index("привет") < text.index("СВОЯ РАБОТА") < text.index("Прежних заметок нет.")
