"""Фиксированные диалоги для integration- и quality-тестов компрессии.

Каждый сценарий это (msgs, expected_facts, episodic_phrases) — что должно
выжить в summary и что должно быть забыто.
"""

from datetime import datetime, timedelta


def _ts(minutes_ago: int):
    return datetime.utcnow() - timedelta(minutes=minutes_ago)


# Сценарий 1: смесь устойчивых фактов и эпизодики.
SCENARIO_HABITS_VS_EPISODICS = {
    "messages": [
        {"_id": f"m{i}", "role": role, "content": content, "timestamp": _ts(120 - i*2), "msg_id": f"id{i}"}
        for i, (role, content) in enumerate([
            ("user",      "Привет, SAM. Я Антон."),
            ("assistant", "Привет, Антон."),
            ("user",      "Запомни: Ирина не пьёт кофе после 18:00 — у неё бессонница потом."),
            ("assistant", "Запомнила, важная вещь."),
            ("user",      "Я сегодня ел оладьи на завтрак."),
            ("assistant", "Ок, на вкус как?"),
            ("user",      "Нормально. Слушай, давай завтра созвонимся с Ириной по поводу отпуска."),
            ("assistant", "Запомнила: завтра созвон с Ириной про отпуск."),
            ("user",      "Ага."),
            ("assistant", "Ага."),
            ("user",      "Спасибо."),
            ("assistant", "Не за что."),
            ("user",      "Кстати, наш кот Мурка — рыжий, любит молоко, боится воды."),
            ("assistant", "Запомнила про Мурку."),
            ("user",      "На обед сегодня борщ был."),
            ("assistant", "Хорошо."),
        ])
    ],
    "facts_must_keep": [
        "ирина", "кофе", "18",          # привычка Ирины
        "созвон", "ирина", "отпуск",    # договорённость
        "мурка", "рыж",                  # стабильный факт о коте
    ],
    "episodics_must_drop": [
        "оладьи", "борщ",  # что ели — эпизодика
    ],
}


# Сценарий 2: тривиальные чанки чередуются с информативными.
SCENARIO_TRIVIAL_CHUNKS = {
    "messages": [
        {"_id": f"m{i}", "role": role, "content": content, "timestamp": _ts(60 - i), "msg_id": f"id{i}"}
        for i, (role, content) in enumerate([
            # Чанк 1 (информативный)
            ("user",      "Запомни: Антон любит дроновую съёмку."),
            ("assistant", "Запомнила."),
            ("user",      "И у Ирины аллергия на пыль."),
            ("assistant", "Запомнила про Ирину."),
            # Чанк 2 (тривиальный — должен SKIP)
            ("user",      "Ага."),
            ("assistant", "Ага."),
            ("user",      "Окей."),
            ("assistant", "Окей."),
            # Чанк 3 (информативный — диф к 1-му)
            ("user",      "Ещё Антон паяет ретро-консоли по выходным."),
            ("assistant", "Запомнила, паяльная мастерская."),
            ("user",      "Да, NES в основном."),
            ("assistant", "Понятно."),
        ])
    ],
    "facts_must_keep": ["дрон", "ирина", "аллерги", "пыль", "ретро", "пая"],
    "expect_at_least_one_skip": True,
}
