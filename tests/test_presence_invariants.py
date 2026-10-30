"""Инварианты перемещения людей (SAM-118): на случайных сценариях слов, со всеми местами плана, ни один кадр не ведёт человека сквозь стену, дальше одного шага
или гоняет его между двумя местами при равных свидетельствах. План выдуманный (tests/fakes_presence.two_room_layout), часы внедрены."""
import random

import numpy as np
import pytest

from radar import presence_continuity as pc
from radar import presence_fuse as fz
from radar import presence_hub as ph
from radar import presence_radio_map as rm
from tests.fakes_presence import two_room_layout

FRAME_S = 0.2
SCENARIOS = 6
SCENARIO_S = 240
WORDS_EVERY_S = 8
RESIDENTS = {"p1": {"name": "первый"}, "p2": {"name": "второй"}}


@pytest.fixture(scope="module")
def layout():
    return two_room_layout(RESIDENTS)


@pytest.mark.parametrize("seed", range(SCENARIOS))
def test_random_told_places_never_walk_a_person_through_a_wall_or_jump(layout, seed):
    rng = random.Random(seed)
    walls, doors = layout["walls"], rm.doorway_openings(layout["objects"])
    now = [50_000.0]
    hub = ph.PresenceHub(ph.MODE_RSSI, layout, clock=lambda: now[0], rng=random.Random(seed))
    hub.set_real(True)
    places = [p["label"].lower() for p in layout["places"]] + ["кровать", "диван"]
    last: dict[str, tuple[float, float]] = {}
    placed = set()
    for step in range(int(SCENARIO_S / FRAME_S)):
        now[0] += FRAME_S
        if step % int(WORDS_EVERY_S / FRAME_S) == 0:
            hub.note_message("told", None, who=rng.choice(list(RESIDENTS)), place=rng.choice(places), now=now[0])
        for row in hub.identities(now[0]):
            if row.get("x") is None:
                continue
            here = (row["x"], row["z"])
            placed.add(row["person"])
            if row["person"] in last:
                a = last[row["person"]]
                assert not rm.crosses_wall(walls, a, here, doors), f"seed {seed} {row['person']} сквозь стену {a} -> {here}"
                assert np.hypot(here[0] - a[0], here[1] - a[1]) <= pc.MAX_STEP_M + 1e-6, f"seed {seed} {row['person']} прыжок {a} -> {here}"
            last[row["person"]] = here
    assert placed, "сценарий никого не поставил на план: инвариант ничего не проверил"


def test_near_equal_evidence_does_not_flip_the_place():
    sofa = dict(source="messages", person="p1", t=0.0, x=9.8, z=5.3, pose="sit")
    toilet_x, toilet_z = 7.7, 7.5
    previous: dict[str, tuple[float, float]] = {}
    switches, current = 0, None
    for k in range(300):                                                               # вес уровня гуляет 0,4 - 0,7, слова на диване 0,6
        wifi_weight = 0.55 + 0.15 * (1 if (k // 3) % 2 else -1)
        evidence = [fz.Evidence(weight=0.6, **sofa), fz.Evidence(source="wifi", person="p1", weight=wifi_weight, t=0.0, x=toilet_x, z=toilet_z)]
        [est] = fz.fuse(evidence, 0.0, previous=previous)
        previous = {"p1": (est.x, est.z)}
        if current is not None and (est.x, est.z) != current:
            switches += 1
        current = (est.x, est.z)
    assert switches <= 1


def test_continuity_holds_a_person_against_a_wall_or_a_far_jump():
    walls = [{"x1": 5.0, "z1": 0.0, "x2": 5.0, "z2": 10.0, "t": 0.1}]
    guard = pc.Continuity(lambda a, b: rm.crosses_wall(walls, a, b), report=lambda text: None)
    guard.apply([{"person": "p", "x": 4.0, "z": 5.0}], 0.0)
    through = guard.apply([{"person": "p", "x": 6.0, "z": 5.0}], 0.2)[0]
    assert (through["x"], through["z"]) == (4.0, 5.0) and through[pc.BLOCKED_KEY]
    far = guard.apply([{"person": "p", "x": 4.0, "z": 12.0}], 0.4)[0]
    assert (far["x"], far["z"]) == (4.0, 5.0)
    ok = guard.apply([{"person": "p", "x": 4.5, "z": 6.0}], 0.6)[0]
    assert (ok["x"], ok["z"]) == (4.5, 6.0)
    back = guard.apply([{"person": "p", "x": 6.0, "z": 5.0}], 100.0)[0]                # давно не было: появился на новом месте
    assert (back["x"], back["z"]) == (6.0, 5.0)
