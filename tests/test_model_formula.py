"""Формула выбора модели агенту (SAM-258): факты вместо догадок, доля бюджета, inside дороже, повтор - к следующей."""
from gateway import model_learning, task_class
from gateway.model_choice import MIN_P, choose
from gateway.model_profiles import ModelProfile
from gateway.pricing import Price, PriceBook

WEAK = ModelProfile(tier=1, local=False, tools=True, context=1_000_000)
MID = ModelProfile(tier=2, local=False, tools=True, context=1_000_000)
STRONG = ModelProfile(tier=3, local=False, tools=True, context=1_000_000)
PROFILES = {"polza/glm": WEAK, "polza/deepseek": MID, "polza/terra": STRONG}
BOOK = PriceBook(prices={"polza/glm": Price(3, 3, "RUB"), "polza/deepseek": Price(5, 10, "RUB"),
                         "polza/terra": Price(57, 285, "RUB")}, fx_per_usd={"USD": 1.0, "RUB": 90.0})
ALL = lambda source, model: True  # noqa: E731


def _runs(model, kind, n, status="done", env="", task_id="", steps=None, spent=None):
    return [{"source": "polza", "model": model, "kind": kind, "env": env, "status": status, "finished_at": i,
             "task_id": task_id, "steps": steps, "steps_spent": spent} for i in range(n)]


def test_without_facts_the_cheapest_model_goes_first():
    """Доказать, что модель не справится, может только её провал: без данных первой идёт самая дешёвая."""
    pick = choose(task_class.of(60, "проектировать", "outside"), PROFILES, BOOK, {}, ALL)
    assert pick.model == "glm" and "фактов нет" in pick.reason
    assert [r["model"] for r in pick.ranking] == ["glm", "deepseek", "terra"]


def test_a_model_that_fails_this_kind_loses_it_but_keeps_other_kinds():
    learned = model_learning.learn(_runs("glm", "создать", model_learning.MIN_RUNS, status="failed"))
    assert choose(task_class.of(10, "создать"), PROFILES, BOOK, {}, ALL, learned=learned).model == "deepseek"
    assert choose(task_class.of(10, "узнать"), PROFILES, BOOK, {}, ALL, learned=learned).model == "glm"


def test_facts_per_env_come_before_facts_per_kind():
    runs = (_runs("glm", "создать", model_learning.MIN_RUNS, status="failed", env="inside")
            + _runs("glm", "создать", model_learning.MIN_RUNS * 2, env="outside"))
    learned = model_learning.learn(runs)
    assert model_learning.success_p("glm", "создать", "inside", learned)[0] == 0.0
    assert model_learning.success_p("glm", "создать", "outside", learned)[0] == 1.0
    assert model_learning.success_p("glm", "создать", "", learned)[0] < 1.0     # среды нет - по виду в целом


def test_inside_never_takes_a_model_below_the_inside_tier():
    pick = choose(task_class.of(5, "исправить", task_class.INSIDE), PROFILES, BOOK, {}, ALL)
    assert pick.model == "deepseek" and "glm" not in [r["model"] for r in pick.ranking]


def test_the_most_reliable_model_that_fits_the_budget_share_wins():
    learned = model_learning.learn(_runs("glm", "исследовать", 10, status="failed")[:6]
                                   + _runs("glm", "исследовать", 4))                   # glm: 4 из 10
    task = task_class.of(20, "исследовать")
    roomy = choose(task, PROFILES, BOOK, {}, ALL, learned=learned, budget_usd=10.0)
    assert roomy.model in ("deepseek", "terra")                                        # p 1 обе, дешевле - deepseek
    assert roomy.model == "deepseek"
    tight = choose(task, PROFILES, BOOK, {}, ALL, learned=learned, budget_usd=1e-9)
    assert tight.model == "deepseek" and "не влезает никто" in tight.reason            # самая дешёвая из надёжных


def test_a_model_below_min_p_is_never_taken_even_free():
    learned = model_learning.learn(_runs("glm", "сделать", 10, status="failed"))
    pick = choose(task_class.of(2, "сделать"), PROFILES, BOOK, {}, ALL, learned=learned)
    assert pick.model != "glm" and all(r["p"] >= MIN_P for r in pick.ranking)


def test_redo_counts_as_a_failure():
    learned = model_learning.learn(_runs("glm", "создать", model_learning.MIN_RUNS, status=model_learning.REDO))
    assert model_learning.success_p("glm", "создать", "", learned)[0] == 0.0


def test_models_that_failed_this_task_are_excluded_from_its_retry():
    runs = _runs("glm", "создать", 1, status=model_learning.REDO, task_id="t1") + _runs("deepseek", "создать", 1, task_id="t1")
    assert model_learning.failed_models(runs, "t1") == frozenset({"glm"})
    assert model_learning.failed_models(runs, "t2") == frozenset()


def test_steps_factor_is_neutral_without_data_and_follows_the_facts_with_it():
    assert model_learning.steps_factor([], "исследовать") == model_learning.NEUTRAL_STEPS_FACTOR
    runs = _runs("glm", "исследовать", model_learning.MIN_RUNS, steps=10, spent=20)
    assert model_learning.steps_factor(runs, "исследовать") == 2.0
    assert model_learning.steps_factor(runs, "создать") == model_learning.NEUTRAL_STEPS_FACTOR


def test_task_class_from_steps_kind_and_env():
    task = task_class.of(None, "Создать", "INSIDE")
    assert (task.steps, task.kind, task.env) == (task_class.DEFAULT_STEPS, "создать", "inside")
    assert task_class.of(3, "мусор", "где-то").kind == "" and task_class.of(3, None, "где-то").env == ""
    assert task.in_tokens == task.steps * task_class.STEP_IN_TOKENS


def test_facts_belong_to_the_model_at_any_seller():
    """glm провалил у Polza - тот же glm у OpenRouter не идёт следом как «без фактов»."""
    profiles = {"polza/glm": WEAK, "openrouter/glm": WEAK, "polza/deepseek": MID}
    book = PriceBook(prices={"polza/glm": Price(3, 3, "RUB"), "openrouter/glm": Price(2, 2, "RUB"),
                             "polza/deepseek": Price(5, 10, "RUB")}, fx_per_usd={"USD": 1.0, "RUB": 90.0})
    learned = model_learning.learn(_runs("glm", "создать", model_learning.MIN_RUNS, status="failed"))
    assert choose(task_class.of(10, "создать"), profiles, book, {}, ALL, learned=learned).source == "polza"
    assert choose(task_class.of(10, "создать"), profiles, book, {}, ALL, learned=learned).model == "deepseek"


def test_more_effort_is_the_next_dearer_model_less_the_next_cheaper():
    from gateway.agent import LESS, MORE, step_model
    ranking = [{"model": "glm", "est_cost_usd": 0.001}, {"model": "deepseek", "est_cost_usd": 0.01},
               {"model": "terra", "est_cost_usd": 0.1}]
    assert step_model(ranking, "deepseek", MORE)["model"] == "terra"
    assert step_model(ranking, "deepseek", LESS)["model"] == "glm"
    assert step_model(ranking, "terra", MORE) is None and step_model(ranking, "glm", LESS) is None
