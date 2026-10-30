"""Окно путей (SAM-118): контроль перебором всех путей на малом мире, память окна, сужение области по срезам. Миры выдуманные: коридор из пяти клеток и два ряда по три
клетки через метр, между рядами стена. Свидетельства подаются готовыми полями, чтобы проверять окно отдельно от источников."""
import itertools
from datetime import datetime

import numpy as np
import pytest

from radar import presence_motion as pm
from radar import presence_window as pw_
from radar import presence_world as pw

NOW = datetime(2026, 1, 5, 12, 0)
STEP_M = 0.3
RADIUS_M = 0.7           # у коридора с шагом 0,3 м это соседи через клетку


class CorridorNavigation:
    def stand_nodes(self, step_m):
        return [(x * step_m, 0.0) for x in range(5)]

    def segment_free(self, a, b):
        return True

    def snap(self, x, z):
        return (x, z)


class TwoRowsNavigation(CorridorNavigation):
    def stand_nodes(self, step_m):
        return [(x * step_m, z) for z in (0.0, 1.0) for x in range(3)]

    def segment_free(self, a, b):
        return (a[1] < 0.5) == (b[1] < 0.5)


class Feed:
    """Свидетельство, которое тест подменяет перед каждым шагом."""

    def __init__(self):
        self.field = None

    def log_field(self, context):
        return self.field


def make_window(world, size, certainty=0.0, radius=RADIUS_M):
    feed = Feed()
    return pw_.Window(world, pm.Motion(locality_m=radius, jump_share=0.0), [feed], size=size, start_certainty=certainty), feed


def run(window, feed, fields):
    result = None
    for field in fields:
        feed.field = np.asarray(field, dtype=float)
        result = window.step(NOW)
    return result


def enumerate_paths(world, fields, radius=RADIUS_M):
    """Контроль: перебор всех последовательностей клеток по срезам; переход допустим по радиусу и свободному отрезку, масса клетки делится по достижимым."""
    reach = world.reach(radius).toarray()
    degree = reach.sum(axis=1)
    total = np.zeros(len(world))
    for path in itertools.product(range(len(world)), repeat=len(fields)):
        weight = np.exp(fields[0][path[0]])
        for k in range(1, len(path)):
            if reach[path[k - 1], path[k]] == 0:
                weight = 0.0
                break
            weight *= np.exp(fields[k][path[k]]) / degree[path[k - 1]]
        total[path[-1]] += weight
    return total / total.sum()


@pytest.fixture(scope="module")
def corridor():
    return pw.World(CorridorNavigation(), step_m=STEP_M)


@pytest.fixture(scope="module")
def two_rows():
    return pw.World(TwoRowsNavigation(), step_m=STEP_M)


def peak_at(world, cell, strength=2.0):
    field = np.zeros(len(world))
    field[cell] = strength
    return field


@pytest.mark.parametrize("world_name, size, radius, seed", [
    ("corridor", 4, RADIUS_M, 1), ("corridor", 4, RADIUS_M, 2), ("corridor", 4, RADIUS_M, 3),
    ("two_rows", 3, 1.5, 1), ("two_rows", 3, 1.5, 2),
])
def test_window_equals_path_enumeration(request, world_name, size, radius, seed):
    """Окно совпадает с перебором всех путей: на коридоре и в двух рядах за стеной."""
    world = request.getfixturevalue(world_name)
    rng = np.random.default_rng(seed)
    fields = [rng.normal(size=len(world)) for _ in range(size)]
    window, feed = make_window(world, size=size, radius=radius)
    assert run(window, feed, fields) == pytest.approx(enumerate_paths(world, fields, radius=radius), abs=1e-9)


def test_evidence_does_not_jump_over_wall(two_rows):
    """Пик свидетельства в одном ряду, затем в другом; радиус позволяет, стена нет. Без стены масса идёт за пиками, за стеной они делят её."""
    class OpenRows(TwoRowsNavigation):
        def segment_free(self, a, b):
            return True

    open_rows = pw.World(OpenRows(), step_m=STEP_M)
    first, second = 0, 3
    fields = [peak_at(two_rows, first, 5.0), peak_at(two_rows, second, 5.0)]
    walled_window, walled_feed = make_window(two_rows, size=2, radius=1.5)
    open_window, open_feed = make_window(open_rows, size=2, radius=1.5)
    walled, opened = run(walled_window, walled_feed, fields), run(open_window, open_feed, fields)
    assert walled[second] < 0.7 * opened[second]


