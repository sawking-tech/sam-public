"""Каркас страниц SAM (gateway/static/shell/): копия рамки DotNetSolutionKit, мост сообщений приложения, отдача."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gateway import builtin_apps, shell_config
from tests.fakes_core import app_client  # noqa: F401  (фикстура)
from tests.test_builtin_apps import ENV

SHELL = Path(__file__).resolve().parent.parent / "gateway" / "static" / "shell"
RADAR_STATIC = Path(__file__).resolve().parent.parent / "radar" / "static"     # страница радара - в его пакете (SAM-198)
RADAR_PAGE = "presence.html"
DNSK = Path(__file__).resolve().parents[2] / "DotNetSolutionKit" / "site"
NODE = shutil.which("node")
COPIES = {"panel.js": ("panel.js", None), "boot.js": ("boot.js", None), "shell.css": ("site.css", 257)}
HEADER_END = "*/\n"
CRLF, LF = "\r\n", "\n"


def page_path(page: str) -> Path:
    """Файл страницы: радар держит свою в radar/static, остальные - в gateway/static."""
    return (RADAR_STATIC if page == RADAR_PAGE else SHELL.parent) / page


def body(text: str) -> str:
    """Копия без своей шапки: всё после первого закрытия комментария."""
    return text.replace(CRLF, LF).split(HEADER_END, 1)[1]


@pytest.mark.skipif(not DNSK.is_dir(), reason="рядом нет репозитория DotNetSolutionKit")
@pytest.mark.parametrize("name", sorted(COPIES))
def test_frame_and_panel_are_copied_as_is(name):
    source, lines = COPIES[name]
    original = (DNSK / source).read_text(encoding="utf-8").replace(CRLF, LF)   # переводы строк - как отдал git
    if lines:
        original = "".join(original.splitlines(keepends=True)[:lines])
    assert body((SHELL / name).read_text(encoding="utf-8")) == original


@pytest.mark.skipif(NODE is None, reason="нет node")
@pytest.mark.parametrize("name", sorted(p.name for p in SHELL.glob("*.js")))
def test_shell_scripts_parse(name):
    assert subprocess.run([NODE, "--check", str(SHELL / name)], capture_output=True).returncode == 0


BRIDGE_CASES = """
const b = require(process.argv[1]);
const frame = {}, other = {};
const ctx = {source: frame, origins: ["null", "http://sam:8000"], base: "/apps/grafik/"};
const ev = (data, source = frame, origin = "null") => ({data, source, origin});
const out = {
  path: b.decide(ev({type: "sam:openPanel", title: "Настройки", path: "settings.html?a=1"}), ctx),
  html: b.decide(ev({type: "sam:openPanel", title: "x", html: "<b>hi</b>"}), ctx),
  close: b.decide(ev({type: "sam:closePanel"}), ctx),
  foreignSource: b.decide(ev({type: "sam:closePanel"}, other), ctx),
  foreignOrigin: b.decide(ev({type: "sam:closePanel"}, frame, "http://evil.example"), ctx),
  noSource: b.decide(ev({type: "sam:closePanel"}, undefined), {...ctx, source: undefined}),
  up: b.decide(ev({type: "sam:openPanel", path: "../../monitor"}), ctx),
  root: b.decide(ev({type: "sam:openPanel", path: "/sam/config"}), ctx),
  scheme: b.decide(ev({type: "sam:openPanel", path: "javascript:alert(1)"}), ctx),
  unknown: b.decide(ev({type: "sam:eval", code: "1"}), ctx),
};
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="нет node")
def test_bridge_takes_only_its_own_frame_and_keeps_app_html_in_a_frame():
    res = subprocess.run([NODE, "-e", BRIDGE_CASES, str(SHELL / "bridge.js")], capture_output=True, text=True,
                         encoding="utf-8")
    out = json.loads(res.stdout)
    assert out["path"] == {"action": "open", "title": "Настройки", "src": "/apps/grafik/settings.html?a=1"}
    assert out["html"] == {"action": "open", "title": "x", "srcdoc": "<b>hi</b>"}
    assert out["close"] == {"action": "close"}
    for refused in ("foreignSource", "foreignOrigin", "noSource", "up", "root", "scheme", "unknown"):
        assert out[refused] is None, refused


