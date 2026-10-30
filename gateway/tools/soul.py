"""soul_show / soul_edit / soul_rollback - SAM смотрит и правит свою душу (характер в хранилище).

Инструкции (SAM.md) и личный слой (data/SAM.md) - не душа: эти инструменты их не показывают и не правят.

Правка и откат привилегированные (gateway/command_auth.py PRIVILEGED_TOOLS): только в ходе с ключом команд.
Работу делает сервис души (gateway/soul_service.py), здесь - разбор параметров и ответ модели текстом.

Использование (примеры):
- «покажи свой характер» → soul_show(section="CHARACTER")
- «какие у тебя разделы души» → soul_show()
- «перестань шутить про погоду» (с ключом) → soul_show, затем soul_edit(section="CHARACTER", version=<из soul_show>,
  text=<весь новый текст>)
- «верни как было» (с ключом) → soul_rollback(section="CHARACTER")
"""
from __future__ import annotations

AUTHOR = "sam"
# Внешний текст в ходе (gateway/turn_taint.py) мог принести инструкцию переписать душу
EXTERNAL_REFUSAL = "Правка души - отдельным ходом с ключом, без внешних данных в ходе."
PREVIEW_CHARS = 80   # строка истории: начало текста версии, чтобы узнать её без полного текста


def _when(entry: dict) -> str:
    value = entry.get("updated_at")
    return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "strftime") else "?"


def _section(params: dict) -> str:
    return str(params.get("section") or "").strip()


def soul_show_impl(params: dict, service) -> str:
    """Без section - список разделов; с section - текст; history=true - версии раздела."""
    if not service.available:
        return "Хранилище души недоступно (Mongo)."
    name = _section(params)
    if not name:
        rows = [f"{e['_id']}: версия {e.get('version')}, {_when(e)}, {e.get('author', '')}" for e in service.entries()]
        return "Разделы души:\n" + "\n".join(rows)
    if params.get("history"):
        versions = service.history(name)
        if not versions:
            return f"Нет раздела {name}."
        return "\n".join(f"версия {v.get('version')} ({_when(v)}, {v.get('author', '')}): "
                         f"{(v.get('text') or '')[:PREVIEW_CHARS]!r}" for v in versions)
    doc = service.show(name)
    if not doc:
        return f"Нет раздела {name}. Список разделов - soul_show без section."
    return f"{name}, версия {doc.get('version')} ({_when(doc)}, {doc.get('author', '')}):\n{doc.get('text', '')}"


def _version(raw) -> int | None:
    return int(raw) if raw not in (None, "") else None


def soul_edit_impl(params: dict, service, external: frozenset = frozenset()) -> str:
    """version - версия раздела из soul_show, поверх которой правка: изменился с тех пор - отказ, перечитать.
    external - источники внешнего текста в этом ходе: есть - отказ."""
    if external:
        return EXTERNAL_REFUSAL
    name, text = _section(params), params.get("text")
    if not name or not isinstance(text, str):
        return "Нужны section и text - весь новый текст раздела."
    try:
        expected = _version(params.get("version"))
    except (TypeError, ValueError):
        expected = None
    if expected is None:
        return "Нужен version - версия раздела из soul_show, поверх которой правка."
    result = service.write(name, text, AUTHOR, expected)
    return f"OK: {result.message}" if result.ok else f"Не записано: {result.message}"


def soul_rollback_impl(params: dict, service, external: frozenset = frozenset()) -> str:
    if external:
        return EXTERNAL_REFUSAL
    name = _section(params)
    if not name:
        return "Нужен section."
    raw = params.get("version")
    try:
        version = _version(raw)
    except (TypeError, ValueError):
        return f"version - номер версии из soul_show(history=true), получено {raw!r}"
    result = service.rollback(name, version, AUTHOR)
    return f"OK: {result.message}" if result.ok else f"Не откатилось: {result.message}"
