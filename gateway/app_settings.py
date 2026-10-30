"""Настройки и опции каркаса живого приложения: что объявлено в манифесте и что выбрал человек.

Приложение объявляет настройки полем `settings` манифеста: список полей {name, type, default, label, options}.
Выбранные значения лежат в `SAM_APPS_DIR/<id>/settings.json` - вне `src/`, приложение их не перепишет. Страница
читает их адресом `sam-settings.json` своего приложения: шлюз отдаёт этот путь из файла настроек, не из контейнера
(`gateway/main.py`). Так их читает и страница без сервера (`python -m http.server`), которой переменные окружения
контейнера не видны; сохранение перезапускает приложение, и страница загружается с новыми.

Поле `shell` манифеста - переопределение каркаса страницы (`docs/features/shell.md`): true (по умолчанию) - весь
каркас, false или {off: true} - без него, {bar: false, panelw: 360} - свои опции. Другого доступа к каркасу у приложения нет.

Решение (поля, проверка значений, опции каркаса) отделено от файлов: `values`, `check`, `shell_options` чистые.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import apps

SETTINGS_FILE = "settings.json"
PAGE_PATH = "sam-settings.json"     # путь, по которому страница приложения читает свои настройки
TEXT, NUMBER, BOOL, CHOICE = "text", "number", "bool", "choice"
TYPES = (TEXT, NUMBER, BOOL, CHOICE)
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_FIELDS = 20                     # форма в панели, не конфиг сервиса
MAX_TEXT = 2000
PANELW_RANGE = range(200, 801)      # ширина панели каркаса, px: уже - форма не помещается, шире - закрывает страницу


def fields(declared) -> list[dict]:
    """Годные поля объявления: имя латиницей, известный тип, у choice - варианты; негодное отбрасывается."""
    out = []
    for f in declared if isinstance(declared, list) else []:
        if not isinstance(f, dict) or not NAME_RE.match(str(f.get("name", ""))) or f.get("type") not in TYPES:
            continue
        options = [str(o) for o in f.get("options") or []] if f["type"] == CHOICE else []
        if f["type"] == CHOICE and not options:
            continue
        item = {"name": f["name"], "type": f["type"], "label": str(f.get("label") or f["name"])[:120],
                "default": f.get("default"), "options": options}
        if check(item, item["default"]) is not None:
            item["default"] = {TEXT: "", NUMBER: 0, BOOL: False, CHOICE: options[0] if options else ""}[f["type"]]
        out.append(item)
        if len(out) == MAX_FIELDS:
            break
    return out


def check(f: dict, value) -> str | None:
    """Почему значение не подходит полю; None - подходит."""
    kind = f["type"]
    if kind == TEXT and not (isinstance(value, str) and len(value) <= MAX_TEXT):
        return f"{f['label']}: текст до {MAX_TEXT} знаков"
    if kind == NUMBER and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return f"{f['label']}: нужно число"
    if kind == BOOL and not isinstance(value, bool):
        return f"{f['label']}: да или нет"
    if kind == CHOICE and value not in f["options"]:
        return f"{f['label']}: одно из {', '.join(f['options'])}"
    return None


def values(declared, saved: dict | None) -> dict:
    """Значения по полям: сохранённое, если оно годно, иначе значение по умолчанию; лишнее не попадает."""
    saved = saved if isinstance(saved, dict) else {}
    return {f["name"]: saved[f["name"]] if f["name"] in saved and check(f, saved[f["name"]]) is None else f["default"]
            for f in fields(declared)}


def merge(declared, current: dict, incoming: dict) -> tuple[dict | None, str | None]:
    """Новые значения поверх текущих; (значения, None) или (None, ошибка одной строкой)."""
    if not isinstance(incoming, dict):
        return None, "настройки - объект {имя: значение}"
    known = {f["name"]: f for f in fields(declared)}
    unknown = [k for k in incoming if k not in known]
    if unknown:
        return None, f"таких настроек нет: {', '.join(unknown)}"
    for name, value in incoming.items():
        if error := check(known[name], value):
            return None, error
    return {**current, **incoming}, None


def shell_options(shell) -> dict:
    """Опции каркаса из поля shell: {"off": bool, "bar": bool, "panelw": int | None}; негодное - по умолчанию."""
    if shell is False or (isinstance(shell, dict) and shell.get("off") is True):
        return {"off": True, "bar": True, "panelw": None}
    opts = shell if isinstance(shell, dict) else {}
    panelw = opts.get("panelw")
    return {"off": False, "bar": opts.get("bar") is not False,
            "panelw": panelw if isinstance(panelw, int) and panelw in PANELW_RANGE else None}


def _path(root: Path, m: apps.Manifest) -> Path:
    return Path(root) / m.id / SETTINGS_FILE


def load(root: Path, m: apps.Manifest) -> dict:
    """Текущие значения настроек приложения; файла нет или он битый - значения по умолчанию."""
    try:
        saved = json.loads(_path(root, m).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return values(m.settings, saved)


def save(root: Path, m: apps.Manifest, incoming: dict) -> tuple[dict | None, str | None]:
    """Проверить и записать новые значения; перезапуск - у вызывающего."""
    merged, error = merge(m.settings, load(root, m), incoming)
    if error:
        return None, error
    apps.write_atomic(_path(root, m), json.dumps(merged, ensure_ascii=False, indent=2))
    return merged, None
