"""Скиллы по запросу (SAM-61): оглавление в промпте, тело через load_skill, как в Claude Code; MCP-сервер скиллов."""
import functools
import importlib.util
from pathlib import Path

import pytest

from gateway import skills

SERVER = Path(__file__).resolve().parents[1] / "mcp" / "skills_server.py"

TEXT_EDIT = """---
name: text-edit
description: Редактура технического текста. Триггерить перед тем, как показать черновик.
---

# Редактура

Тело скилла.
"""


def _write(root, folder, text):
    d = root / folder
    d.mkdir(parents=True)
    path = d / "SKILL.md"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


def _skill(folder, description):
    return f"---\nname: {folder}\ndescription: {description}\n---\nbody"


@pytest.mark.parametrize("text, name, description, body_start, logged", [
    (TEXT_EDIT, "text-edit", "Редактура технического текста", "# Редактура", False),
    ("# Сохранить факт\n\nТекст.", "folder", "# Сохранить факт", "# Сохранить факт", False),
    ("---\nname: [oops\n---\nПервая строка тела.\n", "folder", "Первая строка тела.", "Первая строка тела.", True),
    ("", "folder", "", "", False),
    (b"\xff\xfe\x00 not utf-8", None, None, None, True),
], ids=["header", "name-by-folder", "broken-header", "empty-file", "not-utf8-skipped"])
def test_skill_file_is_parsed_or_skipped_with_one_line(tmp_path, capsys, text, name, description, body_start,
                                                       logged):
    _write(tmp_path, "folder", text)
    _write(tmp_path, "zz-good", "Хороший скилл.\n")
    index = skills.load_index(tmp_path)
    assert index[-1].name == "zz-good"   # битый соседний файл не роняет остальные
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == (1 if logged else 0)
    if logged:
        assert "folder" in out[0]
    if name is None:
        assert [s.name for s in index] == ["zz-good"]
        return
    skill = index[0]
    assert skill.name == name and skill.description.startswith(description)
    assert skill.body.strip().startswith(body_start)


def test_skill_index_is_compact_and_filtered(tmp_path):
    _write(tmp_path, "hidden", "---\nname: hidden\ndescription: x\ndisable-model-invocation: true\n---\nbody")
    _write(tmp_path, "text-edit", TEXT_EDIT)
    _write(tmp_path, "git-style", _skill("git-style", "коммиты"))
    _write(tmp_path, "long", _skill("long", "x" * 5000))
    assert [s.name for s in skills.load_index(tmp_path)] == ["git-style", "long", "text-edit"]
    assert [s.name for s in skills.load_index(tmp_path, allow={"git-style"})] == ["git-style"]
    assert skills.load_index(tmp_path / "nope") == []
    text = skills.render_index(skills.load_index(tmp_path, allow={"text-edit", "long"}))
    assert "load_skill" in text and "- text-edit: Редактура" in text
    assert len(text) < skills.DESCRIPTION_LIMIT + 400
    assert skills.render_index([]) == ""


@pytest.fixture
def skill_dirs(tmp_path, monkeypatch):
    """Папки стенда и личных скиллов на диске; load_all настоящий, подставлены только пути."""
    stand, personal = tmp_path / "stand", tmp_path / "personal"
    stand.mkdir()
    personal.mkdir()
    monkeypatch.setattr(skills, "load_all", functools.partial(skills.load_all, root=stand, personal_root=personal))
    return stand


def _gateway_load(params):
    return skills.load_skill_impl(params, skills.load_all())


def _server_load(params):
    spec = importlib.util.spec_from_file_location("skills_server_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if "list" in params:
        return module.list_skills()
    return module.load_skill(params.get("name", ""))


@pytest.mark.usefixtures("radar_on")
@pytest.mark.parametrize("load", [_gateway_load, _server_load], ids=["gateway", "mcp-server"])
def test_load_skill_by_name(skill_dirs, load):
    _write(skill_dirs, "radar", "---\nname: radar\ndescription: Где мои люди\n---\nтело радара")
    assert load({"name": "radar"}).strip() == "тело радара"
    unknown = load({"name": "нет-такого"})
    assert "нет-такого" in unknown and "radar" in unknown
    assert "radar" in load({})
    (skill_dirs / "radar" / "SKILL.md").write_text("---\nname: radar\ndescription: Где мои люди\n---\nновое тело",
                                                   encoding="utf-8")
    assert load({"name": "radar"}).strip() == "новое тело"   # правка видна без перезапуска
    if load is _server_load:
        listing = load({"list": True})
        assert "radar" in listing and "Где мои люди" in listing


def test_skill_index_goes_to_main_and_worker_not_voice(monkeypatch, tmp_path):
    from gateway import agent
    _write(tmp_path, "text-edit", TEXT_EDIT)
    monkeypatch.setattr(agent, "_skills_index", skills.load_index(tmp_path))
    assert "text-edit" in agent._get_runner_deps().system_prompt
    assert "text-edit" in agent._get_worker_deps().worker_system
    assert "text-edit" not in agent._voice_runner_deps().system_prompt


def test_stand_and_shared_personal_skills(tmp_path, monkeypatch):
    stand, personal = tmp_path / "stand", tmp_path / "personal"
    _write(stand, "drift-guard", _skill("drift-guard", "дрейф"))
    _write(stand, "plan-and-iterate", "---\nname: plan-and-iterate\ndescription: стенд\n---\nstand")
    _write(personal, "plan-and-iterate", "---\nname: plan-and-iterate\ndescription: личный\n---\npersonal")
    _write(personal, "code-review", _skill("code-review", "ревью"))
    _write(personal, "private-notes", _skill("private-notes", "личное"))
    monkeypatch.delenv("SAM_SKILLS_SHARED", raising=False)
    got = skills.load_all(root=stand, personal_root=personal)
    assert [(s.name, s.body) for s in got] == [
        ("drift-guard", "body"), ("plan-and-iterate", "stand"), ("code-review", "body")]
    monkeypatch.setenv("SAM_SKILLS_SHARED", "private-notes")
    assert [s.name for s in skills.load_all(root=stand, personal_root=personal)][-1] == "private-notes"
    monkeypatch.setenv("SAM_SKILLS_SHARED", "")
    assert [s.name for s in skills.load_all(root=stand, personal_root=personal)] == ["drift-guard",
                                                                                    "plan-and-iterate"]
