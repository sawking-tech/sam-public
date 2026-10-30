"""Источник случайности радара (SAM-118): один сид, потоки по именам, повтор прогона."""
import pytest

from radar.presence_random import SEED_ENV, Randomness


def draws(generator, n=5):
    return [float(x) for x in generator.random(n)]


def test_randomness_replays_a_run_by_seed_with_independent_named_streams():
    """Один сид и имя дают те же числа (numpy и python), имена и сиды различаются, порядок запроса потоков не влияет, сид без явного задания повторяет прогон."""
    assert draws(Randomness(7).numpy("flow")) == draws(Randomness(7).numpy("flow"))
    assert draws(Randomness(7).numpy("flow")) != draws(Randomness(7).numpy("sampler"))
    assert draws(Randomness(7).numpy("flow")) != draws(Randomness(8).numpy("flow"))
    first = Randomness(3)
    first.numpy("a"), first.python("b")
    assert draws(first.numpy("flow")) == draws(Randomness(3).numpy("flow"))
    assert [Randomness(5).python("x").random() for _ in range(3)] == [Randomness(5).python("x").random() for _ in range(3)]
    assert Randomness(5).python("x").random() != Randomness(5).python("y").random()
    run = Randomness()
    assert draws(Randomness(run.seed).numpy("flow")) == draws(run.numpy("flow"))
    assert Randomness().seed != run.seed


@pytest.mark.parametrize("value, seed", [("42", 42), ("-1", None), ("abc", None), (None, None)])
def test_seed_comes_from_the_environment_and_garbage_means_entropy(value, seed):
    """Сид - из одной переменной окружения; мусор, отрицательный или нет переменной - сид от системы."""
    got = Randomness.from_environ({} if value is None else {SEED_ENV: value}).seed
    assert got == seed if seed is not None else got >= 0
