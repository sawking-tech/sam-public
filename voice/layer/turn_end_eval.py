"""Офлайн-замер конца хода моделью (turn_end.py) на записанных фразах шины.

  python -m voice.layer.turn_end_eval score  <папка-шины> [--out turn-scores.jsonl] [--pauses 0.3 0.4 0.5]
  python -m voice.layer.turn_end_eval report <turn-scores.jsonl> [...] [--thresholds 0.5 0.7 0.9]

score: каждую фразу из voice-in.jsonl (поле wav, без эха) проигрывает блоками по 100 мс с той же тишиной,
что hearing (SILENCE_RMS), и на каждой паузе из --pauses спрашивает модель по звуку фразы до этого места.
Пауза, после которой речь в той же фразе продолжилась, - внутренняя: закрытие на ней режет фразу посреди
речи (ложное). Последняя пауза - та, на которой слух закрыл фразу тишиной wait: закрытие на ней - выигрыш.
report: по паузе и порогу - сколько фраз модель закрыла бы раньше и на сколько секунд, сколько фраз она
разрезала бы на внутренней паузе. Отдельно - фразы, за которыми через короткий зазор шла следующая: слух
закрыл их тишиной, а человек, возможно, продолжал мысль; это оценка сверху, по времени записей, без разметки.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from statistics import median

import numpy as np

from .bus import VOICE_IN
from .config import TURN_END_MODEL
from .hearing import BLOCK, SAMPLE_RATE, SILENCE_RMS, block_rms, read_wav
from .turn_end import SmartTurn, TurnEnd

SCORES_FILE = "turn-scores.jsonl"
PAUSES = (0.3, 0.4, 0.5)          # пауза, на которой спрашиваем модель (обоснование: 0,3-0,5 с)
THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9)
BLOCK_SECS = BLOCK / SAMPLE_RATE
# Ответ модели слух видит на следующем блоке: замер 9 окт - 81 мс на вызов с признаками, блок 100 мс
ANSWER_DELAY = BLOCK_SECS
# Следующая фраза началась раньше этого после конца текущей - возможно, человек продолжал мысль
CONTINUED_GAP = 1.0
LIST_LIMIT = 15


# --- логика без внешнего мира ---------------------------------------------------------------

def pause_points(levels: list[float], pause: float, silence_rms: float = SILENCE_RMS) -> list[tuple[int, bool]]:
    """Блоки, на которых тишина дошла до паузы, как её считает hearing._grow_phrase, и последняя ли это
    пауза (после неё во фразе нет речи). Счёт - с первого громкого блока: до него в файле предзапись
    (PREROLL_SECS), её hearing в паузу не считает - фраза начинается громким блоком."""
    rule, quiet, points = TurnEnd(pause=pause), 0.0, []
    loud = [rms >= silence_rms for rms in levels]
    start = next((i for i, is_loud in enumerate(loud) if is_loud), len(loud))
    for i, is_loud in enumerate(loud[start:], start):
        was, quiet = quiet, 0.0 if is_loud else quiet + BLOCK_SECS
        if rule.should_ask(was, quiet):
            points.append((i, not any(loud[i + 1:])))
    return points


def verdict(row: dict, pause: float, threshold: float) -> dict:
    """Что сделала бы модель с фразой: закрыла раньше (на сколько), разрезала на внутренней паузе или ждала."""
    rule = TurnEnd(pause=pause, threshold=threshold)
    events = [e for e in row["events"] if e["pause"] == pause]
    cut = next((e for e in events if not e["final"] and rule.closes(e["p"])), None)
    final = next((e for e in events if e["final"]), None)
    early = final is not None and cut is None and rule.closes(final["p"])
    return {"cut": cut, "early": early,
            "gain": round(row["wait"] - pause - ANSWER_DELAY, 2) if early else 0.0}


def continued(rows: list[dict], gap: float = CONTINUED_GAP) -> set[str]:
    """Фразы, за которыми следующая началась меньше чем через gap секунд после их конца.
    Конец фразы - ts записи минус распознавание, начало следующей - её конец минус длина."""
    def end(r: dict) -> float:
        return datetime.fromisoformat(r["ts"]).timestamp() - r.get("stt", 0.0)

    out = set()
    for a, b in zip(rows, rows[1:]):
        if end(b) - b["secs"] - end(a) < gap:
            out.add(a["wav"])
    return out


def summary(rows: list[dict], pause: float, threshold: float, cont: set[str]) -> dict:
    vs = [(r, verdict(r, pause, threshold)) for r in rows]
    gains = [v["gain"] for _, v in vs if v["early"]]
    internal = sum(sum(1 for e in r["events"] if e["pause"] == pause and not e["final"]) for r in rows)
    return {"phrases": len(rows), "early": len(gains), "gain_median": round(median(gains), 2) if gains else 0.0,
            "gain_total": round(sum(gains), 1), "cut": sum(1 for _, v in vs if v["cut"]),
            "internal_pauses": internal,
            "cut_pauses": sum(1 for r in rows for e in r["events"]
                              if e["pause"] == pause and not e["final"] and e["p"] >= threshold),
            "early_continued": sum(1 for r, v in vs if v["early"] and r["wav"] in cont)}


# --- внешний мир ----------------------------------------------------------------------------

def phrases(session: Path) -> list[dict]:
    """Записи voice-in.jsonl с файлом фразы, без эха моей озвучки."""
    rows = []
    for line in (session / VOICE_IN).read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("wav") and not r.get("echo") and (session / r["wav"]).exists():
            rows.append(r)
    return rows


def score(args: argparse.Namespace) -> None:
    session = Path(args.session)
    model = SmartTurn(Path(args.model))
    out = Path(args.out) if args.out else session / SCORES_FILE
    rows = phrases(session)
    with out.open("w", encoding="utf-8") as f:
        for n, r in enumerate(rows, 1):
            audio = read_wav(str(session / r["wav"]))
            blocks = [audio[i:i + BLOCK] for i in range(0, len(audio), BLOCK)]
            levels = [block_rms(b) for b in blocks]
            events = []
            for pause in args.pauses:
                for i, final in pause_points(levels, pause):
                    p = model.probability(audio[:(i + 1) * BLOCK])
                    events.append({"pause": pause, "at": round((i + 1) * BLOCK_SECS, 1), "final": final,
                                   "p": round(p, 3)})
            f.write(json.dumps({"wav": r["wav"], "ts": r["ts"], "text": r.get("text", ""), "who": r.get("who"),
                                "secs": r.get("secs"), "wait": r.get("wait"), "stt": r.get("stt", 0.0),
                                "events": events}, ensure_ascii=False) + "\n")
            if n % 100 == 0:
                print(f"{n}/{len(rows)}", flush=True)
    print(f"{len(rows)} фраз → {out}")


def report(args: argparse.Namespace) -> None:
    rows = []
    for path in args.scores:
        rows += [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
    rows = [r for r in rows if r.get("wait") is not None]
    cont = set()
    for path in args.scores:   # зазоры - внутри одной записи
        part = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
        cont |= continued([r for r in part if r.get("wait") is not None])
    no_final = sum(1 for r in rows if not any(e["final"] for e in r["events"]))
    print(f"фраз {len(rows)}; последней паузы нет в проигрывании у {no_final} (фраза до MAX_PHRASE_SECS "
          f"или тишина не дошла до паузы); за {len(cont)} фразами следующая шла через < {CONTINUED_GAP} с")
    print(f"wait по журналу: медиана {median(r['wait'] for r in rows)}, сумма {sum(r['wait'] for r in rows):.0f} с")
    for pause in args.pauses:
        print(f"\nпауза {pause} с:")
        for t in args.thresholds:
            s = summary(rows, pause, t, cont)
            print(f"  порог {t:.2f}: раньше {s['early']}/{s['phrases']} ({s['early'] / s['phrases']:.0%}), "
                  f"выигрыш медиана {s['gain_median']} с, всего {s['gain_total']} с; "
                  f"разрезано фраз {s['cut']} ({s['cut'] / s['phrases']:.1%}), "
                  f"внутренних пауз {s['cut_pauses']}/{s['internal_pauses']}; "
                  f"раньше среди «продолжал» {s['early_continued']}/{len(cont)}")
    pause, t = args.show
    print(f"\nразрезанные на паузе {pause} с при пороге {t}:")
    shown = 0
    for r in rows:
        v = verdict(r, pause, t)
        if v["cut"] and shown < args.list:
            shown += 1
            print(f"  {r['wav']} {r['secs']} с, срез на {v['cut']['at']} с p={v['cut']['p']}: {r['text'][:90]}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("session")
    s.add_argument("--out")
    s.add_argument("--model", default=str(TURN_END_MODEL))
    s.add_argument("--pauses", type=float, nargs="*", default=list(PAUSES))
    r = sub.add_parser("report")
    r.add_argument("scores", nargs="+")
    r.add_argument("--pauses", type=float, nargs="*", default=list(PAUSES))
    r.add_argument("--thresholds", type=float, nargs="*", default=list(THRESHOLDS))
    r.add_argument("--show", type=float, nargs=2, default=[0.4, 0.5], metavar=("PAUSE", "THRESHOLD"))
    r.add_argument("--list", type=int, default=LIST_LIMIT)
    args = ap.parse_args()
    {"score": score, "report": report}[args.cmd](args)


if __name__ == "__main__":
    main()
