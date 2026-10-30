"""Ручки приложения для SAM: методы его OpenAPI с меткой и инструменты, которые оно объявляет в манифесте.

Приложение с OpenAPI метит методы-ручки (`TOOL_MARK` или тег `TOOL_TAG`); простой сервер объявляет их полем `tools`
манифеста: список {name, description, method, path, params}. `path` - относительный путь API приложения, `params` -
{имя: что это}. SAM вызывает ручку инструментом `app_call(app, tool, args)`: GET передаёт args строкой запроса,
POST - телом JSON. Запрос живого приложения идёт на его пограничный контейнер (`apps.local_base`), как прокси шлюза,
поэтому ручка видит то же, что страница; встроенного (радар) - на его адрес с ключом доступа.

Решение (годные объявления, проверка аргументов, сборка запроса, ответ модели) отделено от сети: функции чистые.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

GET, POST = "GET", "POST"
METHODS = (GET, POST)
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# Путь API внутри приложения: без схемы, хоста, «..» и обратного слэша - запрос не уходит за пределы приложения
PATH_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9_\-./]{1,200}$")
MAX_TOOLS = 20             # ручек на приложение: это его API для SAM, а не весь сервер
MAX_DESCRIPTION = 300
MAX_PARAMS = 20
MAX_ANSWER_CHARS = 4000    # ответ ручки в контекст модели: сводка или записи дня, не дамп базы
DECLARATION_HINT = ("tools - список ручек {name: латиница, description, method: GET|POST, path: путь API приложения, "
                    "params: {имя: что это}}")


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    query: dict
    body: bytes | None


def declared(items) -> list[dict]:
    """Годные объявления: имя латиницей, GET или POST, путь внутри приложения; негодное отбрасывается."""
    out = []
    for t in items if isinstance(items, list) else []:
        if not isinstance(t, dict) or not NAME_RE.match(str(t.get("name", ""))):
            continue
        method = str(t.get("method") or GET).upper()
        path = str(t.get("path") or "").lstrip("/")
        params = t.get("params") or {}
        if method not in METHODS or not PATH_RE.match(path) or not isinstance(params, dict):
            continue
        out.append({"name": t["name"], "description": str(t.get("description") or "")[:MAX_DESCRIPTION],
                    "method": method, "path": path,
                    "params": {str(k): str(v)[:MAX_DESCRIPTION] for k, v in list(params.items())[:MAX_PARAMS]}})
        if len(out) == MAX_TOOLS:
            break
    return out


OPENAPI_PATH = "openapi.json"     # там его отдаёт FastAPI; другой сервер с OpenAPI кладёт его по тому же пути
# Метка метода-ручки: у приложения API для интеграций может быть на 20 методов, а SAM нужны 4. В FastAPI -
# openapi_extra={"x-sam-tool": True} у маршрута или тег "sam"; метод без метки SAM не видит
TOOL_MARK = "x-sam-tool"
TOOL_TAG = "sam"
_NON_NAME = re.compile(r"[^a-z0-9_]+")


def marked(operation: dict) -> bool:
    return bool(operation.get(TOOL_MARK)) or TOOL_TAG in (operation.get("tags") or [])


def _tool_name(method: str, path: str, operation: dict) -> str:
    raw = str(operation.get("operationId") or f"{method}_{path.strip('/')}").lower()
    name = _NON_NAME.sub("_", raw).strip("_")
    return name if name[:1].isalpha() else f"api_{name}"


REF_PREFIX = "#/components/schemas/"     # FastAPI кладёт схему тела-модели pydantic туда и ссылается на неё


def _schema(spec: dict, schema) -> dict:
    """Схема тела; локальная ссылка $ref на components.schemas раскрывается, чужая - пустая схема."""
    if not isinstance(schema, dict):
        return {}
    ref = str(schema.get("$ref") or "")
    if not ref:
        return schema
    found = ((spec.get("components") or {}).get("schemas") or {}).get(ref[len(REF_PREFIX):])         if ref.startswith(REF_PREFIX) else None
    return found if isinstance(found, dict) else {}


def from_openapi(spec) -> list[dict]:
    """Ручки из описания OpenAPI приложения: методы с меткой TOOL_MARK или тегом TOOL_TAG, GET и POST без
    параметров пути; аргументы - параметры запроса и поля JSON тела. Негодное описание - пустой список: приложение
    без API ничем не хуже."""
    out = []
    paths = spec.get("paths") if isinstance(spec, dict) else None
    for path, ops in (paths if isinstance(paths, dict) else {}).items():
        if not isinstance(ops, dict) or "{" in str(path):
            continue
        for method, op in ops.items():
            if method.upper() not in METHODS or not isinstance(op, dict) or not marked(op):
                continue
            params = {str(p.get("name")): str(p.get("description") or p.get("name"))
                      for p in op.get("parameters") or [] if isinstance(p, dict) and p.get("in") == "query"}
            body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
            for key, prop in (_schema(spec, body).get("properties") or {}).items():
                params[str(key)] = str((prop or {}).get("description") or (prop or {}).get("title") or key)
            out.append({"name": _tool_name(method, str(path), op), "method": method.upper(), "path": str(path),
                        "description": str(op.get("summary") or op.get("description") or ""), "params": params})
    return declared(out)


def merged(manifest_tools: list[dict], api_tools: list[dict]) -> list[dict]:
    """Ручки манифеста и OpenAPI вместе; при совпадении имени верх берёт манифест - его писали для SAM."""
    names = {t["name"] for t in manifest_tools}
    return (manifest_tools + [t for t in api_tools if t["name"] not in names])[:MAX_TOOLS]


def describe(tools: list[dict]) -> str:
    """Ручки одной строкой для app_list: имя(параметры) - что делает."""
    return "; ".join(f"{t['name']}({', '.join(t['params'])}) - {t['description']}".rstrip(" -") for t in tools)


def find(tools: list[dict], name: str) -> dict | None:
    return next((t for t in tools if t["name"] == name), None)


def build(tool: dict, args) -> tuple[Call | None, str | None]:
    """Запрос ручки из аргументов модели; лишний аргумент - ошибка одной строкой с объявленными."""
    args = args if isinstance(args, dict) else {}
    extra = sorted(set(args) - set(tool["params"]))
    if extra:
        known = ", ".join(tool["params"]) or "нет"
        return None, f"у ручки {tool['name']} нет аргументов {', '.join(extra)}; объявлены: {known}"
    if tool["method"] == GET:
        query = {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in args.items()}
        return Call(GET, tool["path"], query, None), None
    return Call(POST, tool["path"], {}, json.dumps(args, ensure_ascii=False).encode()), None


def answer(status: int, text: str) -> str:
    """Ответ ручки для модели: код, если не 2xx, и текст не длиннее MAX_ANSWER_CHARS."""
    clipped = text if len(text) <= MAX_ANSWER_CHARS else text[:MAX_ANSWER_CHARS] + f"… [обрезано, всего {len(text)} знаков]"
    return clipped if 200 <= status < 300 else f"ручка ответила {status}: {clipped}"
