"""Команды монитора по «/» (SAM-99): скиллы стенда и команды текущей основной модели.

Стенд от модели не зависит (ADR-018): список собирает шлюз - скиллы из оглавления SAM-61 (проектные
`.claude/skills`) и команды, которые сообщает сама основная модель полем контракта `commands`
(gateway/main_model.py); у модели без команд её часть пуста. Выбранная команда уходит основной модели командой,
а не фразой: модели вне шлюза - полем `command` во входящих, модели в шлюзе - строкой с меткой COMMAND_MARK.

Без сети и FastAPI: на вход оглавление скиллов и модель, на выход список и строки.
"""
from __future__ import annotations

from typing import Any

SKILL_SOURCE = "skill"
PREFIX = "/"
# Метка команды в тексте хода для модели в шлюзе: у неё нет поля входящих, команду она видит строкой
COMMAND_MARK = "[команда монитора]"


def available(skills: list[Any], model: Any) -> list[dict]:
    """Скиллы стенда, потом команды модели (её имя - источник); имя, уже занятое скиллом, модель не перекрывает."""
    out = [{"name": s.name, "description": s.description, "source": SKILL_SOURCE} for s in skills]
    seen = {c["name"] for c in out}
    for c in model.commands():
        name = str(c.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "description": str(c.get("description") or ""), "source": model.name})
    return out


def find(commands: list[dict], name: str) -> dict | None:
    name = name.strip().lstrip(PREFIX)
    return next((c for c in commands if c["name"] == name), None)


def unknown(name: str, commands: list[dict]) -> str:
    names = ", ".join(PREFIX + c["name"] for c in commands) or "команд нет"
    return f"нет команды {PREFIX}{name.strip().lstrip(PREFIX)}; есть: {names}"


def line(name: str, args: str = "") -> str:
    """Строка команды, как её набрали: «/имя аргументы»."""
    return f"{PREFIX}{name} {args.strip()}".strip()


def for_gateway_model(command_line: str) -> str:
    return f"{COMMAND_MARK} {command_line}"
