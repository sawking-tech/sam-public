"""Инструменты живых приложений (SAM-183): сделать, поправить, список, остановить - на моке docker и монитора."""
import json

from gateway import apps
from gateway.tools import apps as tool
from tests.test_apps import FakeDocker

CHART = "<svg><rect fill='red'/></svg>"
SLUG = "grafik"                     # slug имени «график»


def make(tmp_path, docker, events, **kw):
    params = {"name": "график", "files": {"index.html": CHART}, "command": "python -m http.server 8000",
              "request": "сделай страницу с графиком", **kw}
    return tool.app_make_impl(params, tmp_path, events.append, docker)


def test_make_gives_slug_and_url_then_update_by_slug_changes_the_page_and_restarts(tmp_path, monkeypatch):
    monkeypatch.setenv(apps.GATEWAY_ENV, "http://sam:8123")
    docker, events = FakeDocker(), []
    text = make(tmp_path, docker, events)
    app_id = events[0]["id"]
    assert "создано" in text and f"slug {SLUG}" in text and f"адрес http://sam:8123/apps/{SLUG}," in text
    assert events[0]["preview"] == f"/apps/{SLUG}/" and events[0]["ok"] and events[0]["slug"] == SLUG
    assert events[0]["url"] == f"http://sam:8123/apps/{SLUG}"
    runs_before = sum(c[1:3] == ["run", "-d"] for c in docker.calls)
    text = tool.app_update_impl({"app": SLUG, "files": {"index.html": CHART.replace("red", "blue")}},
                                tmp_path, events.append, docker)
    assert "обновлено" in text and events[-1]["action"] == "обновлено"
    assert "blue" in (tmp_path / app_id / apps.SRC / "index.html").read_text(encoding="utf-8")
    assert sum(c[1:3] == ["run", "-d"] for c in docker.calls) == runs_before + 2     # приложение и пограничный
    saved = json.loads((tmp_path / app_id / apps.MANIFEST).read_text(encoding="utf-8"))
    assert saved["request"] == "сделай страницу с графиком"


def test_list_stop_and_start_again(tmp_path):
    docker, events = FakeDocker(), []
    make(tmp_path, docker, events)
    app_id = events[0]["id"]
    created = apps.load(tmp_path, app_id).created
    assert f"{SLUG} график - running, /apps/{SLUG}/, сделано {created}, id {app_id}" in tool.app_list_impl({}, tmp_path)
    assert "остановлено" in tool.app_stop_impl({"app": SLUG}, tmp_path, docker)
    assert "stopped" in tool.app_list_impl({}, tmp_path)
    text = tool.app_update_impl({"app": app_id}, tmp_path, events.append, docker)   # «включи снова»
    assert "запущено" in text and "running" in tool.app_list_impl({}, tmp_path)


def test_command_change_is_saved(tmp_path):
    docker, events = FakeDocker(), []
    make(tmp_path, docker, events)
    app_id = events[0]["id"]
    tool.app_update_impl({"app": app_id, "command": "python serve.py"}, tmp_path, events.append, docker)
    assert apps.load(tmp_path, app_id).command == "python serve.py"
    assert any(c[-1] == "python serve.py" for c in docker.calls if c[1:3] == ["run", "-d"])


def test_failures_are_one_line(tmp_path):
    docker, events = FakeDocker(), []
    assert "Нужны name" in tool.app_make_impl({"name": "x"}, tmp_path, events.append, docker)
    assert "нет: список" in tool.app_update_impl({"app": "нет такого", "files": {"a": "b"}}, tmp_path,
                                                  events.append, docker)
    assert "вне папки" in tool.app_make_impl({"name": "x", "command": "c", "files": {"../x": "y"}}, tmp_path,
                                             events.append, docker)
    broken = FakeDocker(fail="run -d")
    text = make(tmp_path, broken, events)
    assert "не запущено" in text and "\n" not in text and events[-1]["ok"] is False


def test_make_with_a_taken_name_makes_a_new_app_and_leaves_the_old_one(tmp_path):
    """SAM-205: app_make с именем старого остановленного приложения переписывал его через app_update; теперь -
    новое приложение со своим slug, старое не трогается."""
    docker, events = FakeDocker(), []
    make(tmp_path, docker, events)
    old = apps.find(tmp_path, SLUG)
    tool.app_stop_impl({"app": SLUG}, tmp_path, docker)
    before = (tmp_path / old.id / apps.MANIFEST).read_text(encoding="utf-8")
    calls = len(docker.calls)
    again = make(tmp_path, docker, events, files={"index.html": "<p>new</p>"})
    assert "создано" in again and f"slug {SLUG}-2" in again and events[-1]["slug"] == f"{SLUG}-2"
    assert len(apps.all_apps(tmp_path)) == 2
    assert (tmp_path / old.id / apps.MANIFEST).read_text(encoding="utf-8") == before
    assert (tmp_path / old.id / apps.SRC / "index.html").read_text(encoding="utf-8") == CHART
    assert not any(apps.app_container(old.id) in c for c in docker.calls[calls:])


