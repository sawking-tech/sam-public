"""Инструменты живых приложений SAM (SAM-183): сделать, поправить, показать список, вызвать ручку, остановить.

Код приложения пишет сам воркер, как для `sandbox_run`; приложение живёт по `gateway/apps.py` (SAM-182). Агент кода
внутри песочницы - отдельный открытый пункт SAM-83: у контейнера нет сети до API модели.

Тонкие функции `*_impl(params, root, emit, run)`: корень приложений, отправка события в монитор и запуск docker
приходят параметрами - проверяются без Docker и шлюза.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .. import app_settings, app_tools, apps, builtin_apps, builtin_proxy, sandbox

Emit = Callable[[dict], None]
# Запрос ручки приложения: (метод, адрес, query, тело, timeout=, headers=) -> (код, текст); подменяется в тестах.
# headers передаётся только встроенному приложению: ключ доступа (builtin_proxy.auth_headers)
Http = Callable[..., tuple[int, str]]
APP_CALL_TIMEOUT_S = 30    # как прокси шлюза к приложению: дольше - ручка зависла
# Вызов с файлом приложения обрезан пределом ответа модели (SAM-210): как разделить файлы по вызовам
SPLIT_ADVICE = "раздели файл - app_make с частью, остальное app_update или несколько файлов, или пиши короче"
SPLIT_HINTS = {"app_make": SPLIT_ADVICE, "app_update": SPLIT_ADVICE}


def _shown(m: apps.Manifest, error: str | None, emit: Emit, action: str) -> str:
    page = apps.page_path(m)       # монитор показывает страницу во фрейме со своего хоста
    emit({"app": m.slug, "id": m.id, "slug": m.slug, "name": m.name, "action": action, "ok": error is None,
          "preview": None if error else page, "url": apps.page_url(m), "text": error or f"{m.name}: {page}"})
    if error:
        return error
    return (f"Приложение {m.name} {action}: slug {m.slug}, адрес {apps.page_url(m)}, в мониторе показано; "
            f"правка - app_update(app={m.slug})")


def _find(root: Path, ref: str) -> tuple[apps.Manifest | None, str | None]:
    """Приложение по slug или id; по имени не ищется: правят только названное явно (SAM-205). Не нашлось - строка
    ошибки, с подсказкой slug, если ref - имя приложения."""
    ref = (ref or "").strip()
    if b := builtin_apps.find(root, ref):
        how = f"перезапуск - restart_component {b.control}" if b.control else builtin_apps.command(b, "")[1]
        return None, f"{b.slug} - встроенное приложение, не правится инструментами приложений: {how}"
    try:
        m = apps.find(root, ref)
    except apps.ManifestError as e:
        return None, str(e)
    if m:
        return m, None
    named = sorted(a.slug for a in apps.all_apps(root) if a.name == ref)
    hint = f"; с таким именем: {', '.join(named)}" if named else ""
    return None, f"Приложения со slug или id {ref!r} нет{hint}: список - app_list"


EXTRAS = ("pinned", "settings", "shell", "tools", "icon")   # поля манифеста от SAM: без кода и без перезапуска


def _extras(params: dict) -> tuple[dict, str | None]:
    """pinned, settings, shell, tools, icon из вызова: проверенные поля манифеста или ошибка одной строкой."""
    out = {}
    if "pinned" in params:
        if not isinstance(params["pinned"], bool):
            return {}, "pinned - true или false"
        out["pinned"] = params["pinned"]
    if "settings" in params:
        declared = app_settings.fields(params["settings"])
        if len(declared) != len(params["settings"] or []):
            return {}, ("settings - список полей {name: латиница, type: text|number|bool|choice, default, label, "
                        "options для choice}")
        out["settings"] = declared
    if "shell" in params:
        if not isinstance(params["shell"], (bool, dict)):
            return {}, "shell - true, false или {bar: false, panelw: 360}"
        out["shell"] = params["shell"]
    if "tools" in params:
        tools = app_tools.declared(params["tools"])
        if len(tools) != len(params["tools"] or []):
            return {}, app_tools.DECLARATION_HINT
        out["tools"] = tools
    if "icon" in params:
        if error := apps.icon_error(params["icon"]):
            return {}, error
        out["icon"] = params["icon"].strip()
    return out, None


def app_make_impl(params: dict, root: Path, emit: Emit, run: apps.Runner | None = None,
                  sandbox_root: Path | None = None, owner: sandbox.Owner | None = None) -> str:
    run = run or apps.docker_run          # в момент вызова: подмена в тесте и настройка видны
    name, command = str(params.get("name") or "").strip(), str(params.get("command") or "").strip()
    files = params.get("files") or {}
    from_job = str(params.get("from_job") or "").strip()
    if not name or not command or not isinstance(files, dict) or not (files or from_job):
        return ("Нужны name, command (например, python -m http.server 8000) и files {имя: текст} или from_job - "
                "задача песочницы, где приложение собрано")
    extras, error = _extras(params)
    if error:
        return error
    m, error = apps.create(root, name, command, files, description=str(params.get("description") or ""),
                           by=str(params.get("by") or ""),
                           request=str(params.get("request") or ""), run=run, from_job=from_job,
                           from_path=str(params.get("from_path") or ""), sandbox_root=sandbox_root,
                           owner=owner, extras=extras)
    if m is None:
        return error
    return _shown(m, error, emit, "создано")


def app_update_impl(params: dict, root: Path, emit: Emit, run: apps.Runner | None = None,
                    sandbox_root: Path | None = None, owner: sandbox.Owner | None = None) -> str:
    run = run or apps.docker_run          # в момент вызова: подмена в тесте и настройка видны
    m, error = _find(root, str(params.get("app") or ""))
    if m is None:
        return error
    files = params.get("files") or {}
    if not isinstance(files, dict):
        return "files - объект {имя: текст}"
    from_job = str(params.get("from_job") or "").strip()
    extras, error = _extras(params)
    if error:
        return error
    if extras:
        m = replace(m, **extras)
        apps.save(root, m)
    if extras and not (files or from_job or params.get("command")):     # только поля манифеста - без перезапуска
        what = {True: "закреплено", False: "откреплено"}.get(extras.get("pinned"), "обновлено")
        return f"Приложение {m.name} ({m.slug}) {what}"
    m, error = apps.update(root, m, files, str(params.get("command") or ""), run=run, from_job=from_job,
                           from_path=str(params.get("from_path") or ""), sandbox_root=sandbox_root,
                           owner=owner, clean=params.get("clean") is True)
    changed = files or from_job or params.get("command")      # без правок - перезапуск
    return _shown(m, error, emit, "обновлено" if changed else "запущено")


def _builtin_line(b: builtin_apps.Builtin, health: builtin_apps.Health | None, http: Http | None) -> str:
    up = builtin_apps.is_up(b, health)
    tools = builtin_tools(b, http) if up else []
    return (f"{b.slug} {b.name} - встроенное, {'работает' if up else 'не отвечает'}, {builtin_apps.page_path(b)}"
            + (f"; ручки app_call: {app_tools.describe(tools)}" if tools else ""))


def _app_line(m: apps.Manifest, http: Http | None) -> str:
    tools = tools_of(m, http)
    return (f"{m.slug} {m.name} - {m.wanted}, {apps.page_path(m)}, сделано {m.created or '?'}, id {m.id}"
            + (f" ({m.description})" if m.description else "")
            + (f"; ручки app_call: {app_tools.describe(tools)}" if tools else ""))


def app_list_impl(params: dict, root: Path, health: builtin_apps.Health | None = None, http: Http | None = None) -> str:
    builtins, builtin_errors = builtin_apps.scan(root)
    found, errors = apps.scan(root)
    newest = sorted(found, key=lambda m: (m.created, m.id), reverse=True)     # новые сверху
    lines = [_app_line(m, http) for m in newest]
    return "\n".join([_builtin_line(b, health, http) for b in builtins] + builtin_errors + lines + errors) or "Приложений нет"


def http_request(method: str, url: str, query: dict, body: bytes | None,
                 timeout: float = APP_CALL_TIMEOUT_S, headers: dict | None = None) -> tuple[int, str]:
    import httpx
    sent = {**(headers or {}), **({"content-type": "application/json"} if body is not None else {})}
    res = httpx.request(method, url, params=query, content=body, headers=sent, timeout=timeout)
    return res.status_code, res.text


OPENAPI_TIMEOUT_S = 2      # описание API читается на каждом app_list: приложение без него не задерживает список
OPENAPI_CACHE_S = 60       # описание меняется только правкой приложения, а app_list зовут часто
_openapi_cache: dict[str, tuple[float, list[dict]]] = {}


def _openapi_tools(key: str, base: str, http: Http, extra: dict) -> list[dict]:
    """Ручки из OpenAPI по адресу base, из кэша на OPENAPI_CACHE_S; нет описания - пусто. Ответ без описания (404,
    не JSON) кэшируется, обрыв связи - нет: поднятое приложение видно со следующего вызова."""
    now = time.monotonic()
    cached = _openapi_cache.get(key)
    if cached and now - cached[0] < OPENAPI_CACHE_S:
        return cached[1]
    try:
        status, text = http(app_tools.GET, f"{base}/{app_tools.OPENAPI_PATH}", {}, None, timeout=OPENAPI_TIMEOUT_S,
                            **extra)
    except Exception:       # не отвечает: ручки только из манифеста
        return []
    try:
        found = app_tools.from_openapi(json.loads(text)) if status == 200 else []
    except ValueError:      # не JSON: у приложения нет API
        found = []
    _openapi_cache[key] = (now, found)
    return found


def _api_tools(m: apps.Manifest, http: Http) -> list[dict]:
    """Ручки из OpenAPI запущенного приложения."""
    return _openapi_tools(m.id, apps.local_base(m), http, {}) if m.wanted == apps.RUNNING else []


def tools_of(m: apps.Manifest, http: Http | None = None) -> list[dict]:
    """Все ручки приложения: объявленные в манифесте и из его OpenAPI."""
    return app_tools.merged(m.tools, _api_tools(m, http or http_request))


BUILTIN_CACHE_PREFIX = "builtin:"    # ключ кэша OpenAPI встроенного: не пересекается с id живого приложения


def _builtin_auth() -> dict:
    return {"headers": builtin_proxy.auth_headers()}


def builtin_tools(b: builtin_apps.Builtin, http: Http | None = None) -> list[dict]:
    """Ручки встроенного приложения отдельным процессом (радар): методы его OpenAPI с меткой, запрос с ключом
    доступа, как у прокси шлюза. Монитор - страница шлюза, ручек у него нет."""
    if not builtin_apps.is_proxied(b):
        return []
    return _openapi_tools(BUILTIN_CACHE_PREFIX + b.slug, builtin_proxy.base_of(b), http or http_request,
                          _builtin_auth())


def _call(slug: str, base: str, tools: list[dict], params: dict, http: Http, extra: dict) -> str:
    """Ручка из списка: есть, аргументы - объявленные; ответ - текст для модели."""
    tool = app_tools.find(tools, str(params.get("tool") or ""))
    if tool is None:
        return f"У {slug} нет ручки {params.get('tool')!r}; есть: {app_tools.describe(tools)}"
    call, error = app_tools.build(tool, params.get("args"))
    if call is None:
        return error
    try:
        status, text = http(call.method, f"{base}/{call.path}", call.query, call.body, **extra)
    except Exception as e:      # сеть, таймаут, приложение упало: модель видит причину одной строкой
        return f"ручка {tool['name']} приложения {slug} не ответила: {type(e).__name__}: {e}"
    return app_tools.answer(status, text)


def _builtin_call(b: builtin_apps.Builtin, params: dict, http: Http) -> str:
    tools = builtin_tools(b, http)
    if not tools:
        why = (f"{b.name} не отвечает: запусти restart_component {b.control}" if b.control
               else f"у {b.slug} ручек для SAM нет")
        return f"{why}; список - app_list"
    return _call(b.slug, builtin_proxy.base_of(b), tools, params, http, _builtin_auth())


def app_call_impl(params: dict, root: Path, http: Http | None = None) -> str:
    """Ручка приложения (манифест или OpenAPI с меткой): приложение запущено, ручка есть, аргументы - объявленные.
    Встроенное (радар) - ручки из его OpenAPI, вызов на его адрес с ключом доступа."""
    http = http or http_request
    if b := builtin_apps.find(root, str(params.get("app") or "")):
        return _builtin_call(b, params, http)
    m, error = _find(root, str(params.get("app") or ""))
    if m is None:
        return error
    if m.wanted != apps.RUNNING:
        return f"Приложение {m.slug} остановлено: запусти app_update(app={m.slug}) и повтори"
    tools = tools_of(m, http)
    if not tools:
        return (f"У приложения {m.slug} ручек нет: ни OpenAPI ({app_tools.OPENAPI_PATH}), ни поля tools в манифесте "
                f"({app_tools.DECLARATION_HINT})")
    return _call(m.slug, apps.local_base(m), tools, params, http, {})


def app_stop_impl(params: dict, root: Path, run: apps.Runner | None = None) -> str:
    run = run or apps.docker_run          # в момент вызова: подмена в тесте и настройка видны
    m, error = _find(root, str(params.get("app") or ""))
    if m is None:
        return error
    apps.stop(root, m, run)
    return f"Приложение {m.name} ({m.slug}) остановлено; папка и код остались"
