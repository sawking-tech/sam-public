"""Ключ привилегий ручек агентов (ревью 12 окт): агент с командой оболочки не создаёт агентов и не выдаёт права мимо
своих, даже с этой машины и без SAM_API_KEY. Настоящее приложение шлюза по HTTP (mongomock, свежая шина)."""
import contextlib
import functools
import os

import pytest

from gateway import privilege_key as pk
from tests.fakes_agents import GatewayRig

PRIVILEGED = [
    ("post", "/sam/agents", {"description": "x"}),
    ("post", "/sam/agents/ab12/grant", {"tool": "run_command"}),
    ("post", "/sam/main-model", {"main": "llm"}),
    ("post", "/sam/llm/switch", {"adapter": "auto"}),
    ("post", "/sam/sandbox/exec", {"code": "1"}),
]


@pytest.fixture
def gw(monkeypatch, tmp_path):
    return GatewayRig(monkeypatch, tmp_path)


@pytest.mark.parametrize("presented", [None, "foreign-key"], ids=["no_key", "foreign_key"])
@pytest.mark.parametrize("method, path, body", PRIVILEGED, ids=[p for _, p, _ in PRIVILEGED])
def test_privileged_routes_need_privilege_key_even_locally(gw, method, path, body, presented):
    # с этой машины, SAM_API_KEY пуст - раньше это пускалось
    headers = {pk.HEADER_NAME: presented} if presented else {}
    r = getattr(gw.client, method)(path, json=body, headers=headers)
    assert r.status_code == 401 and r.json()["error"] == pk.NEED
    assert gw.handles == {} and gw.bus.events == []


@pytest.mark.parametrize("method, path, body, privileged, status", [
    ("post", "/sam/agents", {"description": "x", "level": "genius"}, True, 400),   # с ключом - до ручки
    ("post", "/sam/sandbox/exec", {"code": "1", "lang": "ruby"}, True, 200),       # отказ пула - строкой в ответе
    ("post", "/sam/agents/ab12/ask", {"question": "?"}, False, 404),               # канал агента - без ключа
    ("post", "/sam/agents/ab12/inbox", {}, False, 404),
], ids=["agents_with_key", "sandbox_exec_with_key", "agent_channel_ask", "agent_channel_inbox"])
def test_routes_reach_their_handler_with_the_right_key_or_without_one(gw, method, path, body, privileged, status):
    r = getattr(gw.client, method)(path, **({"json": body} if body is not None else {}),
                                   headers=gw.privileged if privileged else {})
    assert r.status_code == status


@contextlib.asynccontextmanager
async def _no_process(envs, client, command, *args, env=None, cwd=None):
    """Запуск процесса Cortex на границе ОС: окружение записано, процесс не поднимается."""
    envs.append(env)
    raise OSError("процесс в тесте не запускается")
    yield


def _holding(env: dict, key: str) -> list[str]:
    """Имена переменных со значением ключа: при падении печатаются имена, а не окружение с ключами API."""
    return [name for name, value in env.items() if value == key]


async def test_agents_get_no_privilege_key_in_their_environment(tmp_path, monkeypatch):
    """stand 45: ключа нет в окружении шлюза (воркер идёт в нём, дети его наследуют); даже окажись он там переменной,
    процесс сессии Cortex и агенты кода (все каркасы DEFAULT_BACKENDS) его не получают."""
    from acp.transports import default_environment

    from gateway import agent
    from gateway.acp.agent import AcpError
    from gateway.tools import coding_agents
    from tests.fakes_agents import CliRunner
    monkeypatch.setenv(pk.FILE_ENV, str(tmp_path / "privilege.key"))
    monkeypatch.setattr(pk, "_cached", [])
    key = pk.key()
    assert _holding(dict(os.environ), key) == []

    monkeypatch.setenv("SAM_PRIVILEGE_KEY", key)          # ключ, по ошибке положенный в .env
    monkeypatch.setenv("SAM_ACP_ENV", "GEMINI_API_KEY")
    monkeypatch.setenv("GEMINI_API_KEY", "test-do-not-use")
    for name in ("SAM_ACP_HOME_TEMPLATE", "SAM_ACP_HOME_VAR", "SAM_ACP_HOME"):   # настройки машины не участвуют
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SAM_CORTEX_WORKER_CWD", str(tmp_path))
    envs: list[dict] = []
    workers = agent.build_cortex_workers(spawn_process=functools.partial(_no_process, envs))
    with pytest.raises(AcpError):
        async for _ in workers._deps.agent.turn("x"):
            pass
    [given] = envs
    process_env = {**default_environment(), **given}       # так окружение собирает SDK ACP
    assert given == {"GEMINI_API_KEY": "test-do-not-use"} and _holding(process_env, key) == []

    for make in coding_agents.DEFAULT_BACKENDS:
        runner = CliRunner()
        backend = make(runner)
        monkeypatch.setenv(backend._cli_env, backend.name)
        backend.run("x", str(tmp_path), 1)
        [env] = runner.envs
        assert env is not None and _holding(env, key) == [], backend.name


def test_privilege_key_is_created_once_in_its_file(tmp_path, monkeypatch):
    monkeypatch.setenv(pk.FILE_ENV, str(tmp_path / "secrets" / "privilege.key"))
    monkeypatch.setattr(pk, "_cached", [])
    first = pk.key()
    pk.forget()
    assert len(first) >= pk.KEY_BYTES and pk.key() == first
    assert (tmp_path / "secrets" / "privilege.key").read_text(encoding="utf-8") == first


def test_outside_main_model_agents_and_sandbox_go_from_the_parent_session(gw, monkeypatch):
    """agents 47, sandbox 73: агенты основной модели вне шлюза (POST /sam/agents) и её быстрая песочница
    (POST /sam/sandbox/exec) идут от сессии SAM_AGENT_PARENT_SESSION: монитор относит их к беседе, тёплый контейнер -
    той же беседы. Запуск воркера и пул контейнеров - граница, подменены."""
    from types import SimpleNamespace

    from gateway import sandbox_warm
    monkeypatch.setattr(gw.agent, "AGENT_PARENT_SESSION", "kitchen")
    launched, owners = [], []
    monkeypatch.setattr(gw.agent, "_launch_worker", lambda spec: launched.append(spec) or "b" * 32)
    monkeypatch.setattr(gw.agent, "_resolve_device", lambda device: "pc")

    async def _llm(level, steps=None, kind="", env="", task_id=""):
        return {"source": "test", "model": "m"}
    monkeypatch.setattr(gw.agent, "_llm_for_level", _llm)

    class _Pool:
        def exec(self, owner, params):
            owners.append(owner)
            return SimpleNamespace(text="Код выхода 0", code=0, ms=1.0)
    monkeypatch.setattr(sandbox_warm, "pool", _Pool)
    assert gw.client.post("/sam/agents", json={"description": "x"}, headers=gw.privileged).status_code == 200
    assert gw.client.post("/sam/sandbox/exec", json={"code": "1"}, headers=gw.privileged).status_code == 200
    assert [s.parent_session for s in launched] == ["kitchen"] and [o.session for o in owners] == ["kitchen"]


def test_agent_effort_route_needs_the_key():
    from gateway import privilege_key
    assert privilege_key.needs("POST", "/sam/agents/abcd1234/effort")
    assert not privilege_key.needs("GET", "/sam/agents/abcd1234/info")
