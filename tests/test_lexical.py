"""Лексическая ветка поиска по памяти и слияние RRF (SAM-102, gateway/lexical.py)."""
import pytest

from gateway import lexical


@pytest.mark.parametrize("text,stems", [
    *[(w, ["ирин"]) for w in ("Ирина", "Ирины", "Ирине", "Ирину", "Ириной")],   # падежи имени - одна основа
    ("ЁЛКА", lexical.tokens("елка")),                                       # регистр и ё
    ("я в доме", ["дом"]),                                                  # короткие слова отброшены
    ("кот кота котом", ["кот", "кот", "кот"]),                              # основа короче STEM_MIN не режется
    ("какая погода завтра", ["погод", "завтр"]),                            # служебные слова отброшены
    ("Где был Петя, кто дома", ["где", "пет", "кто", "дом"]),               # «где» и «кто» остаются
], ids=["name-cases-a", "name-cases-y", "name-cases-e", "name-cases-u", "name-cases-oy",
        "case-and-yo", "short-words", "short-stem", "stop-words", "where-who-kept"])
def test_word_forms_share_one_token(text, stems):
    assert lexical.tokens(text) == stems


DOCS = [
    ("birthday", "День рождения Ирины - 3 марта."),
    ("cat", "Кот Барсик: белый пушистый шотландский котик"),
    ("rule", "Правило Антона: SAM всегда отвечает восторженно и тепло"),
]
RARE_DOCS = [("a", "Ирина любит чай"), ("b", "Ирина любит кофе"), ("c", "Антон любит кофе")]


@pytest.mark.parametrize("query,docs,first", [
    ("Барсик", DOCS, "cat"),
    ("Ирина", DOCS, "birthday"),
    ("день рождения", DOCS, "birthday"),
    ("расскажи про кота", DOCS, "cat"),
    ("погода завтра", DOCS, None),        # нет общих слов - пусто
    ("Ирина кофе", RARE_DOCS, "b"),        # редкое слово весит больше частого
], ids=["name", "name-case", "phrase", "word-form", "no-common-word", "rarer-word"])
def test_bm25_ranks_facts_by_words(query, docs, first):
    ranked = lexical.bm25_rank(query, docs, 3)
    assert (ranked[0][0] if ranked else None) == first


@pytest.mark.parametrize("lists,weights,order", [
    ([["a", "b"], ["c", "b"]], None, ["b", "a", "c"]),                       # общий ключ первый
    ([["lex"], ["vec"]], None, ["lex", "vec"]),                              # ничья - ключ раньше переданного списка
    ([[], ["x", "y", "z"]], None, ["x", "y", "z"]),                          # один список - его порядок
    ([["lex1", "both"], ["vec", "both"]], [1.0, 0.0], ["lex1", "both", "vec"]),   # нулевой вес только дописывает
], ids=["both-lists-first", "tie-earlier-list", "single-list", "zero-weight"])
def test_rrf_fuse_order(lists, weights, order):
    kw = {"weights": weights} if weights else {}
    assert [k for k, _ in lexical.rrf_fuse(lists, **kw)] == order
