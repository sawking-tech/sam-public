"""Живое приложение в каркасе SAM: страница `/apps/<slug>` (без косой черты в конце).

Каркас (полоса, навигация, панели) - вокруг, страница приложения - во фрейме `/apps/<slug>/` с CSP sandbox, как
прежде. Поле `shell` манифеста переопределяет каркас атрибутами тега shell.js (`gateway/app_settings.py`,
`shell_options`); доступа к странице каркаса у приложения нет: описание и параметры подставляются здесь, текстом,
с экранированием.

`render` - чистая функция: проверяется без шлюза.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from . import app_settings, apps

PAGE = Path(__file__).resolve().parent / "static" / "app_view.html"
INFO_FIELDS = ("name", "slug", "description", "request", "created")
JSON_LT = "\\u003c"                  # «<» внутри строки JSON, шесть знаков
MARKS = re.compile(r"__(?:TITLE|SLUG|SHELL_ATTRS|SRC|INFO)__")


def shell_attrs(shell) -> str:
    opts = app_settings.shell_options(shell)
    attrs = []
    if opts["off"]:
        attrs.append('data-shell="off"')
    if not opts["bar"]:
        attrs.append('data-bar="off"')
    if opts["panelw"]:
        attrs.append(f'data-panelw="{opts["panelw"]}"')
    return " ".join(attrs)


def render(m: apps.Manifest, template: str | None = None) -> str:
    """Страница приложения: подстановки за один проход, чтобы имя с «__INFO__» внутри не подставилось дважды."""
    template = template if template is not None else PAGE.read_text(encoding="utf-8")
    # «<» в JSON внутри <script> - escape JSON: имя или просьба с «</script>» не закроют тег
    info = json.dumps({k: getattr(m, k) for k in INFO_FIELDS}, ensure_ascii=False).replace("<", JSON_LT)
    values = {"__TITLE__": html.escape(m.name), "__SLUG__": html.escape(m.slug),
              "__SHELL_ATTRS__": shell_attrs(m.shell), "__SRC__": html.escape(apps.page_path(m)), "__INFO__": info}
    return MARKS.sub(lambda match: values[match.group(0)], template)
