"""plan.mjs --within (SAM-257): агент правит только узел своей задачи, маркер и пункты человека остаются как были."""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path.home() / ".claude" / "skills" / "plan-and-iterate" / "plan.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None or not SCRIPT.exists(), reason="нужны node и plan.mjs")

PLAN = '''step owner-item "Пункт человека" now=true since="2026-10-28 10:00" {
    why "w"
    how "h"
    done_when "d"
}

step agent-task "Задача агента" {
    why "w"
    how "h"
    done_when "d"

    step block-read "Прочитать входы" {
        why "w"
        how "h"
        done_when "d"
    }

    step block-code "Модуль" {
        why "w"
        how "h"
        done_when "d"
    }
}
'''


def _run(plan: Path, *args: str) -> tuple[int, str]:
    done = subprocess.run(["node", str(SCRIPT), *args], capture_output=True, text=True, encoding="utf-8",
                          env={"PLAN_FILE": str(plan), "PATH": __import__("os").environ["PATH"]})
    return done.returncode, done.stdout


@pytest.fixture
def plan(tmp_path):
    path = tmp_path / "plan.kdl"
    path.write_text(PLAN, encoding="utf-8")
    return path


def test_take_inside_the_node_keeps_the_owner_marker(plan):
    code, out = _run(plan, "take", "block-code", "--within", "agent-task")
    text = plan.read_text(encoding="utf-8")
    assert code == 0 and "Взят [block-code]" in out
    assert 'step owner-item "Пункт человека" now=true' in text and 'now=true since' in text.split("block-code")[1].split("{")[0] + "now=true since"
    assert text.count("now=true") == 2          # человека и блока агента: маркер человека не снят


def test_take_of_an_owner_item_is_refused_inside_the_scope(plan):
    before = plan.read_text(encoding="utf-8")
    code, out = _run(plan, "take", "owner-item", "--within", "agent-task")
    assert code == 1 and "вне вашей задачи" in out and plan.read_text(encoding="utf-8") == before


def test_done_and_add_are_limited_to_the_node(plan):
    assert _run(plan, "done", "owner-item", "--within", "agent-task")[0] == 1
    assert _run(plan, "add", "x", "Лишнее", "--after", "owner-item", "--within", "agent-task")[0] == 1
    code, _ = _run(plan, "add", "block-test", "Тесты", "--after", "block-code", "--within", "agent-task")
    assert code == 0 and "step block-test" in plan.read_text(encoding="utf-8")


def test_where_shows_only_the_node_and_without_the_key_everything(plan):
    scoped = _run(plan, "where", "6", "--within", "agent-task")[1]
    assert "block-read" in scoped and "owner-item" not in scoped
    assert "owner-item" in _run(plan, "where", "6")[1]


def test_unknown_node_is_an_error(plan):
    code, out = _run(plan, "where", "--within", "nope")
    assert code == 1 and "nope" in out
