"""start_sam.ps1 трогает только процессы SAM из .sam_pids (SAM-64)."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sam_process.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
PS_TIMEOUT_S = 60   # холодный старт PowerShell на занятой машине
pytestmark = pytest.mark.skipif(POWERSHELL is None or sys.platform != "win32",
                                reason="скрипты стенда - PowerShell под Windows")


def _ps(expr: str) -> str:
    cmd = f". '{SCRIPT}'; {expr}"
    out = subprocess.run([POWERSHELL, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=PS_TIMEOUT_S)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.mark.parametrize("process, expected", [
    ("(Get-Process -Id $PID)", "True"),
    *[(f"([pscustomobject]@{{Name='{name}'}})", "False") for name in ("svchost", "explorer", "chrome")],
    ("$null", "False"),
], ids=["own_powershell", "svchost", "explorer", "chrome", "missing"])
def test_only_sam_processes_are_touched(process, expected):
    assert _ps(f"Test-SamProcess {process}") == expected


def test_component_tab_restarts_until_stop_flag(tmp_path):
    """Вкладка компонента поднимает упавший процесс заново, пока Stop-SAM не положил флаг (SAM-80)."""
    counter, flag = tmp_path / "runs.txt", tmp_path / ".sam_stop"
    crash = (f"Add-Content '{counter}' 'run'; "
             f"if ((Get-Content '{counter}').Count -ge 3) {{ New-Item -ItemType File '{flag}' | Out-Null }}; "
             "cmd /c exit 1")
    script = _ps(f"New-ComponentScript '{tmp_path}' 'SAM: Test' \"{crash}\" '{flag}' 0")
    out = subprocess.run([POWERSHELL, "-NoProfile", "-File", script], capture_output=True, text=True, timeout=PS_TIMEOUT_S)
    assert out.returncode == 0
    assert counter.read_text(encoding="utf-8").split() == ["run", "run", "run"]


def test_component_with_a_log_writes_output_and_restarts_there_and_rotates_a_big_log(tmp_path, monkeypatch):
    """15 окт: вкладки с непрерывным выводом вешали Windows Terminal; компонент пишет вывод в свой лог, большой лог
    при старте уходит в .1."""
    counter, flag, log = tmp_path / "runs.txt", tmp_path / ".sam_stop", tmp_path / "logs" / "test.log"
    log.parent.mkdir()
    log.write_text("x" * 2048, encoding="utf-8")
    monkeypatch.setenv("SAM_COMPONENT_LOG_MAX_MB", "0")          # порог 0: старый лог уходит при первом старте
    crash = (f"Add-Content '{counter}' 'run'; Write-Output 'строка лога'; "
             f"if ((Get-Content '{counter}').Count -ge 2) {{ New-Item -ItemType File '{flag}' | Out-Null }}; "
             "cmd /c exit 1")
    script = _ps(f"New-ComponentScript '{tmp_path}' 'SAM: Test' \"{crash}\" '{flag}' 0 '{log}'")
    out = subprocess.run([POWERSHELL, "-NoProfile", "-File", script], capture_output=True, text=True, timeout=PS_TIMEOUT_S)
    assert out.returncode == 0 and "строка лога" not in out.stdout          # в окно ничего
    text = log.read_text(encoding="utf-8-sig")
    assert text.count("строка лога") >= 1 and "restart #1" in text
    assert (tmp_path / "logs" / "test.log.1").exists()


def test_component_log_name_comes_from_its_title():
    assert _ps("Get-ComponentLogName 'SAM: Gateway'") == "gateway.log"
    assert _ps("Get-ComponentLogName 'SAM: Silero'") == "silero.log"


# ─── что гасит Stop-SAM (10 окт: рестарт оставлял вкладки и процессы прошлого запуска) ─────────────────

STOP = r"C:\sam\.sam_stop"
_TAB = f"`$Host.UI.RawUI.WindowTitle = '{{title}}'`nwhile (-not (Test-Path '{STOP}')) {{{{ python }}}}"
# Вкладки, их скрипты и процессы стенда, как 10 окт 02:14: старая вкладка шлюза, мост, тикер, надзор, сессия
_SCRIPTS = {
    r"C:\t\gw_old.ps1": _TAB.format(title="SAM: Gateway"),
    r"C:\t\gw.ps1": _TAB.format(title="SAM: Gateway"),
    r"C:\t\voice.ps1": _TAB.format(title="SAM: Voice"),
    r"C:\t\lead.ps1": "`$Host.UI.RawUI.WindowTitle = 'SAM: Claude Code'`n& claude",
    r"C:\t\other.ps1": _TAB.format(title="SAM: Gateway").replace(STOP, r"D:\other\.sam_stop"),
}
_PROCS = [
    (10, 1, "powershell.exe", r"powershell -NoExit -File C:\t\gw_old.ps1"),
    (11, 1, "powershell.exe", r"powershell -NoExit -File C:\t\gw.ps1"),
    (12, 1, "powershell.exe", r"powershell -NoExit -File C:\t\voice.ps1"),
    (13, 1, "powershell.exe", r"powershell -NoExit -File C:\t\lead.ps1"),
    (14, 1, "powershell.exe", r"powershell -NoExit -File C:\t\other.ps1"),
    (20, 11, "python.exe", "python -m uvicorn gateway.main:app --port 8000"),
    (21, 12, "python.exe", "python -m voice.layer.voice"),
    (22, 99, "python.exe", "python -m voice.layer.voice"),
    (23, 1, "cmd.exe", 'cmd /c "python -u -m voice.layer.bridge >> logs/bridge.log"'),
    (24, 23, "python.exe", "python -u -m voice.layer.bridge"),
    (25, 1, "python.exe", "python -u -m voice.layer.ticker"),
    (26, 1, "python.exe", "python -u scripts/stand_watch.py"),
    (27, 1, "python.exe", "python -u scripts/lead_heartbeat.py"),
    (28, 1, "python.exe", "python -u -m voice.layer.vmic"),
    (29, 1, "python.exe", "python -m voice.layer.voice_bank"),
    (30, 1, "pwsh.exe", "pwsh -Command Get-Process | ? CommandLine -like '*voice.layer.voice*'"),
]


def _fixture() -> str:
    procs = ", ".join(f"[pscustomobject]@{{ProcessId={pid}; ParentProcessId={ppid}; Name='{name}'; "
                      f"CommandLine='{cmd.replace(chr(39), chr(39) * 2)}'}}" for pid, ppid, name, cmd in _PROCS)
    texts = "; ".join(f"'{path}' = \"{text}\"" for path, text in _SCRIPTS.items())
    return f"$procs = @({procs}); $texts = @{{ {texts} }}; $read = {{ param($p) $texts[$p] }}; "


def _targets(keys: str = "") -> list[str]:
    arg = f" -Keys {keys}" if keys else ""
    out = _ps(_fixture() + f"Select-SamTargets $procs '{STOP}' $read{arg} | "
              "ForEach-Object { \"$($_.Kind) $($_.Component) $($_.ProcessId)\" }")
    return out.splitlines()


_CHILD_PROCS = ("$procs = @([pscustomobject]@{ProcessId=1; ParentProcessId=0; Name='python.exe'; CommandLine='python mcp/client_agent.py'},"
                "[pscustomobject]@{ProcessId=2; ParentProcessId=0; Name='python.exe'; CommandLine='python mcp/mcp_aggregator.py'},"
                "[pscustomobject]@{ProcessId=3; ParentProcessId=1; Name='python.exe'; CommandLine='python mcp/mcp_aggregator.py'}); ")


@pytest.mark.parametrize("run, expected", [
    # все цели: сначала вкладки, потом процессы, надзор тоже (SAM-195); сессия, тикер, живость, vmic и чужой
    # checkout не трогаются
    (lambda: _targets(), ["tab Gateway 10", "tab Gateway 11", "tab Voice 12",
                          "process Gateway 20", "process Voice 21", "process Voice 22", "process Bridge 24",
                          "process Watch 26"]),
    (lambda: _targets("Gateway"), ["tab Gateway 10", "tab Gateway 11", "process Gateway 20"]),
    (lambda: _ps(_fixture() + f"Find-SamDuplicates $procs '{STOP}' $read").splitlines(),
     ["Gateway: 2 tabs (PID 10, 11)", "Voice: 2 processes (PID 21, 22)"]),
    (lambda: _ps(_CHILD_PROCS + f"Find-SamDuplicates $procs '{STOP}' {{ $null }}"), ""),
], ids=["all_targets", "single_key", "duplicates", "child_is_not_duplicate"])
def test_stop_targets_and_duplicates(run, expected):
    assert run() == expected


# ─── чужой процесс на порту стенда (14 окт: -Stop убил mitmdump другого проекта на 8000) ─────────────────

_PORT_PROCS = ("$procs = @("
               "[pscustomobject]@{ProcessId=40; ParentProcessId=1; Name='python.exe'; CommandLine='python mitmdump.exe --listen-port 8000'},"
               "[pscustomobject]@{ProcessId=41; ParentProcessId=1; Name='python.exe'; CommandLine='python -m uvicorn gateway.main:app --port 8000'},"
               "[pscustomobject]@{ProcessId=42; ParentProcessId=41; Name='python.exe'; CommandLine='python -c spawn_main'},"
               "[pscustomobject]@{ProcessId=43; ParentProcessId=1; Name='node.exe'; CommandLine='node dev-server.js --port 3000'},"
               # uvicorn --reload из venv: лаунчер (компонент) -> reloader -> лаунчер spawn_main -> spawn_main держит порт
               "[pscustomobject]@{ProcessId=50; ParentProcessId=1; Name='python.exe'; CommandLine='venv\python -m uvicorn gateway.main:app --reload'},"
               "[pscustomobject]@{ProcessId=51; ParentProcessId=50; Name='python.exe'; CommandLine='python reloader'},"
               "[pscustomobject]@{ProcessId=52; ParentProcessId=51; Name='python.exe'; CommandLine='venv\python -c spawn_main'},"
               "[pscustomobject]@{ProcessId=53; ParentProcessId=52; Name='python.exe'; CommandLine='python -c spawn_main'},"
               # родителя-компонента уже нет, дочерний пережил taskkill /T
               "[pscustomobject]@{ProcessId=60; ParentProcessId=999; Name='python.exe'; CommandLine='python -c spawn_main'},"
               # PID повторно выдан: цикл родителей
               "[pscustomobject]@{ProcessId=70; ParentProcessId=71; Name='python.exe'; CommandLine='python a'},"
               "[pscustomobject]@{ProcessId=71; ParentProcessId=70; Name='python.exe'; CommandLine='python b'},"
               # PID родителя достался шлюзу SAM позже: чужой mitmdump старше «родителя»
               "[pscustomobject]@{ProcessId=80; ParentProcessId=81; Name='python.exe'; CommandLine='python mitmdump.exe';"
               " CreationDate=[datetime]'2026-10-14 04:55'},"
               "[pscustomobject]@{ProcessId=81; ParentProcessId=1; Name='python.exe'; CommandLine='python -m uvicorn gateway.main:app';"
               " CreationDate=[datetime]'2026-10-14 09:00'}); ")


def _split(holders: str, procs: str = "$procs") -> str:
    out = _ps(_PORT_PROCS + f"$s = Split-PortHolders @({holders}) {procs}; "
              "\"own=$(($s.Own | ForEach-Object { $_.ProcessId }) -join ',') foreign=$(($s.Foreign | ForEach-Object { $_.ProcessId }) -join ',')\"")
    return out


@pytest.mark.parametrize("holders, procs, expected", [
    ("40, 41", "$procs", "own=41 foreign=40"),      # чужой python на 8000 и шлюз SAM
    ("42", "$procs", "own=42 foreign="),            # дочерний процесс шлюза - свой
    ("43", "$procs", "own= foreign=43"),            # чужой dev-сервер на 3000
    ("53", "$procs", "own=53 foreign="),            # держатель на три уровня ниже компонента
    ("60", "$procs", "own= foreign=60"),            # родитель ушёл: чужой (Stop-SAM снимает своих до остановки)
    ("70", "$procs", "own= foreign=70"),            # цикл родителей не вешает разбор
    ("99", "$procs", "own= foreign=99"),            # процесса нет в таблице - не свой
    ("41", "@()", "own= foreign=41"),               # таблица не прочиталась - все чужие
    ("80", "$procs", "own= foreign=80"),            # номер родителя переиспользован: «родитель» моложе потомка
], ids=["foreign_python", "gateway_child", "foreign_node", "reload_grandchild", "orphan_child", "pid_cycle",
        "not_in_table", "empty_table", "reused_parent_pid"])
def test_port_holders_split_own_and_foreign(holders, procs, expected):
    assert _split(holders, procs) == expected


_T0, _T1 = "[datetime]'2026-10-14 09:00'", "[datetime]'2026-10-14 09:30'"


@pytest.mark.parametrize("split, snapshot, expected", [
    # свой сейчас - добить
    (f"@{{Own=@([pscustomobject]@{{ProcessId=41; CreationDate={_T0}}}); Foreign=@()}}", "@()", "41"),
    # сирота, но тот же процесс, что в снимке до остановки - добить
    (f"@{{Own=@(); Foreign=@([pscustomobject]@{{ProcessId=60; CreationDate={_T0}}})}}",
     f"@([pscustomobject]@{{ProcessId=60; CreationDate={_T0}}})", "60"),
    # номер из снимка достался другому процессу - ничего не убивать
    (f"@{{Own=@(); Foreign=@([pscustomobject]@{{ProcessId=60; CreationDate={_T1}}})}}",
     f"@([pscustomobject]@{{ProcessId=60; CreationDate={_T0}}})", "none"),
    # чужой, которого не было в снимке, - ничего не убивать, и своих тоже
    (f"@{{Own=@([pscustomobject]@{{ProcessId=41; CreationDate={_T0}}}); Foreign=@([pscustomobject]@{{ProcessId=40}})}}",
     "@()", "none"),
    # в снимке нет времени создания - не доказано, что тот же процесс
    ("@{Own=@(); Foreign=@([pscustomobject]@{ProcessId=60})}", "@([pscustomobject]@{ProcessId=60})", "none"),
], ids=["own_now", "orphan_from_snapshot", "reused_pid", "foreign_outside_snapshot", "no_creation_date"])
def test_port_kill_targets(split, snapshot, expected):
    out = _ps(f"$t = Select-PortKillTargets ([pscustomobject]{split}) {snapshot}; "
              "if ($null -eq $t) { 'none' } else { $t -join ',' }")
    assert out == expected


LEAD = SCRIPT.parent / "sam_lead.ps1"


@pytest.mark.parametrize("env, dotenv_line, expected", [
    ("12202", "SILERO_PORT=13202", "12202"),            # окружение процесса главнее .env, как у серверов
    ("", "SILERO_PORT=13202", "13202"),
    ("", "SILERO_PORT=13202  # порт синтеза", "13202"),  # комментарий без кавычек срезается, как у python-dotenv
    ("", "SILERO_PORT=tts", "10202"),                   # не число - умолчание со строкой в лог
    ("", "", "10202"),
], ids=["env", "dotenv", "dotenv_comment", "not_a_number", "default"])
def test_stand_port_setting(tmp_path, env, dotenv_line, expected):
    """Порт синтеза в start_sam.ps1 берётся так же, как его берёт сервер: окружение, .env, умолчание; сервису
    он передаётся явно (--port)."""
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{dotenv_line}\n", encoding="utf-8")
    expr = (f"$env:SILERO_PORT='{env}'; . '{LEAD}'; "
            f"Get-SamPortSetting 'SILERO_PORT' 10202 (Read-DotEnv '{dotenv}') 6>$null")
    out = subprocess.run([POWERSHELL, "-NoProfile", "-Command", expr], capture_output=True, text=True, timeout=PS_TIMEOUT_S)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == expected


LEAD_SCRIPT = SCRIPT.parent / "sam_lead.ps1"


def _lead_ps(expr: str) -> str:
    out = subprocess.run([POWERSHELL, "-NoProfile", "-Command", f". '{LEAD_SCRIPT}'; {expr}"], capture_output=True,
                         text=True, timeout=PS_TIMEOUT_S)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.mark.parametrize("age_s, alive", [(4.9, "True"), (5.1, "False"), (None, "False")],
                         ids=["fresh", "stale", "no-file"])
def test_lead_tab_opens_only_when_lead_alive_is_older_than_five_seconds(age_s, alive):
    """stand 29: вкладка сессии Claude Code не открывается второй раз, пока lead.alive моложе 5 с - порога шлюза."""
    last = "$null" if age_s is None else f"$now.AddSeconds(-{age_s})"
    assert _lead_ps(f"$now = Get-Date; Test-LeadAlive {last} $now $LEAD_ALIVE_S; $LEAD_ALIVE_S") == f"{alive}\n5"


def test_bridge_is_found_by_its_python_command_line_only():
    """stand 20: мост второй раз не запускается - живой мост находится по командной строке python; cmd.exe с той же
    строкой (держит перенаправление лога) и чужой python мостом не считаются."""
    procs = ("@([pscustomobject]@{ProcessId=1; Name='cmd.exe'; CommandLine='cmd /c python -u -m voice.layer.bridge'},"
             " [pscustomobject]@{ProcessId=2; Name='python.exe'; CommandLine='python -u -m voice.layer.bridge'},"
             " [pscustomobject]@{ProcessId=3; Name='python.exe'; CommandLine='python -m voice.layer.voice'})")
    assert _lead_ps(f"(Select-BridgeProcess {procs}).ProcessId -join ','") == "2"
    assert _lead_ps("(Select-BridgeProcess @()).Count") == "0"


# ─── вкладка Claude Code открывается, только если она основная модель ─────────────────────────────

LEAD_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sam_lead.ps1"


@pytest.mark.parametrize("order, expected", [
    ("claude-code,llm", "True"),
    (" claude-code ", "True"),
    ("llm,claude-code", "False"),
    ("llm", "False"),
    ("", "False"),
], ids=["lead_first", "lead_padded", "llm_first", "llm_only", "empty"])
def test_lead_tab_opens_only_when_claude_code_is_first_main_model(order, expected):
    cmd = f". '{LEAD_SCRIPT}'; Test-LeadWanted '{order}'"
    out = subprocess.run([POWERSHELL, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=PS_TIMEOUT_S)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == expected
