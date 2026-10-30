"""Скилл html-design (SAM-202): образец страницы работает без сети и соблюдает просьбу о меньшем движении."""
import re
from html.parser import HTMLParser
from pathlib import Path

from gateway import skills

SKILL_DIR = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "html-design"
EXAMPLE = SKILL_DIR / "example.html"
URL_ATTRS = ("src", "href", "srcset", "poster", "action", "data")
EXTERNAL = re.compile(r"^\s*(?:https?:)?//", re.IGNORECASE)        # http://, https:// и адрес без схемы «//host»
CSS_EXTERNAL = re.compile(r"(?:url\(\s*['\"]?|@import\s+['\"])\s*(?:https?:)?//", re.IGNORECASE)
REDUCED_MOTION_CSS = re.compile(r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)")
REDUCED_MOTION_JS = re.compile(r"matchMedia\(\s*['\"]\(prefers-reduced-motion:\s*reduce\)['\"]\s*\)")


class _Page(HTMLParser):
    """Адреса в атрибутах, тексты <style> и <script> страницы."""

    def __init__(self):
        super().__init__()
        self.urls, self.styles, self.scripts = [], [], []
        self._in = None

    def handle_starttag(self, tag, attrs):
        self.urls += [v for k, v in attrs if k in URL_ATTRS and v]
        self.urls += [v for k, v in attrs if k == "style" and v and CSS_EXTERNAL.search(v)]
        self._in = tag if tag in ("style", "script") else None

    def handle_endtag(self, tag):
        self._in = None

    def handle_data(self, data):
        if self._in == "style":
            self.styles.append(data)
        elif self._in == "script":
            self.scripts.append(data)


def _page() -> _Page:
    p = _Page()
    p.feed(EXAMPLE.read_text(encoding="utf-8"))
    return p


def test_example_has_no_external_urls():
    p = _page()
    assert [u for u in p.urls if EXTERNAL.match(u)] == []
    assert [s for s in p.styles if CSS_EXTERNAL.search(s)] == []


def test_example_respects_reduced_motion_in_css_and_js():
    p = _page()
    assert any(REDUCED_MOTION_CSS.search(s) for s in p.styles)
    assert any(REDUCED_MOTION_JS.search(s) for s in p.scripts)


def test_example_has_light_and_dark_tokens():
    css = "\n".join(_page().styles)
    assert "color-scheme: light dark" in css
    assert re.search(r"@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)", css)


def test_skill_is_listed_with_name_and_description():
    skill = skills.parse_skill((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"), SKILL_DIR.name)
    assert skill.name == "html-design"
    assert skill.description and "example.html" in skill.body
