"""Прогон голосового хода по набору случаев (SAM-81).

  python scripts/eval_voice.py export [--limit N]        случаи из истории Mongo -> data/evals/cases.jsonl
  python scripts/eval_voice.py run [--cases ФАЙЛ]        прогон через run_voice_turn, итог JSON в stdout

Набор из истории лежит вне git (data/evals/, в нём реплики домашних); без --cases и без файла
берётся образец tests/fixtures/voice_eval_cases.py. Нужны Mongo и поставщик LLM, как у gateway.
Ходы прогона не пишутся в историю: у шины отключается база. Токены и этапы хода (stages)
пишутся в _token_usage, как у живого хода.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CASES_FILE = Path(os.getenv("SAM_EVAL_CASES", str(REPO / "data" / "evals" / "cases.jsonl")))
EVAL_SESSION = "eval"
DEFAULT_LIMIT = 200


def export(limit: int) -> None:
    from gateway.agent import _db, _mongo_ok
    from gateway.evals import cases_from_sessions
    if not _mongo_ok:
        sys.exit("MongoDB недоступна: набор собирается из истории sessions")
    cases = []
    for sid in _db.sessions.distinct("session_id"):
        if str(sid).startswith("task:"):
            continue
        cases += cases_from_sessions(_db.sessions.find({"session_id": sid}).sort("timestamp", 1))
    cases = cases[-limit:]
    CASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CASES_FILE.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
    print(f"случаев: {len(cases)} -> {CASES_FILE}")


def load_cases(path: Path | None):
    from gateway.evals import EvalCase
    if path is None and not CASES_FILE.exists():
        from tests.fixtures.voice_eval_cases import VOICE_EVAL_CASES
        return list(VOICE_EVAL_CASES)
    rows = [json.loads(r) for r in (path or CASES_FILE).read_text(encoding="utf-8").splitlines() if r.strip()]
    return [EvalCase(**{**row, "history": tuple(row.get("history", ()))}) for row in rows]


async def run(path: Path | None) -> None:
    from gateway.agent import run_voice_turn
    from gateway.evals import run_cases, summary
    from gateway.event_bus import event_bus
    event_bus.set_db(None)  # прогон не засоряет историю

    async def _turn(case):
        return await run_voice_turn(case.phrase, f"{EVAL_SESSION}:{case.case_id}")
    report = summary(await run_cases(load_cases(path), _turn))
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    r = sub.add_parser("run")
    r.add_argument("--cases", type=Path)
    a = p.parse_args()
    if a.cmd == "export":
        export(a.limit)
    else:
        asyncio.run(run(a.cases))


if __name__ == "__main__":
    main()
