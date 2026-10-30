"""Выбор положения выборкой (SAM-118): частоты по вероятностям, липкость и сброс при отвергнутом месте. Случайность - сидированный random.Random."""
import random

from radar.presence_pick import StickySampler

POSTERIOR = {"диван": 0.6, "кровать": 0.3, "стол": 0.1}
CROWDED = {"туалет": 0.57, "диван": 0.18, "кровать": 0.25}
DRAWS = 3000


def test_sampler_frequencies_follow_posterior_and_many_draws_steady():
    sampler = StickySampler(random.Random(7), min_hold_s=0.0, switch_ratio=0.0, draws=1)
    counts = {p: 0 for p in POSTERIOR}
    for i in range(DRAWS):
        counts[sampler.pick("phone", POSTERIOR, float(i))] += 1
    shares = {p: c / DRAWS for p, c in counts.items()}
    assert all(abs(shares[p] - q) < 0.04 for p, q in POSTERIOR.items())
    assert shares["кровать"] > 0.2                                               # вершину не берём каждый раз
    # одна частица прыгает между местами, двести частиц всегда дают самое населённое место
    one = StickySampler(random.Random(11), min_hold_s=0.0, switch_ratio=0.0, draws=1)
    many = StickySampler(random.Random(11), min_hold_s=0.0, switch_ratio=0.0, draws=200)
    assert len({one.pick("p", CROWDED, float(t)) for t in range(300)}) > 1
    assert {many.pick("p", CROWDED, float(t)) for t in range(300)} == {"туалет"}


def test_sampler_holds_place_until_rejected_or_much_likelier():
    # выдержка: до трёх секунд выбор держится, после - может смениться
    held = StickySampler(random.Random(3), min_hold_s=3.0, switch_ratio=0.0, draws=1)
    first = held.pick("phone", POSTERIOR, 0.0)
    assert {held.pick("phone", POSTERIOR, t / 10) for t in range(1, 30)} == {first}
    assert len({held.pick("phone", POSTERIOR, 3.0 + i * 3.0) for i in range(60)}) > 1
    # отвергнутое место (вероятность ниже порога) бросается сразу, пустое распределение - ничего
    rejected = StickySampler(random.Random(1), min_hold_s=100.0)
    rejected.pick("phone", {"стол": 1.0}, 0.0)
    assert rejected.pick("phone", {"кровать": 0.97, "стол": 0.03}, 1.0) == "кровать"
    assert rejected.pick("phone", {}, 2.0) is None
    # без выдержки держит отношение вероятностей: на 0,18 и 0,25 против 0,57 не уходит, на в восемь раз вероятнее - уходит
    ratio = StickySampler(random.Random(5), min_hold_s=0.0)
    start = ratio.pick("phone", CROWDED, 0.0)
    stays = {ratio.pick("phone", CROWDED, float(t)) for t in range(1, 400)}
    assert stays <= {start, "туалет"} and len(stays) <= 2
    assert ratio.pick("phone", {"туалет": 0.1, "диван": 0.8, "кровать": 0.1}, 500.0) == "диван"
