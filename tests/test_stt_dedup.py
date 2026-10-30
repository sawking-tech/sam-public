"""Повтор распознавания в чате (SAM-32, gateway/main.py): уже сказанный кусок фразы вырезается, новое остаётся."""
import pytest

from gateway.stt_dedup import deduplicate_stt, history_for_dedup


@pytest.mark.parametrize("text, history, kept", [
    ("", ["что-то"], ""),
    ("Привет SAM.", [], "Привет SAM."),
    ("Какая погода в Москве?", ["Какая погода в Москве?"], ""),
    ("Какая погода в Москве? А ещё включи свет на кухне.", ["Какая погода в Москве?"], "А ещё включи свет на кухне."),
    ("Открой телеграм пожалуйста.", ["Какая погода?"], "Открой телеграм пожалуйста."),
    ("А что у нас на завтрак?", ["Что у нас на ужин?"], "А что у нас на завтрак?"),   # ужин и завтрак - ниже порога
    ("Открой Telegram. И запусти браузер. И включи музыку.", ["Открой Telegram."],
     "И запусти браузер. И включи музыку."),
    ("???", ["что-то"], "???"),             # на пунктуации словарь TF-IDF пуст - фраза как есть
])
def test_repeated_stt_chunk_is_cut_new_text_kept(text, history, kept):
    assert deduplicate_stt(text, history) == kept


@pytest.mark.parametrize("docs, limit, phrases", [
    ([{"role": "user", "content": "первое"}, {"role": "assistant", "content": "ответ"},
      {"role": "user", "content": "второе"}, {"role": "tool_call", "content": ""}], 10, ["первое", "второе"]),
    ([{"role": "user", "content": ""}, {"role": "user", "content": "   "}, {"role": "user", "content": "норм"}], 10,
     ["норм"]),
    ([{"role": "user", "content": f"msg{i}"} for i in range(20)], 5, ["msg15", "msg16", "msg17", "msg18", "msg19"]),
])
def test_dedup_history_is_last_user_phrases(docs, limit, phrases):
    assert history_for_dedup(docs, limit=limit) == phrases
