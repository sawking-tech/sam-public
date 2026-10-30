"""Ручки живого приложения для SAM: из манифеста и из OpenAPI по метке, вызов app_call - на моке docker и HTTP."""
import json

import pytest

from gateway import app_tools
from gateway.tools import apps as tool
from tests.test_apps import FakeDocker

SLUG = "kalorii"                    # slug имени «калории»
MEAL = {"name": "add_meal", "description": "записать еду", "method": "POST", "path": "api/meal",
        "params": {"who": "кто ел", "dish": "блюдо"}}
SPEC = {"paths": {
    "/api/day": {"get": {"operationId": "day", "summary": "еда за день", "x-sam-tool": True,
                         "parameters": [{"name": "date", "in": "query", "description": "день"}]}},
    "/api/week": {"get": {"summary": "сводка недели", "tags": ["sam"]}},
    "/api/export": {"get": {"summary": "выгрузка для интеграций"}},                     # без метки - не ручка
    "/api/meal/{id}": {"post": {"summary": "правка", "x-sam-tool": True}},              # параметр пути - не ручка
    "/api/water": {"post": {"operationId": "add_water", "x-sam-tool": True, "requestBody": {"content": {
        "application/json": {"schema": {"properties": {"ml": {"description": "миллилитры"}}}}}}}},
}}


class FakeHttp:
    def __init__(self, spec=None, status=200, answer='{"ok": true}'):
        self.spec, self.status, self.answer, self.calls = spec, status, answer, []

    def __call__(self, method, url, query, body, timeout=None):
        if url.endswith("/" + app_tools.OPENAPI_PATH):
            return (200, json.dumps(self.spec)) if self.spec else (404, "not found")
        self.calls.append((method, url, query, json.loads(body) if body else None))
        return self.status, self.answer


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(tool, "_openapi_cache", {})


def make(tmp_path, **kw):
    params = {"name": "калории", "files": {"index.html": "<p>еда</p>"}, "command": "python serve.py", **kw}
    events = []
    tool.app_make_impl(params, tmp_path, events.append, FakeDocker())
    return events[0]["id"]


def test_only_marked_openapi_methods_without_path_params_become_tools():
    tools = app_tools.from_openapi(SPEC)
    assert [t["name"] for t in tools] == ["day", "get_api_week", "add_water"]
    assert tools[0] == {"name": "day", "description": "еда за день", "method": "GET", "path": "api/day",
                        "params": {"date": "день"}}
    assert tools[2]["params"] == {"ml": "миллилитры"} and tools[2]["method"] == "POST"
    assert app_tools.from_openapi({"paths": "битое"}) == [] and app_tools.from_openapi(None) == []


@pytest.mark.parametrize("ref, params", [
    ("#/components/schemas/Meal", {"who": "кто ел", "dish": "dish"}),     # тело-модель pydantic у FastAPI - ссылкой
    ("#/components/schemas/Missing", {}),
    ("http://elsewhere/schema.json#/Meal", {}),                            # чужая ссылка не читается
])
def test_body_schema_reference_is_resolved_from_components(ref, params):
    spec = {"paths": {"/api/meal": {"post": {"operationId": "add_meal", "x-sam-tool": True, "requestBody": {"content": {
        "application/json": {"schema": {"$ref": ref}}}}}}},
        "components": {"schemas": {"Meal": {"properties": {"who": {"description": "кто ел"}, "dish": {}}}}}}
    assert app_tools.from_openapi(spec)[0]["params"] == params


def test_manifest_tools_are_listed_and_called_with_a_json_body(tmp_path):
    make(tmp_path, tools=[MEAL])
    http = FakeHttp()
    assert "ручки app_call: add_meal(who, dish) - записать еду" in tool.app_list_impl({}, tmp_path, http=http)
    out = tool.app_call_impl({"app": SLUG, "tool": "add_meal", "args": {"who": "Человек", "dish": "суп"}}, tmp_path, http)
    assert out == '{"ok": true}'
    method, url, query, body = http.calls[0]
    assert method == "POST" and url.endswith("/api/meal") and body == {"who": "Человек", "dish": "суп"} and query == {}


def test_openapi_tools_join_the_manifest_ones_and_get_args_go_to_the_query(tmp_path):
    make(tmp_path, tools=[MEAL])
    http = FakeHttp(SPEC)
    listed = tool.app_list_impl({}, tmp_path, http=http)
    assert "add_meal(who, dish)" in listed and "day(date) - еда за день" in listed and "export" not in listed
    tool.app_call_impl({"app": SLUG, "tool": "day", "args": {"date": "2026-10-15"}}, tmp_path, http)
    assert http.calls[-1][0] == "GET" and http.calls[-1][2] == {"date": "2026-10-15"}


@pytest.mark.parametrize("params, http, expected", [
    ({"tool": "nope"}, FakeHttp(), "нет ручки 'nope'; есть: add_meal"),
    ({"tool": "add_meal", "args": {"price": 1}}, FakeHttp(), "нет аргументов price; объявлены: who, dish"),
    ({"tool": "add_meal"}, FakeHttp(status=500, answer="boom"), "ручка ответила 500: boom"),
], ids=["unknown_tool", "undeclared_arg", "app_error"])
def test_call_failures_are_one_line(tmp_path, params, http, expected):
    make(tmp_path, tools=[MEAL])
    out = tool.app_call_impl({"app": SLUG, **params}, tmp_path, http)
    assert expected in out and "\n" not in out


def test_app_without_tools_says_how_to_declare_them_and_bad_declarations_are_refused(tmp_path):
    make(tmp_path)
    out = tool.app_call_impl({"app": SLUG, "tool": "x"}, tmp_path, FakeHttp())
    assert "ручек нет" in out and "x-sam-tool" not in out and "tools" in out
    events = []
    refused = tool.app_update_impl({"app": SLUG, "tools": [{"name": "Bad Name", "path": "../etc"}]}, tmp_path,
                                   events.append, FakeDocker())
    assert refused == app_tools.DECLARATION_HINT


def test_long_answer_is_clipped_for_the_model():
    text = app_tools.answer(200, "x" * (app_tools.MAX_ANSWER_CHARS + 50))
    assert len(text) < app_tools.MAX_ANSWER_CHARS + 60 and "обрезано" in text
