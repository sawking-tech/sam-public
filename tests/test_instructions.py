"""Инструкции SAM - файл SAM.md (gateway/prompts/instructions.py): разделы приходят из файла, инструменты души их не
правят, душа и инструкции не пересекаются."""
import pytest

from gateway import agent, self_config
from gateway.prompts import instructions, lead, prompt_md, sections, soul, system
from gateway.prompts.identity import MALE
from gateway.repos.soul_repo import SoulRepo
from gateway.soul_service import SoulService
from gateway.tools import soul as soul_tool

INSTRUCTION = "DOING"
MARK = "Инструкция из подменённого файла."


@pytest.fixture
def prompt_file(tmp_path, monkeypatch):
    """SAM.md из временной копии с правкой раздела DOING; кэш файла сбрасывается до и после."""
    text = instructions.DEFAULT_PATH.read_text(encoding="utf-8")
    doing = instructions.instructions().sections[INSTRUCTION]
    path = tmp_path / "SAM.md"
    path.write_text(text.replace(doing, MARK), encoding="utf-8")
    monkeypatch.setenv(instructions.PROMPT_ENV, str(path))
    monkeypatch.setattr(self_config, "_instance", self_config.SelfConfig())
    instructions.instructions.cache_clear()
    yield path
    instructions.instructions.cache_clear()


def test_soul_and_instructions_do_not_overlap():
    assert set(soul.read_seed()) & set(instructions.names()) == set()
    assert set(instructions.names()) >= {"DOING", "TOOLS", "WHEN_WHAT", "ORAL_SPEECH", "COMMAND_KEY", "HEARD"}


def test_instruction_sections_come_from_the_file(prompt_file):
    assert sections.section(INSTRUCTION) == MARK
    assert MARK in system.system_prompt() and MARK in agent._system_prompt()


def test_character_sections_come_from_the_soul(monkeypatch):
    soul.configure(lambda: {**soul.read_seed(), "CHARACTER": "Характер из хранилища."})
    try:
        assert "Характер из хранилища." in lead.lead_prompt(MALE)
        assert sections.section("CHARACTER") == "Характер из хранилища."
    finally:
        soul.configure(soul.read_seed)


def test_instruction_records_in_the_store_do_not_override_the_file():
    """Запись DOING, оставшаяся в хранилище, в промпт не идёт: инструкции - только из SAM.md."""
    soul.configure(lambda: {**soul.read_seed(), INSTRUCTION: "Старая инструкция из Mongo."})
    try:
        assert sections.section(INSTRUCTION) == instructions.section(INSTRUCTION)
        assert "Старая инструкция из Mongo." not in system.system_prompt()
    finally:
        soul.configure(soul.read_seed)


def test_soul_edit_refuses_an_instruction_section(mongo_db):
    svc = SoulService(SoulRepo(mongo_db, 5), invalidate=lambda: None, log=lambda line: None)
    svc.seed()
    answer = soul_tool.soul_edit_impl({"section": INSTRUCTION, "version": 1, "text": "Новая инструкция."}, svc)
    assert answer.startswith("Не записано:") and "SAM.md" in answer
    assert soul_tool.soul_show_impl({"section": INSTRUCTION}, svc).startswith("Нет раздела")
    assert INSTRUCTION not in soul_tool.soul_show_impl({}, svc)


def test_missing_file_is_one_line(tmp_path, monkeypatch):
    monkeypatch.setenv(instructions.PROMPT_ENV, str(tmp_path / "нет.md"))
    instructions.instructions.cache_clear()
    try:
        with pytest.raises(prompt_md.PromptError) as exc:
            instructions.section(INSTRUCTION)
        assert "нет файла" in exc.value.code and "\n" not in exc.value.code
    finally:
        instructions.instructions.cache_clear()


def test_instruction_texts_carry_no_service_comments():
    """Решения с датами и ссылками на задачи живут в журнале и ADR, а не в тексте промпта."""
    text = instructions.DEFAULT_PATH.read_text(encoding="utf-8")
    comments = [c for c in prompt_md._COMMENT.findall(text)]
    assert [c for c in comments if any(m in c for m in ("Решение", "SAM-", "ADR", " окт"))] == []
