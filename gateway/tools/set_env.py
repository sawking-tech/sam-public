"""set_env / get_env — агент сохраняет токен/ключ в .env по голосу или копипасту.

Use cases:
- Человек: «Сэм, сохрани PLAYWRIGHT_MCP_EXTENSION_TOKEN=abc123»
  → tool set_env(key, value) → пишет в .env, обновляет os.environ
- Человек: «какой у нас QDRANT_URL» → tool get_env(key)

Безопасность:
- Только uppercase keys [A-Z_][A-Z0-9_]+
- Whitelist symbols в value: printable без newlines/escape
- .env пишется атомарно через temp file → rename
- В логах значение скрыто (показывается длина + первые/последние 4 символа)

Restart MCP после изменения env у конкретных ключей — не автоматом,
агент сам решит когда сказать «перезапусти MCP» (или есть отдельный
tool в будущем).
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_FORBIDDEN_VAL_CHARS = re.compile(r"[\r\n\x00]")


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]} ({len(value)} chars)"


def _read_env(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []


def _write_env_atomic(path: Path, lines: list[str]) -> None:
    """Атомарная запись: temp в той же директории → rename."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            if lines and not lines[-1].endswith(("\n", "")):
                f.write("\n")
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise
    os.replace(tmp, path)


def _upsert_env_line(lines: list[str], key: str, value: str) -> list[str]:
    """Заменить существующую строку KEY=... или добавить новую."""
    new_line = f"{key}={value}"
    found = False
    out = []
    for line in lines:
        s = line.lstrip()
        if s.startswith(f"{key}=") or s.startswith(f"export {key}="):
            if not found:
                out.append(new_line)
                found = True
        else:
            out.append(line)
    if not found:
        out.append(new_line)
    return out


def set_env_impl(params: dict, env_path: Path | str | None = None) -> str:
    """Записать значение в .env и применить runtime."""
    key = (params.get("key") or "").strip()
    value = params.get("value")

    if not key:
        return "Нужен key (имя переменной, UPPER_CASE)"
    if not _KEY_RE.match(key):
        return (
            f"Невалидное имя '{key}'. Допустимы только UPPER_CASE буквы, "
            "цифры и подчёркивания, начинается с буквы."
        )
    if value is None:
        return "Нужен value"
    value = str(value)
    if _FORBIDDEN_VAL_CHARS.search(value):
        return "Значение не должно содержать переводы строк или нулевые байты"

    path = Path(env_path) if env_path else Path(os.getcwd()) / ".env"

    try:
        lines = _read_env(path)
        new_lines = _upsert_env_line(lines, key, value)
        _write_env_atomic(path, new_lines)
    except Exception as e:
        return f"Не удалось записать в {path}: {e}"

    # Применяем runtime — новые subprocess'ы будут видеть значение
    old_set = key in os.environ
    os.environ[key] = value

    note = ""
    # Для ключей которые читают MCP-серверы при старте — намёк на рестарт
    if key.startswith(("PLAYWRIGHT_", "BRAVE_", "TAVILY_", "GITHUB_TOKEN")):
        note = " (для применения нужен рестарт MCP сервера)"
    elif key == "LLM_SOURCE":
        note = " (роутер читает его на каждый вызов, рестарт не нужен; с проверкой источника - switch_main_model)"
    elif key.startswith(("LMSTUDIO_", "GEMINI_", "DASHSCOPE_", "ANTHROPIC_")):
        note = " (для применения нужен рестарт gateway)"

    action = "обновлён" if old_set else "сохранён"
    return f"OK: {key} {action} в .env: {_mask(value)}{note}"


def get_env_impl(params: dict, env_path: Path | str | None = None) -> str:
    """Показать значение env переменной (с маскированием)."""
    key = (params.get("key") or "").strip()
    if not key:
        return "Нужен key"
    if not _KEY_RE.match(key):
        return f"Невалидное имя '{key}'"
    value = os.environ.get(key)
    if value is None:
        return f"{key} не установлен"
    show = bool(params.get("unsafe_show", False))
    return f"{key}={value}" if show else f"{key}={_mask(value)}"


def list_env_impl(params: dict, prefix: str = "") -> str:
    """Список env-ключей (без значений). С опциональным префиксом."""
    pref = (params.get("prefix") or prefix).strip().upper()
    keys = sorted(k for k in os.environ if _KEY_RE.match(k))
    if pref:
        keys = [k for k in keys if k.startswith(pref)]
    if not keys:
        return f"Нет env-переменных" + (f" с префиксом '{pref}'" if pref else "")
    return "\n".join(keys)
