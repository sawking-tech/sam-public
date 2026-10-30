"""Препятствие впереди идущего (SAM-105): шаговая поза не показывается, если шагнуть некуда."""
import math

import pytest

from radar import presence_ahead as pa


def _row(person, x, z, face=0.0, walking=True):
    return {"person": person, "x": x, "z": z, "face": face, "walking": walking}


@pytest.mark.parametrize("rows, way_free, expected", [
    ([_row("a", 1.0, 1.0)], True, {"a": False}),                                          # путь свободен
    ([_row("a", 1.0, 1.0)], False, {"a": True}),                                          # впереди стена или предмет
    ([_row("a", 0.0, 0.0), _row("b", 0.8, 0.1, walking=False)], True, {"a": True, "b": False}),    # человек на пути в метре
    ([_row("a", 0.0, 0.0), _row("b", -0.8, 0.0, walking=False)], True, {"a": False, "b": False}),  # сзади
    ([_row("a", 0.0, 0.0), _row("b", 2.5, 0.0, walking=False)], True, {"a": False, "b": False}),   # далеко
    ([_row("a", 0.0, 0.0), _row("b", 0.3, 0.9, walking=False)], True, {"a": False, "b": False}),   # сбоку, вне конуса
    ([_row("a", 0.0, 0.0, face=math.pi / 2), _row("b", 0.1, 0.9, walking=False)], True, {"a": True, "b": False}),
    ([_row("a", 1.0, 1.0, walking=False), _row("b", 5.0, 5.0, face=None)], False, {"a": False, "b": False}),  # стоит или без взгляда
])
def test_walking_person_is_blocked_by_a_wall_object_or_person_ahead(rows, way_free, expected):
    got = pa.mark_blocked(rows, lambda a, b: way_free)
    assert {r["person"]: r.get("blocked_ahead", False) for r in got} == expected
