"""Контракт завершения агента (SAM-237): итог с артефактом принимается, пустое «Готово.» - нет."""
import pytest

from gateway import finish_contract as fc

REAL = {"8bb56da"}
exists = REAL.__contains__
LONG = "Проверил монитор в браузере: метка есть в ленте, порядок сообщений по времени отправки, дублей нет."


@pytest.mark.parametrize("description, result, reason", [
    ("проверь монитор", "Готово.", fc.REASON_EMPTY.format(text="Готово.")),
    ("проверь монитор", "  ", fc.REASON_EMPTY.format(text="")),
    ("проверь монитор", "ок", fc.REASON_EMPTY.format(text="ок")),
    ("проверь монитор", "Сделал, ок", fc.REASON_SHORT.format(limit=fc.MIN_CHARS_DEFAULT)),
    ("найди новости", "3 новости за неделю", None),
    ("проверь монитор", LONG, None),
    ("правка в рабочем месте agent/badge", LONG, fc.REASON_NO_HASH),
    ("закоммить правку", "Готово, коммит deadbee в ветке", fc.REASON_BAD_HASH.format(hash="deadbee")),
    ("закоммить правку", "Готово, коммит 8bb56da в ветке agent/badge", None),
    ("закоммить правку", "коммиты deadbee и 8bb56da", None),
], ids=["done", "blank", "ok", "short", "short_but_real", "long_ok", "code_no_hash", "code_bad_hash", "code_good_hash", "one_real_of_two"])
def test_finish_check(description, result, reason):
    assert fc.check(description, result, exists, environ={}) == reason


def test_threshold_is_configurable():
    assert fc.check("проверь", "Сделал, ок", exists, environ={fc.MIN_CHARS_ENV: "10"}) is None
