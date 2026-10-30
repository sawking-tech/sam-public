"""Оценка агента таблицей блоков (SAM-257): число без таблицы не принимается, пул = сумма с запасом."""
import pytest

from gateway import agent_budget as ab

TABLE = [{"block": "прочитать входы", "steps": 2}, {"block": "модуль", "steps": 3}, {"block": "тесты", "steps": 3},
         {"block": "коммит", "steps": 1}]


def test_default_reserve_is_twenty_five_percent():
    assert ab.ESTIMATE_RESERVE == 0.25 and ab.table_total(TABLE) == (12, "")        # 9 + 25% = 11.25 -> 12


def test_table_total_adds_the_reserve_rounded_up():
    assert ab.table_total(TABLE, reserve=0.10) == (10, "")           # 9 + 10% = 9.9 -> 10
    assert ab.table_total(TABLE, reserve=0.0) == (9, "")
    assert ab.table_total([{"block": "a", "steps": 10}], reserve=0.15) == (12, "")   # 11.5 -> 12


@pytest.mark.parametrize("plan, expected", [
    (7, ab.NEED_TABLE), ([], ab.NEED_TABLE), (None, ab.NEED_TABLE), ("7", ab.NEED_TABLE),
], ids=["number", "empty", "none", "string"])
def test_a_bare_number_or_no_table_is_rejected_with_an_example(plan, expected):
    assert ab.table_total(plan) == (0, expected) and "estimate_steps(plan=" in expected


@pytest.mark.parametrize("plan", [
    [{"block": "", "steps": 2}], [{"block": "x", "steps": 0}], [{"block": "x", "steps": "много"}], ["x"],
], ids=["no-name", "zero", "not-a-number", "not-a-row"])
def test_a_broken_block_is_named_in_the_error(plan):
    total, error = ab.table_total(plan)
    assert total == 0 and error.startswith("Блок 1 таблицы")


def test_too_many_blocks_are_rejected():
    many = [{"block": f"b{n}", "steps": 1} for n in range(ab.MAX_BLOCKS + 1)]
    assert ab.table_total(many) == (0, ab.TOO_MANY_BLOCKS.format(limit=ab.MAX_BLOCKS))


def _account(creator_steps, agent_total, done=0):
    budgets = ab.Budgets()
    budgets.open("t1", creator_steps)
    for _ in range(done + 1):
        budgets.spend("t1")
    budgets.estimate("t1", agent_total - done)
    return budgets


def test_request_inside_the_creator_ceiling_is_granted_without_asking():
    budgets = _account(creator_steps=7, agent_total=3)          # старт 3, потолок 7
    ok, text = budgets.autogrant("t1", 2)
    assert ok and "выдано 2" in text
    assert budgets.left("t1") == 4                               # пул вырос с 3 до 5, один шаг потрачен


def test_pool_grows_to_the_ceiling_and_not_beyond():
    budgets = _account(creator_steps=7, agent_total=3)
    assert budgets.autogrant("t1", 2)[0] and budgets.autogrant("t1", 2)[0]      # 3 + 2 + 2 = 7
    ok, text = budgets.autogrant("t1", 1)
    assert not ok and "потолок 7" in text and "Работа встаёт" in text


def test_the_agent_was_right_when_it_estimated_more_than_the_creator():
    budgets = _account(creator_steps=3, agent_total=5)          # старт 3, потолок 5
    assert budgets.autogrant("t1", 2)[0]
    assert not budgets.autogrant("t1", 1)[0]
