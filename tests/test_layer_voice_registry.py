"""Проверка реестра голосов, выбора движка и банка слепков без звука и видеокарты

SAM-09.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from voice.layer.tts_client import pick_backend
from voice.layer.voice_bank import VoiceBank
from voice.layer.voice_registry import REGISTRY, Live, RegistryError, load, parse

BASE = Path("voices-base")
DATA = {
    "defaults": {"silero": "eugene", "cosy": "cam"},
    "voices": {
        "xenia": {"engine": "silero", "about": "Ксюша",
                  "silero": {"speaker": "xenia"},
                  "cosy": {"ref": "x.wav", "text": "x.txt", "imprint": "cosy-xenia.pt"}},
        "cam": {"engine": "cosy", "cosy": {"ref": "C:/abs/cam.wav", "text": "cam.txt", "imprint": "cosy-cam.pt"}},
        "eugene": {"engine": "silero", "silero": {"speaker": "eugene"}},
    },
}


def registry():
    return parse(DATA, BASE)


def raises(fn) -> bool:
    try:
        fn()
    except RegistryError:
        return True
    return False


def test_names_by_engine() -> None:
    assert registry().names("cosy") == ["xenia", "cam"]
    assert registry().names("silero") == ["xenia", "eugene"]


def test_relative_paths_from_registry_folder() -> None:
    xenia = registry().get("xenia", "cosy")
    assert xenia.ref == BASE / "x.wav" and xenia.imprint == BASE / "cosy-xenia.pt"
    assert registry().get("cam", "cosy").ref == Path("C:/abs/cam.wav")


def test_unknown_voice_falls_to_engine_default() -> None:
    assert registry().choose("unknown-voice", "silero").speaker == "eugene"   # голоса нет в реестре
    assert registry().choose("eugene", "cosy").name == "cam"                        # у eugene нет клона
    assert registry().choose(None, "cosy").name == "cam"
    assert registry().choose("xenia", "cosy").name == "xenia"


def test_engine_follows_the_voice() -> None:
    r = registry()
    assert r.engine_for("cam", "silero") == "cosy"     # Кэм есть только на CosyVoice
    assert r.engine_for("xenia", "cosy") == "cosy"     # клон Ксюши
    assert r.engine_for("xenia", None) == "silero"     # свой движок Ксюши
    assert r.engine_for("unknown-voice", "unknown-engine") is None


def test_switch_cam_xenia_by_session_voice() -> None:
    r = registry()
    assert pick_backend({"voice": "cam", "tts": "silero"}, None, r, None) == "cosy"
    assert pick_backend({"voice": "xenia", "tts": "silero"}, None, r, None) == "silero"
    assert pick_backend({"voice": "xenia"}, None, r, "cosy") == "cosy"            # SAM_TTS как выбор движка
    assert pick_backend({"voice": "xenia", "tts": "silero"}, "cam", r, None) == "cosy"   # голос реплики главнее
    assert pick_backend({}, None, r, None) == "silero"
    assert pick_backend({"tts": "nonsense"}, None, r, None) == "silero"


def test_bad_registry_is_one_line_error() -> None:
    bad_engine = {"voices": {"cam": {"engine": "silero", "cosy": {}}}}
    bad_default = {"defaults": {"cosy": "eugene"}, "voices": {"eugene": {"engine": "silero", "silero": {}}}}
    assert raises(lambda: parse(bad_engine, BASE))
    assert raises(lambda: parse(bad_default, BASE))
    assert raises(lambda: registry().check("nobody"))
    assert raises(lambda: registry().check("eugene", "cosy"))
    assert not raises(lambda: registry().check("xenia", "cosy"))


@pytest.mark.skipif(not REGISTRY.exists(), reason="реестр голосов машины не скопирован в каталог данных SAM")
def test_stand_registry_has_known_voices() -> None:
    r = load(REGISTRY)
    assert r.engine_for("xenia", None) == "silero" and r.get("xenia", "silero").speaker == "xenia"
    assert r.engine_for("cam", None) == "cosy" and r.get("cam", "cosy").imprint.name == "cosy-cam.pt"
    assert r.get("xenia", "cosy").imprint.name == "cosy-xenia.pt"
    assert r.choose(None, "silero").name == "eugene"


def test_live_rereads_on_edit_and_keeps_last_good() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "registry.json"
        path.write_text(json.dumps(DATA), encoding="utf-8")
        live = Live(path)
        assert live.get().names("cosy") == ["xenia", "cam"]
        edited = {**DATA, "voices": {k: v for k, v in DATA["voices"].items() if k != "xenia"}}
        path.write_text(json.dumps(edited) + " ", encoding="utf-8")   # другой размер: правка видна и при том же mtime
        assert live.get().names("cosy") == ["cam"]
        path.write_text("{ сломан", encoding="utf-8")
        assert live.get().names("cosy") == ["cam"]


def test_bank_installs_each_voice_once_and_switches() -> None:
    installed, lines = [], []
    bank = VoiceBank(lambda v: installed.append(v.name) or "из теста", lines.append, "[синтез] cosy:")
    r = registry()
    assert bank.use(r.choose("cam", "cosy")) == "cam"
    assert bank.use(r.choose("xenia", "cosy")) == "xenia"
    assert bank.use(r.choose("cam", "cosy")) == "cam"
    assert installed == ["cam", "xenia"]                       # слепок ставится один раз
    assert lines[-1] == "[синтез] cosy: говорю голосом cam"
    changed = parse({**DATA, "voices": {**DATA["voices"], "cam": {"engine": "cosy", "cosy": {"ref": "new.wav"}}}}, BASE)
    bank.use(changed.choose("cam", "cosy"))
    assert installed == ["cam", "xenia", "cam"]                # запись в реестре поменялась - поставить заново

