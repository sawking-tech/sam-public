"""Бюджет тяжёлой работы (SAM-260): расход по окнам, ступени, решение о запуске агента."""
from datetime import datetime, timedelta

from gateway import dev_budget as db
from gateway.pricing import PriceBook

BOOK = PriceBook(cost_currency={"polza": "RUB", "openrouter": "USD"}, fx_per_usd={"USD": 1.0, "RUB": 90.0})
NOW = datetime(2026, 10, 28, 12, 0, 0)
LIMITS = db.Limits(window_rub=40, day_rub=150, week_rub=700, month_rub=3000, window_h=5)


def _row(ago_h, cost, role="worker", source="polza"):
    return {"timestamp": NOW - timedelta(hours=ago_h), "role": role, "source": source, "model": "m", "cost": cost,
            "input_tokens": 1, "output_tokens": 1}


def test_only_agent_rows_count_and_each_window_has_its_own_edge():
    rows = [_row(1, 10), _row(6, 20), _row(30, 5), _row(24 * 8, 7), _row(1, 999, role="main"), _row(2, 999, role="guard")]
    spent = db.spent_heavy(rows, BOOK, NOW, LIMITS)
    assert (spent.window, spent.day, spent.week, spent.month) == (10, 30, 35, 42)      # быт не считается нигде


def test_only_the_budget_account_is_counted():
    rows = [_row(1, 10), _row(1, 5, source="openrouter")]                                # у OpenRouter счёт пуст, это не бюджет
    assert db.spent_heavy(rows, BOOK, NOW, LIMITS).window == 10
    assert db.spent_heavy(rows, BOOK, NOW, LIMITS, sources=frozenset({"openrouter", "polza"})).window > 10


def test_light_task_sessions_do_not_take_the_window_but_heavy_ones_do():
    light = dict(_row(1, 10), session_id="task:aaaa1111")
    heavy = dict(_row(1, 7), session_id="task:bbbb2222")
    spent = db.spent_heavy([light, heavy], BOOK, NOW, LIMITS, light_sessions=frozenset({"task:aaaa1111"}))
    assert spent.window == 7 and spent.month == 7


def test_foreign_currency_is_converted_by_the_price_book_rate():
    spent = db.spent_heavy([_row(1, 0.5, source="openrouter")], BOOK, NOW, LIMITS, sources=frozenset({"openrouter"}))      # 0.5 USD = 45 RUB
    assert round(spent.window, 2) == 45.0


def test_a_call_without_a_price_is_not_counted():
    row = {"timestamp": NOW - timedelta(hours=1), "role": "worker", "source": "unknown", "model": "x",
           "input_tokens": 100, "output_tokens": 10}
    assert db.spent_heavy([row], BOOK, NOW, LIMITS).window == 0.0


def test_gate_refuses_by_window_and_says_when_it_opens():
    spent = db.spent_heavy([_row(4, 41)], BOOK, NOW, LIMITS)                           # окно кончится через 1 час
    ok, reason = db.gate(spent, NOW, LIMITS)
    assert not ok and "окно" in reason and "через 60 мин" in reason


def test_gate_checks_day_week_and_month_and_lets_a_quiet_window_through():
    assert db.gate(db.spent_heavy([_row(6, 150)], BOOK, NOW, LIMITS), NOW, LIMITS)[1].startswith("Лимит тяжёлой работы: сутки")
    assert "неделя" in db.gate(db.spent_heavy([_row(30, 700)], BOOK, NOW, LIMITS), NOW, LIMITS)[1]
    assert "месяц" in db.gate(db.spent_heavy([_row(24 * 10, 3000)], BOOK, NOW, LIMITS), NOW, LIMITS)[1]
    assert db.gate(db.spent_heavy([_row(1, 5)], BOOK, NOW, LIMITS), NOW, LIMITS) == (True, "")


def test_emergency_lifts_every_limit():
    spent = db.spent_heavy([_row(1, 9999)], BOOK, NOW, LIMITS)
    assert db.gate(spent, NOW, LIMITS, emergency=True) == (True, "")


def test_heavy_is_decided_by_the_step_estimate_not_by_words():
    """Слова задачу не классифицируют (SAM-258): тяжёлая - прикидка больше порога лёгкой работы; прикидки нет - пул
    по умолчанию, тяжёлая."""
    assert db.is_heavy("найди новости", steps=40)
    assert not db.is_heavy("Сделай рефакторинг модуля учёта", steps=5)           # слово «рефактор» больше не решает
    assert not db.is_heavy("выключи свет на кухне", steps=2)
    assert db.is_heavy("выключи свет на кухне")                                  # без прикидки - пул по умолчанию


