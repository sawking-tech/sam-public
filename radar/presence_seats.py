"""Места на опорах (SAM-105): сколько людей вмещают диван и кровать, лишний садится на угол.

Одна формула вместимости: на опору
помещается столько людей, сколько раз по их ширине укладывается её размер. Люди, чья точка легла на опору, делят её:
первые встают в слоты по вместимости (поза опоры), остальные садятся на углы. Остальных разводит presence_separate.
Чистая функция: планировка и строки приходят аргументами.
"""
from __future__ import annotations

import math
from typing import Callable

SEAT_W_M = 0.6          # ширина места на диване
BODY_W_M = 0.7          # ширина лежащего человека
CORNER_INSET_M = 0.2    # сидящий на углу сдвинут внутрь от края
EDGE_MARGIN_M = 0.1     # точка чуть за краем опоры всё ещё на ней: оценка места неточна
SOFA_LIE_PERIOD_S = 300.0   # раз в столько секунд у одиноко сидящего на диване выпадает шанс прилечь
SOFA_LIE_EVERY = 4          # из стольких периодов один человек лежит на диване
CHAIR_WORDS = ("стул", "кресл")     # стул вмещает одного: первый по очереди жильцов сидит, остальные стоят рядом
CHAIR_REACH_M = 0.35         # кто не дальше этого от края сиденья, тот претендует на него
TOILET_WORD = "унитаз"
TOILET_REACH_M = 0.9        # кто ближе этого к унитазу в туалете, тот им пользуется (в туалете один: вместимость области)
TOILET_STAND_M = 0.45       # стоящий перед чашей держится на этом расстоянии от центра, лицом к стене
TOILET_SIT_PERIOD_S = 150.0
TOILET_SIT_EVERY = 3        # из стольких периодов один тот, кто обычно стоит, сидит
TUB_WORD = "ванн"           # в ванну иногда залезает один и лежит; вдвоём в ванной стоят по слотам области
TUB_PERIOD_S = 240.0
TUB_EVERY = 3
TUB_REACH_M = 0.8           # в комнате с ванной считается, кто рядом с ней: «один в ванной»
HUG_CLOSER_M = 0.5          # обнимающий придвигается к партнёру на столько ближе обычной полосы (0,8 м на двухместной кровати -> 0,3 м)
HUG_MIN_GAP_M = 0.25        # но не ближе этого: тела не слипаются
HUG_PERIOD_S = 120.0        # раз в столько секунд решается, обниматься ли двоим из `hugs`
HUG_APART_EVERY = 3         # из стольких периодов в одном лежат порознь, в остальных обнимаются
SUPPORTS = (("диван", "sit", SEAT_W_M), ("кроват", "lie", BODY_W_M))     # (слово в названии, поза, ширина на человека)


def _support_kind(obj: dict) -> tuple[str, float] | None:
    title = (obj.get("title") or "").lower()
    return next(((pose, width) for word, pose, width in SUPPORTS if word in title), None)


def _frame(obj: dict) -> tuple[float, float, float, float, tuple[float, float], tuple[float, float]]:
    """Центр, длинная и короткая стороны, оси вдоль них (единичные) в осях плана."""
    cx, cz, a = obj["x"] + obj["w"] / 2, obj["z"] + obj["d"] / 2, obj.get("angle", 0.0)
    ex, ez = (math.cos(a), math.sin(a)), (-math.sin(a), math.cos(a))          # локальные x и z предмета
    return (cx, cz, *((obj["w"], obj["d"], ex, ez) if obj["w"] >= obj["d"] else (obj["d"], obj["w"], ez, ex)))


def _inside(frame, x: float, z: float) -> bool:
    cx, cz, long_, short, along, across = frame
    dx, dz = x - cx, z - cz
    return abs(dx * along[0] + dz * along[1]) <= long_ / 2 + EDGE_MARGIN_M and abs(dx * across[0] + dz * across[1]) <= short / 2 + EDGE_MARGIN_M


