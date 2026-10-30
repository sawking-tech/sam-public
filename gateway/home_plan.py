"""План квартиры из проекта Sweet Home 3D (.sh3d) в планировку радара (SAM-105).

Источник - папка «Ремонт» с планом квартиры: стены, двери, окна и мебель в сантиметрах по уровням. Здесь файл читается в
словарь планировки: метры, ось z - вниз по плану, как y в проекте. Разбор XML - чистая функция parse_home, архив и файл
читает read_sh3d. Комнат в проекте может не быть (только стены), тогда планировка берёт габариты по стенам.
"""
from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

CM = 100.0                       # сантиметров в метре: единицы Sweet Home 3D
DEFAULT_WALL_THICKNESS_CM = 8.0
DEFAULT_WALL_HEIGHT_CM = 250.0
ITEM_TAGS = ("pieceOfFurniture", "doorOrWindow", "light")
DECOR_WORDS = ("тарелка", "бутылка", "пицца", "сковорода", "камень", "урна", "курица", "мишка", "тапочки", "доска",
               "ведро", "женщина", "мужчина")     # мелочь и фигурки из проекта: на радаре предметами не показываются
MIN_ITEM_AREA_M2 = 0.05          # мельче - подставка или мелочь, не ориентир на сцене


def parse_place(item: str) -> dict:
    """«название=X,Z» в метку места плана {label, x, z}; непонятное - ValueError с подсказкой."""
    label, _, coords = item.partition("=")
    try:
        x, z = (float(v) for v in coords.split(","))
    except ValueError:
        raise ValueError(f"--place {item!r}: нужно название=X,Z в метрах") from None
    if not label.strip():
        raise ValueError(f"--place {item!r}: у места нет названия")
    return {"label": label.strip(), "x": x, "z": z}


def parse_object(item: str, index: int = 0) -> dict:
    """«название=центрX,центрZ,ширина,глубина,высота[,угол в градусах]» в предмет плана, помеченный approx: так добавляется то,
    чего нет в проекте и что известно со слов человека. Угол как в плане: 180 - спинкой к +z (на юг)."""
    title, _, rest = item.partition("=")
    try:
        cx, cz, w, d, h, *angle = (float(v) for v in rest.split(","))
        if len(angle) > 1 or not title.strip():
            raise ValueError
    except ValueError:
        raise ValueError(f"--object {item!r}: нужно название=центрX,центрZ,ширина,глубина,высота[,угол] в метрах и градусах") from None
    return {"id": f"man-{index}", "title": title.strip(), "kind": "furniture", "x": cx - w / 2, "z": cz - d / 2, "w": w, "d": d, "h": h,
            "angle": math.radians(angle[0]) if angle else 0.0, "approx": True}


def levels_of(root: ET.Element) -> list[dict]:
    return [{"id": l.get("id"), "name": l.get("name"), "elevation_cm": float(l.get("elevation") or 0)}
            for l in root.findall("level")]


def parse_home(xml: bytes, level: str | None = None) -> dict:
    """Планировка одного уровня (по имени или id, по умолчанию первый): стены и предметы в метрах."""
    root = ET.fromstring(xml)
    levels = levels_of(root)
    chosen = next((l for l in levels if level in (l["id"], l["name"])), levels[0] if levels else None)
    lid = chosen["id"] if chosen else None
    walls = [{"x1": float(w.get("xStart")) / CM, "z1": float(w.get("yStart")) / CM,
              "x2": float(w.get("xEnd")) / CM, "z2": float(w.get("yEnd")) / CM,
              "t": float(w.get("thickness") or DEFAULT_WALL_THICKNESS_CM) / CM,
              "h": float(w.get("height") or DEFAULT_WALL_HEIGHT_CM) / CM}
             for w in root.findall("wall") if w.get("level") == lid]
    objects = []
    for tag in ITEM_TAGS:
        for f in root.findall(tag):
            if f.get("level") != lid:
                continue
            w, d, h = float(f.get("width")) / CM, float(f.get("depth")) / CM, float(f.get("height")) / CM
            name = (f.get("name") or "").strip()
            if not name or w * d < MIN_ITEM_AREA_M2 or any(word in name.lower() for word in DECOR_WORDS):
                continue
            objects.append({"id": f"{tag[:3]}-{len(objects)}", "title": name,
                            "kind": "opening" if tag == "doorOrWindow" else "furniture",
                            "x": float(f.get("x")) / CM - w / 2, "z": float(f.get("y")) / CM - d / 2, "w": w, "d": d, "h": h,
                            "angle": float(f.get("angle") or 0.0)})   # радианы, как в проекте: поворот вокруг центра
    xs = [v for w in walls for v in (w["x1"], w["x2"])]
    zs = [v for w in walls for v in (w["z1"], w["z2"])]
    if not xs:
        raise ValueError(f"на уровне {level!r} нет стен: проверь имя уровня, есть {[l['name'] for l in levels]}")
    # один общий «дом» по габаритам стен: комнаты в проекте не размечены, их подписывают зонами (Shift на странице)
    # уровни в проекте стоят на разных местах (уровень 3 сдвинут на 14 м): угол квартиры переносится в нуль
    ox, oz = min(xs), min(zs)
    for w in walls:
        w["x1"], w["x2"], w["z1"], w["z2"] = w["x1"] - ox, w["x2"] - ox, w["z1"] - oz, w["z2"] - oz
    for o in objects:
        o["x"], o["z"] = o["x"] - ox, o["z"] - oz
    room = {"id": "home", "title": "квартира", "x": 0.0, "z": 0.0, "w": max(xs) - ox, "d": max(zs) - oz}
    return {"name": f"sh3d:{chosen['name'] if chosen else '-'}", "rooms": [room], "walls": walls, "objects": objects,
            "levels": [l["name"] for l in levels]}


def read_sh3d(path: Path, level: str | None = None) -> dict:
    with zipfile.ZipFile(path) as z:
        if "Home.xml" not in z.namelist():
            raise ValueError(f"в {path.name} нет Home.xml: сохрани проект в Sweet Home 3D версии 5 или новее")
        return parse_home(z.read("Home.xml"), level)


def write_layout(layout: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(layout, ensure_ascii=False, indent=1), encoding="utf-8")
