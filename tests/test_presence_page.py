"""Страница радара (SAM-105): статические проверки того, что node --check не видит, а страница от этого ломается при загрузке или в кадре."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "radar"
PAGE = ROOT / "static" / "presence.html"
SHADER_CONSTANTS = ("GLOW_K", "TWINKLE_CYCLE_S", "TWINKLE_EDGE", "WALL_DOT_PX", "BEARING_DOT_K")


def shader_numbers_without_fixed_point(html):
    """Числа в текст шейдера идут через toFixed: целое «1» без точки GLSL не умножает на дробное, видеокарта отвергает программу и точки людей пропадают."""
    return [(name, use) for name in SHADER_CONSTANTS
            for use in re.findall(r"\$\{([^}]*\b" + name + r"\b[^}]*)\}", html) if "toFixed" not in use]


def functions_using_dt_without_it(html):
    """Переменная кадра dt, не переданная в функцию, роняет кадровый цикл ReferenceError, и страница замирает."""
    script = re.search(r'<script type="module">(.*?)</script>', html, re.S).group(1)
    bad = []
    for m in re.finditer(r"^(?:async )?function (\w+)\(([^)]*)\) \{\n(.*?)^\}", script, re.S | re.M):
        name, params, body = m.groups()
        if re.search(r"\bdt\b", body) and not re.search(r"\bdt\b", params) and not re.search(r"\b(?:const|let|var)\b[^;\n]*\bdt\b", body):
            bad.append(name)
    return bad


def imported_names_not_exported(html):
    """Имя, которого модуль не отдаёт, роняет страницу при загрузке."""
    missing = []
    for names, module in re.findall(r"import \{([^}]*)\} from '(/static/presence/[^']+)'", html):
        src = (ROOT / module.lstrip("/")).read_text(encoding="utf-8")
        for name in [n.strip().split(" as ")[0] for n in names.split(",") if n.strip()]:
            if not re.search(rf"export (?:const|function|class|let) {re.escape(name)}\b", src) and not re.search(rf"export \{{[^}}]*\b{re.escape(name)}\b", src):
                missing.append((module, name))
    return missing


@pytest.mark.parametrize("problems", [shader_numbers_without_fixed_point, functions_using_dt_without_it, imported_names_not_exported],
                         ids=["shader-fixed-point", "dt-passed", "imports-exported"])
def test_page_static_guards(problems):
    assert problems(PAGE.read_text(encoding="utf-8")) == []