def _beside(members: list[dict], sits_with: dict, width: float, proj) -> list[dict]:
    """Толпа в одной точке: тот, кто садится рядом с кем-то (sits_with), встаёт сразу за ним; порядок остальных прежний.
    Разошедшихся по дивану не трогаем: их порядок - реальное положение."""
    people = {r["person"] for r in members}
    spans = [proj(r) for r in members]
    if max(spans) - min(spans) >= width:
        return members
    ordered = []
    for r in members:
        if sits_with.get(r["person"]) in people:
            continue                                          # встанет за тем, к кому садится
        ordered.append(r)
        ordered += [q for q in members if sits_with.get(q["person"]) == r["person"]]
    return ordered + [r for r in members if r not in ordered]       # цепочка «рядом с» замкнулась на себя: не теряем людей


def _lies(person: str, now: float | None, period_s: float, every: int) -> bool:
    """Иногда один прилёг: у каждого свой ритм по имени, без случайных чисел, чтобы не дрожало от кадра к кадру."""
    return now is not None and (int(now // period_s) + sum(map(ord, str(person)))) % every == 0


Rhythm = Callable[[str, "float | None", float, int], bool]     # (человек, время, период, из скольких периодов один) -> выпал ли этот период


def _lies_alone_on_sofa(row: dict, obj: dict, rows: list[dict], now: float | None, rooms: list[dict], lie_alone, sofa_pose, rhythm: Rhythm = _lies) -> bool:
    """Ляжет ли одинокий на диване: доли поз решают, если они есть; иначе ритм по имени или привычка лежать одному в комнате."""
    chosen = sofa_pose(row["person"], obj.get("title") or "", now) if sofa_pose and now is not None else None
    if chosen is not None:
        return chosen == "lie"
    return rhythm(row["person"], now, SOFA_LIE_PERIOD_S, SOFA_LIE_EVERY) or (row["person"] in lie_alone and _alone_in_room(row, rows, rooms))


def settle(rows: list[dict], objects: list[dict], front=None, now: float | None = None, genders: dict | None = None,
           order: dict | None = None, door_at=None, sit_only: frozenset | set = frozenset(),
           rooms: list[dict] | None = None, lie_alone: frozenset | set = frozenset(), sits_with: dict | None = None, chair_owners: list[tuple[float, float, str]] | None = None, hugs: dict | None = None, hug_always: bool = False,
           sofa_pose: Callable[[str, str, float], str | None] | None = None, rhythm: Rhythm = _lies) -> list[dict]:
    """front - необязательная функция «куда смотрит сидящий на этом предмете»: сидящие на диване поворачиваются лицом в комнату.
    order - очередь жильцов (кто раньше, тот ложится), genders - пол, door_at(x, z) - куда смотреть на дверь (сидящий на краю кровати),
    sit_only - кто на кровати только сидит и не занимает лежачего места; lie_alone - кто один в комнате лежит на её диване; sits_with - кто на диване садится рядом с кем (человек -> с кем); chair_owners - (x, z, человек) рабочих мест: хозяин садится на свой стул первым; hugs - кто на кровати иногда обнимает кого (человек -> партнёр), hug_always - обнимать всё время (отладка); rooms - комнаты плана для этого счёта;
    sofa_pose(человек, название дивана, время) - поза одинокого на диване по долям (SAM-120): «lie» или «sit», None - прежний ритм и lie_alone;
    rhythm - ритм «иногда» (прилечь на диване, лечь в ванну, сесть на унитаз вместо стойки, лечь порознь), по умолчанию по имени и времени без случайных чисел."""
    out = [dict(r) for r in rows]
    for obj in objects:
        if any(w in (obj.get("title") or "").lower() for w in CHAIR_WORDS) and obj.get("kind") != "opening":
            _settle_chair(out, obj, order or {}, chair_owners)
            continue
        if TOILET_WORD in (obj.get("title") or "").lower() and obj.get("kind") != "opening":
            _settle_toilet(out, obj, now, front, genders or {}, rhythm)
            continue
        if TUB_WORD in (obj.get("title") or "").lower() and obj.get("kind") != "opening":
            _settle_tub(out, obj, now, door_at, rhythm)
            continue
        kind = _support_kind(obj)
        if not kind or obj.get("kind") == "opening":
            continue
        pose, width = kind
        frame = _frame(obj)
        cx, cz, long_, short, along, across = frame
        members = [r for r in out if r.get("x") is not None and not r.get("walking") and _inside(frame, r["x"], r["z"])]
        if not members:
            continue
        rank = order or {}
        if pose == "lie":
            members.sort(key=lambda r: (rank.get(r["person"], 99), str(r["person"])))                      # лежат по очереди жильцов, потом гости
        else:
            members.sort(key=lambda r: ((r["x"] - cx) * along[0] + (r["z"] - cz) * along[1], str(r["person"])))     # по порядку вдоль опоры: места не перекрещиваются
            if sits_with:
                members = _beside(members, sits_with, width, lambda r: (r["x"] - cx) * along[0] + (r["z"] - cz) * along[1])
        spread, span = (across, short) if pose == "lie" else (along, long_)         # лежащие рядом по короткой стороне, сидящие по длинной
        capacity = max(1, int(span // width))
        sitters = [r for r in members if pose == "lie" and r["person"] in sit_only]
        lyers = [r for r in members if r not in sitters]
        seated, extra = lyers[:capacity], lyers[capacity:] + sitters
        pitch = span / capacity
        if pose == "lie":
            _lay_on_bed(obj, frame, seated, extra, pitch, front, door_at, hugs or {}, now, hug_always, rhythm)
            continue
        for k, r in enumerate(seated):
            off = (k - (len(seated) - 1) / 2) * pitch
            r["x"], r["z"], r["pose"] = cx + spread[0] * off, cz + spread[1] * off, pose
            if front and pose == "sit":
                r["face"] = front(obj)
            if pose == "sit" and "диван" in (obj.get("title") or "").lower() and len(members) == 1 and _lies_alone_on_sofa(
                    r, obj, out, now, rooms or [], lie_alone, sofa_pose, rhythm):
                r["pose"], r["x"], r["z"], r["face"] = "lie", cx, cz, math.atan2(along[1], along[0])      # одинокий прилёг вдоль дивана
        corners = [(sa, sc) for sa in (1, -1) for sc in (1, -1)]
        for k, r in enumerate(extra):                                               # лишний садится на угол, следующий на другой
            sa, sc = corners[k % 4]
            r["x"] = cx + along[0] * sa * (long_ / 2 - CORNER_INSET_M) + across[0] * sc * (short / 2 - CORNER_INSET_M)
            r["z"] = cz + along[1] * sa * (long_ / 2 - CORNER_INSET_M) + across[1] * sc * (short / 2 - CORNER_INSET_M)
            r["pose"] = "sit"
    return out


def _settle_tub(rows: list[dict], obj: dict, now: float | None, door_at=None, rhythm: Rhythm = _lies) -> None:
    """Один человек у ванны иногда лежит в ней вдоль; двое и больше - ничего (они стоят по слотам области)."""
    cx, cz, long_, short, along, across = _frame(obj)
    near = [r for r in rows if r.get("x") is not None and not r.get("walking")
            and abs((r["x"] - cx) * along[0] + (r["z"] - cz) * along[1]) <= long_ / 2 + TUB_REACH_M
            and abs((r["x"] - cx) * across[0] + (r["z"] - cz) * across[1]) <= short / 2 + TUB_REACH_M]
    if len(near) == 1 and rhythm(near[0]["person"], now, TUB_PERIOD_S, TUB_EVERY):
        head = along
        if door_at is not None and (toward := door_at(cx, cz)) is not None and along[0] * math.cos(toward) + along[1] * math.sin(toward) < 0:
            head = (-along[0], -along[1])                                                  # голова у ближнего к двери торца ванны
        near[0]["x"], near[0]["z"], near[0]["pose"], near[0]["face"] = cx, cz, "lie", math.atan2(head[1], head[0])


def _settle_toilet(rows: list[dict], obj: dict, now: float | None, front, genders: dict, rhythm: Rhythm = _lies) -> None:
    """Поза по полю пола в `genders`: значение female - сидит лицом ко входу, иначе обычно стоит лицом к стене, иногда сидит (тоже лицом ко входу)."""
    cx, cz = obj["x"] + obj["w"] / 2, obj["z"] + obj["d"] / 2
    users = [r for r in rows if r.get("x") is not None and not r.get("walking") and math.hypot(r["x"] - cx, r["z"] - cz) <= TOILET_REACH_M]
    if len(users) != 1:
        return
    r = users[0]
    face = front(obj, along_long=True) if front else r.get("face")
    if face is None:
        return
    sits = genders.get(r["person"]) == "female" or rhythm(r["person"], now, TOILET_SIT_PERIOD_S, TOILET_SIT_EVERY)
    if sits:
        r["x"], r["z"], r["pose"], r["face"] = cx, cz, "sit", face
    else:
        r["x"], r["z"], r["pose"], r["face"] = cx + math.cos(face) * TOILET_STAND_M, cz + math.sin(face) * TOILET_STAND_M, "stand", face + math.pi


def _lay_on_bed(obj: dict, frame, lying: list[dict], extra: list[dict], pitch: float, front, door_at, hugs: dict, now: float | None, hug_always: bool = False, rhythm: Rhythm = _lies) -> None:
    """Лежат в очереди жильцов, первый у стены; лишний садится на край в ногах у соседа и смотрит на дверь. Изголовье у предмета на локальном -z, ноги на +z."""""
    cx, cz, _long, short, _along, across = frame
    a = obj.get("angle", 0.0)
    foot, head = (-math.sin(a), math.cos(a)), (math.sin(a), -math.cos(a))
    wall_sign = -1.0
    if front:                                                                    # стена там, где у кровати не свободно: с противоположной стороны от «вперёд»
        fx, fz = math.cos(front(obj)), math.sin(front(obj))
        wall_sign = -1.0 if fx * across[0] + fz * across[1] > 0 else 1.0
    lanes = []
    for k, r in enumerate(lying):
        off = (k - (len(lying) - 1) / 2) * pitch * -wall_sign                    # k = 0 - полоса у стены
        lanes.append(off)
        r["x"], r["z"] = cx + across[0] * off, cz + across[1] * off
        r["pose"], r["face"] = "lie", math.atan2(head[1], head[0])               # лицом по направлению к изголовью
    if not extra:                                                                  # втроём на кровати не обнимаются
        _hug(lying, hugs, now, hug_always, rhythm)
    lane = lanes[-1] if lanes else None                                           # край в ногах у последнего лежащего (не у стены); лежащих нет - на углу
    half = obj["d"] / 2 if obj["w"] < obj["d"] else obj["w"] / 2
    for k, r in enumerate(extra):
        shift = (short / 2 - CORNER_INSET_M) * (1 if k % 2 == 0 else -1) if lane is None else (lane if k == 0 else -lane)
        r["x"] = cx + foot[0] * (half - CORNER_INSET_M) + across[0] * shift
        r["z"] = cz + foot[1] * (half - CORNER_INSET_M) + across[1] * shift
        r["pose"] = "sit"
        r["face"] = door_at(r["x"], r["z"]) if door_at else (front(obj) if front else r.get("face"))


def _hug(lying: list[dict], hugs: dict, now: float | None, always: bool = False, rhythm: Rhythm = _lies) -> None:
    """Двое из `hugs` на одной кровати часто обнимаются: обнимающий придвигается к партнёру на HUG_CLOSER_M (не ближе HUG_MIN_GAP_M), обнимающему ставится hug_side
    (+1 партнёр с той стороны, куда обращён живот, -1 с другой: страница зеркалит позу). Обнимаются чаще, чем нет: порознь лежат один период из HUG_APART_EVERY. Ритм по времени и именам, без случайных чисел."""
    by = {r["person"]: r for r in lying}
    for r in lying:
        mate = by.get(hugs.get(r["person"]))
        if mate is None or not (always or not rhythm(r["person"], now, HUG_PERIOD_S, HUG_APART_EVERY)):
            continue
        dx, dz = mate["x"] - r["x"], mate["z"] - r["z"]
        dist = math.hypot(dx, dz)
        if dist < 1e-6:
            continue
        step = min(HUG_CLOSER_M, max(0.0, dist - HUG_MIN_GAP_M))
        r["x"], r["z"] = r["x"] + dx / dist * step, r["z"] + dz / dist * step              # партнёр остаётся на месте, обнимающий придвигается
        dx, dz = mate["x"] - r["x"], mate["z"] - r["z"]
        face = r["face"]
        r["hug"], r["hug_side"] = mate["person"], 1 if -math.sin(face) * dx + math.cos(face) * dz > 0 else -1


def _alone_in_room(row: dict, rows: list[dict], rooms: list[dict]) -> bool:
    """В комнате, где стоит человек, больше нет никого из остальных; вне комнат плана (коридор, улица) - нет."""
    room = next((rm for rm in rooms if rm["x"] <= row["x"] <= rm["x"] + rm["w"] and rm["z"] <= row["z"] <= rm["z"] + rm["d"]), None)
    if room is None:
        return False
    return not any(o is not row and o.get("x") is not None and room["x"] <= o["x"] <= room["x"] + room["w"] and room["z"] <= o["z"] <= room["z"] + room["d"] for o in rows)


SUPPORT_WORDS = ("диван", "кроват", "кресл", "стул", "унитаз", "ванн")     # на чём можно сидеть и лежать
GROUND_MARGIN_M = 0.35                                                       # точка чуть за краем опоры ещё на ней: оценка места неточна


def ground_poses(rows: list[dict], objects: list[dict]) -> list[dict]:
    """Страховка: сидеть и лежать можно только над опорой; без неё человек стоит.
    Разведение людей или сдвиг места могли унести сидящего на пустой пол, а поза «сидит» осталась."""
    supports = [o for o in objects if o.get("kind") != "opening" and any(w in (o.get("title") or "").lower() for w in SUPPORT_WORDS)]
    out = []
    for row in rows:
        if row.get("pose") in ("sit", "lie") and row.get("x") is not None and not any(_over(o, row["x"], row["z"]) for o in supports):
            row = {**row, "pose": "stand"}
        out.append(row)
    return out


def _over(obj: dict, x: float, z: float) -> bool:
    cx, cz, a = obj["x"] + obj["w"] / 2, obj["z"] + obj["d"] / 2, obj.get("angle", 0.0)
    dx, dz = x - cx, z - cz
    lx, lz = dx * math.cos(a) + dz * math.sin(a), -dx * math.sin(a) + dz * math.cos(a)
    return abs(lx) <= obj["w"] / 2 + GROUND_MARGIN_M and abs(lz) <= obj["d"] / 2 + GROUND_MARGIN_M


def _settle_chair(rows: list[dict], obj: dict, order: dict, owners: list[tuple[float, float, str]] | None = None) -> None:
    """Сидит один: первый по очереди жильцов; остальные претенденты на тот же стул встают (разведение отодвинет их от сидящего)."""
    cx, cz, a = obj["x"] + obj["w"] / 2, obj["z"] + obj["d"] / 2, obj.get("angle", 0.0)

    def over(r: dict) -> bool:
        dx, dz = r["x"] - cx, r["z"] - cz
        lx, lz = dx * math.cos(a) + dz * math.sin(a), -dx * math.sin(a) + dz * math.cos(a)
        return abs(lx) <= obj["w"] / 2 + CHAIR_REACH_M and abs(lz) <= obj["d"] / 2 + CHAIR_REACH_M
    claim = [r for r in rows if r.get("x") is not None and not r.get("walking") and r.get("pose") == "sit" and over(r)]
    mine = {person for x, z, person in owners or [] if over({"x": x, "z": z})}                  # чей это стул: рабочее место человека у него
    claim.sort(key=lambda r: (r["person"] not in mine, order.get(r["person"], 99), str(r["person"])))
    for extra in claim[1:]:
        extra["pose"] = "stand"
