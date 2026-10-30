"""Монитор - модули gateway/static/monitor (SAM-230): тесты блоков и модулей без DOM гоняет node --test
(tests/js/monitor), здесь - их запуск, разбор каждого модуля, пути импортов и раздача модулей шлюзом."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.fakes_core import app_client  # noqa: F401 - фикстура

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "gateway" / "static"
MODULES = STATIC / "monitor"
JS_TESTS = Path(__file__).resolve().parent / "js" / "monitor"
NODE_TIMEOUT_S = 60          # один процесс node на файл тестов; с запасом на холодный старт Windows
ENTRY = "/static/monitor/main.js"
SHELL_SCRIPT = '<script src="/static/shell/shell.js"'
IMPORT_RE = re.compile(r"""^\s*(?:import|export)\b[^'"]*?from\s+['"](\.[^'"]+)['"]""", re.M)

ALL_MODULES = sorted(MODULES.rglob("*.js"))
BLOCKS = sorted((MODULES / "blocks").glob("*.js"))


def require_node() -> None:
    """Без node тест пропускается; в CI (CI=1) это ошибка: зелёный прогон без node ничего бы не проверил."""
    if shutil.which("node") is None:
        if os.environ.get("CI") == "1":
            pytest.fail("нужен node: в CI тесты модулей монитора не пропускаются")
        pytest.skip("нужен node: модули монитора проверяются в node")


def _node(*args):
    return subprocess.run(["node", *args], capture_output=True, text=True, encoding="utf-8", timeout=NODE_TIMEOUT_S,
                          cwd=ROOT)


@pytest.mark.parametrize("test_file", sorted(JS_TESTS.glob("*.test.mjs")), ids=lambda p: p.name)
def test_node_tests_pass(test_file):
    require_node()
    res = _node("--test", str(test_file))
    assert res.returncode == 0, res.stdout[-4000:] + res.stderr[-2000:]


@pytest.mark.parametrize("module", ALL_MODULES, ids=lambda p: p.relative_to(MODULES).as_posix())
def test_module_parses(module):
    """Модуль с синтаксической ошибкой гасит монитор целиком (main.js его не загрузит)."""
    require_node()
    res = _node("--check", str(module))
    assert res.returncode == 0, res.stderr.strip().splitlines()[-1:]


@pytest.mark.parametrize("module", ALL_MODULES, ids=lambda p: p.relative_to(MODULES).as_posix())
def test_relative_imports_point_to_modules(module):
    for spec in IMPORT_RE.findall(module.read_text(encoding="utf-8")):
        assert spec.endswith(".js"), f"{module.name}: {spec} - браузеру нужен путь с расширением"
        assert (module.parent / spec).resolve().is_file(), f"{module.name}: нет {spec}"


@pytest.mark.parametrize("block", BLOCKS, ids=lambda p: p.name)
def test_every_block_has_a_node_test(block):
    tested = "\n".join(p.read_text(encoding="utf-8") for p in JS_TESTS.glob("*.test.mjs"))
    assert f"/blocks/{block.name}'" in tested, f"у блока {block.name} нет теста в {JS_TESTS}"


def test_page_is_markup_and_one_module_before_the_shell():
    """monitor.html - разметка и корень сборки: встроенного скрипта нет, модуль идёт раньше каркаса (как прежний
    встроенный скрипт: history.state читается до panel.js)."""
    html = (STATIC / "monitor.html").read_text(encoding="utf-8")
    inline = [b for a, b in re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S) if "src=" not in a]
    assert inline == []
    tag = f'<script type="module" src="{ENTRY}"></script>'
    assert html.count('type="module"') == 1 and html.index(tag) < html.index(SHELL_SCRIPT)


def test_gateway_serves_the_modules_as_javascript(app_client):  # noqa: F811
    client, _ = app_client
    for path in (ENTRY, "/static/monitor/blocks/reply.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "javascript" in r.headers["content-type"], r.headers["content-type"]   # модуль браузер иначе не примет


NAMED_IMPORT_RE = re.compile(r"""^\s*import\s*\{([^}]*)\}\s*from\s*['"](\.[^'"]+)['"]""", re.M)
EXPORT_RE = re.compile(r"""^\s*export\s+(?:async\s+)?(?:const|let|var|function\*?|class)\s+([A-Za-z_$][\w$]*)""", re.M)
EXPORT_LIST_RE = re.compile(r"""^\s*export\s*\{([^}]*)\}""", re.M)


EXPORT_DECL_RE = re.compile(r"""^\s*export\s+(?:const|let|var)\s+([^;]*?);?\s*$""", re.M)
DECLARATOR_RE = re.compile(r"""(?:^|,)\s*([A-Za-z_$][\w$]*)\s*=""")


def _exports(path: Path) -> set[str]:
    """Экспортированные имена модуля; `export const A = 1, B = 2` даёт оба (простые константы в одну строку)."""
    text = path.read_text(encoding="utf-8")
    names = set(EXPORT_RE.findall(text))
    for decl in EXPORT_DECL_RE.findall(text):
        names |= set(DECLARATOR_RE.findall(decl))
    for group in EXPORT_LIST_RE.findall(text):
        names |= {part.split(" as ")[-1].strip() for part in group.split(",") if part.strip()}
    return names


@pytest.mark.parametrize("module", ALL_MODULES, ids=lambda p: str(p.relative_to(MODULES)))
def test_every_named_import_is_exported(module):
    """Ревью SAM-230: импорт имени, которого модуль не экспортирует, роняет монитор целиком (SyntaxError при загрузке)
    - а node --check этого не видит, он разбирает файл один."""
    text = module.read_text(encoding="utf-8")
    for names, rel in NAMED_IMPORT_RE.findall(text):
        target = (module.parent / rel).resolve()
        wanted = {part.split(" as ")[0].strip() for part in names.split(",") if part.strip()}
        assert wanted <= _exports(target), f"{module.name}: нет в {rel}: {sorted(wanted - _exports(target))}"


def test_card_headers_match_what_the_gateway_writes():
    """Карточки запроса к модели (SAM-230) находят блоки по заголовкам шлюза: строки совпадают с Python."""
    from gateway import recall_history, runner
    cards = (MODULES / "blocks" / "cards.js").read_text(encoding="utf-8")
    assert f"export const RAG_HEADER = '{runner.RAG_HEADER}';" in cards
    assert f"export const ARCHIVE_HEADER = '{recall_history.ARCHIVE_HEADER}';" in cards
    assert runner.LINGER_HEADER.format(name="load_skill") == "[итог load_skill]" and "итог (load_skill)" in cards