def test_update_by_name_is_refused_with_the_slugs_of_that_name(tmp_path):
    docker, events = FakeDocker(), []
    make(tmp_path, docker, events)
    make(tmp_path, docker, events)
    text = tool.app_update_impl({"app": "график", "files": {"a": "b"}}, tmp_path, events.append, docker)
    assert "нет" in text and f"{SLUG}, {SLUG}-2" in text and "\n" not in text
    assert not any((tmp_path / m.id / apps.SRC / "a").exists() for m in apps.all_apps(tmp_path))


def test_gateway_tool_emits_app_result_into_the_turn_session(tmp_path, monkeypatch):
    from gateway import agent, token_usage
    seen = []
    monkeypatch.setattr(agent._apps, "APPS_DIR", tmp_path)
    monkeypatch.setattr(agent._apps, "docker_run", FakeDocker())      # тот же модуль apps, что у инструментов
    monkeypatch.setattr(agent.event_bus, "emit", lambda kind, data: seen.append((kind, data)))
    with token_usage.usage_scope("s-apps", "worker"):
        text = agent._impl_app_make({"name": "график", "files": {"index.html": CHART}, "command": "python -m x"})
    assert "создано" in text
    kind, data = seen[0]
    assert kind == "app_result" and data["session"] == "s-apps" and data["preview"] == f"/apps/{SLUG}/"
    assert apps.all_apps(tmp_path)[0].by == "SAM"


def test_worker_app_result_goes_to_the_creator_session(tmp_path, monkeypatch):
    # воркер ходит в сессии task:...; строка приложения нужна в ленте создателя, а не в свёрнутой секции задачи
    from gateway import agent, token_usage
    seen = []
    monkeypatch.setattr(agent._apps, "APPS_DIR", tmp_path)
    monkeypatch.setattr(agent._apps, "docker_run", FakeDocker())
    monkeypatch.setattr(agent.event_bus, "emit", lambda kind, data: seen.append((kind, data)))
    monkeypatch.setitem(agent._task_context_map, "task:abcd1234", ("s-apps", "abcd1234"))
    token = agent._worker_session_var.set("task:abcd1234")
    try:
        with token_usage.usage_scope("task:abcd1234", "worker"):
            agent._impl_app_make({"name": "график", "files": {"index.html": CHART}, "command": "python -m x"})
    finally:
        agent._worker_session_var.reset(token)
    assert seen[0][1]["session"] == "s-apps" and seen[0][1]["task_id"] == "abcd1234"


def test_worker_gets_the_app_tools():
    from gateway import agent
    for name in ("app_make", "app_update", "app_list", "app_stop"):
        assert name in agent.WORKER_TOOL_NAMES and name in agent.TOOL_IMPL


def test_list_names_broken_manifests_beside_the_working_apps(tmp_path):
    """apps 40: app_list показывает id, имя, состояние и адрес, а битые манифесты - строками, не роняя список."""
    docker, events = FakeDocker(), []
    make(tmp_path, docker, events)
    app_id = events[0]["id"]
    (tmp_path / "dddddddddddd").mkdir()
    (tmp_path / "dddddddddddd" / apps.MANIFEST).write_text("{not json", encoding="utf-8")
    lines = tool.app_list_impl({}, tmp_path).splitlines()
    assert lines[0] == f"{SLUG} график - running, /apps/{SLUG}/, сделано {apps.load(tmp_path, app_id).created}, id {app_id}"
    assert len(lines) == 2 and "dddddddddddd" in lines[1]


def test_list_puts_the_newest_app_first(tmp_path):
    for app_id, created in (("aaaaaaaaaaaa", "2026-10-13 10:00"), ("bbbbbbbbbbbb", "2026-10-14 09:00"),
                            ("cccccccccccc", "2026-10-12 08:00")):
        (tmp_path / app_id).mkdir()
        apps.save(tmp_path, apps.Manifest(id=app_id, name=app_id[0], command="c", host_port=18400, created=created,
                                          slug=app_id[0]))
    lines = tool.app_list_impl({}, tmp_path).splitlines()
    assert [ln.split()[0] for ln in lines] == ["b", "a", "c"] and "сделано 2026-10-14 09:00" in lines[0]


def test_loop_of_the_gateway_gets_the_split_advice_of_app_tools():
    """SAM-210: обрезанный пределом вызов app_make или app_update получает в цикле шлюза совет разделить файлы."""
    from gateway import agent
    hints = agent._get_agent_loop_deps().split_hints
    assert hints["app_make"] == hints["app_update"] == tool.SPLIT_ADVICE
    assert "app_list" not in hints
