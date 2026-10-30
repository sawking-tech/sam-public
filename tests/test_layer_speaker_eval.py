"""Офлайн-замер опознания по голосу (voice/layer/speaker_eval.py, SAM-30)."""

from __future__ import annotations

import pytest

from voice.layer.speaker_eval import best, sweep_row, threshold_errors

ALIASES = {"owner-loud": "owner"}
MIN = 0.4


def row(full: dict[str, float], person: str = "owner") -> dict:
    return {"wav": "x.wav", "ts": "", "text": "", "person": person, "full": full}


def test_alias_names_the_person() -> None:
    assert best({"owner-loud": 0.6, "friend": 0.3}, ALIASES) == ("owner", 0.6)


@pytest.mark.parametrize("scores, person, kind", [
    ({"owner": 0.35, "friend": 0.2}, "owner", "unknown"),       # ниже порога - не узнан
    ({"owner": 0.3, "friend": 0.55}, "owner", "wrong"),         # чужой профиль выше порога - ошибка
    ({"friend": 0.7}, "friend", None),                         # записи других людей не считаются
], ids=["below-min-is-unknown", "other-over-min-is-wrong", "other-people-not-counted"])
def test_threshold_errors_by_person(scores, person, kind) -> None:
    r = row(scores, person=person)
    err = threshold_errors([r], "owner", ALIASES, MIN)
    assert {k: v for k, v in err.items() if v} == ({kind: [r]} if kind else {})


def test_sweep_prices_a_threshold() -> None:
    rows = [row({"owner": 0.45, "friend": 0.2}), row({"owner": 0.3, "friend": 0.35}),
            row({"friend": 0.7, "owner": 0.3}, person="friend"), row({"owner": 0.42}, person="partner")]
    for r, secs in zip(rows, (8.0, 2.0, 3.0, 3.0)):
        r["secs"] = secs
    assert sweep_row(rows, "owner", ALIASES, t=0.4, long_secs=6.0) == {
        "unknown": 1, "unknown_long": 0, "wrong": 0, "taken": 1}
