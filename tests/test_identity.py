"""Род речи SAM по голосу (SAM-78): метки {женский|мужской} в промптах, настройка voice_gender."""
import pytest

from gateway import agent, self_config
from gateway.prompts import WORKER_SYSTEM, identity
from gateway.prompts.personal import personalize
from gateway.prompts.system import SYSTEM_PROMPT_TEMPLATE

MARKED = "Ты {сама|сам} {открыла|открыл} окно."


@pytest.mark.parametrize("stored, by_tune, gender, phrase", [
    ("male", True, identity.MALE, "Ты сам открыл окно."),
    ("female", True, identity.FEMALE, "Ты сама открыла окно."),
    ("robot", True, identity.FEMALE, "Ты сама открыла окно."),
    ("женский", False, identity.FEMALE, "Ты сама открыла окно."),
], ids=["male", "female", "unknown_refused_by_setting", "stored_garbage_falls_back"])
def test_voice_gender_setting_switches_prompt_forms(monkeypatch, stored, by_tune, gender, phrase):
    cfg = self_config.SelfConfig()
    monkeypatch.setattr(self_config, "_instance", cfg)
    if by_tune:
        ok, msg = cfg.tune("voice_gender", stored)
        assert ok == (stored in identity.GENDERS)
        assert ok or "female" in msg
    else:
        cfg._values["voice_gender"] = stored   # так могло лечь в Mongo руками
    assert agent._system_prompt() == personalize(identity.render(SYSTEM_PROMPT_TEMPLATE, gender))
    assert identity.render(MARKED, gender) == phrase
    voice = agent._voice_runner_deps().system_prompt
    assert ("открыла" in voice) == (gender == identity.FEMALE)


def test_render_with_unknown_gender_is_an_error_in_one_line():
    with pytest.raises(ValueError, match="voice_gender"):
        identity.render("x", "robot")


@pytest.mark.parametrize("plain, words", [
    (identity.strip_markers(SYSTEM_PROMPT_TEMPLATE), ("сама ", "открыла", "нашла", "готова", "сказала", "самой", "подруга")),
    (WORKER_SYSTEM, ("сама ", "открыла", "нашла", "сделала")),
], ids=["main", "worker"])
def test_no_hard_gender_outside_markers(plain, words):
    assert [w for w in words if w in plain] == []


@pytest.mark.parametrize("registry, voice, gender", [
    ({"voices": {"cam": {"engine": "cosy", "gender": "male"}}}, "cam", identity.MALE),
    ({"voices": {"xenia": {"engine": "silero"}}}, "xenia", None),
    ({"voices": {"cam": {"gender": "male"}}}, "nope", None),
    ({"voices": {"x": "female"}}, "x", None),
    ({"voices": ["x"]}, "x", None),
    ({}, "x", None),
    ({"voices": None}, "x", None),
], ids=["male", "no_gender", "unknown_voice", "entry_not_object", "voices_list", "empty", "voices_none"])
def test_gender_of_registry_voice(registry, voice, gender):
    assert identity.gender_of_voice(registry, voice) == gender
