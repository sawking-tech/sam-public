"""Хук терминала (SAM-118): запрос радару собирается из настроек, без настроек молчит, недоступный радар не роняет хук.
Сеть подменена на границе: urlopen запоминает запрос или падает ошибкой сети."""
import json
import urllib.error

import pytest

from tests.fakes_presence import load_script

rp = load_script("radar_ping")
DEVICE, OWNER = "work-computer", "owner-id"
CONFIG = {rp.Setting.DEVICE: DEVICE, rp.Setting.WHO: OWNER}


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sent(monkeypatch):
    requests = []
    monkeypatch.setattr(rp.urllib.request, "urlopen", lambda request, timeout: requests.append(request) or Response())
    return requests


@pytest.mark.parametrize("config, url", [
    (CONFIG, rp.DEFAULT_URL + rp.PATH),
    ({**CONFIG, rp.Setting.URL: "http://10.0.0.5:9000"}, "http://10.0.0.5:9000" + rp.PATH),
    ({}, None),                                                                      # без устройства и человека - ничего
    ({rp.Setting.DEVICE: DEVICE}, None),
], ids=["default_url", "url_setting", "empty", "no_person"])
def test_ping_builds_chat_message_from_settings_or_stays_silent(sent, config, url):
    """Сообщение уходит чатом с настроенного устройства по адресу из настроек; без устройства или человека хук молчит."""
    assert rp.ping(config) is (url is not None)
    assert [r.full_url for r in sent] == ([url] if url else [])
    if url:
        assert json.loads(sent[0].data) == {"channel": rp.CHANNEL, "device": DEVICE, "who": OWNER}


def test_the_environment_wins_over_the_env_file_and_other_variables_are_ignored(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{rp.Setting.DEVICE}=from-file\n{rp.Setting.WHO}=from-file\nOTHER=1\n", encoding="utf8")
    config = rp.settings({rp.Setting.DEVICE: "from-env", "UNRELATED": "x"}, str(env_file))
    assert config == {rp.Setting.DEVICE: "from-env", rp.Setting.WHO: "from-file"}


def test_an_unreachable_radar_gives_false_and_no_exception(monkeypatch):
    def refuse(request, timeout):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(rp.urllib.request, "urlopen", refuse)
    assert rp.ping(CONFIG) is False
