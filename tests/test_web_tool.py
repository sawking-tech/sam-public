"""search_web (gateway/tools/web.py): поисковики по порядку; граница - HTTP (httpx.MockTransport)."""
import httpx
import pytest

from gateway.tools import web

INVIDIOUS = [{"videoId": "abc", "title": "Lana - клип"}]
DDG_PAGE = '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F&rut=1">Python docs</a>'
EMPTY_PAGE = "<html>no results</html>"


def _transport(hosts, pages):
    """Отвечает по хосту: Invidious - список видео, DuckDuckGo - страница; хосты запросов пишет в hosts."""
    def handle(request):
        hosts.append(request.url.host)
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(200, text=pages["ddg"])
        return httpx.Response(200, json=pages["invidious"])
    return httpx.MockTransport(handle)


@pytest.mark.parametrize("params, pages, expected, asked_invidious", [
    ({}, {}, "Нужен query — поисковый запрос", False),
    ({"query": "новый клип Lana", "intent": "youtube"}, {"invidious": INVIDIOUS, "ddg": DDG_PAGE},
     "Lana - клип → https://www.youtube.com/watch?v=abc", True),                    # видео - сначала Invidious
    ({"query": "посмотреть фильм"}, {"invidious": [], "ddg": DDG_PAGE},
     "Python docs → https://docs.python.org/", True),                                # видео без находок - дальше
    ({"query": "python async tutorial"}, {"invidious": INVIDIOUS, "ddg": DDG_PAGE},
     "Python docs → https://docs.python.org/", False),                               # общий запрос Invidious не трогает
    ({"query": "test query"}, {"invidious": [], "ddg": EMPTY_PAGE},
     "Ничего не найдено по запросу: test query", False),
], ids=["empty_query", "video_invidious", "video_falls_through", "general_skips_invidious", "nothing_found"])
async def test_search_web_tries_backends_in_order(monkeypatch, params, pages, expected, asked_invidious):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CX", raising=False)
    hosts = []
    assert await web.search_web_impl(params, transport=_transport(hosts, pages)) == expected
    assert any(h != "html.duckduckgo.com" for h in hosts) is asked_invidious
