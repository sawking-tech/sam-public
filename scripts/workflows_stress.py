"""Стресс-тест workflow на локальной модели (SAM-126): планировщик и очередь push с доставкой в LM Studio.

1. Замер модели тем же путём, что у шлюза (gateway.llm_client.make_llm_call): вызов с системным промптом SAM и
   пачкой поводов, первый токен голосового хода в простое.
2. Расчётное N*: workflow, при котором поводы одной сессии за окно ожидания фонового шага заполняют пачку до
   max_items; дальше - лишние пачки и свёртка перегруза. Модель склеенными пачками почти не занята (калибровка:
   вызов 0,2 с), поэтому колено дизайна - пачка, а не время модели; занятость модели - колонкой таблицы.
3. Сквозной прогон на долях N*: настоящий планировщик (часы настоящие, тик 2 с) и очередь push, доставка в модель;
   раз в VOICE_PROBE_S - голосовой вызов с приоритетом голоса (llm_gate), время до первого токена под нагрузкой.

Только локальная модель: стресс облачные лимиты не тратит. Запуск:
  python scripts/workflows_stress.py [--levels 0.5,1,2,10] [--seconds 180] [--lmstudio http://127.0.0.1:1234/v1]
                                     [--model qwen/qwen3-30b-a3b-2507] [--out docs/workflows-stress-<дата>.md]
"""
from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from gateway import llm_client, llm_gate, push_dispatch, push_queue, workflows  # noqa: E402
from gateway.prompts.system import SYSTEM_PROMPT  # noqa: E402
from workflows_load import MIX, make_workflows, nearest_rank  # noqa: E402

GATE_PASS = 0.1          # доля срабатываний ворот с поводом: остальные пустые, модель не зовётся
STEP_IMPORTANCE = 0.2    # фоновый шаг: ждёт попутчиков до (1 - 0.2) * 30 = 24 с
CALIBRATION_CALLS = 3
CALIBRATION_ITEMS = 10   # пачка калибровки: столько поводов в одном входе
VOICE_PROBE_S = 15.0     # проба голоса: так часто человек мог бы заговорить
VOICE_TEXT = "Привет! Как дела?"
ANSWER_TOKENS = 128      # ответ на пачку и на голос: одна-две фразы
REASON = "Проверка нашла изменение, скажи одной фразой."
MINUTE_S = 60.0
MS = 1000
P99 = 0.99


def messages_per_minute_per_workflow(gate_pass: float = GATE_PASS) -> float:
    """Поводов в минуту на один workflow смеси MIX: частота расписания, у ворот - доля с поводом."""
    def per_minute(schedule: dict) -> float:
        if "every" in schedule:
            return 1.0 / float(schedule["every"].rstrip("m"))
        minute = schedule["cron"].split()[0]
        return 1.0 / int(minute[2:]) if minute.startswith("*/") else 1.0 / MINUTE_S
    return sum(share * per_minute(s) * (gate_pass if gate else 1.0) for share, s, gate in MIX)


def n_star(max_items: int, rate: float, hold_s: float) -> int:
    """Workflow, при которых поводы сессии за окно ожидания hold_s заполняют одну пачку до max_items."""
    return int(max_items * MINUTE_S / (rate * hold_s))


async def _call(cfg: dict, user: str, voice: bool = False) -> tuple[float, float | None]:
    """Время вызова и до первого токена (поток); голос - с приоритетом голосового хода."""
    first: list[float] = []
    began = time.perf_counter()

    def on_delta(piece: str) -> None:
        if not first:
            first.append(time.perf_counter() - began)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    priority = llm_gate.PRIORITY_VOICE if voice else llm_gate.PRIORITY_DEFAULT
    with llm_gate.priority_scope(priority):
        await llm_client.make_llm_call(cfg, messages=messages, max_tokens=ANSWER_TOKENS, on_delta=on_delta)
    return time.perf_counter() - began, (first[0] if first else None)


def _batch_text(n: int) -> str:
    return "\n\n".join(f"[workflow:load-{i}/say] {REASON}" for i in range(n))


async def calibrate(cfg: dict) -> dict:
    calls = [await _call(cfg, _batch_text(CALIBRATION_ITEMS)) for _ in range(CALIBRATION_CALLS)]
    voice = [await _call(cfg, VOICE_TEXT, voice=True) for _ in range(CALIBRATION_CALLS)]
    return {"call_s": statistics.median(c[0] for c in calls),
            "voice_ttft_s": statistics.median(v[1] for v in voice if v[1] is not None)}


class Recorder:
    """Что дошло до модели: пачки, пункты, время вызовов, сбои."""

    def __init__(self) -> None:
        self.batches, self.items, self.busy_s, self.errors = 0, 0, 0.0, 0

    def deliver(self, cfg: dict):
        async def deliver(batch) -> str:
            began = time.perf_counter()
            try:
                await _call(cfg, batch.numbered_text())
                # пропускная способность: модель подтверждает все пункты; соблюдение формата - отдельная проверка
                return "ok\n" + push_queue.ACK_HEAD + " " + ", ".join(map(str, range(1, len(batch.items()) + 1)))
            except Exception:
                self.errors += 1
                raise
            finally:
                self.batches += 1
                self.items += len(batch.messages) + sum(n for m in batch.messages for _, n in m.folded) - sum(
                    1 for m in batch.messages if m.folded)
                self.busy_s += time.perf_counter() - began
        return deliver


