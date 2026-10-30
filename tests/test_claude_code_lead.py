"""Claude Code ведёт разговор SAM по шине (ADR-012, SAM-59): тикер будит ведущего, say пишет его реплики, поставщик
claude_code находит CLI и модель."""
import asyncio
import json

import pytest

from voice.layer import say as say_mod
from voice.layer import ticker as tk
from voice.layer.bus import LEAD_INBOX, REPLY_TO, SAID, SAY_OUT, TICKS, VOICE_IN
from voice.layer.turns import FAST, retracted_rows

AT = "2026-10-10T02:00:00+06:00"


def _append(path, *recs, tail=""):
    with path.open("a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.write(tail)


def _ticks(out):
    """Тики без своего времени (ts тика - время печати)."""
    return [{k: v for k, v in json.loads(line).items() if k != "ts"} for line in out]


def test_ticker_wakes_on_new_lines_with_phrases_and_unheard_sam(tmp_path):
    """Тикер читает только новые законченные строки шины и пропускает мусор; голосовой тик несёт кто, когда и признак
    названного ключа, неуслышанное (cut, dropped, stale, но не dup) подписано SAM и источником; нет файлов - тишина."""
    out = []
    tk.Ticker(tmp_path, out=out.append).poll()
    assert out == []
    voice, said = tmp_path / VOICE_IN, tmp_path / SAID
    _append(voice, {"text": "старая фраза"})
    t = tk.Ticker(tmp_path, out=out.append)
    _append(voice, {"ts": "t1", "text": "новая фраза", "who": "partner"}, {"text": "эхо", "echo": True},
            {"ts": "t2", "text": "удали историю", "who": "owner", "secret": ["command"]}, tail="{не json\n")
    _append(said, {"status": "played", "text": "a"}, {"status": "cut", "text": "b", "from": "observer"},
            {"status": "dup", "text": "c"}, {"status": "stale", "text": "d"}, {"status": "dropped", "text": "e"})
    with voice.open("a", encoding="utf-8") as f:
        f.write('{"text": "без перевода строки"')       # строка ещё пишется - ждёт конца
    t.poll()
    assert _ticks(out) == [
        {"kind": "voice", "text": "новая фраза", "who": "partner", "at": "t1"},
        {"kind": "voice", "text": "удали историю", "who": "owner", "at": "t2", "secret": ["command"]},   # SAM-88
        {"kind": "unheard", "status": "cut", "text": "b", "speaker": "SAM · ведущий", "from": "observer"},
        {"kind": "unheard", "status": "stale", "text": "d", "speaker": "SAM"},
        {"kind": "unheard", "status": "dropped", "text": "e", "speaker": "SAM"},
    ]
    assert (tmp_path / TICKS).exists()
    with voice.open("a", encoding="utf-8") as f:
        f.write("}\n")
    t.poll()
    assert _ticks(out)[-1] == {"kind": "voice", "text": "без перевода строки", "who": None}


@pytest.mark.parametrize("records, ticks", [
    # SAM-101: факты к входу - полем facts в тике chat; опоздавшие - тиком rag со временем входа; пустые - не тикают
    ([{"ts": AT, "text": "где Ирина", "source": "monitor", "facts": ["Ирина - жена Антона"]},
      {"ts": "x", "source": "rag", "at": AT, "phrase": "где Ирина", "session": "voice", "facts": ["Барсик - кот"]},
      {"source": "rag", "at": AT, "facts": []}],
     [{"kind": "chat", "text": "где Ирина", "source": "monitor", "at": AT, "facts": ["Ирина - жена Антона"]},
      {"kind": "rag", "at": AT, "phrase": "где Ирина", "session": "voice", "facts": ["Барсик - кот"]}]),
    # SAM-88: ведущий видит только признак ключа, сам ключ слух и вход замаскировали
    ([{"text": "удали историю", "source": "monitor", "session": "s", "secret": ["command"]}],
     [{"kind": "chat", "text": "удали историю", "source": "monitor", "session": "s", "secret": ["command"]}]),
    ([{"text": "привет", "source": "monitor", "session": "s"}],
     [{"kind": "chat", "text": "привет", "source": "monitor", "session": "s"}]),
    # SAM-174: сообщения очереди push ведущий подтверждает по их id
    ([{"text": "[workflow:disk-watch/check] мало места", "source": "workflow:disk-watch/check",
       "push_ids": ["a1", "b2"], "session": "voice"}],
     [{"kind": "chat", "text": "[workflow:disk-watch/check] мало места", "source": "workflow:disk-watch/check",
       "session": "voice", "push_ids": ["a1", "b2"]}]),
], ids=["facts", "secret", "no-secret", "push-ids"])
def test_inbox_ticks_carry_facts_secret_flag_and_push_ids(tmp_path, records, ticks):
    out = []
    t = tk.Ticker(tmp_path, out=out.append)
    _append(tmp_path / LEAD_INBOX, *records)
    t.poll()
    assert _ticks(out) == ticks


def test_lead_say_is_observer_line_and_fix_retracts_fast(tmp_path):
    rec = say_mod.say(tmp_path, "Привет, бро!")
    line = json.loads((tmp_path / SAY_OUT).read_text(encoding="utf-8").splitlines()[-1])
    assert line["text"] == "Привет, бро!" and line["from"] == say_mod.OBSERVER and "ts" in line
    assert rec == {k: v for k, v in line.items() if k != "ts"}
    (tmp_path / REPLY_TO).write_text("2026-10-09T04:00:00+06:00\n", encoding="utf-8")
    fast = say_mod.say(tmp_path, "Завтра пятница.", who=FAST)
    fix = say_mod.say(tmp_path, "Ой, поправлюсь: суббота.", fix=True)
    assert fast["reply_to"] == "2026-10-09T04:00:00+06:00"
    assert "reply_to" not in fix and fix["fix"] is True
    rows = [("owner", "какой завтра день", False), (fast["from"], fast["text"], False), (fix["from"], fix["text"], True)]
    assert retracted_rows(rows) == {1}


def test_claude_code_provider_finds_cli_and_model_from_env(monkeypatch):
    """Подпроцесс подменён: модель sonnet или из SAM_CLAUDE_LEAD_MODEL, CLI из PATH или SAM_CLAUDE_CLI."""
    from gateway import llm_router
    from gateway.tools import coding_agents
    seen = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b'{"result": "ok"}', b""

    async def _fake_exec(*argv, **kw):
        seen.append(list(argv))
        return _Proc()

    monkeypatch.delenv("SAM_CLAUDE_CLI", raising=False)
    monkeypatch.setattr(coding_agents.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    assert llm_router._claude_cli() == "C:/bin/claude.exe"
    monkeypatch.setenv("SAM_CLAUDE_CLI", "D:/claude.exe")
    assert llm_router._claude_cli() == "D:/claude.exe"

    monkeypatch.setattr(llm_router.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.delenv("SAM_CLAUDE_LEAD_MODEL", raising=False)
    asyncio.run(llm_router.claude_code_call([{"role": "user", "content": "привет"}]))
    monkeypatch.setenv("SAM_CLAUDE_LEAD_MODEL", "haiku")
    asyncio.run(llm_router.claude_code_call([{"role": "user", "content": "привет"}]))
    assert [a[0] for a in seen] == ["D:/claude.exe"] * 2
    assert [a[a.index("--model") + 1] for a in seen] == ["sonnet", "haiku"]


def test_say_with_the_gateway_down_still_speaks_and_tells_what_was_lost(tmp_path, monkeypatch, capsys):
    """stand 23: шлюз лежит - `say` всё равно пишет реплику в шину (её озвучат), а одной строкой говорит, что в монитор и
    историю она не попала. Шлюз - закрытый порт: отказ соединения сразу."""
    import json
    import sys
    monkeypatch.setenv(say_mod.SERVER_ENV, "http://127.0.0.1:1")
    monkeypatch.setattr(sys, "argv", ["say", "--bus", str(tmp_path), "Ужин готов"])
    say_mod.main()

    [line] = (tmp_path / SAY_OUT).read_text(encoding="utf-8").splitlines()
    assert json.loads(line)["text"] == "Ужин готов"
    out = capsys.readouterr().out.strip()
    assert "\n" not in out and out.startswith("в монитор и историю не попало (")
