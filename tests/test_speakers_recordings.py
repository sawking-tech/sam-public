"""Опознание домашних на записях разговоров (SAM-30).

Набор лежит вне git (data/voice/eval: фразы записи и labels.json по ручной разметке,
не размеченная фраза - хозяина записи); профили - data/voice/voices. Идёт под маркером quality; нет набора,
профилей или модели ECAPA - пропускается. Порог опознания и замер на всей записи - voice/layer/speaker_eval.py.
Свой голос (профиль claude) здесь не меряется: ручной разметки эха нет, а пометка echo в записи
сама выведена по тексту - одна такая фраза оказалась голосом хозяина записи на 0,78 (журнал speakers).
"""
import json
from collections import Counter
from pathlib import Path

import pytest

from voice.layer import speakers
from voice.layer.config import DATA_DIR
from voice.layer.hearing import read_wav

EVAL = Path(DATA_DIR) / "eval"
ALIASES = {"owner-loud": "owner"}
# Доля верно опознанных фраз от 3 с на человека. Ниже - регрессия профилей или порога; число из
# первого прогона, записано в журнал speakers
MIN_ACCURACY = 0.7


def _ready() -> bool:
    if not (EVAL / "labels.json").exists() or not speakers.profiles():
        return False
    try:
        speakers.encoder()
        return True
    except Exception:
        return False


# Замер на личных данных, не тест кода: по умолчанию не собирается (pytest -m quality)
pytestmark = [pytest.mark.quality, pytest.mark.slow]


def test_household_voices_are_recognised():
    if not _ready():   # проверка в тесте: на сборке ECAPA не грузится
        pytest.skip("нет набора, профилей или ECAPA")
    labels = json.loads((EVAL / "labels.json").read_text(encoding="utf-8"))
    right, total = Counter(), Counter()
    for wav, person in labels.items():
        who, _ = speakers.identify(read_wav(str(EVAL / wav)))
        total[person] += 1
        right[person] += ALIASES.get(who, who) == person
    accuracy = {p: right[p] / total[p] for p in total}
    print("accuracy", {p: f"{right[p]}/{total[p]}" for p in total})
    assert all(a >= MIN_ACCURACY for a in accuracy.values()), accuracy