def test_certainty_controls_memory(corridor):
    """Уверенность 1 - обычный рекурсивный фильтр, 0 - старое забыто; повтор среза сужает распределение, в истории только срезы окна;
    плохие параметры отвергнуты одной строкой."""
    rng = np.random.default_rng(5)
    fields = [rng.normal(size=len(corridor)) for _ in range(9)]
    windowed, feed = make_window(corridor, size=3, certainty=1.0)
    plain, plain_feed = make_window(corridor, size=len(fields), certainty=1.0)
    assert run(windowed, feed, fields) == pytest.approx(run(plain, plain_feed, fields), abs=1e-9)

    recent = [rng.normal(size=len(corridor)) for _ in range(3)]
    first, first_feed = make_window(corridor, size=3, certainty=0.0)
    second, second_feed = make_window(corridor, size=3, certainty=0.0)
    old_a, old_b = [peak_at(corridor, 0, 4.0)] * 5, [peak_at(corridor, 4, 4.0)] * 5
    assert run(first, first_feed, old_a + recent) == pytest.approx(run(second, second_feed, old_b + recent), abs=1e-9)

    entropy = lambda p: float(-(p * np.log(np.maximum(p, 1e-300))).sum())
    one_window, one_feed = make_window(corridor, size=6, certainty=0.0)
    many_window, many_feed = make_window(corridor, size=6, certainty=0.0)
    one = run(one_window, one_feed, [0.5 * peak_at(corridor, 2)])
    many = run(many_window, many_feed, [0.5 * peak_at(corridor, 2)] * 6)
    assert entropy(many) < entropy(one)

    window, feed = make_window(corridor, size=3)
    run(window, feed, [np.zeros(len(corridor))] * 7)
    assert len(window.history) == 3 and window.history[-1] == pytest.approx(np.full(len(corridor), 1 / len(corridor)), abs=0.2)

    with pytest.raises(ValueError, match="срезов нужно не меньше одного"):
        make_window(corridor, size=0)
    with pytest.raises(ValueError, match="уверенность от нуля до единицы"):
        make_window(corridor, size=3, certainty=1.5)


def test_sampled_paths_legal_and_follow_distribution(two_rows, corridor):
    """Выбранные пути не идут через стену и не рвут радиус; концы следуют последнему срезу или заданному концу."""
    window, feed = make_window(two_rows, size=4, certainty=0.5, radius=1.5)
    rng = np.random.default_rng(3)
    run(window, feed, [rng.normal(size=len(two_rows)) for _ in range(6)])
    paths = window.sample_paths(300, rng=np.random.default_rng(4))
    reach = two_rows.reach(1.5).toarray()
    assert paths.shape == (300, 4)
    assert all(reach[a, b] == 1 for row in paths for a, b in zip(row, row[1:]))

    window, feed = make_window(corridor, size=3, certainty=0.0)
    last = run(window, feed, [np.array([0.0, 0.5, 1.0, 0.5, 0.0])] * 3)
    ends = window.sample_paths(4000, rng=np.random.default_rng(5))[:, -1]
    assert np.bincount(ends, minlength=len(corridor)) / 4000 == pytest.approx(last, abs=0.04)

    end = np.zeros(len(corridor))
    end[1] = 1.0
    assert (window.sample_paths(50, end=end, rng=np.random.default_rng(6))[:, -1] == 1).all()


def test_smoothed_and_progress_narrow_to_legal_paths(two_rows, corridor):
    """Сглаживание исключает клетки, с которых нет законного пути к концу, и совпадает с выборкой путей; прогресс по секундам
    сходится к сглаживанию всего окна; части свидетельств и переход для слоя нормированы."""
    window, feed = make_window(two_rows, size=3, certainty=0.5, radius=1.5)
    both = np.zeros(len(two_rows))
    run(window, feed, [both, both, both])
    end = np.zeros(len(two_rows))
    end[1] = 1.0                                                                      # конец в первом ряду
    gammas = window.smoothed(end)
    other_row = [i for i in range(len(two_rows)) if two_rows.point_of(i)[1] > 0.5]
    assert all(g[other_row].sum() < 1e-12 and g.sum() == pytest.approx(1.0) for g in gammas)
    paths = window.sample_paths(6000, end=end, rng=np.random.default_rng(8))
    for k, gamma in enumerate(gammas):
        assert np.bincount(paths[:, k], minlength=len(two_rows)) / 6000 == pytest.approx(gamma, abs=0.03)

    window, feed = make_window(two_rows, size=4, certainty=0.5, radius=1.5)
    rng = np.random.default_rng(11)
    run(window, feed, [rng.normal(size=len(two_rows)) for _ in range(6)])
    steps = window.progress()
    assert [len(step) for step in steps] == [1, 2, 3, 4]
    assert all(g.sum() == pytest.approx(1.0) for step in steps for g in step)
    assert steps[0][0] == pytest.approx(window.history[0]) and steps[-1][-1] == pytest.approx(window.history[-1])         # конец каждого набора - фильтр этого среза
    first_support = [int((step[0] > 1e-12).sum()) for step in steps]
    assert first_support == sorted(first_support, reverse=True)                                                            # чем больше секунд, тем меньше клеток на законных путях
    assert all(np.allclose(a, b, atol=1e-12) for a, b in zip(steps[-1], window.smoothed()))

    window, feed = make_window(corridor, size=3, certainty=0.0)
    run(window, feed, [np.array([0.0, 1.0, 2.0, 1.0, 0.0])] * 5)
    parts = window.evidence_history
    assert len(parts) == 3 and all(p["feed"].sum() == pytest.approx(1.0) for p in parts)
    assert window.transition is not None and window.transition.sum() == pytest.approx(1.0)
