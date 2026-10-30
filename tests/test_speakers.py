"""Опознание голоса (voice/layer/speakers.py): профиль, порог, короткий звук, битые профили. Настоящие speakers.embed,
identify, profiles и save_profile; подменена модель ECAPA (граница модели): эмбеддинг задаётся первым сэмплом звука."""
from __future__ import annotations

import numpy as np
import pytest

from voice.layer import speakers

torch = pytest.importorskip("torch")

RATE = speakers.SAMPLE_RATE
HALF_WRITTEN = bytes([0x93]) + b"NUMPY"      # начало файла .npy без заголовка
SHORTEST_S = 0.5          # короче звук не опознаётся (speakers.identify)
# Эмбеддинги модели по метке звука: метка - значение первого сэмпла
VECTORS = {
    1: [1.0, 0.0, 0.0], 2: [0.8, 0.6, 0.0],                 # две записи Антона
    3: [0.0, 1.0, 0.0],                                    # Ирина
    4: [0.6, 0.0, 0.8],                                    # гость: к Антону 0,6 по одной оси
    5: [0.3, 0.2, np.sqrt(1 - 0.13)],                      # далёкий голос: лучший косинус ниже 0,4
}


class _Encoder:
    def encode_batch(self, wav):
        mark = int(round(float(wav[0, 0]) * 32768))
        return torch.tensor([[VECTORS[mark]]], dtype=torch.float32)


@pytest.fixture
def voices(tmp_path, monkeypatch):
    monkeypatch.setattr(speakers, "VOICES", tmp_path)
    monkeypatch.setattr(speakers, "_encoder", _Encoder())
    monkeypatch.setattr(speakers, "_reported", set())
    return tmp_path


def _sound(mark: int, secs: float = 1.0) -> np.ndarray:
    audio = np.zeros(int(RATE * secs), dtype=np.int16)
    audio[0] = mark
    return audio


def test_profile_is_the_normalised_mean_of_recordings(voices):
    """voice-id 1: профиль - средний нормированный эмбеддинг записей, файл <имя>.npy в папке голосов."""
    speakers.save_profile("owner", [speakers.embed(_sound(1)), speakers.embed(_sound(2))])
    profile = np.load(voices / "owner.npy")
    expected = np.array([1.8, 0.6, 0.0]) / np.linalg.norm([1.8, 0.6, 0.0])
    assert np.allclose(profile, expected) and np.isclose(np.linalg.norm(profile), 1.0)


@pytest.mark.parametrize("mark, secs, who", [
    (1, 1.0, "owner"),
    (3, 1.0, "partner"),
    (5, 1.0, None),                    # лучший косинус ниже VOICE_MIN_SCORE - «неизвестно», оценка пишется
    (1, 0.49, None),                   # короче 0,5 с - не опознаётся вовсе
], ids=["owner", "partner", "below-min", "too-short"])
def test_best_profile_above_the_threshold_names_the_voice(voices, mark, secs, who):
    """voice-id 2, 3, 4: оценка - лучший косинус к профилям; ниже 0,4 - who None с оценкой; звук короче 0,5 с -
    без опознания."""
    speakers.save_profile("owner", [speakers.embed(_sound(1))])
    speakers.save_profile("partner", [speakers.embed(_sound(3))])
    name, score = speakers.identify(_sound(mark, secs))

    assert speakers.MIN_SCORE == 0.4 and name == who
    if mark == 5:
        assert 0 < score < speakers.MIN_SCORE
    if secs < SHORTEST_S:
        assert score == 0.0


def test_broken_profiles_are_skipped_once_and_the_rest_still_work(voices, capsys):
    """voice-id 6: недописанный файл, не вектор, профиль другой модели (другой размер) - пропуск с одной строкой
    за запуск; остальные профили опознают как прежде."""
    speakers.save_profile("owner", [speakers.embed(_sound(1))])
    (voices / "half.npy").write_bytes(HALF_WRITTEN)                            # недописан
    np.save(voices / "matrix.npy", np.ones((2, 3)))                            # не вектор
    np.save(voices / "nan.npy", np.array([np.nan, 0.0, 0.0]))                  # запись оборвалась на NaN
    np.save(voices / "other_model.npy", np.ones(192) / np.sqrt(192))           # другая модель эмбеддингов

    for _ in range(3):
        assert speakers.identify(_sound(1))[0] == "owner"
    lines = [line for line in capsys.readouterr().out.splitlines() if "пропущен" in line]
    assert sorted(line.split()[2] for line in lines) == ["half", "matrix", "nan", "other_model"]


def _wav(path, audio):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.tobytes())
    return str(path)


def test_cli_enrolls_a_person_and_sam_herself_names_a_voice_and_lists_profiles(voices, tmp_path, monkeypatch,
                                                                               capsys):
    """voice-id 10: CLI enroll строит профиль из записей, enroll-claude - свой профиль из синтезированных фраз, who
    называет голос записи, list - профили. Подменены модель ECAPA и синтез (граница модели и движка)."""
    import sys

    from voice.layer import tts_client
    files = [_wav(tmp_path / f"v{m}.wav", _sound(m)) for m in (1, 2)]
    monkeypatch.setattr(tts_client, "synthesize", lambda line: (_sound(3), RATE))    # голос движка - метка 3
    for argv in (["enroll", "owner", *files], ["enroll-claude"], ["who", _wav(tmp_path / "q.wav", _sound(3))],
                 ["list"]):
        monkeypatch.setattr(sys, "argv", ["speakers", *argv])
        speakers.main()
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "профиль owner: 2 записей" and out[1].startswith(f"профиль {speakers.CLAUDE}: 5 ")
    assert out[2] == f"{speakers.CLAUDE} (1.00)" and out[3:] == sorted([speakers.CLAUDE, "owner"])
    mean = np.mean([VECTORS[1], VECTORS[2]], axis=0)
    assert np.allclose(np.load(voices / "owner.npy"), mean / np.linalg.norm(mean))     # обе записи в профиле
