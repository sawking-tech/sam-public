"""Субъект потока положения (SAM-118): карта предпочтений один раз на выходе, человек тянется к телефону, кот к человеку, стена не обрывает связь совсем.
Мир выдуманный: два ряда по пять клеток через метр, между рядами стена; ряд A это z = 0, ряд B это z = 1."""
from datetime import datetime

import numpy as np
import pytest

from radar import presence_motion as pm
from radar import presence_near as pn
from radar import presence_preference as pp
from radar.presence_report import PlaceReport
from radar import presence_rings as pr
from radar import presence_subject as ps
from radar import presence_window as pw_
from radar import presence_world as pw

NOW = datetime(2026, 1, 5, 12, 0)
STEP_M = 0.3
ROW = 5
EXPECTED_DB = np.array([-40.0 - 3.0 * (i % ROW) for i in range(2 * ROW)])      # уровень падает по ряду, ряды одинаковые: по одному уровню ряд не различить


class TwoRowsNavigation:
    def stand_nodes(self, step_m):
        return [(x * step_m, z) for z in (0.0, 1.0) for x in range(ROW)]

    def segment_free(self, a, b):
        return (a[1] < 0.5) == (b[1] < 0.5)

    def snap(self, x, z):
        return (x, z)


class RowHint:
    """Свидетельство, которое различает ряды: по одному уровню их не различить."""

    def __init__(self, world, row_z):
        self._field = np.array([0.0 if world.point_of(i)[1] == row_z else -6.0 for i in range(len(world))])

    def log_field(self, context):
        return self._field


@pytest.fixture(scope="module")
def world():
    return pw.World(TwoRowsNavigation(), step_m=STEP_M)


def make_subject(world, name, evidence, output=(), size=4):
    window = pw_.Window(world, pm.Motion(locality_m=0.7, jump_share=0.0), evidence, size=size, start_certainty=0.5)
    return ps.Subject(name, window, output, samples=200, rng=np.random.default_rng(1))


def row_mass(world, p, z):
    return float(sum(p[i] for i in range(len(world)) if world.point_of(i)[1] == z))


def test_follower_is_drawn_to_its_leader_and_a_wall_weakens_but_keeps_the_link(world):
    phone = make_subject(world, "phone", [pr.Rings(lambda: -40.0, EXPECTED_DB, sd_db=1.0, weight=1.0)])
    person = make_subject(world, "person", [pn.NearSubject(lambda: phone.distribution, share=0.9, reach_m=0.5)])
    cat = make_subject(world, "cat", [pn.NearSubject(lambda: person.distribution, share=0.9, reach_m=0.5)])
    for subject in (phone, person, cat):                                      # ведомый шагает после ведущего
        subject.step(NOW)
    near = lambda s: float(s.distribution[[0, 1, ROW, ROW + 1]].sum())       # четыре клетки из десяти: у ровного распределения 0,4
    assert near(phone) > near(person) > near(cat) > 0.4

    phone = make_subject(world, "phone", [pr.Rings(lambda: -40.0, EXPECTED_DB, weight=1.0), RowHint(world, row_z=0.0)])
    person = make_subject(world, "person", [pn.NearSubject(lambda: phone.distribution, share=0.7, reach_m=2.0)])
    phone.step(NOW)
    person.step(NOW)
    assert row_mass(world, person.distribution, 0.0) > row_mass(world, person.distribution, 1.0) > 0.0


def test_subject_estimate_gives_ordered_lobes_places_and_a_point_per_second(world):
    shares = [lobe.share for lobe in make_subject(world, "phone", [pr.Rings(lambda: -43.0, EXPECTED_DB, weight=1.0)]).step(NOW).lobes]
    assert sum(shares) == pytest.approx(1.0) and shares == sorted(shares, reverse=True)
    report = PlaceReport(world, {"start": (0.0, 0.0), "far": (1.2, 1.0)}, radius_m=0.5)
    window = pw_.Window(world, pm.Motion(locality_m=0.7), [pr.Rings(lambda: -40.0, EXPECTED_DB, sd_db=1.0, weight=1.0)], size=4)
    subject = ps.Subject("phone", window, report=report, samples=60, rng=np.random.default_rng(2))
    estimate = subject.step(NOW)
    assert estimate.places and estimate.places[0][0] == "start"
    assert sum(p for _, p in estimate.places) <= 1.0 + 1e-9
    for _ in range(4):
        estimate = subject.step(NOW)
    points = subject.second_points(estimate)
    assert len(points) == len(subject.window.history) == 4
    assert points[-1] == estimate.point and points[0] == estimate.first_point and all(0 <= c < len(world) for c in points)


def test_output_preference_is_applied_once_and_does_not_accumulate(world):
    pref = np.array([0.5, 0.1, 0.1, 0.1, 0.2] * 2)
    pref = pref / pref.sum()
    subject = make_subject(world, "phone", [pr.Rings(lambda: -43.0, EXPECTED_DB, weight=0.5)], [pp.Preference(lambda now: pref)])
    for _ in range(6):
        subject.step(NOW)
    alpha = subject.window.history[-1]
    assert subject.distribution == pytest.approx(alpha * pref / (alpha * pref).sum(), abs=1e-9)
