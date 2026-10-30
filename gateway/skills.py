"""Скиллы по запросу (SAM-61): оглавление в системном промпте, тело через tool load_skill.

Устроено как скиллы Claude Code (code.claude.com/docs/en/skills): в контексте с начала только имя и
описание, тело SKILL.md читается, когда модель решила, что скилл нужен.
- name по умолчанию - имя папки, description - первая непустая строка тела;
- в оглавлении description вместе с when_to_use режется до DESCRIPTION_LIMIT знаков;
- disable-model-invocation: true - скилла нет в оглавлении;
- битая шапка не роняет скилл: он идёт с именем папки и строкой в логе.

Разбор шапки (parse_skill) отделён от чтения диска (load_index). Папка - SAM_SKILLS_DIR, по умолчанию проектная
`.claude/skills` - скиллы стенда, их получает любая основная модель; `~/.claude/skills` - личные скиллы Claude Code,
шлюз их не раздаёт (SAM-99), кроме перечисленных в SAM_SKILLS_SHARED: по умолчанию глобальные plan-and-iterate и
code-review. Белый список имён - SAM_SKILLS_ALLOW через запятую (пусто - все).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

PROJECT_SKILLS_DIR = Path(__file__).resolve().parents[1] / ".claude" / "skills"
# Пустая переменная в .env - умолчание, а не текущая папка
SKILLS_DIR = Path(os.getenv("SAM_SKILLS_DIR", "").strip() or PROJECT_SKILLS_DIR)
PERSONAL_SKILLS_DIR = Path(os.getenv("SAM_PERSONAL_SKILLS_DIR", "").strip() or Path.home() / ".claude" / "skills")
DEFAULT_SHARED = "plan-and-iterate,code-review"   # личные скиллы Claude Code, которые стенд отдаёт всем основным моделям
SKILL_FILE = "SKILL.md"
FRONTMATTER = "---"
DESCRIPTION_LIMIT = 1536    # как у Claude Code: description + when_to_use в оглавлении
TRUE_WORDS = {"true", "yes", "on", "1"}
INDEX_HEADER = "Скиллы - инструкции на задачу. Подходит задача - вызови load_skill(name) и действуй по телу:"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    listed: bool = True


def allow_list() -> set[str] | None:
    names = {n.strip() for n in os.getenv("SAM_SKILLS_ALLOW", "").split(",") if n.strip()}
    return names or None


def shared_names() -> set[str]:
    raw = os.getenv("SAM_SKILLS_SHARED")   # пустая переменная - ничего не раздаём, не заданная - умолчание
    return {n.strip() for n in (DEFAULT_SHARED if raw is None else raw).split(",") if n.strip()}


def parse_skill(text: str, folder: str) -> Skill:
    meta, body = _split(text, folder)
    description = str(meta.get("description") or _first_line(body)).strip()
    if meta.get("when_to_use"):
        description = f"{description} {str(meta['when_to_use']).strip()}"
    listed = str(meta.get("disable-model-invocation", "false")).strip().lower() not in TRUE_WORDS
    return Skill(str(meta.get("name") or folder), description[:DESCRIPTION_LIMIT], body, listed)


def _split(text: str, folder: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER:
        return {}, text
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == FRONTMATTER)
    except StopIteration:
        print(f"[Skills] {folder}: шапка не закрыта ---, скилл без описания из шапки")
        return {}, text
    body = "\n".join(lines[end + 1:])
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
        if not isinstance(meta, dict):
            raise ValueError("шапка не словарь")
        return meta, body
    except (yaml.YAMLError, ValueError) as e:
        print(f"[Skills] {folder}: шапка не разобрана ({str(e).splitlines()[0]}), имя - по папке")
        return {}, body


def _first_line(body: str) -> str:
    return next((line.strip() for line in body.splitlines() if line.strip()), "")


def load_index(root: Path = SKILLS_DIR, allow: set[str] | None = None) -> list[Skill]:
    """Скиллы из root/*/SKILL.md, видимые модели, по имени."""
    if not Path(root).is_dir():
        return []
    out = []
    for path in sorted(Path(root).glob(f"*/{SKILL_FILE}")):
        try:
            skill = parse_skill(path.read_text(encoding="utf-8"), path.parent.name)
        except (OSError, UnicodeDecodeError) as e:   # битый файл не роняет оглавление остальных
            print(f"[Skills] {path.parent.name}: {SKILL_FILE} не прочитан ({e}); нужен текст в UTF-8")
            continue
        if skill.listed and (allow is None or skill.name in allow):
            out.append(skill)
    return out


def load_all(allow: set[str] | None = None, root: Path = SKILLS_DIR,
             personal_root: Path = PERSONAL_SKILLS_DIR, shared: set[str] | None = None) -> list[Skill]:
    """Скиллы стенда плюс раздаваемые личные; имя, уже занятое скиллом стенда, личный не перекрывает."""
    out = load_index(root, allow)
    taken = {s.name for s in out}
    wanted = shared_names() if shared is None else shared
    out.extend(s for s in load_index(personal_root, allow) if s.name in wanted and s.name not in taken)
    return out


def render_index(index: list[Skill]) -> str:
    if not index:
        return ""
    return "\n".join([INDEX_HEADER] + [f"- {s.name}: {s.description[:DESCRIPTION_LIMIT]}" for s in index])


def load_skill_impl(params: dict, index: list[Skill]) -> str:
    name = str(params.get("name") or "").strip()
    names = ", ".join(s.name for s in index) or "нет ни одного"
    if not name:
        return f"Нужен name скилла. Есть: {names}"
    skill = next((s for s in index if s.name == name), None)
    if skill is None:
        return f"Скилла {name!r} нет. Есть: {names}"
    return skill.body
