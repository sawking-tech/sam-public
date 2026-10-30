"""Пакеты песочницы из проверенных реестров (SAM-83, ADR-016).

Модель просит пакеты списком `pip:name==1.2.3` и `npm:name@1.2.3`: только точная версия из официального реестра
(PyPI, npm) - без диапазонов, URL, git, путей и флагов. До установки каждую версию проверяет OSV (osv.dev): есть
известные уязвимости или OSV не ответил - отказ, а не тихий пропуск. Установка идёт отдельным запуском контейнера
с сетью песочницы и не читает ничего из папки задачи: pip получает версии в командной строке и ставит только колёса
(`--only-binary=:all:`, чужой setup.py не выполняется) в `/work/.pkgs` из изолированного интерпретатора
(`python -I`: ни PYTHONPATH, ни sitecustomize из /work); npm ставит в чистой папке в /tmp по своему package.json с
`--ignore-scripts`, готовые node_modules копируются в /work - .npmrc, package-lock.json и overrides задачи не
участвуют. Зависимости ставит сам pip или npm; их список (pip list, npm ls) обязан прийти из вывода установки и
тоже проходит OSV. Lock-файлы задачи (`requirements.lock`, `package.json`) - запись того, что поставлено, из
разобранных пакетов; шлюз пишет их после успешной проверки.

Решения (разбор, тела запросов, вердикт, lock-файлы, команды) отделены от сети: HTTP-клиент приходит параметром.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable

import httpx

from .env import env_int

PIP, NPM = "pip", "npm"
ECOSYSTEM = {PIP: "PyPI", NPM: "npm"}
MAX_PACKAGES = 20                   # пакетов на один запрос: больше - уже не «поставить нужное», а окружение
# Реестры - официальные и не настраиваются: зеркало или свой индекс - ровно то, от чего защищает шаг
PYPI_INDEX = "https://pypi.org/simple"
NPM_REGISTRY = "https://registry.npmjs.org"
PKGS_DIR, NPM_DIR = ".pkgs", "node_modules"         # в папке задачи
PKGS_DIR_IN, NPM_DIR_IN = f"/work/{PKGS_DIR}", f"/work/{NPM_DIR}"   # то же в контейнере; .pkgs - в PYTHONPATH образа
NPM_BUILD_DIR = "/tmp/sam-npm"     # чистая папка установки npm: tmpfs своего запуска
PIP_LOCK, NPM_MANIFEST, NPM_LOCK = "requirements.lock", "package.json", "package-lock.json"
NPM_JOB_NAME = "sam-sandbox-job"
# Файлы, которыми задача могла бы направить установку мимо реестра или подменить записанное: при packages их
# нельзя передать через files
RESERVED_FILES = frozenset({PIP_LOCK, NPM_MANIFEST, NPM_LOCK, "npm-shrinkwrap.json", ".npmrc", "pip.conf",
                            "pip.ini", ".yarnrc", ".yarnrc.yml"})
RESERVED_DIRS = frozenset({PKGS_DIR, NPM_DIR})
# Запрос пачкой: одна поездка в OSV на все пакеты, ответ - список уязвимостей на каждый запрос по порядку
OSV_URL_ENV, DEFAULT_OSV_URL = "SAM_SANDBOX_OSV_URL", "https://api.osv.dev/v1/querybatch"
OSV_TIMEOUT_ENV, DEFAULT_OSV_TIMEOUT_S = "SAM_SANDBOX_OSV_TIMEOUT_S", 10
VULN_IDS_SHOWN = 5                  # столько id уязвимостей называет отказ
# Метки в выводе установки: после них - JSON со списком поставленного (pip list, npm ls)
PIP_LIST_MARK, NPM_LIST_MARK = "@@SAM-PIP-LIST@@", "@@SAM-NPM-LIST@@"
NPM_LOCK_IN = f"/work/{NPM_LOCK}"
# package-lock.json копируется вместе с node_modules до проверки зависимостей - при отказе уходит с ними
CLEANUP = f"rm -rf {PKGS_DIR_IN} {NPM_DIR_IN} {NPM_LOCK_IN}"

# fullmatch и ASCII: перевод строки в конце и юникодные цифры именем или версией не проходят
_PIP_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?", re.ASCII)
_PIP_VERSION = re.compile(r"\d{1,8}(?:\.\d{1,8}){0,5}(?:(?:a|b|rc)\d{1,8})?(?:\.post\d{1,8})?(?:\.dev\d{1,8})?",
                          re.ASCII)
_NPM_NAME = re.compile(r"(?:@[a-z0-9][a-z0-9._-]{0,100}/)?[a-z0-9][a-z0-9._-]{0,100}", re.ASCII)
_NPM_VERSION = re.compile(r"\d{1,8}\.\d{1,8}\.\d{1,8}(?:-[0-9A-Za-z.-]{1,64})?(?:\+[0-9A-Za-z.-]{1,64})?", re.ASCII)
_RANGE_CHARS = set("<>~^*!=| ")
_SOURCE_MARKERS = ("://", "git+", "file:", "link:", "github:")


@dataclass(frozen=True)
class Package:
    manager: str        # pip | npm
    name: str
    version: str

    @property
    def spec(self) -> str:
        return f"{self.name}=={self.version}" if self.manager == PIP else f"{self.name}@{self.version}"

    def __str__(self) -> str:
        return f"{self.manager}:{self.spec}"


# ─── разбор ───────────────────────────────────────────────────────────────────

def parse(items) -> tuple[list[Package], str | None]:
    """Список строк модели → пакеты; ошибка - одной строкой, тогда пакетов нет."""
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        return [], 'packages - список строк вида "pip:six==1.16.0" или "npm:left-pad@1.3.0"'
    if len(items) > MAX_PACKAGES:
        return [], f"packages: не больше {MAX_PACKAGES} пакетов за раз"
    packages = []
    for item in items:
        package, error = parse_one(item.strip())
        if error:
            return [], error
        packages.append(package)
    return packages, None


def parse_one(item: str) -> tuple[Package | None, str | None]:
    manager, sep, body = item.partition(":")
    if not sep or manager not in ECOSYSTEM:
        return None, f"Пакет {item!r}: нужен префикс pip: или npm:, например pip:six==1.16.0"
    if refusal := _source_refusal(item, body):
        return None, refusal
    return _parse_pip(item, body) if manager == PIP else _parse_npm(item, body)


def _source_refusal(item: str, body: str) -> str | None:
    if body.startswith("-"):
        return f"Пакет {item!r}: флаги установки (-e и другие) нельзя - только имя и точная версия"
    if any(m in body for m in _SOURCE_MARKERS) or body.startswith((".", "/", "~")) or "\\" in body:
        return f"Пакет {item!r}: URL, git и пути нельзя - только пакет из официального реестра"
    return None


def _parse_pip(item: str, body: str) -> tuple[Package | None, str | None]:
    name, sep, version = body.partition("==")
    if not sep:
        return None, _no_exact(item, body, f"pip:{body}==1.0.0")
    if not _PIP_NAME.fullmatch(name):
        return None, f"Пакет {item!r}: имя пакета PyPI без extras и пробелов, например pip:six==1.16.0"
    if not _PIP_VERSION.fullmatch(version):
        return None, f"Пакет {item!r}: нужна одна точная версия (==1.2.3), диапазоны и * нельзя"
    return Package(PIP, name, version), None


def _parse_npm(item: str, body: str) -> tuple[Package | None, str | None]:
    at = body.rfind("@")
    if at <= 0:     # нет версии, или только @scope без версии
        return None, _no_exact(item, body, f"npm:{body}@1.0.0")
    name, version = body[:at], body[at + 1:]
    if "/" in name and not name.startswith("@"):
        return None, f"Пакет {item!r}: URL, git и пути нельзя - только пакет из официального реестра"
    if not _NPM_NAME.fullmatch(name):
        return None, f"Пакет {item!r}: имя пакета npm строчными, например npm:left-pad@1.3.0"
    if not _NPM_VERSION.fullmatch(version):
        return None, f"Пакет {item!r}: нужна одна точная версия (@1.2.3), диапазоны, теги и * нельзя"
    return Package(NPM, name, version), None


def _no_exact(item: str, body: str, example: str) -> str:
    kind = "диапазон версий нельзя" if _RANGE_CHARS & set(body) else "нужна точная версия"
    return f"Пакет {item!r}: {kind}, например {example}"


def reserved(relative: str) -> bool:
    """Путь в папке задачи, который при packages нельзя передать через files: lock-файлы, конфиги менеджеров,
    папки пакетов. Без учёта регистра: на хосте Windows .NPMRC - тот же файл."""
    parts = [p.lower() for p in PurePosixPath(relative).parts]
    return bool(parts) and (parts[0] in RESERVED_DIRS or parts[-1] in RESERVED_FILES)


# ─── OSV ──────────────────────────────────────────────────────────────────────

def osv_payload(packages: list[Package]) -> dict:
    return {"queries": [{"package": {"name": p.name, "ecosystem": ECOSYSTEM[p.manager]}, "version": p.version}
                        for p in packages]}


def osv_verdict(packages: list[Package], answer) -> str | None:
    """Ответ OSV → отказ одной строкой с id уязвимостей или None, если все версии чистые."""
    results = answer.get("results") if isinstance(answer, dict) else None
    if not isinstance(results, list) or len(results) != len(packages):
        return "OSV ответил не на все пакеты: проверка не прошла, установки нет"
    found = []
    for package, result in zip(packages, results):
        result = {} if result is None else result
        vulns = result.get("vulns") or [] if isinstance(result, dict) else None
        if not isinstance(vulns, list) or not all(isinstance(v, dict) for v in vulns):
            return "OSV ответил не по формату: проверка не прошла, установки нет"
        ids = [str(v.get("id", "?")) for v in vulns]
        if ids:
            more = f" и ещё {len(ids) - VULN_IDS_SHOWN}" if len(ids) > VULN_IDS_SHOWN else ""
            found.append(f"{package} - {', '.join(ids[:VULN_IDS_SHOWN])}{more}")
    if not found:
        return None
    return f"У пакетов известные уязвимости (OSV): {'; '.join(found)}; возьмите другую версию"


def osv_check(packages: list[Package], client: httpx.Client | None = None) -> str | None:
    """Проверить версии в OSV. Отказ - одной строкой; OSV недоступен - тоже отказ: без проверки не ставим."""
    if not packages:
        return None
    timeout = env_int(OSV_TIMEOUT_ENV, DEFAULT_OSV_TIMEOUT_S)
    own = client is None
    client = client or httpx.Client()
    try:
        res = client.post(_osv_url(), json=osv_payload(packages), timeout=timeout)
        if res.status_code != 200:
            return f"OSV ответил {res.status_code}: пакеты не проверены, установки нет; повторите позже"
        return osv_verdict(packages, res.json())
    except httpx.TimeoutException:
        return f"OSV не ответил за {timeout} с: пакеты не проверены, установки нет; повторите позже"
    except (httpx.HTTPError, OSError, ValueError) as e:
        return f"OSV недоступен ({type(e).__name__}): пакеты не проверены, установки нет; проверьте интернет"
    finally:
        if own:
            client.close()


def _osv_url() -> str:
    return os.getenv(OSV_URL_ENV) or DEFAULT_OSV_URL


# ─── lock-файлы и команда установки ───────────────────────────────────────────

@dataclass(frozen=True)
class Plan:
    """Что ставить и что записать: пины pip и npm (прежние из lock-файлов задачи и новые) и выброшенные строки."""
    pip: tuple[Package, ...]
    npm: tuple[Package, ...]
    new: tuple[Package, ...]
    dropped: tuple[str, ...] = ()


def plan(packages: list[Package], existing: Callable[[str], str | None]) -> Plan:
    """Прежние пины задачи проходят тот же разбор, что и запрос; строка, которая не разобралась (флаг, URL,
    диапазон), выбрасывается и называется в `dropped`. `existing(имя)` - текст файла задачи или None."""
    old_pip, dropped = _old_pip(existing(PIP_LOCK) or "")
    old_npm, dropped_npm = _old_npm(existing(NPM_MANIFEST))
    pip = _merge(old_pip, [p for p in packages if p.manager == PIP])
    npm = _merge(old_npm, [p for p in packages if p.manager == NPM])
    return Plan(tuple(pip), tuple(npm), tuple(packages), tuple(dropped + dropped_npm))


def _key(p: Package) -> str:
    return re.sub(r"[-_.]+", "-", p.name).lower() if p.manager == PIP else p.name   # PEP 503: Six и six - один


def _merge(old: list[Package], new: list[Package]) -> list[Package]:
    merged = {_key(p): p for p in old}
    merged.update({_key(p): p for p in new})        # та же библиотека - новой версией
    return list(merged.values())


def _old_pip(text: str) -> tuple[list[Package], list[str]]:
    found, dropped = [], []
    for line in (s.strip() for s in text.splitlines()):
        if not line:
            continue
        package, error = parse_one(f"{PIP}:{line}")
        (dropped.append(f"{PIP_LOCK}: {line[:100]}") if error else found.append(package))
    return found, dropped


def _old_npm(text: str | None) -> tuple[list[Package], list[str]]:
    try:
        deps = (json.loads(text) if text else {}).get("dependencies") or {}
    except (ValueError, AttributeError):
        return [], [f"{NPM_MANIFEST}: не JSON-объект"]
    if not isinstance(deps, dict):
        return [], [f"{NPM_MANIFEST}: dependencies не объект"]
    found, dropped = [], []
    for name, version in deps.items():
        package, error = parse_one(f"{NPM}:{name}@{version}") if isinstance(version, str) else (None, "не строка")
        (dropped.append(f"{NPM_MANIFEST}: {str(name)[:60]}: {str(version)[:60]}") if error else found.append(package))
    return found, dropped


def manifest(npm: tuple[Package, ...]) -> str:
    """package.json установки и задачи: только имя, private и точные версии - без scripts, overrides и прочего."""
    return json.dumps({"name": NPM_JOB_NAME, "private": True, "dependencies": {p.name: p.version for p in npm}},
                      ensure_ascii=False, indent=2) + "\n"


def lock_files(p: Plan) -> dict[str, str]:
    """Lock-файлы задачи - запись поставленного; пишутся после успешной установки и проверки."""
    files = {}
    if p.pip:
        files[PIP_LOCK] = "".join(f"{x.spec}\n" for x in p.pip)
    if p.npm:
        files[NPM_MANIFEST] = manifest(p.npm)
    return files


def install_command(p: Plan) -> str:
    """Команда установки: только официальные реестры, ничего из папки задачи и без кода пакетов при установке;
    после неё - список поставленного JSON-ом за меткой. Пины прошли разбор, shlex.quote - на всякий случай."""
    steps = []
    new_pip = [x for x in p.new if x.manager == PIP]
    if new_pip:
        pins = " ".join(shlex.quote(x.spec) for x in new_pip)
        steps.append(f"cd /tmp && python -I -m pip install --isolated --only-binary=:all: --no-cache-dir "
                     f"--disable-pip-version-check --upgrade --target {PKGS_DIR_IN} --index-url {PYPI_INDEX} {pins}")
        steps.append(f"echo {PIP_LIST_MARK}")
        steps.append(f"python -I -m pip list --isolated --disable-pip-version-check --path {PKGS_DIR_IN} --format json")
    if any(x.manager == NPM for x in p.new):
        steps.append(f"rm -rf {NPM_BUILD_DIR} && mkdir -p {NPM_BUILD_DIR} && cd {NPM_BUILD_DIR}")
        steps.append(f"printf '%s' {shlex.quote(manifest(p.npm))} > {NPM_MANIFEST}")
        steps.append(f"npm install --ignore-scripts --no-audit --no-fund --registry {NPM_REGISTRY}")
        steps.append(f"echo {NPM_LIST_MARK}")
        steps.append("(npm ls --all --json 2>/dev/null || true)")     # ls ругается кодом на лишнее, JSON при этом есть
        steps.append(f"rm -rf {NPM_DIR_IN} && cp -r {NPM_DIR} {NPM_DIR_IN} && cp {NPM_LOCK} {NPM_LOCK_IN}")
    return " && ".join(steps)


# ─── что поставилось ──────────────────────────────────────────────────────────

def installed(output: str, managers: set[str]) -> tuple[list[Package], str | None]:
    """Поставленное по выводу установки. Для каждого менеджера из запроса метка и разобранный список обязательны:
    без них зависимости не проверить - отказ одной строкой, а не «проверено: 0»."""
    found = []
    if PIP in managers:
        pip = _json_after(output, PIP_LIST_MARK)
        if not isinstance(pip, list) or not all(isinstance(d, dict) for d in pip):
            return [], "Установка не вернула список пакетов pip: зависимости не проверены, поставленное убирается"
        found += [Package(PIP, str(d["name"]), str(d["version"])) for d in pip if d.get("name") and d.get("version")]
    if NPM in managers:
        npm = _json_after(output, NPM_LIST_MARK)
        if not isinstance(npm, dict):
            return [], "Установка не вернула список пакетов npm: зависимости не проверены, поставленное убирается"
        found += _npm_tree(npm.get("dependencies") or {})
    return sorted(set(found), key=str), None


def _npm_tree(deps: dict) -> list[Package]:
    found, stack = [], [deps]
    while stack:            # обход без рекурсии: дерево npm бывает глубоким
        level = stack.pop()
        if not isinstance(level, dict):
            continue
        for name, info in level.items():
            if isinstance(info, dict) and info.get("version"):
                found.append(Package(NPM, str(name), str(info["version"])))
                stack.append(info.get("dependencies") or {})
    return found


def _json_after(output: str, mark: str):
    _, sep, rest = output.partition(mark)
    if not sep:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(rest.lstrip())
        return value
    except ValueError:
        return None


def unchecked(all_installed: list[Package], requested: list[Package]) -> list[Package]:
    """Поставленное сверх запрошенного: зависимости, которые OSV ещё не видел."""
    asked = {(p.manager, _key(p), p.version) for p in requested}
    return [p for p in all_installed if (p.manager, _key(p), p.version) not in asked]
