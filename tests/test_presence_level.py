"""Место устройства по уровню сигнала: отбор мест с координатами (SAM-118)."""
import pytest

from radar import presence_level as pl

PLACES = [{"label": "Стол", "x": 8.0, "z": 4.0}, {"label": "Диван", "x": 9.8, "z": 5.3}, {"label": "Кровать", "x": 4.8, "z": 0.9}]
KITCHEN = {"label": "кухня", "x": 11.0, "z": 7.0}


@pytest.mark.parametrize("probs, places, kwargs, expected", [
    # лучшие места получают доли в сумме 1, слабое отброшено
    ({"стол": 0.6, "диван": 0.3, "кровать": 0.1}, PLACES, {},
     [(8.0, 4.0, 0.6 / 0.9, "стол"), (9.8, 5.3, 0.3 / 0.9, "диван")]),
    # остаются только top мест
    ({"стол": 0.4, "диван": 0.3, "кровать": 0.2, "кухня": 0.1}, PLACES + [KITCHEN], {"top": 2},
     [(8.0, 4.0, 0.4 / 0.7, "стол"), (9.8, 5.3, 0.3 / 0.7, "диван")]),
    # большая часть вероятности на местах без координат - ответа нет
    ({"стол": 0.3, "без координат": 0.5, "ещё": 0.2}, PLACES, {}, []),
    ({}, PLACES, {}, []),
    ({"стол": 1.0}, [], {}, []),
])
def test_level_spots_from_place_probabilities(probs, places, kwargs, expected):
    got = pl.spots(probs, places, **kwargs)
    assert [(x, z, label) for x, z, _, label in got] == [(x, z, label) for x, z, _, label in expected]
    assert [q for _, _, q, _ in got] == pytest.approx([q for _, _, q, _ in expected])
