"""Копия файлов с ПК в задачу песочницы (SAM-83, ADR-016): только из SAM_SANDBOX_IMPORT_ROOTS, без ссылок и
секретов, с потолками; копия байтами, без общих монтирований; отказ - до первой записи."""
import json
import os
import threading
from datetime import datetime, timezone

import pytest

from gateway import sandbox, sandbox_import
from gateway.tools.coding_agents import CliResult

LIMITS = sandbox.Limits(image=sandbox.DEFAULT_IMAGE, network="none", memory="512m", cpus="1", pids="128")
JOB = "0123456789ab"
AT = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
SPECIAL_FILE_WAIT_S = 10   # копия папки в тесте идёт доли секунды
BINARY = bytes(range(256)) * 4 + b"\r\n\x00\xff"


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """Папка на ПК, разрешённая для копии, и папка задач."""
    root = tmp_path / "pc"
    (root / "project" / "data").mkdir(parents=True)
    (root / "project" / "main.py").write_bytes(b"print('hi')\n")
    (root / "project" / "data" / "blob.bin").write_bytes(BINARY)
    (root / "project" / ".env").write_text("KEY=secret", encoding="utf-8")
    (root / "project" / ".git").mkdir()
    (root / "project" / ".git" / "config").write_text("x", encoding="utf-8")
    (root / "project" / "secrets").mkdir()
    (root / "project" / "secrets" / "a.txt").write_text("x", encoding="utf-8")
    (root / "project" / "server.pem").write_text("x", encoding="utf-8")
    (root / "project" / "id_rsa.pub").write_text("x", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("OUTSIDE", encoding="utf-8")
    monkeypatch.setenv(sandbox_import.ROOTS_ENV, f"{tmp_path / 'other'};{root}")
    for env in (sandbox_import.MAX_FILES_ENV, sandbox_import.MAX_BYTES_ENV, sandbox_import.SKIP_ENV):
        monkeypatch.delenv(env, raising=False)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    return root, jobs


def _run(jobs, params, runner=None):
    return sandbox.run(params, root=jobs, limits=LIMITS, runner=runner or (lambda *a, **k: CliResult(0, "ok", "")),
                       new_id=lambda: JOB, now=lambda: AT, osv=lambda p: None)


def _in_time(call, seconds=SPECIAL_FILE_WAIT_S):
    """Вызов в потоке со сроком: копия, открывшая FIFO на чтение, висела бы вечно - тест падает, а не виснет."""
    done = []
    worker = threading.Thread(target=lambda: done.append(call()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert done, f"копия не закончилась за {seconds} с: спецфайл открыт на чтение"
    return done[0]


def _journal(jobs):
    return [json.loads(s) for s in (jobs / f"{JOB}.log").read_text(encoding="utf-8").splitlines()]


def test_folder_is_copied_byte_for_byte_without_secrets(pc):
    root, jobs = pc
    out = _run(jobs, {"import": [{"from": str(root / "project"), "to": "src"}]})
    base = jobs / JOB
    assert (base / "src" / "data" / "blob.bin").read_bytes() == BINARY
    assert (base / "src" / "main.py").read_bytes() == b"print('hi')\n"
    for secret in (".env", ".git", "secrets", "server.pem", "id_rsa.pub"):
        assert not (base / "src" / secret).exists()
    assert "Скопировано с ПК: 2 файлов" in out.text and "пропущено 5" in out.text
    line = [s for s in _journal(jobs) if s["kind"] == "import"][0]
    assert line["files"] == 2 and line["bytes"] == len(BINARY) + len("print('hi')\n") and line["code"] == 0
    assert line["skipped_total"] == 5 and any(".env" in s for s in line["skipped"])
    assert "secret" not in json.dumps(line).replace("secrets (", "")      # содержимое секретов не в журнале


def test_relative_source_and_default_destination(pc):
    root, jobs = pc
    _run(jobs, {"import": [{"from": "project/main.py"}]})
    assert (jobs / JOB / "main.py").read_text(encoding="utf-8") == "print('hi')\n"


@pytest.mark.parametrize("source, expected", [
    ("{tmp}/outside.txt", "вне разрешённых папок"),
    ("{root}/project/../../outside.txt", "с «..»"),
    ("{root}/missing.txt", "нет на ПК"),
    ("", "нет from"),
], ids=["outside_roots", "dot_dot", "missing", "empty"])
def test_source_outside_roots_is_refused(pc, source, expected):
    root, jobs = pc
    out = _run(jobs, {"import": [{"from": source.format(tmp=root.parent, root=root), "to": "x"}],
                      "command": "ls"})
    assert expected in out.text and "\n" not in out.text
    assert not (jobs / JOB / "x").exists()
    assert _journal(jobs)[0]["kind"] == "import" and expected in _journal(jobs)[0]["error"]


def test_source_through_a_link_is_refused(pc):
    root, jobs = pc
    try:
        os.symlink(root.parent, root / "link", target_is_directory=True)
    except OSError as e:
        pytest.skip(f"симлинк не создать ({e.__class__.__name__}): на Windows без прав разработчика")
    out = _run(jobs, {"import": [{"from": str(root / "link" / "outside.txt")}]})
    assert "через ссылку" in out.text


def test_links_inside_the_folder_are_skipped(pc):
    root, jobs = pc
    try:
        os.symlink(root.parent / "outside.txt", root / "project" / "out.txt")
    except OSError as e:
        pytest.skip(f"симлинк не создать ({e.__class__.__name__}): на Windows без прав разработчика")
    out = _run(jobs, {"import": [{"from": str(root / "project"), "to": "src"}]})
    assert not (jobs / JOB / "src" / "out.txt").exists() and "пропущено 6" in out.text


def test_special_files_inside_the_folder_are_skipped(pc):
    """sandbox 24: FIFO внутри папки пропускается, а не читается (чтение FIFO повисло бы на записи с ПК)."""
    root, jobs = pc
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO на этой ОС не создать; на Linux идёт")
    os.mkfifo(root / "project" / "pipe")
    out = _in_time(lambda: _run(jobs, {"import": [{"from": str(root / "project"), "to": "src"}]}))
    assert not (jobs / JOB / "src" / "pipe").exists() and "пропущено 6" in out.text


def test_import_is_off_without_roots(pc, monkeypatch):
    root, jobs = pc
    monkeypatch.setenv(sandbox_import.ROOTS_ENV, "")
    out = _run(jobs, {"import": [{"from": str(root / "project")}]})
    assert "Копия с ПК выключена" in out.text and sandbox_import.ROOTS_ENV in out.text


@pytest.mark.parametrize("env, value, expected", [
    (sandbox_import.MAX_FILES_ENV, "1", "больше 1 файлов"),
    (sandbox_import.MAX_BYTES_ENV, "100", "больше 100 байт"),
], ids=["files", "bytes"])
def test_ceilings_refuse_before_any_write(pc, monkeypatch, env, value, expected):
    root, jobs = pc
    monkeypatch.setenv(env, value)
    out = _run(jobs, {"import": [{"from": str(root / "project"), "to": "src"}]})
    assert expected in out.text and not (jobs / JOB / "src").exists()


def test_extra_masks_from_env(pc, monkeypatch):
    root, jobs = pc
    monkeypatch.setenv(sandbox_import.SKIP_ENV, "*.bin")
    _run(jobs, {"import": [{"from": str(root / "project"), "to": "src"}]})
    assert not (jobs / JOB / "src" / "data" / "blob.bin").exists() and (jobs / JOB / "src" / "main.py").exists()


@pytest.mark.parametrize("to, expected", [("../x", "вне папки задачи"), ("busy", "занят папкой")],
                         ids=["outside_job", "taken_by_folder"])
def test_destination_must_be_a_free_place_in_the_job(pc, to, expected):
    root, jobs = pc
    (jobs / JOB / "busy").mkdir(parents=True)
    out = _run(jobs, {"import": [{"from": str(root / "project" / "main.py"), "to": to}]})
    assert expected in out.text


def test_lock_files_are_not_imported_with_packages(pc):
    root, jobs = pc
    (root / "project" / "requirements.lock").write_text("--index-url http://evil #==1\n", encoding="utf-8")
    out = _run(jobs, {"import": [{"from": str(root / "project" / "requirements.lock")}],
                      "packages": ["pip:six==1.16.0"]})
    assert "при packages не копируется" in out.text


@pytest.mark.parametrize("value", [{"from": "x"}, ["x"], [{"from": "a"}] * 11], ids=["object", "strings", "too_many"])
def test_import_shape_is_checked(pc, value):
    root, jobs = pc
    out = _run(jobs, {"import": value})
    assert "import" in out.text and "\n" not in out.text


def test_import_then_command_in_one_run(pc):
    root, jobs = pc
    seen = []

    def runner(argv, cwd, timeout, env=None):
        seen.append(sorted(p.name for p in (jobs / JOB).iterdir()))
        return CliResult(0, "ok", "")
    out = _run(jobs, {"import": [{"from": str(root / "project" / "main.py")}], "command": "python main.py"}, runner)
    assert seen == [["main.py"]] and out.code == 0 and out.text.startswith("Скопировано с ПК: 1 файлов")


@pytest.mark.parametrize("source", ["project/.git", "project/.git/config", "project/secrets/a.txt", "project/.env",
                                    "project/server.pem"],
                         ids=["git_dir", "git_file", "secrets_file", "env", "pem"])
def test_source_inside_a_secret_name_is_refused(pc, source):
    root, jobs = pc
    out = _run(jobs, {"import": [{"from": source, "to": "x"}]})
    assert "через папку или файл секретов" in out.text and not (jobs / JOB / "x").exists()


def test_ntfs_stream_name_is_refused(pc):
    root, jobs = pc
    out = _run(jobs, {"import": [{"from": "project/.env::$DATA", "to": "x"}]})
    # Windows: имя с «:» - отказ сразу; Linux: такого файла нет - тоже отказ; содержимое .env не копируется
    assert out.code is None and not (jobs / JOB / "x").exists()
    if os.name == "nt":
        assert "поток NTFS" in out.text


def test_short_8_3_name_is_checked_by_its_long_name(pc):
    root, jobs = pc
    if os.name != "nt":
        pytest.skip("короткие имена 8.3 - только NTFS")
    import ctypes
    target = root / "project" / "credentials_store"
    target.mkdir()
    (target / "a.txt").write_text("x", encoding="utf-8")
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.kernel32.GetShortPathNameW(str(target), buf, 512)
    short = os.path.basename(buf.value)
    if not short or short.lower() == "credentials_store":
        pytest.skip("на этом томе короткие имена 8.3 выключены (fsutil 8dot3name)")
    out = _run(jobs, {"import": [{"from": f"project/{short}/a.txt", "to": "x"}]})
    assert "через папку или файл секретов" in out.text


def test_junction_on_the_way_or_inside_is_not_followed(pc):
    root, jobs = pc
    if os.name != "nt":
        pytest.skip("junction - только Windows; симлинки Linux проверяют тесты выше")
    import _winapi
    _winapi.CreateJunction(str(root.parent), str(root / "jump"))
    _winapi.CreateJunction(str(root.parent), str(root / "project" / "inner"))
    out = _run(jobs, {"import": [{"from": "jump/outside.txt", "to": "x"}]})
    assert "через ссылку" in out.text
    out = _run(jobs, {"import": [{"from": "project", "to": "src"}]})
    assert not (jobs / JOB / "src" / "inner").exists() and "OUTSIDE" not in out.text


@pytest.mark.parametrize("name", [".ssh", "id_ed25519", "id_ecdsa.pub", "key.ppk", ".aws", ".kube", "token_a.json",
                                  "service-account-1.json", "terraform.tfstate", "prod.env", ".npmrc", ".pypirc",
                                  ".git-credentials", ".netrc", "store.kdbx", "cert.p12", "Secrets.yaml", ".envrc"])
def test_extended_masks(pc, name):
    root, jobs = pc
    (root / "project" / "extra").mkdir()
    (root / "project" / "extra" / name).write_text("SECRET", encoding="utf-8")
    (root / "project" / "extra" / "ok.txt").write_text("ok", encoding="utf-8")
    _run(jobs, {"import": [{"from": "project/extra", "to": "e"}]})
    assert (jobs / JOB / "e" / "ok.txt").exists() and not (jobs / JOB / "e" / name).exists()


@pytest.mark.parametrize("name", [".credentials.json", "google_credentials.json", ".claude.json", ".pgpass",
                                  "token.json", "tokens.json", "token_x.json", "gh_token.json"])
def test_dotted_and_tool_secrets_are_masked(pc, name):
    root, jobs = pc
    (root / "project" / "extra").mkdir()
    (root / "project" / "extra" / name).write_text("SECRET", encoding="utf-8")
    (root / "project" / "extra" / "tokenizer.json").write_text("{}", encoding="utf-8")
    (root / "project" / "extra" / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    _run(jobs, {"import": [{"from": "project/extra", "to": "e"}]})
    assert not (jobs / JOB / "e" / name).exists()
    assert (jobs / JOB / "e" / "tokenizer.json").exists() and (jobs / JOB / "e" / "tokenizer_config.json").exists()


def test_github_cli_folder_is_masked(pc):
    root, jobs = pc
    (root / "project" / ".config" / "gh").mkdir(parents=True)
    (root / "project" / ".config" / "gh" / "hosts.yml").write_text("oauth_token: x", encoding="utf-8")
    (root / "project" / ".config" / "ok.txt").write_text("ok", encoding="utf-8")
    _run(jobs, {"import": [{"from": "project/.config", "to": "c"}]})
    assert (jobs / JOB / "c" / "ok.txt").exists() and not (jobs / JOB / "c" / "gh").exists()


def test_home_and_folders_above_it_are_not_roots(tmp_path, monkeypatch, caplog):
    home = tmp_path / "home" / "user"
    home.mkdir(parents=True)
    monkeypatch.setattr(sandbox_import.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv(sandbox_import.ROOTS_ENV, f"{home};{tmp_path / 'home'};{home / 'projects'}")
    assert sandbox_import.roots() == [(home / "projects").resolve()]
    assert "домашняя папка" in caplog.text


def test_clash_ignores_case_only_on_windows():
    error = sandbox_import._clash(["src/Makefile", "src/makefile"])
    assert (error is not None) == (os.name == "nt")
    assert sandbox_import._clash(["a", "a"]) is not None


def test_unsafe_roots_are_not_used(tmp_path, caplog):
    sandbox_dir = tmp_path / "data" / "sandbox"
    sandbox_dir.mkdir(parents=True)
    anchor = tmp_path.anchor
    os.environ[sandbox_import.ROOTS_ENV] = f"{anchor};{tmp_path};{sandbox_dir / 'x'};{tmp_path / 'data'}"
    try:
        assert sandbox_import.roots(protected=(sandbox_dir,)) == []
    finally:
        del os.environ[sandbox_import.ROOTS_ENV]
    assert "корень диска" in caplog.text and "пересекается с папкой SAM" in caplog.text


def test_file_swapped_after_the_plan_is_not_copied(pc, monkeypatch):
    root, jobs = pc
    real_read = sandbox_import.read_source

    def swapped(planned):
        # тот же размер, другой файл на диске: новый создаётся, пока старый жив, - номер не переиспользуется
        other = planned.source.with_name("other.tmp")
        other.write_bytes(b"print('hi')\n")
        os.replace(other, planned.source)
        return real_read(planned)
    monkeypatch.setattr(sandbox_import, "read_source", swapped)
    out = _run(jobs, {"import": [{"from": "project/main.py"}]})
    assert "подменили после проверки" in out.text and str(root) not in out.text


@pytest.mark.parametrize("items", [
    [{"from": "project/main.py", "to": "a"}, {"from": "project/data/blob.bin", "to": "a"}],
    [{"from": "project/main.py", "to": "a"}, {"from": "project/data/blob.bin", "to": "a/b"}],
], ids=["same_place", "file_over_folder"])
def test_destinations_inside_the_plan_do_not_clash(pc, items):
    root, jobs = pc
    out = _run(jobs, {"import": items})
    assert "разведите to" in out.text and not (jobs / JOB / "a").exists()


def test_answers_name_paths_from_the_root(pc):
    root, jobs = pc
    out = _run(jobs, {"import": [{"from": str(root / "missing.txt")}]})
    assert "#2:missing.txt нет на ПК" in out.text and str(root) not in out.text
    out = _run(jobs, {"import": [{"from": str(root.parent / "outside.txt")}]})
    assert str(root.parent) not in out.text


@pytest.mark.integration
def test_live_imported_bytes_reach_the_container(pc):
    """Копия видна коду в контейнере байт в байт: sha256 в контейнере совпадает с файлом ПК."""
    import hashlib
    import shutil
    import subprocess
    try:
        ready = bool(shutil.which("docker")) and subprocess.run(
            ["docker", "image", "inspect", sandbox.DEFAULT_IMAGE], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ready = False
    if not ready:
        pytest.skip(f"Docker не запущен или нет образа {sandbox.DEFAULT_IMAGE}")
    root, jobs = pc
    out = sandbox.run({"import": [{"from": str(root / "project"), "to": "src"}],
                       "command": "sha256sum src/data/blob.bin && ls -A src"}, root=jobs, limits=LIMITS)
    assert out.code == 0 and hashlib.sha256(BINARY).hexdigest() in out.text, out.text
    assert ".env" not in out.text and ".git" not in out.text


def test_tool_offers_import():
    from gateway import agent
    assert "import" in {p["name"] for p in agent.SandboxRunTool.parameters}