def test_stage_follows_the_month_remainder():
    assert db.stage(0, LIMITS) == db.NORMAL
    assert db.stage(3000 * 0.70, LIMITS) == db.SAVING          # осталось 30% < 35%
    assert db.stage(3000 * 0.90, LIMITS) == db.MINIMAL         # осталось 10% < 15%
    assert db.stage(3000, LIMITS) == db.MINIMAL


def test_floor_by_account_balance_beats_the_month_shares():
    assert db.target_stage(0, 100.0, LIMITS) == db.FLOOR           # на счёте меньше пола
    assert db.target_stage(0, 4000.0, LIMITS) == db.NORMAL
    assert db.target_stage(3000 * 0.7, None, LIMITS) == db.SAVING  # остатка счёта не знаем: по долям месяца


def test_stage_falls_at_once_and_rises_one_step_after_the_hold():
    st = db.Stager(hold_min=30)
    assert st.update(db.MINIMAL, NOW) == (db.MINIMAL, True)                      # вниз сразу, через ступень
    assert st.update(db.NORMAL, NOW + timedelta(minutes=10)) == (db.MINIMAL, False)    # цифры хороши, выдержка не прошла
    assert st.update(db.NORMAL, NOW + timedelta(minutes=41)) == (db.SAVING, True)      # 30 мин от первого хорошего замера: на шаг
    assert st.update(db.NORMAL, NOW + timedelta(minutes=45)) == (db.SAVING, False)     # выдержка отсчитывается заново
    assert st.update(db.NORMAL, NOW + timedelta(minutes=76)) == (db.NORMAL, True)
    assert st.update(db.SAVING, NOW + timedelta(minutes=80)) == (db.SAVING, True)      # снова хуже: сразу


def test_gate_stops_heavy_work_on_minimal_and_floor_but_emergency_lifts_it():
    spent = db.spent_heavy([], BOOK, NOW, LIMITS)
    for current in (db.MINIMAL, db.FLOOR):
        ok, reason = db.gate(spent, NOW, LIMITS, current=current)
        assert not ok and current in reason and "в очередь" in reason
    assert db.gate(spent, NOW, LIMITS, current=db.SAVING) == (True, "")
    assert db.gate(spent, NOW, LIMITS, emergency=True, current=db.FLOOR) == (True, "")


def test_total_monthly_cap_includes_household_and_turns_on_the_floor():
    rows = [_row(1, 3000, role="main"), _row(2, 1600), _row(30, 5, role="guard")]        # быт 3005 + агенты 1600
    total = db.spent_all_month(rows, BOOK, NOW)
    assert total == 3000 + 1600 + 5
    assert db.target_stage(0, None, LIMITS, total_spent=total) == db.FLOOR              # осталось меньше пола 500 от 5000
    assert db.target_stage(0, None, LIMITS, total_spent=1000) == db.NORMAL


def test_hour_limit_stops_a_burst_inside_the_five_hour_window():
    spent = db.spent_heavy([_row(0.5, 16)], BOOK, NOW, LIMITS)              # 16 ₽ за полчаса: окно 40 не тронуто, час 15 выбит
    ok, reason = db.gate(spent, NOW, LIMITS)
    assert not ok and "час" in reason and "через 30 мин" in reason
    after = db.spent_heavy([_row(1.1, 16)], BOOK, NOW, LIMITS)              # трата старше часа: час свободен
    assert db.gate(after, NOW, LIMITS) == (True, "")


def test_week_limit_is_off_by_default_and_works_when_set():
    assert db.WEEK_RUB == 0.0
    off = db.Limits(week_rub=0)
    spent_off = db.spent_heavy([_row(30, 100), _row(60, 100), _row(100, 100), _row(140, 100)], BOOK, NOW, off)
    assert db.gate(spent_off, NOW, off)[0]
    on = db.Limits(week_rub=300, window_rub=1000, hour_rub=1000, day_rub=1000, month_rub=10000)
    assert "неделя" in db.gate(db.spent_heavy([_row(30, 150), _row(60, 160)], BOOK, NOW, on), NOW, on)[1]


def test_gate_names_the_limit_that_opens_last():
    """Закрыты и час, и окно: отказ называет окно - очередь ждёт, пока откроются оба."""
    limits = db.Limits(hour_rub=15, window_rub=40, day_rub=150, week_rub=0, month_rub=3000, window_h=5)
    spent = db.spent_heavy([_row(0.5, 20), _row(4, 25)], BOOK, NOW, limits)   # час 20 из 15, окно 45 из 40
    ok, reason = db.gate(spent, NOW, limits)
    assert not ok and "окно 45 из 40" in reason and "через 60 мин" in reason
