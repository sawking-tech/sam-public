"""Душа SAM (gateway/prompts/soul.py): разбор заготовки, проверка структуры, кэш снимка и сбой одной строкой."""
import pytest

from gateway.prompts import prompt_md, soul

TEXT = """---
owner: человек
---
Пояснение для людей.

## FIRST
<!-- причина правила -->
Строка {owner}, {OWNER}.
  с отступом

## SECOND

Второй.
"""
SEED = soul.seed_entries(TEXT)
REQUIRED = soul.required_sections(SEED)


def test_parse_reads_header_sections_and_drops_comments():
    parsed = prompt_md.parse(TEXT, "SAM.md")
    assert parsed.meta == {"owner": "человек"}
    assert parsed.sections == {"FIRST": "Строка {owner}, {OWNER}.\n  с отступом", "SECOND": "Второй."}
    assert prompt_md.fill(parsed.sections["FIRST"], parsed.meta).startswith("Строка человек, ЧЕЛОВЕК.")


def test_seed_entries_are_sections_only_and_compose_drops_comments():
    """Шапка заготовки - не душа; в промпт раздел идёт без комментариев, как из файла."""
    assert set(SEED) == {"FIRST", "SECOND"} and "<!-- причина правила -->" in SEED["FIRST"]
    assert REQUIRED == ("FIRST", "SECOND")
    assert soul.compose(SEED, REQUIRED).sections == prompt_md.parse(TEXT, "SAM.md").sections


def test_records_outside_the_seed_do_not_reach_the_snapshot():
    assert soul.compose({**SEED, "DOING": "Инструкция."}, REQUIRED).sections.keys() == {"FIRST", "SECOND"}


def test_real_seed_is_character_only_without_comments():
    """Душа - только характер; служебных комментариев с решениями в ней нет."""
    seed = soul.read_seed()
    assert tuple(seed) == ("IDENTITY", "CHARACTER", "TONE")
    assert [name for name, text in seed.items() if "<!--" in text] == []


@pytest.mark.parametrize("call, message", [
    (lambda tmp: soul.read_seed(tmp / "нет.md"), "нет заготовки"),
    (lambda tmp: soul.seed_entries("---\nowner: x\n"), "шапка не закрыта"),
    (lambda tmp: soul.compose({**SEED, "SECOND": "<!-- только комментарий -->"}, REQUIRED), "SECOND"),
    (lambda tmp: soul.compose({k: v for k, v in SEED.items() if k != "FIRST"}, REQUIRED), "FIRST"),
], ids=["no_seed", "header_open", "empty_section", "missing_section"])
def test_failure_is_one_line(tmp_path, call, message):
    with pytest.raises(soul.SoulError) as exc:
        call(tmp_path)
    assert message in str(exc.value.code) and "\n" not in str(exc.value.code) and exc.value.code.startswith("[Soul]")


def test_missing_value_is_one_log_line_and_the_gateway_lives(monkeypatch):
    """Опечатка в {ключе} раздела не роняет шлюз: PromptError - SystemExit, а сборка промпта идёт в ходе."""
    monkeypatch.setattr(prompt_md, "_reported", set())
    lines = []
    assert prompt_md.fill("Привет, {nobody}! {NOBODY}", {}, log=lines.append) == "Привет, {nobody}! {NOBODY}"
    assert prompt_md.fill("{nobody}", {}, log=lines.append) == "{nobody}"
    assert len(lines) == 1 and "нет значения для {nobody}" in lines[0] and "data/SAM.md" in lines[0]


def test_seed_path_from_env(monkeypatch):
    monkeypatch.setenv(soul.SEED_ENV, "data/other.md")
    assert soul.seed_path() == prompt_md.REPO_ROOT / "data" / "other.md"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make_store(load, lines, clock=None):
    return soul.SoulStore(load, lambda: SEED, retry_s=30, log=lines.append, clock=clock or Clock(),
                          background=lambda job: job())


def test_store_caches_until_invalidated():
    entries, calls, lines = dict(SEED), [], []

    def load():
        calls.append(1)
        return dict(entries)
    store = make_store(load, lines)
    assert store.current().sections["SECOND"] == "Второй."
    entries["SECOND"] = "Новый."
    assert store.current().sections["SECOND"] == "Второй." and len(calls) == 1
    store.invalidate()
    assert store.current().sections["SECOND"] == "Новый." and len(calls) == 2 and lines == []


