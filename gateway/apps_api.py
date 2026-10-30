"""Приложения в API стенда (SAM-204, ADR-021): страница «Приложения», список, настройки и действия над приложениями.

Страница приложений - встроенное приложение `apps` (`gateway/builtin_apps.py`), как монитор: шлюз отдаёт её сам по
`PAGE_PATH`, её health check - `/health` шлюза. Docker страница не трогает: она зовёт ручки шлюза `API`, а
контейнерами управляет ядро (`gateway/apps.py`), встроенными - их команды (`builtin_apps.control`).

Защита - доступ шлюза (`gateway/access.py`): ключ `SAM_API_KEY` или эта машина, Host этой машины (DNS rebinding),
POST только с `Content-Type: application/json`. Сверх того действие с заголовком Origin чужой страницы получает 400,
а тип тела проверяется и здесь: роутер не теряет защиту, если его подключат без AccessMiddleware.

Встроенные приложения идут в списке первыми: запуск и остановка - командой компонента, удаляется только радар.
Живость плиток не проверяется: список - файлы с диска, он приходит в самой странице. Функции радара на плитке -
ручки его OpenAPI (`tools.apps.builtin_tools`, кэш на минуту); радар не отвечает - плитка без функций.

`make_router(root, run, builtin_run, http)`: корень приложений берётся в момент запроса, запуск docker, команды
встроенных и HTTP к ним приходят параметрами - роутер проверяется без Docker, сети и процессов.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import access, app_settings, app_view, apps, builtin_apps
from .tools import apps as apps_tool

API = "/sam/apps"
PAGE_PATH = builtin_apps.APPS_PATH
PAGE = Path(__file__).resolve().parent / "static" / "apps_panel.html"
APPS_MARK = "__APPS__"              # в apps_panel.html: список приложений JSON, плитки рисуются без запроса
ACTIONS = ("start", "stop", "delete", "pin", "unpin", "settings", "install")
START, STOP, DELETE, PIN, UNPIN, SETTINGS, INSTALL = ACTIONS
BAD_REQUEST, NOT_FOUND, CONFLICT = 400, 404, 409
BODY_HINT = "тело - JSON-объект, например {\"confirm\": \"<slug>\"}"
CONFIRM_HINT = "удаление - с подтверждением: {\"confirm\": \"<slug>\"}"
FOREIGN_PAGE = "запрос с чужой страницы"
# Страница несёт список в себе: из кэша браузера она показала бы старые плитки
PAGE_HEADERS = {"X-Frame-Options": "DENY", "Cache-Control": "no-store"}

Root = Callable[[], Path]


# ─── решения: карточки, тело, подтверждение ────────────────────────────────────

def card(m: apps.Manifest) -> dict:
    return {**asdict(m), "path": apps.page_path(m), "view": apps.view_path(m)}


def builtin_card(b: builtin_apps.Builtin, tools: list[dict]) -> dict:
    """Плитка встроенного: страница на шлюзе по slug, без порта самого сервиса; функции - его ручки."""
    path = builtin_apps.page_path(b)
    functions = [{"name": t["name"], "description": t["description"]} for t in tools]
    return {**asdict(b), "id": b.slug, "builtin": True, "path": path, "url": path, "functions": functions}


def listing(root: Path, http: apps_tool.Http | None = None) -> dict:
    """Список с диска, миллисекунды; ручки встроенных - из кэша или запросом к ним (до OPENAPI_TIMEOUT_S)."""
    builtins, builtin_errors = builtin_apps.scan(root)
    found, errors = apps.scan(root)
    tiles = [builtin_card(b, apps_tool.builtin_tools(b, http)) for b in builtins]
    return {"apps": tiles + [card(m) for m in found], "removed": builtin_apps.removed(root),
            "errors": builtin_errors + errors}


def parse_body(raw: bytes) -> dict:
    """Тело действия - JSON-объект; пустое - {}."""
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=BAD_REQUEST, detail=BODY_HINT)
    if not isinstance(body, dict):
        raise HTTPException(status_code=BAD_REQUEST, detail=BODY_HINT)
    return body


def confirm(body: dict, *accepted: str) -> None:
    """Удаление - только с подтверждением: slug (или id) приложения в теле."""
    if body.get("confirm") not in accepted:
        raise HTTPException(status_code=BAD_REQUEST, detail=CONFIRM_HINT)


def refusal(path: str, origin: str | None, host: str, content_type: str) -> tuple[int, str] | None:
    """Действие с чужой страницы - (статус, причина); своё - None."""
    if origin and access.origin_reason(origin, host, access.allowed_origins()):
        return BAD_REQUEST, FOREIGN_PAGE
    if reason := access.body_reason("POST", path, content_type):
        return access.UNSUPPORTED_MEDIA_TYPE, reason
    return None


def find(root: Path, ref: str) -> apps.Manifest:
    try:
        m = apps.find(root, ref)
    except apps.ManifestError as e:
        raise HTTPException(status_code=CONFLICT, detail=str(e))
    if m is None:
        raise HTTPException(status_code=NOT_FOUND, detail=f"приложения {ref} нет")
    return m


def render_page(data: dict) -> str:
    """Страница со списком внутри: плитки рисуются в первом кадре; «<» в JSON экранирован, тег script не закроется."""
    return PAGE.read_text(encoding="utf-8").replace(
        APPS_MARK, json.dumps(data, ensure_ascii=False).replace("<", app_view.JSON_LT))


# ─── действия ──────────────────────────────────────────────────────────────────

async def act(root: Path, ref: str, action: str, body: dict, run: apps.Runner,
              builtin_run: builtin_apps.Run | None = None) -> dict:
    """Действие над приложением по slug или id: приостановить и продолжить (stop, start), закрепить, настройки,
    удалить с подтверждением slug; встроенное - поставить снова (install). Docker - в потоке: команда идёт до
    DOCKER_TIMEOUT_S, а цикл шлюза в это время ведёт ходы и сокеты."""
    if action not in ACTIONS:
        raise HTTPException(status_code=NOT_FOUND, detail=f"действия {action} нет: {', '.join(ACTIONS)}")
    if action == INSTALL:
        error = await asyncio.to_thread(builtin_apps.install, root, ref)
        error = error or await builtin_apps.restart_surface(builtin_run)
        return {"ok": error is None, "text": error or f"{ref}: поставлено"}
    if b := await asyncio.to_thread(builtin_apps.find, root, ref):
        return await builtin_action(root, b, action, body, builtin_run)
    m = await asyncio.to_thread(find, root, ref)
    if action == STOP:
        await asyncio.to_thread(apps.stop, root, m, run)
        return {"ok": True, "text": f"{m.name}: приостановлено"}
    if action == START:
        error = await asyncio.to_thread(apps.start, root, m, None, run)
        return {"ok": error is None, "text": error or f"{m.name}: продолжает работу"}
    if action in (PIN, UNPIN):
        await asyncio.to_thread(apps.save, root, replace(m, pinned=action == PIN))
        return {"ok": True, "text": f"{m.name}: " + ("закреплено" if action == PIN else "откреплено")}
    if action == SETTINGS:
        saved, error = app_settings.save(root, m, body.get("values"))
        if error:
            raise HTTPException(status_code=BAD_REQUEST, detail=error)
        error = await asyncio.to_thread(apps.start, root, m, None, run) if m.wanted == apps.RUNNING else None
        return {"ok": error is None, "text": error or f"{m.name}: настройки сохранены", "values": saved}
    confirm(body, m.slug, m.id)
    error = await asyncio.to_thread(apps.delete, root, m, run)
    return {"ok": error is None, "text": error or f"{m.name} удалено"}


async def builtin_action(root: Path, b: builtin_apps.Builtin, action: str, body: dict,
                         builtin_run: builtin_apps.Run | None = None) -> dict:
    if action == DELETE:
        if not b.removable:
            raise HTTPException(status_code=BAD_REQUEST, detail=f"{b.name} не удаляется: это страница самого шлюза")
        confirm(body, b.slug)
        await builtin_apps.control(b, builtin_apps.STOP, builtin_run)       # не запущен - не страшно
        error = builtin_apps.remove(root, b.slug) or await builtin_apps.restart_surface(builtin_run)
        return {"ok": error is None, "text": error or f"{b.name}: снято; данные остались, вернуть - «поставить»"}
    if action in (PIN, UNPIN, SETTINGS):
        raise HTTPException(status_code=BAD_REQUEST, detail=f"{b.name}: встроенное, закреплено всегда, настроек нет")
    error = await builtin_apps.control(b, action, builtin_run)
    done = "продолжает работу" if action == START else "приостановлено"
    return {"ok": error is None, "text": error or f"{b.name}: {done}"}


# Удалить и поставить - только с этой машины, как у прежней панели на 127.0.0.1 (SAM-204): общий ключ API есть у
# телефона и голосового клиента, а удаление стирает приложение с данными
LOCAL_ONLY_ACTIONS = frozenset({DELETE, INSTALL})
FORBIDDEN = 403


def local_only(action: str, client_host: str | None, hosts: set[str]) -> tuple[int, str] | None:
    """Отказ (код, причина) для удаления и установки не с этой машины; иначе None."""
    if action in LOCAL_ONLY_ACTIONS and client_host not in hosts:
        return FORBIDDEN, f"{action} - только с этой машины: удаление и установка стирают и ставят приложения"
    return None


# ─── маршруты ──────────────────────────────────────────────────────────────────

def make_router(root: Root, run: apps.Runner | None = None, builtin_run: builtin_apps.Run | None = None,
                http: apps_tool.Http | None = None) -> APIRouter:
    router = APIRouter()

    @router.get(PAGE_PATH)
    async def page():
        data = await asyncio.to_thread(listing, root(), http)
        return HTMLResponse(render_page(data), headers=PAGE_HEADERS)

    @router.get(API)
    async def list_apps():
        return await asyncio.to_thread(listing, root(), http)

    @router.get(API + "/{app_ref}/settings")
    async def get_settings(app_ref: str):
        m = await asyncio.to_thread(find, root(), app_ref)
        return {"fields": app_settings.fields(m.settings), "values": app_settings.load(root(), m)}

    @router.post(API + "/{app_ref}/{action}")
    async def post_action(app_ref: str, action: str, request: Request):
        refused = refusal(request.url.path, request.headers.get("origin"), request.headers.get("host", ""),
                          request.headers.get("content-type", "")) \
            or local_only(action, request.client.host if request.client else None, access.local_hosts())
        if refused:
            raise HTTPException(status_code=refused[0], detail=refused[1])
        body = parse_body(await request.body())
        return await act(root(), app_ref, action, body, run or apps.docker_run, builtin_run)

    return router
