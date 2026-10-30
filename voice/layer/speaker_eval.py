"""Офлайн-замер опознания говорящего на записанных фразах шины.


  python -m voice.layer.speaker_eval score  <папка-шины> [--out scores.jsonl]
  python -m voice.layer.speaker_eval report <scores.jsonl> [--labels labels.json] [--default owner]
                                      [--alias owner-loud=owner] [--min-score 0.4] [--list 20]
                                      [--sweep 0.3 0.4 0.5] [--long-secs 6]

score: каждую фразу из voice-in.jsonl (поле wav, без эха) прогоняет через ECAPA целиком, как её
видит слух (speakers.identify, запись в журнал), - балл по каждому профилю.
report: кто на самом деле говорил - из файла меток {"phrases/HHMMSS.wav": "partner"}, остальные
фразы - человек по умолчанию; печатает распределение баллов, границу между людьми и ошибки
порогов. Журнальное поле who в разметку не идёт: это ответ того самого опознания, которое меряем.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

from . import speakers
from .hearing import read_wav

from .bus import VOICE_IN

SCORES_FILE = "speaker-scores.jsonl"
# Чья запись по умолчанию: не размеченная фраза - этого профиля; псевдонимы «профиль=профиль» через запятую. Профили
# домашних - в .env, в коде их нет
DEFAULT_PERSON = os.getenv("SAM_EVAL_DEFAULT_PERSON", "")
DEFAULT_ALIASES = tuple(a for a in os.getenv("SAM_EVAL_ALIASES", "").split(",") if "=" in a)
PERCENTILES = (0, 5, 25, 50, 75, 95, 100)
LIST_LIMIT = 20
SWEEP = (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60)   # пороги, цену которых печатает отчёт
LONG_SECS = 6.0                 # фраза от стольких секунд - не обрывок: на ней «неизвестно» обиднее
ECHO_PROFILE = speakers.CLAUDE  # эхо моей озвучки - не человек, в разметке не участвует


# --- логика без внешнего мира ---------------------------------------------------------------

def cosine_all(emb: np.ndarray, known: dict[str, np.ndarray]) -> dict[str, float]:
    return {n: round(float(emb @ v), 4) for n, v in known.items()}


def best(scores: dict[str, float], aliases: dict[str, str]) -> tuple[str, float]:
    """Победитель как в speakers.identify, имя через псевдонимы (owner-loud → owner)."""
    name, score = max(scores.items(), key=lambda x: x[1])
    return aliases.get(name, name), score


def person_score(scores: dict[str, float], person: str, aliases: dict[str, str]) -> float:
    """Лучший балл среди профилей одного человека."""
    own = [s for n, s in scores.items() if aliases.get(n, n) == person]
    return max(own) if own else 0.0


def percentiles(values: list[float]) -> str:
    if not values:
        return "-"
    return " ".join(f"p{p}={np.percentile(values, p):.2f}" for p in PERCENTILES)


def threshold_errors(rows: list[dict], person: str, aliases: dict[str, str],
                     min_score: float) -> dict[str, list[dict]]:
    """Ошибки порога на фразах одного человека.

    unknown - ни один профиль не дотянул до min_score (фраза уйдёт как «неизвестно»);
    wrong - победил чужой профиль на min_score и выше.
    """
    out: dict[str, list[dict]] = {"unknown": [], "wrong": []}
    for r in rows:
        if r["person"] != person:
            continue
        who, score = best(r["full"], aliases)
        if score < min_score:
            out["unknown"].append(r)
        elif who != person:
            out["wrong"].append(r)
    return out


def sweep_row(rows: list[dict], person: str, aliases: dict[str, str],
              t: float, long_secs: float) -> dict[str, int]:
    """Цена порога t: чего лишается человек и что пропускает от чужих.

    unknown / unknown_long - его фразы ниже t (все / не короче long_secs);
    wrong - его фразы, где победил чужой профиль на t и выше;
    taken - чужие фразы, названные им на t и выше.
    """
    def top(r: dict) -> tuple[str, float]:
        return best(r["full"], aliases)

    mine = [r for r in rows if r["person"] == person]
    others = [r for r in rows if r["person"] != person]
    return {
        "unknown": sum(top(r)[1] < t for r in mine),
        "unknown_long": sum(top(r)[1] < t for r in mine if (r["secs"] or 0) >= long_secs),
        "wrong": sum(top(r)[0] != person and top(r)[1] >= t for r in mine),
        "taken": sum(top(r)[0] == person and top(r)[1] >= t for r in others),
    }


def label(rows: list[dict], labels: dict[str, str], default: str) -> list[dict]:
    return [{**r, "person": labels.get(r["wav"], default)} for r in rows]


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
    known = speakers.profiles()
    out = Path(args.out) if args.out else session / SCORES_FILE
    rows = phrases(session)
    with out.open("w", encoding="utf-8") as f:
        for i, r in enumerate(rows, 1):
            audio = read_wav(str(session / r["wav"]))
            f.write(json.dumps({"wav": r["wav"], "ts": r["ts"], "text": r.get("text", ""),
                                "secs": r.get("secs"), "logged": r.get("who"),
                                "full": cosine_all(speakers.embed(audio), known)},
                               ensure_ascii=False) + "\n")
            if i % 100 == 0:
                print(f"{i}/{len(rows)}", flush=True)
    print(f"{len(rows)} фраз → {out}")


def show(r: dict, aliases: dict[str, str]) -> str:
    who, s = best(r["full"], aliases)
    tops = " ".join(f"{n}={v:.2f}" for n, v in sorted(r["full"].items(), key=lambda x: -x[1]))
    return f"  {r['wav']} {r['ts'][11:16]} [{r['person']}] {who} {s:.2f} | {tops} | {r['text'][:70]}"


def report(args: argparse.Namespace) -> None:
    aliases = dict(a.split("=", 1) for a in args.alias)
    labels = json.loads(Path(args.labels).read_text(encoding="utf-8")) if args.labels else {}
    rows = [json.loads(line) for line in Path(args.scores).read_text(encoding="utf-8").splitlines()]
    rows = label(rows, labels, args.default)
    people = sorted({r["person"] for r in rows} - {ECHO_PROFILE})
    names = sorted({aliases.get(n, n) for n in rows[0]["full"]})

    print(f"фраз {len(rows)}; по разметке: "
          + ", ".join(f"{p} {sum(r['person'] == p for r in rows)}" for p in people))
    print("\nбалл фразы целиком по профилю (строка - кто говорил):")
    for p in people:
        mine = [r for r in rows if r["person"] == p]
        for n in names:
            print(f"  {p:6} → {n:6} {percentiles([person_score(r['full'], n, aliases) for r in mine])}")
    print("\nпобедитель целиком (кто говорил → кого назвало):")
    for p in people:
        mine = [r for r in rows if r["person"] == p]
        wins: dict[str, int] = {}
        for r in mine:
            who = best(r["full"], aliases)[0]
            wins[who] = wins.get(who, 0) + 1
        print(f"  {p:6} → " + ", ".join(f"{n} {c}" for n, c in sorted(wins.items(), key=lambda x: -x[1])))

    for p in people:
        err = threshold_errors(rows, p, aliases, args.min_score)
        mine = sum(r["person"] == p for r in rows)
        print(f"\n{p}: порог {args.min_score} из {mine}: неизвестно {len(err['unknown'])}, "
              f"чужой {len(err['wrong'])}")
        for r in err["wrong"][:args.list]:
            print(" wrong" + show(r, aliases))

    print(f"\nцена порога для {args.default} (длинная фраза - от {args.long_secs} с):")
    for t in args.sweep:
        c = sweep_row(rows, args.default, aliases, t, args.long_secs)
        print(f"  {t:.2f}: " + ", ".join(f"{k} {v}" for k, v in c.items()))

    print(f"\nфразы, где победил не человек по умолчанию ({args.default}):")
    for r in [r for r in rows if best(r["full"], aliases)[0] not in (args.default, ECHO_PROFILE)]:
        print(show(r, aliases))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("session")
    s.add_argument("--out")
    r = sub.add_parser("report")
    r.add_argument("scores")
    r.add_argument("--labels")
    r.add_argument("--default", default=DEFAULT_PERSON)
    r.add_argument("--alias", nargs="*", default=list(DEFAULT_ALIASES))
    r.add_argument("--min-score", type=float, default=speakers.MIN_SCORE)
    r.add_argument("--list", type=int, default=LIST_LIMIT)
    r.add_argument("--sweep", type=float, nargs="*", default=list(SWEEP))
    r.add_argument("--long-secs", type=float, default=LONG_SECS)
    args = ap.parse_args()
    {"score": score, "report": report}[args.cmd](args)


if __name__ == "__main__":
    main()