async def run_level(cfg: dict, n: int, seconds: float, seed: int) -> dict:
    rng = random.Random(seed)
    rec = Recorder()
    queue = push_queue.from_env(parallel=cfg["parallel"])
    dispatcher = push_dispatch.PushDispatcher(queue, rec.deliver(cfg), lambda *a: None, log=lambda m: None)
    pushed, max_pending = [0], [0]

    async def step(st, src, text, session):
        pushed[0] += 1
        reply = await dispatcher.push(src, text, session, st.importance, st.delivery)
        max_pending[0] = max(max_pending[0], queue.pending_count())
        return workflows.StepOutcome("push", reply)

    def gate(ctx) -> str:
        return REASON if rng.random() < GATE_PASS else ""

    flows = make_workflows(n, seed, importance=STEP_IMPORTANCE)
    scheduler = workflows.Scheduler(flows, workflows.MemoryStore(), {"quiet": gate}, step, lambda *a: None,
                                    command_gate=lambda c: _command_gate(rng), log=lambda m: None,
                                    jitter_s=workflows.JITTER_S)
    tasks = [asyncio.ensure_future(scheduler.run(workflows.TICK_S)), asyncio.ensure_future(dispatcher.run())]
    voice_ttft: list[float] = []
    began = time.perf_counter()
    try:
        while time.perf_counter() - began < seconds:
            await asyncio.sleep(VOICE_PROBE_S)
            _, ttft = await _call(cfg, VOICE_TEXT, voice=True)
            if ttft is not None:
                voice_ttft.append(ttft)
    finally:
        for task in tasks:
            task.cancel()
    elapsed = time.perf_counter() - began
    folded = sum(n for msgs in queue._pending.values() for m in msgs for _, n in m.folded)
    return {"n": n, "pushed": pushed[0], "per_min": pushed[0] / elapsed * MINUTE_S, "batches": rec.batches,
            "items": rec.items, "busy": rec.busy_s / elapsed, "max_pending": max_pending[0],
            "left": queue.pending_count(), "folded_left": folded, "errors": rec.errors,
            "voice_p50": statistics.median(voice_ttft) if voice_ttft else None,
            "voice_p99": nearest_rank(voice_ttft, P99) if voice_ttft else None}


async def _command_gate(rng: random.Random) -> str:
    return REASON if rng.random() < GATE_PASS else ""


def _fmt(value, digits=1) -> str:
    return "-" if value is None else f"{value:,.{digits}f}".replace(",", " ")


def row(level: float, r: dict) -> str:
    return (f"| {level:g} N* | {r['n']:,} | {r['pushed']:,} | {_fmt(r['per_min'])} | {r['batches']} | "
            f"{_fmt(r['items'] / r['batches'] if r['batches'] else None)} | {_fmt(r['busy'] * 100, 0)}% | "
            f"{r['max_pending']} | {r['left']} | {r['errors']} | {_fmt(r['voice_p50'], 2)} | {_fmt(r['voice_p99'], 2)} |"
            ).replace(",", " ")


HEADER = ("| Нагрузка | Workflow | Поводов | Поводов в минуту | Пачек | Пунктов в пачке | Модель занята | "
          "Ждущих max | Осталось в очереди | Сбоев | Голос, первый токен p50, с | Голос, первый токен p99, с |\n"
          "|---|---|---|---|---|---|---|---|---|---|---|---|")


async def main_async(args) -> str:
    cfg = {"source": "lmstudio", "model": args.model, "model_server": args.lmstudio, "api_key": "lm-studio",
           "parallel": args.parallel}
    cal = await calibrate(cfg)
    rate = messages_per_minute_per_workflow()
    hold_s = (1 - STEP_IMPORTANCE) * push_queue.DEFAULT_MAX_HOLD_S
    star = n_star(push_queue.DEFAULT_MAX_ITEMS, rate, hold_s)
    head = (f"Калибровка: вызов с пачкой из {CALIBRATION_ITEMS} поводов - {cal['call_s']:.2f} с (медиана из "
            f"{CALIBRATION_CALLS}); первый токен голоса в простое - {cal['voice_ttft_s']:.2f} с. Поводов на workflow - "
            f"{rate:.3f} в минуту, окно ожидания фона {hold_s:.0f} с, пачка до {push_queue.DEFAULT_MAX_ITEMS}. "
            f"N* = {star:,}.".replace(",", " "))
    print(head, flush=True)
    rows = []
    for level in args.levels:
        n = max(1, int(star * level))
        print(f"[stress] {level:g} N* = {n}...", flush=True)
        rows.append(row(level, await run_level(cfg, n, args.seconds, seed=int(level * 100))))
        print(rows[-1], flush=True)
    return head + "\n\n" + HEADER + "\n" + "\n".join(rows)


def _floats(raw: str) -> list[float]:
    return [float(s) for s in raw.split(",") if s.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--levels", type=_floats, default=_floats("0.5,1,2,10"))
    ap.add_argument("--seconds", type=float, default=180.0, help="длительность каждого уровня, реальные секунды")
    ap.add_argument("--lmstudio", default="http://127.0.0.1:1234/v1")
    ap.add_argument("--model", default="qwen/qwen3-30b-a3b-2507")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--parallel", type=int, default=4, help="мест модели: LLM_CONCURRENCY, у LM Studio - PARALLEL")
    args = ap.parse_args()
    text = asyncio.run(main_async(args))
    print(text)
    if args.out:
        with args.out.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
