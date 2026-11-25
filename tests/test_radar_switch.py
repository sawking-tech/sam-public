"""Выключатель радара SAM_RADAR (gateway/radar_switch.py): выключенный радар не видят ни модель, ни система, включённый
на месте. Проверки идут по границам, которые спрашивают выключатель: надзор, перезапуск, приложения, навигация, скиллы,
workflow, инструменты, промпты, голос, сервисы."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from cortex.acp import tool_search
from gateway import agent, apps, builtin_apps, radar_client, radar_switch, restart, scanning, shell_config, skills
from gateway import stand_checks as sc
from gateway import stand_report
from gateway.tools import apps as app_tool
from gateway.tools import capabilities
from radar import presence_service
from tests.fakes_core import app_client  # noqa: F401  (фикстура)
from tests.test_mcp_aggregator import agg  # noqa: F401  (фикстура)
from voice.layer import radar_feed

ROOT = Path(__file__).resolve().parents[1]
MENTION = re.compile(r"radar|радар", re.I)
ON = {radar_switch.ENV: "1"}
SKILLS_DIR = ROOT / ".claude" / "skills"
SKILLS_WITHOUT_RADAR = ("sam-self", "make-app")
RADAR_FILES = ("workflows/stand-silence.yml", "SAM.md")


@pytest.fixture
def switched(request, monkeypatch):
    """Параметр - включён ли радар в этом прогоне."""
    monkeypatch.setenv(radar_switch.ENV, "1" if request.param else "0")
    return request.param


both = pytest.mark.parametrize("switched", [False, True], ids=["off", "on"], indirect=True)


@pytest.mark.parametrize("environ, expected", [({}, False), ({radar_switch.ENV: ""}, False), ({radar_switch.ENV: "0"}, False),
                                               (ON, True), ({radar_switch.ENV: "мусор"}, False)],
                         ids=["unset", "empty", "zero", "one", "garbage"])
def test_radar_is_off_unless_the_flag_says_so(environ, expected):
    assert radar_switch.enabled(environ) is expected


@both
def test_stand_supervises_the_radar_only_when_on(switched, monkeypatch):
    monkeypatch.setattr(sc, "fetch_health", lambda url: {"status": "ok"})
    monkeypatch.setattr(sc, "python_cmdlines", lambda: [])
    states, bodies = sc.component_states()
    assert ("radar" in states) is switched and ("radar" in bodies) is switched


@both
def test_restart_knows_the_radar_only_when_on(switched):
    assert ("radar" in restart.names()) is switched
    assert (restart.restart_name("radar") == "radar") is switched
    assert (stand_report.restart_name("radar") is not None) is switched


def test_health_says_nothing_about_a_switched_off_radar(app_client):  # noqa: F811
    client, _ = app_client
    res = client.get("/health")
    assert res.status_code == 200 and not MENTION.search(res.text)


@both
def test_builtin_app_and_navigation(switched, tmp_path):
    left = tmp_path / "radar"                     # манифест с прошлого включения
    left.mkdir()
    (left / apps.MANIFEST).write_text(json.dumps({"slug": "radar", "name": "Радар", "description": "", "url": "http://x/",
                                                  "health": "http://x/health", "control": "radar"}), encoding="utf-8")
    assert ("radar" in [b.slug for b in builtin_apps.defaults()]) is switched
    assert ("radar" in [b.slug for b in builtin_apps.scan(tmp_path)[0]]) is switched
    assert (builtin_apps.find(tmp_path, "radar") is not None) is switched
    assert ("radar" in [item["key"] for item in shell_config.nav(tmp_path, 8000)]) is switched
    assert ("radar" in app_tool.app_list_impl({}, tmp_path, lambda url: False, lambda *a, **k: (500, ""))) is switched


def test_switched_off_radar_cannot_be_installed_or_called(tmp_path):
    (tmp_path / "radar").mkdir()
    (tmp_path / "radar" / builtin_apps.REMOVED).write_text("", encoding="utf-8")
    assert builtin_apps.removed(tmp_path) == []
    refused = builtin_apps.install(tmp_path, "radar")
    assert refused.startswith("встроенного приложения radar нет") and "есть apps, monitor" in refused
    out = app_tool.app_call_impl({"app": "radar", "tool": "where"}, tmp_path, lambda *a, **k: (200, "{}"))
    assert "нет" in out


def test_gateway_does_not_serve_a_switched_off_radar(app_client):  # noqa: F811
    client, _ = app_client
    assert client.get("/apps/radar", follow_redirects=False).status_code == 404
    assert client.get("/apps/radar/", follow_redirects=False).status_code == 404


def test_apps_page_does_not_name_the_radar_in_its_text():
    page = (ROOT / "gateway" / "static" / "apps_panel.html").read_text(encoding="utf-8")
    assert "монитор и радар" not in page and "Радар снимется" not in page


@both
def test_skill_is_listed_only_when_on(switched):
    names = [s.name for s in skills.load_index(SKILLS_DIR)]
    assert ("radar" in names) is switched and "sam-self" in names
    text = skills.load_skill_impl({"name": "radar"}, skills.load_index(SKILLS_DIR))
    assert text.startswith("Скилла") is not switched
    assert ("radar:" in skills.render_index(skills.load_index(SKILLS_DIR))) is switched


@pytest.mark.parametrize("name", SKILLS_WITHOUT_RADAR)
def test_general_skills_do_not_mention_the_radar(name):
    assert not MENTION.search((SKILLS_DIR / name / skills.SKILL_FILE).read_text(encoding="utf-8"))


@both
def test_workflow_is_registered_only_when_on(switched):
    names = [w.name for w in agent._load_workflows(lambda line: None)[2]]
    assert ("scanning" in names) is switched and "stand-silence" in names


@pytest.mark.parametrize("path", RADAR_FILES)
def test_shared_texts_for_the_model_do_not_mention_the_radar(path):
    assert not MENTION.search((ROOT / path).read_text(encoding="utf-8"))


def test_scanning_gate_stays_quiet_without_the_radar(monkeypatch, capsys):
    monkeypatch.setenv(scanning.ENABLED_ENV, "1")
    monkeypatch.setattr(scanning, "read_radar", lambda: pytest.fail("ворота не должны идти в радар"))
    assert scanning.main() == 0 and capsys.readouterr().out == ""


def _prompt_sources():
    folder = ROOT / "gateway" / "prompts"
    return sorted(p for p in folder.rglob("*") if p.suffix in (".py", ".md"))


def test_prompts_do_not_mention_the_radar():
    assert [p.name for p in _prompt_sources() if MENTION.search(p.read_text(encoding="utf-8"))] == []


def _tool_text(tool_cls) -> str:
    return json.dumps([tool_cls.description, tool_cls.parameters], ensure_ascii=False, default=str)


def test_gateway_tools_do_not_show_a_switched_off_radar():
    from qwen_agent.tools.base import TOOL_REGISTRY
    bad = [name for name, cls in TOOL_REGISTRY.items() if MENTION.search(_tool_text(cls))]
    assert bad == []


def test_capabilities_do_not_show_a_switched_off_radar():
    from qwen_agent.tools.base import TOOL_REGISTRY
    tools = {name: TOOL_REGISTRY[name] for name in ("app_call", "app_list", "restart_component")}
    assert not MENTION.search(capabilities.list_capabilities_impl({}, tools, []))


def test_restart_tool_lists_the_radar_when_the_flag_was_on():
    assert "radar" in restart.names(ON) and "radar" not in restart.names({})


def test_tool_search_example_is_about_something_else():
    assert not MENTION.search(json.dumps(tool_search.bridge_schema(["a"]), ensure_ascii=False))


@both
def test_aggregator_starts_the_radar_server_only_when_on(switched, agg, tmp_path):  # noqa: F811
    assert agg.skipped_server("radar", str(tmp_path)) is not switched
    assert agg.skipped_server("fetch", str(tmp_path)) is False


def test_radar_server_is_declared_for_the_aggregator():
    config = json.loads((ROOT / "mcp" / "configs" / "local.json").read_text(encoding="utf-8"))
    assert "radar" in [s["name"] for s in config["servers"]]


@pytest.mark.parametrize("command", [[str(ROOT / "mcp" / "radar_server.py")], ["-m", "gateway.radar_switch"]], ids=["mcp", "check"])
def test_processes_refuse_in_one_line_when_off(command):
    out = subprocess.run([sys.executable, *command], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8", radar_switch.ENV: "0"})
    assert out.returncode == 1 and out.stderr.strip().endswith(radar_switch.OFF_LINE)


def test_radar_service_does_not_start_when_off(monkeypatch):
    monkeypatch.setattr(presence_service.uvicorn, "run", lambda *a, **k: pytest.fail("сервис поднялся"))
    monkeypatch.setattr(presence_service.stand_watch_keeper, "start", lambda name: pytest.fail("надзор разбужен"))
    with pytest.raises(SystemExit, match=radar_switch.ENV):
        presence_service.main()


@both
def test_voice_and_client_feed_only_when_on(switched):
    env = {**({radar_switch.ENV: "1"} if switched else {}), radar_feed.ENABLED_ENV: "1"}
    assert radar_feed.build(env, client=object()).enabled is switched
    assert radar_client.build(env).enabled is switched
