"""Трекинг радара присутствия (SAM-105): габариты, человек или питомец, профили, стабильные id, не терять из виду."""
import pytest

from radar import presence as pr
from radar import presence_track as tk


def _m(height=1.75, width=0.44, x=1.0, z=1.0) -> tk.Measure:
    return tk.Measure(height, width, x, z)


TALL = tk.Profile("tall", tk.PERSON, 1.85, 0.50)
SHORT = tk.Profile("short", tk.PERSON, 1.66, 0.42)
PET = tk.Profile("pet", tk.PET, 0.28, 0.45)
ALL = [TALL, SHORT, PET]


def test_measure_reads_height_width_and_floor_center_from_the_cloud():
    got = tk.measure(pr.body_points(2.0, 3.0))
    assert 1.7 < got.height <= pr.BODY_HEIGHT_M
    assert got.width <= 2 * pr.BODY_RADIUS_M + 1e-9
    assert (round(got.x, 1), round(got.z, 1)) == (2.0, 3.0)


@pytest.mark.parametrize("measure, kind, profiles, profile", [
    (_m(1.84, 0.49), tk.PERSON, ALL, TALL),
    (_m(1.67, 0.43), tk.PERSON, ALL, SHORT),
    (_m(0.3, 0.5), tk.PET, ALL, PET),                                 # кошка
    (_m(1.75, 0.46), tk.PERSON, ALL, None),                           # посередине между профилями: гость
    (_m(1.85, 0.50), tk.PERSON, [PET], None),                         # профиль другого вида не подходит
    (_m(0.05), None, ALL, None),                                      # шум ниже питомца
    (_m(2.6), None, ALL, None),                                       # выше человека
])
def test_measure_classifies_and_matches_profile(measure, kind, profiles, profile):
    """Габарит решает, человек это, питомец или шум, а профиль в допуске того же вида - кто именно, иначе гость."""
    assert measure.kind == kind
    if kind is not None:
        assert tk.match_profile(measure, profiles) is profile


def test_profile_learns_from_confirmed_sightings():
    p = tk.Profile("tall", tk.PERSON, 1.80, 0.50)
    for _ in range(30):
        p.enroll(_m(1.86, 0.52))
    assert p.height == pytest.approx(1.86, abs=0.01) and p.seen == 31


def _walk(tracker):
    return len({tracker.update([_m(x=1.0 + 0.2 * i)], now=0.2 * i)[0].tid for i in range(10)}) == 1


def _crowd(tracker):
    frame = lambda t: [_m(1.85, 0.5, 1.0 + t, 1.0), _m(1.66, 0.42, 1.6 + t, 1.0), _m(0.3, 0.45, 1.3 + t, 1.2)]
    first = {t.tid: t.kind for t in tracker.update(frame(0.0), 0.0)}
    later = {t.tid: t.kind for t in tracker.update(frame(0.3), 0.2)}
    return first == later and sorted(first.values()) == [tk.PERSON, tk.PERSON, tk.PET]


def _lost_and_found(tracker):
    tid = tracker.update([_m(x=1.0)], 0.0)[0].tid
    unseen = tracker.update([], 3.0) == []                            # три секунды не видели
    return unseen and tracker.update([_m(x=3.0)], 4.0)[0].tid == tid  # дошла за 4 с: на 2 м


def _far_jump_and_expiry(tracker):
    tid = tracker.update([_m(x=0.0)], 0.0)[0].tid
    new_track = tracker.update([_m(x=20.0)], 1.0)[0].tid != tid      # за секунду на 20 м не бывает
    tracker.update([], 100.0)
    return new_track and tracker.tracks == {}


def _other_size(tracker):
    adult = tracker.update([_m(1.85, 0.5, 1.0, 1.0)], 0.0)[0].tid
    return tracker.update([_m(1.30, 0.35, 1.1, 1.0)], 0.2)[0].tid != adult


@pytest.mark.parametrize("scenario", [_walk, _crowd, _lost_and_found, _far_jump_and_expiry, _other_size],
                         ids=["walk", "crowd", "lost", "far_jump", "other_size"])
def test_tracker_keeps_ids_through_walk_crowd_and_loss(scenario):
    """id живёт при ходьбе, рядом с другими и после потери в окне; далёкий скачок и чужой габарит дают новый трек, старые истекают."""
    assert scenario(tk.Tracker())
