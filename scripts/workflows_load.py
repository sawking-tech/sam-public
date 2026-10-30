"""Мок-тест планировщика workflow без LLM (SAM-126): тысячи workflow, живая Mongo, шаг-заглушка вместо модели.

Сравнивает планировщик из рабочего дерева («new») с планировщиком до ADR-020 п. 7-10 («old», коммит OLD_COMMIT):
один генератор смеси, одна Mongo в отдельной базе, одни часы-заглушка с шагом тика. Меряет время tick() по стене,
обращения к хранилищу за тик, запуски и пик памяти Python (tracemalloc). База удаляется после каждого прогона.

Запуск:
  python scripts/workflows_load.py [--sizes 10000,50000,100000] [--minutes 15] [--old-sizes 10000] [--old-minutes 5]
                                   [--mongo mongodb://localhost:27017] [--out docs/workflows-load-<дата>.md]
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import random
import statistics
import subprocess
import sys
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway import workflows as new_workflows  # noqa: E402
from gateway.workflow_spec import parse_workflow  # noqa: E402

OLD_COMMIT = "c991def"          # последний коммит до кучи и состояния в памяти (ADR-020, п. 7-10)
DB_NAME = "sam_load_test"       # своя база: стенд не трогается, после прогона удаляется
OLD_TICK_S = 30.0               # период тика до ADR-020 п. 7-10
START = datetime(2026, 10, 12, 3, 0, tzinfo=timezone(timedelta(hours=6)))
SEED = 12
MS = 1000
PERCENTILE_99 = 0.99
# Смесь workflow: доля, расписание, ворота. Частые и выровненные cron - худший случай для тика
MIX = (
    (0.40, {"cron": "*/5 * * * *"}, None),
    (0.25, {"cron": "0 * * * *"}, None),
    (0.15, {"every": "1m"}, {"fn": "quiet"}),
    (0.10, {"every": "15m"}, {"command": "check"}),
    (0.10, {"cron": "*/15 * * * *"}, {"fn": "quiet"}),
)
COMMAND_GATE_S = 0.002          # ворота-команда: заглушка процесса, пустой вывод - модель не зовётся
LAG_PROBE_S = 0.001             # шаг пробы лага цикла: так часто просыпался бы голосовой ход


def make_workflows(n: int, seed: int = SEED, importance: float = 1.0) -> list:
    """n workflow смеси MIX через тот же разбор, что у файлов workflows/*.yml; importance - у шага модели."""
    rng = random.Random(seed)
    flows = []
    for i in range(n):
        roll, acc = rng.random(), 0.0
        for share, schedule, gate in MIX:
            acc += share
            if roll <= acc:
                break
        data = {"name": f"load-{i}", "on": {"schedule": schedule}, "session": "voice",
                "steps": [{"name": "say", "main_model": "Скажи коротко, что проверка прошла.",
                           "importance": importance}]}
        if gate:
            data["gate"] = gate
        flows.append(parse_workflow(data, f"load-{i}.yml"))
    return flows


class CountingStore:
    """Обёртка хранилища: сколько обращений к Mongo за тик, по методам."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: dict[str, int] = {}
        self.seconds = 0.0

    def __getattr__(self, name: str):
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        def counted(*args, **kwargs):
            self.calls[name] = self.calls.get(name, 0) + 1
            began = time.perf_counter()
            try:
                return target(*args, **kwargs)
            finally:
                self.seconds += time.perf_counter() - began
        return counted

    def take(self) -> tuple[int, float]:
        """Обращений и мс в хранилище с прошлого take."""
        total, ms = sum(self.calls.values()), self.seconds * MS
        self.calls, self.seconds = {}, 0.0
        return total, ms


def load_old_module():
    """Планировщик OLD_COMMIT модулем пакета gateway: относительные импорты те же."""
    source = subprocess.run(["git", "show", f"{OLD_COMMIT}:gateway/workflows.py"], cwd=ROOT, check=True,
                            capture_output=True, text=True, encoding="utf-8").stdout
    path = ROOT / "data" / "load_test" / "workflows_old.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("gateway._workflows_old", path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "gateway"
    sys.modules[spec.name] = module   # dataclass ищет свой модуль по имени
    spec.loader.exec_module(module)
    return module


async def run_case(module, n: int, minutes: int, mongo_uri: str, jitter: bool, tick_s: float) -> dict:
    tick = timedelta(seconds=tick_s)
    from pymongo import MongoClient
    client = MongoClient(mongo_uri)
    client.drop_database(DB_NAME)
    store = CountingStore(module.MongoStore(client[DB_NAME]))
    clock = [START]
    fired_total = [0]

    async def step(st, src, text, session):
        fired_total[0] += 1
        return module.StepOutcome("stub")

    async def command_gate(command: str) -> str:
        await asyncio.sleep(COMMAND_GATE_S)
        return ""

    flows = make_workflows(n)
    kwargs = {"jitter_s": new_workflows.JITTER_S} if jitter else {}
    scheduler = module.Scheduler(flows, store, {"quiet": lambda ctx: ""}, step, lambda *a: None,
                                 now=lambda: clock[0], command_gate=command_gate, log=lambda m: None, **kwargs)
    tracemalloc.start()
    ticks_ms, ops, fired, mongo_ms, lags = [], [], [], [], []
    lag = [0.0]

    async def probe():
        """Самое долгое опоздание пробуждения за тик: столько ждал бы голосовой ход, пока тик держит цикл."""
        while True:
            began = time.perf_counter()
            await asyncio.sleep(LAG_PROBE_S)
            lag[0] = max(lag[0], time.perf_counter() - began - LAG_PROBE_S)
    prober = asyncio.ensure_future(probe())
    try:
        for _ in range(int(minutes * 60 / tick_s)):
            clock[0] += tick
            lag[0] = 0.0
            began = time.perf_counter()
            names = await scheduler.tick()
            lags.append(lag[0] * MS)
            ticks_ms.append((time.perf_counter() - began) * MS)
            count, store_ms = store.take()
            ops.append(count)
            mongo_ms.append(store_ms)
            fired.append(len(names))
            await asyncio.gather(*list(scheduler._running.values()), return_exceptions=True)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        prober.cancel()
        tracemalloc.stop()
        client.drop_database(DB_NAME)
    steady = ticks_ms[1:] or ticks_ms
    loop = [t - m for t, m in zip(ticks_ms, mongo_ms)]   # тик минус ожидание Mongo в потоке: время, когда цикл занят
    return {"n": n, "first_ms": ticks_ms[0], "p50": statistics.median(steady),
            "p99": nearest_rank(steady, PERCENTILE_99),
            "max": max(steady), "ops_first": ops[0], "ops_tick": statistics.mean(ops[1:] or ops),
            "mongo_ms": statistics.median(mongo_ms[1:] or mongo_ms), "fired": sum(fired),
            "loop_p50": statistics.median(loop[1:] or loop), "loop_p99": nearest_rank(loop[1:] or loop, PERCENTILE_99),
            "lag_p99": nearest_rank(lags[1:] or lags, PERCENTILE_99), "steps": fired_total[0], "peak_mb": peak / 2 ** 20, "ticks": len(ticks_ms)}


def nearest_rank(values: list[float], q: float) -> float:
    """Квантиль ближайшим рангом: значение из замеров, не экстраполяция при малом числе тиков."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * len(ordered)) - 1))]


def row(kind: str, r: dict) -> str:
    return (f"| {kind} | {r['n']:,} | {r['ticks']} | {r['first_ms']:,.0f} | {r['p50']:,.1f} | {r['p99']:,.1f} | "
            f"{r['max']:,.1f} | {r['ops_first']:,} | {r['ops_tick']:,.1f} | {r['mongo_ms']:,.1f} | {r['loop_p50']:,.1f} | {r['loop_p99']:,.1f} | {r['lag_p99']:,.1f} | {r['fired']:,} | "
            f"{r['peak_mb']:,.0f} |"
            ).replace(",", " ")


HEADER = ("| Планировщик | Workflow | Тиков | Первый тик, мс | Тик p50, мс | Тик p99, мс | Тик max, мс | "
          "Mongo на первом тике | Mongo за тик | Mongo мс за тик | Цикл занят p50, мс | Цикл занят p99, мс | Лаг цикла p99, мс | Запусков | Пик памяти, МБ |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")


async def main_async(args) -> str:
    rows = []
    old = load_old_module() if args.old_sizes else None
    for n in args.old_sizes:
        print(f"[load] old {n}...", flush=True)
        rows.append(row(f"до {OLD_COMMIT} (тик {OLD_TICK_S:g} с)",
                        await run_case(old, n, args.old_minutes, args.mongo, jitter=False, tick_s=OLD_TICK_S)))
        print(rows[-1], flush=True)
    for n in args.sizes:
        print(f"[load] new {n}...", flush=True)
        rows.append(row(f"после (тик {args.tick_s:g} с)",
                        await run_case(new_workflows, n, args.minutes, args.mongo, jitter=True, tick_s=args.tick_s)))
        print(rows[-1], flush=True)
    return HEADER + "\n" + "\n".join(rows)


def _sizes(raw: str) -> list[int]:
    return [int(s) for s in raw.split(",") if s.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sizes", type=_sizes, default=_sizes("10000,50000,100000"))
    ap.add_argument("--minutes", type=int, default=15, help="минут на часах-заглушке для нового планировщика")
    ap.add_argument("--old-sizes", type=_sizes, default=_sizes("10000"))
    ap.add_argument("--old-minutes", type=int, default=5, help="старый тик ходит в Mongo на каждый workflow - короче")
    ap.add_argument("--mongo", default="mongodb://localhost:27017")
    ap.add_argument("--tick-s", type=float, default=new_workflows.TICK_S, help="шаг часов = период тика планировщика")
    ap.add_argument("--out", type=Path, default=None, help="куда дописать таблицу (markdown)")
    args = ap.parse_args()
    table = asyncio.run(main_async(args))
    print(table)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("a", encoding="utf-8") as f:
            f.write(table + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