def test_shell_never_puts_markup_into_its_own_page():
    # содержимое от приложения идёт фреймом (srcdoc или src); разметку строкой каркас в свою страницу не вставляет
    text = (SHELL / "shell.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in text, sink
    assert 'sandbox: "allow-scripts"' in text


def test_nav_lists_monitor_apps_and_an_installed_radar(tmp_path):
    assert [i["key"] for i in shell_config.nav(tmp_path, 8000)] == ["apps", "monitor"]
    builtin_apps.ensure(tmp_path, builtin_apps.defaults(ENV))
    items = shell_config.nav(tmp_path, 8000)
    assert items == [{"key": "apps", "name": "Приложения", "port": 8000, "path": "/apps"},     # страница шлюза
                     {"key": "monitor", "name": "Монитор", "port": 8000, "path": "/monitor"},
                     {"key": "radar", "name": "Радар", "port": 8000, "path": "/apps/radar/"}]     # радар - через шлюз
    js = shell_config.config_js(items)
    assert js.startswith('window.SITE = {"key": "sam"};') and '"name": "Радар"' in js


def test_gateway_serves_the_shell_and_its_config(app_client):  # noqa: F811
    client, _ = app_client
    script = client.get("/static/shell/shell.js")
    assert script.status_code == 200 and "SAMShell" in script.text
    config = client.get("/shell/config.js")
    assert config.status_code == 200 and config.headers["content-type"].startswith("text/javascript")
    assert '"key": "monitor"' in config.text and '"key": "apps"' in config.text
    assert client.get("/static/shell/../main.py").status_code == 404


def test_radar_page_takes_the_shell_from_the_gateway_of_its_host(monkeypatch):
    from fastapi.testclient import TestClient

    from radar import presence_api
    from radar import presence_hub as ph
    from radar import presence as pr
    monkeypatch.setenv(presence_api.GATEWAY_ENV, "http://localhost:8123")
    client = TestClient(presence_api.create_app(ph.PresenceHub(ph.MODE_OFF, pr.DEMO_LAYOUT)),
                        base_url="http://localhost:8010")
    page = client.get("/").text
    assert presence_api.GATEWAY_MARK not in page
    assert '<script src="//localhost:8123/shell/config.js"></script>' in page     # навигация - со шлюза
    assert '<script src="./static/shell/shell.js" defer data-page="radar" data-maxw="4000">' in page   # каркас - свой, от адреса страницы
    assert "fonts.googleapis" not in page
    for token in ("var(--ink)", "var(--text)", "  --ink:", "  --text:"):     # токены каркаса радар не трогает
        assert token not in page, token


@pytest.mark.parametrize("page", ["monitor.html", "presence.html", "apps_panel.html", "app_view.html"])
def test_pages_take_fonts_from_the_shell_not_a_cdn(page):
    """Шрифты лежат в gateway/static/shell/fonts: страница не ждёт Google Fonts (340-570 мс на отрисовку)."""
    html = page_path(page).read_text(encoding="utf-8")
    assert "fonts.googleapis" not in html and "fonts.gstatic" not in html
    assert 'shell/fonts/onest-cyrillic.woff2" as="font" type="font/woff2" crossorigin>' in html
    css = (SHELL / "sam.css").read_text(encoding="utf-8")
    assert "googleapis" not in css
    for face in ("onest-cyrillic", "onest-latin", "jetbrainsmono-cyrillic", "jetbrainsmono-latin"):
        assert f'url("fonts/{face}.woff2")' in css and (SHELL / "fonts" / f"{face}.woff2").stat().st_size > 1000


STATIC_PAGES = ("monitor.html", "apps_panel.html", "presence.html", "app_view.html", "app_stub.html")


@pytest.mark.skipif(shutil.which("node") is None, reason="node нет на машине")
@pytest.mark.parametrize("page", STATIC_PAGES)
def test_inline_scripts_of_every_page_parse(page, tmp_path):
    """Встроенный скрипт страницы с синтаксической ошибкой гасит её целиком (15 окт: лишняя «}» - пустой монитор)."""
    import re
    html = page_path(page).read_text(encoding="utf-8")
    scripts = [(attrs, body) for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S)
               if "src=" not in attrs and "json" not in attrs and "importmap" not in attrs]
    for i, (attrs, body) in enumerate(scripts):
        src = tmp_path / f"{page}.{i}.{'mjs' if 'module' in attrs else 'js'}"
        src.write_text(body, encoding="utf-8")
        res = subprocess.run(["node", "--check", str(src)], capture_output=True, text=True)
        assert res.returncode == 0, f"{page}, скрипт {i}: {res.stderr.strip().splitlines()[-1] if res.stderr else ''}"
