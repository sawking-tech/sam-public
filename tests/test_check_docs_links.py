"""Проверка ссылок документации (scripts/check_docs_links.py): что считается ссылкой и что проходит мимо.

CI зовёт скрипт на каждый PR отдельным job; здесь - правила разбора, без сети и без живого дерева:
временный корень с парой файлов, чтобы каждая строка отбора проверялась отдельно.

Две формы ссылки разбираются по-разному. Markdown `](x.md)` - всегда ссылка, путь проверяется от папки файла,
поэтому имя без папки (`apps.md` рядом) и `../` работают. Имя в бэктиках - часто сокращение в прозе
(«agent_loop.py»), его путь проверяется, только если начинается с известной папки репозитория.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_docs_links as links  # noqa: E402


def tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize("raw, is_link, target", [
    ("docs/features/apps.md", False, "docs/features/apps.md"),
    ("docs/features/apps.md#поиск", False, "docs/features/apps.md"),     # якорь отрезается
    ("gateway/main.py", False, "gateway/main.py"),
    ("https://example.com/docs/x.md", False, None),                       # внешнее
    ("mailto:a@b.c", False, None),
    ("/apps/x", False, None),                                             # абсолютный путь сайта
    ("data/SAM.md", False, None),                                         # личные данные, вне git
    ("voice/layer/*.py", False, None),                                    # шаблон, не один файл
    ("agent_loop.py", False, None),                                       # имя модуля в прозе
    ("docs/", False, None),                                               # папка без расширения
    ("pip install -r voice/requirements.txt", False, None),                # не путь от корня
    ("apps.md", True, "apps.md"),                                         # markdown-ссылка рядом с файлом
    ("../adr/001-x.md", True, "../adr/001-x.md"),                          # ссылка вверх по дереву
    ("agent_loop.py", True, "agent_loop.py"),                              # markdown-ссылка проверяется всегда
])
def test_what_counts_as_a_document_link(raw, is_link, target):
    assert links._strip_target(raw, is_link) == target


def test_broken_and_live_links_are_told_apart(tmp_path):
    tree(tmp_path, {
        "README.md": "Смотри [apps](docs/features/apps.md) и [нет](docs/features/missing.md)\n",
        "docs/features/apps.md": "# Приложения\n",
    })
    broken = links.check(tmp_path)
    assert broken == ["README.md:1 -> docs/features/missing.md"]


def test_paths_in_a_code_block_are_examples_not_links(tmp_path):
    tree(tmp_path, {
        "README.md": "```bash\ncat docs/nowhere.md\n```\n",
    })
    assert links.check(tmp_path) == []


def test_a_link_may_point_relative_to_its_own_document(tmp_path):
    tree(tmp_path, {
        "docs/features/index.md": "Рядом: [apps](apps.md)\n",
        "docs/features/apps.md": "# Приложения\n",
    })
    assert links.check(tmp_path) == []


def test_a_relative_link_that_leaves_the_folder_is_checked(tmp_path):
    """`../` в цели нормализуется: ссылка вверх по дереву проверяется, а не пропускается."""
    tree(tmp_path, {
        "docs/features/index.md": "[есть](../adr/001-x.md) и [нет](../adr/002-y.md)\n",
        "docs/adr/001-x.md": "# ADR\n",
    })
    assert links.check(tmp_path) == ["docs/features/index.md:1 -> ../adr/002-y.md"]
