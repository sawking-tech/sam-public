"""Разбор env (SAM-80): мусор в числовой или булевой переменной - одна строка с именем и типом, не стек."""
import os
import pathlib
import subprocess
import sys

import pytest

from gateway.env import env_bool, env_float, env_int

_REPO = pathlib.Path(__file__).resolve().parents[1]
IMPORT_TIMEOUT_S = 60   # импорт шлюза в чистом подпроцессе на холодной машине
# Переменные, без которых интерпретатор на Windows не стартует; окружение разработчика (.env, Mongo) не передаётся
_SYSTEM_VARS = ("PATH", "SYSTEMROOT", "TEMP", "TMP")


@pytest.mark.parametrize("reader, raw, default, expected, warning_type", [
    (env_int, "7", None, 7, None),
    (env_int, " 7 ", None, 7, None),
    (env_float, "0.5", None, 0.5, None),
    (env_float, "0,5", None, 0.5, None),
    (env_bool, "1", None, True, None),
    (env_bool, "TRUE", None, True, None),
    (env_bool, "off", None, False, None),
    (env_bool, "0", None, False, None),
    (env_int, None, 4, 4, None),
    (env_int, "", 4, 4, None),
    (env_int, "   ", 4, 4, None),
    (env_int, "abc", 4, 4, "целое число"),
    (env_int, "1.5", 4, 4, "целое число"),
    (env_float, "fast", 0.5, 0.5, "число"),
    (env_bool, "maybe", False, False, "1/false"),
], ids=["int", "int_spaces", "float", "float_comma", "bool_1", "bool_TRUE", "bool_off", "bool_0",
        "unset", "empty", "blank", "int_garbage", "int_fraction", "float_garbage", "bool_garbage"])
def test_env_value_parsed_or_one_line_warning(reader, raw, default, expected, warning_type):
    lines: list[str] = []
    got = reader("SAM_X", default, environ={} if raw is None else {"SAM_X": raw}, warn=lines.append)
    assert got == expected
    if warning_type is None:
        assert lines == []
    else:
        assert len(lines) == 1 and "\n" not in lines[0]
        assert "SAM_X" in lines[0] and repr(raw) in lines[0] and warning_type in lines[0]


def test_garbage_env_does_not_break_gateway_import():
    """SAM_NO_PROGRESS_LIMIT=abc раньше ронял импорт ValueError без имени переменной; процесс отдельный,
    чтобы перезагрузка модуля не подменила классы другим тестам, окружение чистое, Mongo - закрытый порт."""
    code = "import gateway.agent_loop as m; print(m.NO_PROGRESS_LIMIT)"
    clean = {k: os.environ[k] for k in _SYSTEM_VARS if k in os.environ}
    clean.update({"SAM_NO_PROGRESS_LIMIT": "abc", "MONGODB_URI": "mongodb://127.0.0.1:1", "PYTHONIOENCODING": "utf-8"})
    res = subprocess.run([sys.executable, "-c", code], cwd=_REPO, capture_output=True, text=True, encoding="utf-8",
                         env=clean, timeout=IMPORT_TIMEOUT_S)
    assert res.returncode == 0, res.stderr
    assert "[Env] SAM_NO_PROGRESS_LIMIT='abc'" in res.stdout and res.stdout.strip().endswith("3")
