"""План квартиры из .sh3d в файл планировки радара (SAM-105).

  python scripts/sh3d_to_layout.py "C:/Users/.../План.sh3d" --level "Уровень 0" --out data/home_layout.json

Потом в .env: SAM_HOME_LAYOUT=data/home_layout.json и рестарт шлюза.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.home_plan import parse_object, parse_place, read_sh3d, write_layout  # noqa: E402

DEFAULT_OUT = "data/home_layout.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sh3d", type=Path)
    ap.add_argument("--level", default=None, help="имя уровня в проекте; по умолчанию первый")
    ap.add_argument("--out", type=Path, default=Path(DEFAULT_OUT))
    ap.add_argument("--router", default=None, help="где роутер в метрах планировки: X,Z,H (например 6.6,4.0,1.6)")
    ap.add_argument("--object", action="append", default=[],
                    help="предмет, которого нет в проекте, со слов человека: название=центрX,центрZ,ширина,глубина,высота[,угол]; рисуется пунктиром")
    ap.add_argument("--place", action="append", default=[],
                    help="подпись места на плане: название=X,Z в метрах (кресло=10.4,1.0); можно несколько раз")
    ap.add_argument("--room", action="append", default=[],
                    help="комната: имя=X,Z,W,D в метрах (кабинет=7.34,3.19,4.57,2.61); если заданы, заменяют общую комнату «квартира»")
    args = ap.parse_args()
    try:
        layout = read_sh3d(args.sh3d, args.level)
    except (OSError, ValueError) as e:
        print(f"план не прочитан: {e}", file=sys.stderr)
        return 1
    if args.router:
        try:
            x, z, h = (float(v) for v in args.router.split(","))
        except ValueError:
            print("--router задаётся тремя числами через запятую: X,Z,H в метрах", file=sys.stderr)
            return 1
        layout["router"] = {"x": x, "z": z, "h": h}
    try:
        places = [parse_place(item) for item in args.place]
        layout["objects"] += [parse_object(item, i) for i, item in enumerate(args.object)]
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    if places:
        layout["places"] = places
    rooms = []
    for item in args.room:
        name, _, coords = item.partition("=")
        try:
            x, z, w, d = (float(v) for v in coords.split(","))
        except ValueError:
            print(f"--room {item!r}: нужно имя=X,Z,W,D в метрах", file=sys.stderr)
            return 1
        rooms.append({"id": f"room-{len(rooms) + 1}", "title": name.strip(), "x": x, "z": z, "w": w, "d": d})
    if rooms:
        layout["rooms"] = rooms
    write_layout(layout, args.out)
    print(f"{args.out}: {layout['name']}, стен {len(layout['walls'])}, предметов {len(layout['objects'])}, "
          f"уровни проекта {layout['levels']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
