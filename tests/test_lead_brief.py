"""SessionStart-хук ведущего (.claude/hooks/lead_brief.py): кто основной агент и что ему поднять на старте (SAM-91)."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from tests.fakes_core import patch_module_sleep

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "lead_brief.py"
spec = importlib.util.spec_from_file_location("lead_brief", HOOK)
lead_brief = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lead_brief)
HOOK_TIMEOUT_S = 30   # холодный старт python на занятой машине


@pytest.mark.parametrize("flag, ticker_writes, lead", [
    ("1", False, True),        # сессию открыл start_sam: ведущая ещё до первого тика
    (None, True, True),        # открыта руками, пока тикер пишет в шину
    (None, False, False),
    ("0", False, False),
])
def test_lead_detected_by_flag_or_live_ticker(tmp_path, flag, ticker_writes, lead):
    if ticker_writes:
        (tmp_path / lead_brief.LIVE_FILE).touch()
    assert lead_brief.is_lead(flag, lead_brief.is_live(tmp_path / lead_brief.LIVE_FILE, lead_brief.LIVE_MINUTES)) is lead


def test_heartbeat_step_first_only_when_stale(tmp_path):
    """Живость сессии считается по файлу с порогом шлюза; шаг живости первым, только если файл не обновляется."""
    alive = tmp_path / lead_brief.ALIVE_FILE
    stale = lead_brief.start_steps(heartbeat_alive=lead_brief.is_live(alive, lead_brief.ALIVE_S / 60))
    alive.touch()
    fresh = lead_brief.start_steps(heartbeat_alive=lead_brief.is_live(alive, lead_brief.ALIVE_S / 60))
    assert "lead_heartbeat.py" in stale[0]
    assert not any("lead_heartbeat.py" in s for s in fresh)
    assert any("voice.layer.ticker" in s for s in fresh) and any("logs/stand_watch.log" in s for s in fresh)


def test_brief_sections_and_one_line_reason_without_gateway():
    """SAM-101: выжимка памяти о доме по темам без повторов в лимите; SAM-95: промпт SAM в роде голоса шлюза. Шлюза
    нет - память и промпт одной строкой с причиной, род по умолчанию."""
    from gateway.prompts.identity import DEFAULT_GENDER, MALE
    from gateway.prompts.lead import lead_prompt

    def down(*args):
        raise OSError("connection refused")
    found = {"люди": ["Ирина - жена Антона", "Петя - сын"], "питомцы": ["Ирина - жена Антона", "Барсик - кот"],
             "правила": ["я" * 100]}
    digest = lead_brief.home_digest(lambda t: found[t], topics=("люди", "питомцы", "правила"), limit_chars=50)
    assert digest == ["Ирина - жена Антона", "Петя - сын", "Барсик - кот"]

    assert lead_brief.memory_section(lambda t: ["Барсик - кот"]) == "- Барсик - кот"
    section = lead_brief.memory_section(down)
    assert "\n" not in section and "connection refused" in section

    assert lead_brief.prompt_section(lambda: MALE) == lead_prompt(MALE)
    assert "ГОЛОС ЛЮДЕЙ - ЗАПИСЬ РАСПОЗНАВАТЕЛЯ" in lead_brief.prompt_section(lambda: MALE)
    reason, prompt = lead_brief.prompt_section(down).split("\n", 1)
    assert "connection refused" in reason and DEFAULT_GENDER in reason and prompt == lead_prompt(DEFAULT_GENDER)


def test_brief_carries_the_last_half_hour_of_voice_and_the_after_clear_section(tmp_path, monkeypatch, capsys):
    """main-model 28: бриф после /clear - раздел «После /clear» из current.md (без соседних разделов) и разговор из
    шины за SAM_LEAD_BRIEF_MINUTES (30) минут: старше - нет, повтор частичной фразы - один раз. Шлюз (промпт и память)
    подменён: это граница сети."""
    import json
    from datetime import datetime, timedelta
    now = datetime.now().astimezone()

    def at(minutes):
        return (now - timedelta(minutes=minutes)).isoformat(timespec="seconds")

    def lines(*rows):
        return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    (tmp_path / "voice-in.jsonl").write_text(lines(
        {"ts": at(45), "who": "Антон", "text": "старая фраза про отпуск"},
        {"ts": at(20), "who": "Антон", "text": "Сэм, купи хлеба"},
        {"ts": at(20), "who": "Антон", "text": "Сэм, купи хлеба"}), encoding="utf-8")
    (tmp_path / "say-out.jsonl").write_text(lines({"ts": at(19), "from": "gateway", "text": "Записала хлеб"}),
                                            encoding="utf-8")
    current = tmp_path / "current.md"
    current.write_text("# Сессия\n\n## После /clear\nДоговорились про хлеб.\n\n## Следующие шаги\nчужое\n",
                       encoding="utf-8")
    monkeypatch.setattr(lead_brief, "BUS_DIR", tmp_path)
    monkeypatch.setattr(lead_brief, "CURRENT_MD", current)
    monkeypatch.setattr(lead_brief, "prompt_section", lambda: "ПРОМПТ")
    monkeypatch.setattr(lead_brief, "memory_section", lambda: "ПАМЯТЬ")
    monkeypatch.setenv(lead_brief.LEAD_FLAG, "1")

    lead_brief.main()
    out = capsys.readouterr().out
    after_clear = out.split("## После /clear (current.md)\n", 1)[1].split("\n## ", 1)[0]
    dialogue = out.split(f"## Разговор за {lead_brief.MINUTES} мин\n", 1)[1]

    assert lead_brief.MINUTES == 30
    assert after_clear.strip() == "Договорились про хлеб." and "чужое" not in out
    assert "старая фраза" not in dialogue and dialogue.count("Сэм, купи хлеба") == 1
    assert "Записала хлеб" in dialogue and dialogue.index("купи хлеба") < dialogue.index("Записала хлеб")


def test_heartbeat_touches_lead_alive_every_second_well_inside_the_gateway_threshold(tmp_path, monkeypatch):
    """main-model 30: scripts/lead_heartbeat.py отмечает lead.alive раз в SAM_LEAD_BEAT_S (1 с) - в пять раз чаще
    порога шлюза SAM_LEAD_ALIVE_S (5 с). Часы подменены: сон считает обороты и останавливает цикл на третьем."""
    import os
    import time

    from gateway import agent
    monkeypatch.setenv("SAM_VOICE_BUS_DIR", str(tmp_path))
    monkeypatch.delenv("SAM_LEAD_BEAT_S", raising=False)
    path = HOOK.parents[2] / "scripts" / "lead_heartbeat.py"
    heartbeat_spec = importlib.util.spec_from_file_location("lead_heartbeat", path)
    heartbeat = importlib.util.module_from_spec(heartbeat_spec)
    heartbeat_spec.loader.exec_module(heartbeat)
    alive, slept, ages = tmp_path / "lead.alive", [], []

    class Stop(Exception):
        pass

    def sleep(seconds):
        ages.append(time.time() - alive.stat().st_mtime)
        os.utime(alive, (0, 0))              # отметка «старая»: следующий оборот обязан её обновить
        slept.append(seconds)
        if len(slept) == 3:
            raise Stop
    patch_module_sleep(monkeypatch, heartbeat, sleep)
    with pytest.raises(Stop):
        heartbeat.main()

    assert heartbeat.ALIVE == alive and slept == [1.0, 1.0, 1.0]
    assert all(age < 1 for age in ages)
    assert heartbeat.BEAT_S * 5 <= agent._LEAD_ALIVE_S


def _session_end_commands() -> list[str]:
    settings = json.loads((HOOK.parents[1] / "settings.json").read_text(encoding="utf-8"))
    return [h["command"] for group in settings["hooks"]["SessionEnd"] for h in group["hooks"]]


def test_session_end_hook_removes_lead_alive(tmp_path):
    """main-model 31: хук SessionEnd удаляет lead.alive в шине - шлюз сразу видит, что ведущего нет; без файла хук
    не падает. Команда берётся из .claude/settings.json и запускается в папке-копии репозитория, как её запускает
    Claude Code (cwd - корень проекта)."""
    alive = tmp_path / "data" / "voice" / "bus" / lead_brief.ALIVE_FILE
    alive.parent.mkdir(parents=True)
    alive.touch()
    [command] = _session_end_commands()
    for _ in range(2):                                   # второй раз - файла уже нет
        out = subprocess.run(command, shell=True, cwd=tmp_path, capture_output=True, text=True, timeout=HOOK_TIMEOUT_S)
        assert out.returncode == 0, out.stderr
        assert not alive.exists()
