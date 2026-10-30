"""Поверхность SAM вживую (SAM-204 «Поверхность»): монитор и страница приложений глазами человека.

Запуск: pytest -m integration tests/integration/test_live_surface.py. Нужен работающий стенд (start_sam.ps1) и Chrome
на машине; ход разговора идёт в настоящую основную модель. Требования - docs/features/shell-layout.md (монитор) и
docs/features/apps.md (приложения): проверяется то, что видит человек, а не числа реализации."""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

playwright = pytest.importorskip("playwright.sync_api")

MONITOR = os.getenv("SAM_MONITOR_URL", "http://127.0.0.1:8000/monitor")
APPS = os.getenv("SAM_APPS_PAGE_URL", "http://127.0.0.1:8000/apps")     # страница шлюза (SAM-204)
# Реплики проверки идут в свою сессию с меткой тестовых данных (gateway/session_kinds.py CHECK_PREFIX), не в
# канал человека; в конце модуля она удаляется ручкой монитора
SESSION = os.getenv("SAM_SURFACE_SESSION", "test-do-not-use-surface")
CLEANUP_TIMEOUT_S = 10
BROWSER_CHANNEL = os.getenv("SAM_SURFACE_BROWSER", "chrome")
SETTLE_MS = 2500                 # снимок по сокету и шрифты: до этого лента ещё собирается
TURN_TIMEOUT_MS = 90_000         # ход облачной модели: предел ожидания, не пауза
FIRST_FRAME_S = 1.5              # «сразу» для человека: плитки до того, как он успеет заметить загрузку
TABS_IN_A_ROW = 12               # больше, чем мест у сокета монитора: закрытые вкладки обязаны их вернуть

ROWS = "[...document.querySelectorAll('#log > :not(#log-older)')].filter(r => r.offsetParent !== null)"
ROW_TEXTS = f"{ROWS}.map(r => r.innerText)"
FEED_TEXT = "document.getElementById('log').innerText"
AT_BOTTOM = "(l => l.scrollHeight - l.scrollTop - l.clientHeight < 40)(document.getElementById('log'))"
ARTIFACTS = ("▍", "<voice>", "</voice>", "<tool_call>")


@pytest.fixture(scope="module", autouse=True)
def own_session():
    """Сессия проверки после модуля удаляется: 404 - реплик не было, ошибка связи - строка, прогон не падает."""
    yield
    base = urllib.parse.urljoin(MONITOR, "/")
    req = urllib.request.Request(f"{base}sam/sessions/{urllib.parse.quote(SESSION, safe='')}", method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=CLEANUP_TIMEOUT_S).close()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"сессия {SESSION} не удалена: {e.code} {e.read().decode(errors='replace')}")
    except OSError as e:
        print(f"сессия {SESSION} не удалена: {e}")


