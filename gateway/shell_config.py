"""Настройка каркаса страниц SAM (gateway/static/shell/): навигация полосы и ключ памяти браузера.

Шлюз отдаёт её скриптом `/shell/config.js`: страницы на других портах (радар) подключают его тегом script, без
запроса к API шлюза с чужого origin. Ссылка навигации - порт и путь: хост браузер берёт свой,
поэтому та же навигация работает с этой машины и с другого устройства.

Решение (`nav`) отделено от отдачи (`config_js`): проверяется без шлюза.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import apps, builtin_apps

STORAGE_KEY = "sam"                 # префикс памяти браузера: тема, сторона и открытость панели
MONITOR_KEY, APPS_KEY = apps.MONITOR_SLUG, apps.APPS_SLUG
APPS_TITLE = "Приложения"


def nav(root: Path, gateway_port: int | None) -> list[dict]:
    """Страницы SAM по порядку (docs/features/shell.md): «Приложения» - домашний экран, затем закреплённые:
    монитор, радар (если поставлен), свои закреплённые - новые первыми. Список строится из манифестов; все страницы
    - на шлюзе (приложения - его страница с SAM-204)."""
    builtins = {b.slug: b for b in builtin_apps.scan(root)[0]}
    items = [{"key": APPS_KEY, "name": APPS_TITLE, "port": gateway_port, "path": builtin_apps.APPS_PATH},
             {"key": MONITOR_KEY, "name": "Монитор", "port": gateway_port, "path": builtin_apps.MONITOR_PATH}]
    radar = builtins.get(apps.RADAR_SLUG)
    if radar:                       # радар - отдельный процесс, шлюз отдаёт его по /apps/radar/ (gateway/builtin_proxy.py)
        items.append({"key": radar.slug, "name": radar.name, "port": gateway_port, "path": builtin_apps.page_path(radar)})
    pinned = sorted((m for m in apps.all_apps(root) if m.pinned), key=lambda m: (m.created, m.id), reverse=True)
    items += [{"key": m.slug, "name": m.name, "port": gateway_port, "path": apps.view_path(m), "pinned": True}
              for m in pinned]
    return items


def config_js(items: list[dict]) -> str:
    return (f"window.SITE = {json.dumps({'key': STORAGE_KEY})};\n"
            f"window.SAM_SHELL = {json.dumps({'nav': items}, ensure_ascii=False)};\n")
