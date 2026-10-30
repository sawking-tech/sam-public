"""Личный слой data/SAM.md: подстановки и личные инструкции после общего промпта SAM (gateway/prompts/personal.py)."""
import pytest

from gateway import agent, self_config
from gateway.prompts import personal
from gateway.prompts.identity import MALE
from gateway.prompts.lead import lead_prompt

RULE = "Утром не напоминай про спорт."


@pytest.fixture
def personal_file(tmp_path, monkeypatch):
    """Задать текст личного файла: SAM_PERSONAL_FILE - временный файл."""
    monkeypatch.setattr(self_config, "_instance", self_config.SelfConfig())   # род по умолчанию, не со стенда
    path = tmp_path / "SAM.md"
    monkeypatch.setenv(personal.PERSONAL_ENV, str(path))

    def set_text(text: str) -> None:
        path.write_text(text, encoding="utf-8")
    return set_text


def test_block_is_empty_without_text():
    assert personal.instructions_block("") == ""


@pytest.mark.parametrize("build", [
    lambda: agent._system_prompt(),
    lambda: agent._voice_runner_deps().system_prompt,
    lambda: lead_prompt(MALE),
], ids=["main", "voice", "lead"])
def test_prompt_ends_with_owner_instructions(personal_file, build):
    personal_file("  \n")
    assert "ЛИЧНЫЕ ИНСТРУКЦИИ" not in build()
    personal_file(f"\n{RULE}\n")
    prompt = build()
    assert prompt.endswith(RULE)
    assert "ЛИЧНЫЕ ИНСТРУКЦИИ (личный слой, data/SAM.md)" in prompt


def test_personal_header_names_override_defaults(personal_file):
    personal_file("---\nplace: Дача\n---\n" + RULE)
    prompt = personal.personalize("Слушай {place}.")
    assert prompt.startswith("Слушай Дача.") and prompt.endswith(RULE) and "---" not in prompt


def test_broken_file_is_one_log_line_and_no_layer(personal_file):
    personal_file("---\nowner: x\n")
    lines = []
    assert personal.read_personal(log=lines.append) == personal.PromptFile()
    assert len(lines) == 1 and "шапка не закрыта" in lines[0]