def choose_session(page):
    """Выбрать сессию проверки в селекторе: новой сессии в нём ещё нет, монитор добавляет её как при первом событии."""
    page.evaluate("id => trackSess(id)", SESSION)
    page.select_option("#sess-sel", SESSION)


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel=BROWSER_CHANNEL)
        except Exception as e:
            pytest.skip(f"браузер {BROWSER_CHANNEL} не запускается: {str(e).splitlines()[0]}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    try:
        pg.goto(MONITOR, wait_until="load")
    except Exception:
        pytest.skip(f"стенд не отвечает на {MONITOR}: запусти start_sam.ps1")
    pg.wait_for_timeout(SETTLE_MS)
    yield pg
    ctx.close()


def artifacts_in(text: str) -> list[str]:
    return [a for a in ARTIFACTS if a in text]


def test_monitor_shows_the_latest_conversation_and_a_reload_keeps_it_in_place(page):
    """Открыл монитор - последние сообщения сразу внизу, без служебного мусора; F5 - то же самое и сразу, без прыжка."""
    before = page.evaluate(ROW_TEXTS)
    assert before, "лента пуста"
    assert page.evaluate(AT_BOTTOM), "лента открылась не на последних сообщениях"
    assert not artifacts_in(page.evaluate(FEED_TEXT)), "в ленте служебные знаки"

    page.reload(wait_until="domcontentloaded")
    assert page.evaluate(f"{ROWS}.length") > 1, "после F5 лента пустая до прихода снимка - экран мигает"
    assert page.evaluate(AT_BOTTOM), "после F5 лента не на последних сообщениях"
    page.wait_for_timeout(SETTLE_MS)
    assert page.evaluate(ROW_TEXTS) == before, "после F5 лента другая: строки пропали или задвоились"
    assert not page.errors, page.errors


def test_a_message_from_the_monitor_gets_one_reply_and_stays_after_reload(page):
    """Человек пишет в мониторе - видит свою строку и ровно один ответ SAM; после F5 разговор на месте."""
    probe = f"проверка поверхности {int(time.time())}: ответь одной короткой фразой"
    choose_session(page)
    page.fill("#inp", probe)
    page.press("#inp", "Enter")
    page.wait_for_function(
        """probe => { const t = document.getElementById('log').innerText; const i = t.lastIndexOf(probe);
          return i >= 0 && /SAM:/.test(t.slice(i + probe.length)) && !document.documentElement.classList.contains('sam-busy'); }""",
        arg=probe, timeout=TURN_TIMEOUT_MS)
    tail = page.evaluate(FEED_TEXT)
    tail = tail[tail.rfind(probe):]
    assert tail.count("SAM:") == 1, f"ответов не один: {tail[:300]}"
    assert not artifacts_in(tail), f"в ответе служебные знаки: {tail[:300]}"

    page.reload(wait_until="load")
    page.wait_for_timeout(SETTLE_MS)
    assert probe in page.evaluate(FEED_TEXT), "после F5 реплики нет: ход не записан в историю"
    assert not page.errors, page.errors


def test_feed_controls_do_what_they_say(page):
    """Очистить - лента пуста до F5; автопрокрутка выключена - новое не уводит взгляд; «отладка» прячет и
    показывает запросы к модели."""
    page.evaluate("clearLog()")
    page.wait_for_timeout(1500)
    assert page.evaluate(f"{ROWS}.length") == 0, "очищенная лента наполнилась сама"
    page.reload(wait_until="load")
    page.wait_for_timeout(SETTLE_MS)
    assert page.evaluate(f"{ROWS}.length") > 0, "после F5 история не вернулась"

    debug_visible = "[...document.querySelectorAll('.ev-debug')].filter(e => e.offsetParent !== null).length"
    assert page.evaluate(debug_visible) == 0, "отладочные строки видны без флажка"
    page.evaluate("toggleDebug(true)")
    assert page.evaluate(debug_visible) > 0, "флажок «отладка» ничего не показал"
    page.evaluate("toggleDebug(false); localStorage.removeItem('sam.monitor.debug')")

    page.evaluate("document.getElementById('auto-btn').checked = false; toggleAuto()")
    page.evaluate("document.getElementById('log').scrollTop = 400")
    top = page.evaluate("document.getElementById('log').scrollTop")
    choose_session(page)
    page.fill("#inp", f"проверка автопрокрутки {int(time.time())}: ответь одним словом")
    page.press("#inp", "Enter")
    page.wait_for_timeout(8000)
    assert abs(page.evaluate("document.getElementById('log').scrollTop") - top) < 60, "лента уехала при выключенной автопрокрутке"
    page.evaluate("document.getElementById('auto-btn').checked = true; toggleAuto()")
    assert not page.errors, page.errors


def test_many_monitor_tabs_opened_and_closed_leave_the_monitor_connected(browser):
    """Человек открывал и закрывал монитор много раз - новая вкладка всё равно на связи."""
    ctx = browser.new_context()
    for _ in range(TABS_IN_A_ROW):
        tab = ctx.new_page()
        tab.goto(MONITOR, wait_until="load")
        tab.wait_for_timeout(300)
        tab.close()
    last = ctx.new_page()
    last.goto(MONITOR, wait_until="load")
    last.wait_for_function("document.getElementById('conn-lbl').textContent === 'на связи'", timeout=15_000)
    ctx.close()


def test_apps_home_shows_tiles_at_once_with_the_shell_fonts_and_details_on_the_gear(browser):
    """Приложения - плитки видны сразу, шрифт каркаса загружен; шестерёнка открывает подробности, удаление - только
    после подтверждения, «Отмена» его сворачивает."""
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    started = time.time()
    try:
        pg.goto(APPS, wait_until="commit")
    except Exception:
        pytest.skip(f"страница приложений не отвечает на {APPS}")
    pg.wait_for_selector("#pinned li", timeout=10_000)
    assert time.time() - started < FIRST_FRAME_S, "плитки появились не сразу"
    pg.wait_for_timeout(1500)
    assert pg.evaluate("[...document.fonts].some(f => f.family.includes('Onest') && f.status === 'loaded')"), \
        "шрифт каркаса не загрузился"

    pg.locator("#pinned li button[aria-label^='Подробнее']").last.click()
    pg.wait_for_timeout(500)
    details = pg.evaluate("document.getElementById('sidebar').innerText")
    assert "Адрес" in details, f"в подробностях нет адреса: {details[:200]}"
    delete = pg.get_by_role("button", name="Удалить", exact=True)
    if delete.count():
        delete.first.click()
        assert pg.get_by_role("button", name="Удалить навсегда").count() == 1, "удаление без подтверждения"
        pg.get_by_role("button", name="Отмена").first.click()
    pg.close()


def test_history_off_needs_a_confirmation_and_stays_visible_in_the_bar(page):
    """«Не сохранять историю» не включается щелчком по подписи и без подтверждения; пока включено, полоса монитора
    говорит, что история не сохраняется (15 окт режим включился незаметно и полчаса разговоров не записались)."""
    current = "fetch('/sam/config').then(r => r.json()).then(c => c.debug_mode)"
    tune = "v => fetch('/sam/config/tune', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key: 'debug_mode', value: v})})"
    try:
        page.click("#burger")
        page.click(".sam-tab[data-tab='settings']")
        page.wait_for_timeout(1200)
        row = page.locator(".sam-switch[title='debug_mode']")
        row.locator("span").first.click()
        page.once("dialog", lambda d: d.dismiss())
        row.locator("input").click()
        page.wait_for_timeout(800)
        assert page.evaluate(current) is False, "режим включился без подтверждения"
        assert not page.locator("#history-off").is_visible()
        page.once("dialog", lambda d: d.accept())
        row.locator("input").click()
        page.wait_for_timeout(800)
        assert page.evaluate(current) is True and page.locator("#history-off").is_visible(), "включённый режим не виден"
    finally:
        page.evaluate(f"({tune})(false)")


def test_the_same_question_typed_twice_gets_two_replies(page):
    """Человек переспрашивает тем же текстом - это осознанный повтор, SAM отвечает снова (15 окт повтор молча
    отбрасывал дедуп эха микрофона, и монитор оставался без ответа)."""
    probe = f"повтор вопроса {int(time.time())}: ответь одним словом"
    choose_session(page)
    for n in (1, 2):
        page.fill("#inp", probe)
        page.press("#inp", "Enter")
        page.wait_for_function(
            """([probe, n]) => { const t = document.getElementById('log').innerText;
              return t.split(probe).length - 1 >= n && /SAM:/.test(t.slice(t.lastIndexOf(probe) + probe.length))
                && !document.documentElement.classList.contains('sam-busy'); }""",
            arg=[probe, n], timeout=TURN_TIMEOUT_MS)
    assert not page.errors, page.errors
