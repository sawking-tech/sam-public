"""Голос как свидетельство радару (radar-in-sam): узел, человек по опознанию, громкость; выключено флагом.
Граница - клиент радара шлюза: его подменяет запоминающий клиент."""
import pytest

from gateway import radar_switch
from voice.layer import radar_feed as rf

PEOPLE = "first,second"          # метки людей без дефиса: дефис отделяет вариант голоса


class FakeClient:
    def __init__(self):
        self.sent = []

    def note_message(self, **kw):
        self.sent.append(kw)


def _env(**extra):
    return {radar_switch.ENV: "1", rf.PEOPLE_ENV: PEOPLE, **extra}


@pytest.mark.parametrize("env, rec, sent", [
    (_env(), {"who": "first", "rms": 900}, []),                                                   # флаг по умолчанию выключен
    (_env(**{rf.ENABLED_ENV: "0"}), {"who": "first", "rms": 900}, []),
    (_env(**{rf.ENABLED_ENV: "1"}), {"who": "first-loud", "rms": 656},                            # вариант голоса через дефис - тот же человек
     [{"channel": "voice", "device": rf.DEFAULT_DEVICE, "who": "first", "level": 656.0}]),
    (_env(**{rf.ENABLED_ENV: "1", rf.DEVICE_ENV: "node-b"}), {"who": "assistant", "rms": 300},         # не человек дома: голос без человека, узел из конфига
     [{"channel": "voice", "device": "node-b", "who": None, "level": 300.0}]),
    (_env(**{rf.ENABLED_ENV: "1"}), {"who": None},
     [{"channel": "voice", "device": rf.DEFAULT_DEVICE, "who": None, "level": None}]),
], ids=["default_off", "off", "variant", "unknown_on_node", "no_label"])
def test_heard_voice_reaches_radar_only_when_enabled(env, rec, sent):
    """Услышанная фраза уходит радару, только когда флаг включён: узел из конфига, человек по метке опознания, громкость."""
    client = FakeClient()
    rf.build(env, client=client).heard(rec)
    assert client.sent == sent


def test_people_default_to_voice_profiles_without_variants():
    from voice.layer import radar_feed
    assert radar_feed._people({"SAM_SPEAKER_NAMES": "a=Аня,a-loud=Аня,b=Боря,c=Вера"}) == {"a", "b", "c"}
    assert radar_feed._people({}) == frozenset()
