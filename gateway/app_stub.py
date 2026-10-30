"""Заглушка приложения, которое не открылось: одна страница на все приложения, во фрейме вместо их страницы.

Шлюз отдаёт её на запрос страницы (`GET`, браузер ждёт HTML), когда приложение приостановлено или не ответило;
запросы API приложения по-прежнему получают JSON с ошибкой. `render` - чистая функция: проверяется без шлюза.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

PAGE = Path(__file__).resolve().parent / "static" / "app_stub.html"
MARKS = re.compile(r"__(?:TITLE|STATE|HINT)__")
HTML_TYPE = "text/html"

STOPPED, NOT_ANSWERING = "stopped", "not_answering"
TEXTS = {
    STOPPED: ("приостановлено", "Продолжить - на плитке в приложениях, кнопка «продолжить»."),
    NOT_ANSWERING: ("не отвечает", "SAM видит это в мониторе; перезапустить - на плитке в приложениях."),
}


def wants_page(method: str, accept: str) -> bool:
    """Браузер открывает страницу: GET и HTML в Accept. API приложения заглушку не получает."""
    return method.upper() == "GET" and HTML_TYPE in (accept or "")


def render(name: str, reason: str, template: str | None = None) -> str:
    template = template if template is not None else PAGE.read_text(encoding="utf-8")
    state, hint = TEXTS[reason]
    values = {"__TITLE__": html.escape(name), "__STATE__": state, "__HINT__": hint}
    return MARKS.sub(lambda match: values[match.group(0)], template)
