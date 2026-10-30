"""Сканирование (radar-in-sam): ограничения вопроса в коде - порог уверенности, интервал, ночь, тихий режим,
вопрос партнёру после ожидания без ответа и без повторов; услышанное о положении; флаг."""
import pytest

from gateway import scanning as sc

CFG = sc.Config(every_s=7200, wait_s=600, sure=0.5, stale_s=10800, night_from_h=23, night_to_h=8)
NAMES = {"clara": "Клара", "bella": "Белла"}
PARTNERS = {"clara": "bella", "bella": "clara"}
WORDS = ("лёг", "кухня", "диван в зале")
T = 1_000_000.0
DAY = 14.0


def _people(clara=0.3, bella=0.9):
    return [{"person": "clara", "confidence": clara}, {"person": "bella", "confidence": bella}]


def _decide(state=None, people=None, heard=(), hour=DAY, quiet=False, now=T, partners=PARTNERS):
    return sc.decide(now, hour, quiet, _people() if people is None else people, partners, NAMES,
                     state or sc.State(cursor=now - 60), list(heard), WORDS, CFG)


def _questions(lines):
    return [line for line in lines if line.startswith("Вопрос:")]


PENDING = {"person": "clara", "asked_at": T - 900}


@pytest.mark.parametrize("state, people, kw, asked", [
    (None, None, {}, "Клара"),
    (None, _people(clara=0.9, bella=0.9), {}, None),
    (sc.State(cursor=T - 60, marked={"clara": T - 4 * 3600, "bella": T}), _people(clara=0.9, bella=0.9), {}, "Клара"),
    (sc.State(cursor=T - 60, asked={"clara": T - 3600}), None, {}, None),
    (sc.State(cursor=T - 60, asked={"clara": T - 7300}), None, {}, "Клара"),
    (sc.State(cursor=T - 60, pending=PENDING), None, {"hour": 23.5}, None),
    (sc.State(cursor=T - 60, pending=PENDING), None, {"hour": 3.0}, None),
    (sc.State(cursor=T - 60, pending=PENDING), None, {"hour": 7.9}, None),
    (None, None, {"hour": 8.0}, "Клара"),
    (sc.State(cursor=T - 60, pending=PENDING), None, {"quiet": True}, None),
], ids=["unsure_person", "everyone_sure", "long_silence_makes_sure_due", "asked_within_interval", "interval_passed",
        "night_before_midnight", "night_after_midnight", "night_end_edge", "morning", "quiet_mode"])
def test_scanning_asks_only_when_due(state, people, kw, asked):
    """Вопрос - только тому, в ком радар не уверен или кого давно не слышно, раз за интервал; ночь и тихий режим
    вопрос снимают вместе с открытым."""
    lines, after = _decide(state, people=people, **kw)
    questions = _questions(lines)
    if asked:
        assert len(questions) == 1 and f"по имени {asked}" in questions[0] and after.pending["person"] == "clara"
    else:
        assert questions == [] and after.pending is None


def test_unanswered_question_goes_to_partner_once():
    asked = sc.State(cursor=T - 60, asked={"clara": T - 300}, pending={"person": "clara", "asked_at": T - 300})
    lines, state = _decide(asked)
    assert _questions(lines) == [] and state.pending is not None                     # ждём ответа M минут
    late = sc.State(cursor=T - 60, asked={"clara": T - 700}, pending={"person": "clara", "asked_at": T - 700})
    lines, state = _decide(late)
    assert "по имени Белла" in _questions(lines)[0] and "где сейчас Клара" in _questions(lines)[0] and state.pending is None
    again, _ = _decide(state, now=T + 300)
    assert not any("по имени Белла" in q for q in _questions(again))                     # без повторов
    answer = sc.Heard(T - 650, None, "я на кухне")                                    # опознания нет: ответ засчитан
    lines, state = _decide(late, heard=[answer])
    assert not any("Белла" in q for q in _questions(lines)) and state.pending is None and state.marked["clara"] == T
    lines, state = _decide(late, partners={})                                          # партнёра нет - вопроса нет
    assert _questions(lines) == [] and state.pending is None


def test_heard_position_goes_to_model_once():
    heard = [sc.Heard(T - 30, "bella", "Я на кухне, чайник ставлю"), sc.Heard(T - 20, "clara", "смотри какой кот")]
    lines, state = _decide(people=_people(clara=0.9, bella=0.9), heard=heard)
    assert lines[0] == sc.HEARD_HEADER and len(lines) == 2 and "Белла: Я на кухне" in lines[1]
    assert _decide(state, people=_people(clara=0.9, bella=0.9), heard=heard)[0] == []   # уже разобранное не повторяется
    # из шины слуха: вариант голоса - тот же человек, частичные и эхо не идут; положение - по словам из списка
    phrases = [{"ts": "2026-10-12T01:00:00+06:00", "text": "лёг", "who": "clara-loud"},
               {"ts": "2026-10-12T01:00:01+06:00", "text": "лёг", "partial": True},
               {"ts": "2026-10-12T01:00:02+06:00", "text": "эхо", "echo": True}]
    assert [(h.who, h.text) for h in sc.heard_from(phrases)] == [("clara", "лёг")]
    assert sc.mentions_position("Ну всё, я лёг.", WORDS) and sc.mentions_position("сижу на диване в зале", WORDS)
    assert not sc.mentions_position("смотри какой кот", WORDS) and not sc.mentions_position("на диване", ("диван в зале",))


def test_scanning_gate_silent_when_off(monkeypatch, capsys):
    monkeypatch.delenv(sc.ENABLED_ENV, raising=False)
    monkeypatch.setattr(sc, "read_radar", lambda: pytest.fail("выключенное сканирование не ходит в радар"))
    assert sc.main() == 0 and capsys.readouterr().out == ""