def test_broken_snapshot_logs_once_and_keeps_the_last_good_one():
    entries, lines = dict(SEED), []
    store = make_store(lambda: dict(entries), lines)
    store.current()
    entries.pop("FIRST")
    store.invalidate()
    assert store.current().sections["FIRST"].startswith("Строка {owner}")
    assert len(lines) == 1 and "FIRST" in lines[0] and "последнюю рабочую версию" in lines[0]


def test_unreachable_store_takes_the_seed_and_retries_later():
    clock, lines, state = Clock(), [], {"up": False}

    def load():
        if not state["up"]:
            raise ConnectionError("connection refused")
        return {**SEED, "SECOND": "Из базы."}
    store = make_store(load, lines, clock)
    assert store.current().sections["SECOND"] == "Второй."
    assert len(lines) == 1 and "заготовку" in lines[0] and "connection refused" in lines[0]
    state["up"] = True
    assert store.current().sections["SECOND"] == "Второй."   # повтор не раньше retry_s
    clock.now = 31
    assert store.current().sections["SECOND"] == "Из базы."


def test_without_store_the_copy_keeps_the_edits(tmp_path):
    """Хранилище было, потом пропало: новый процесс собирает из копии в data/, правки характера на месте."""
    copy = soul.SoulCopy(tmp_path / "soul-cache.json")
    soul.SoulStore(lambda: {**SEED, "SECOND": "Правка."}, lambda: SEED, retry_s=30, log=lambda line: None,
                   copy=copy).current()
    lines = []

    def down():
        raise ConnectionError("connection refused")
    restarted = soul.SoulStore(down, lambda: SEED, retry_s=30, log=lines.append, copy=soul.SoulCopy(copy.path))
    assert restarted.current().sections["SECOND"] == "Правка."
    assert len(lines) == 1 and "копию" in lines[0]


def test_record_that_is_not_text_keeps_its_last_good_version():
    entries, lines = dict(SEED), []
    store = make_store(lambda: dict(entries), lines)
    store.current()
    entries["SECOND"] = 42
    store.invalidate()
    current = store.current()
    assert current.sections["SECOND"] == "Второй." and current.sections["FIRST"].startswith("Строка")
    assert len(lines) == 1 and "SECOND не текст (int)" in lines[0]


def test_missing_seed_does_not_raise_into_the_turn(tmp_path):
    """Заготовки нет (файл удалён): ни required(), ни откат на заготовку не бросают SoulError - SystemExit."""
    lines = []

    def no_seed():
        return soul.read_seed(tmp_path / "нет.md")
    store = soul.SoulStore(lambda: dict(SEED), no_seed, retry_s=30, log=lines.append)
    assert store.current().sections["SECOND"] == "Второй."
    blind = soul.SoulStore(lambda: None, no_seed, retry_s=30, log=lines.append)
    assert blind.current() == soul.Soul() and blind.required() == ()
    assert any("нет заготовки" in line for line in lines)


def test_retry_after_outage_runs_in_background_and_the_turn_takes_the_current_snapshot():
    """Ход не ждёт синхронный драйвер Mongo: повтор после сбоя уходит фоном, ход берёт текущий снимок."""
    clock, jobs, state = Clock(), [], {"up": False}

    def load():
        if not state["up"]:
            raise ConnectionError("down")
        return {**SEED, "SECOND": "Из базы."}
    store = soul.SoulStore(load, lambda: SEED, retry_s=30, log=lambda line: None, clock=clock, background=jobs.append)
    assert store.current().sections["SECOND"] == "Второй."
    state["up"], clock.now = True, 31
    assert store.current().sections["SECOND"] == "Второй." and len(jobs) == 1
    assert store.current() and len(jobs) == 1   # пока фон идёт, второй не ставится
    jobs[0]()
    assert store.current().sections["SECOND"] == "Из базы."


def test_write_refreshes_in_the_writer_thread():
    entries, calls = dict(SEED), []

    def load():
        calls.append(1)
        return dict(entries)
    store = make_store(load, [])
    store.current()
    entries["SECOND"] = "Новый."
    store.invalidate()
    assert len(calls) == 2
    assert store.current().sections["SECOND"] == "Новый." and len(calls) == 2
