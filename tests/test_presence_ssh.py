"""Поток станций роутера (SAM-105): разбор кругов опроса, переподключение, режим rssi в хабе. Роутер подставной: процесс ssh, его вывод и пауза между попытками - фальшивки."""
import asyncio
import json

import pytest

from radar import presence as pr
from radar import presence_devices as pdv
from radar import presence_hub as ph
from radar import presence_rssi as rs
from radar import presence_ssh as ssh

ROUND = """#IFACE phy0-ap0
Station aa:bb:cc:00:00:01 (on phy0-ap0)
\tinactive time:\t100 ms
\tsignal:  \t-40 [-42, -41] dBm
#IFACE phy1-ap0
Station aa:bb:cc:00:00:02 (on phy1-ap0)
\tinactive time:\t200 ms
\tsignal:  \t-70 [-72, -71] dBm
#END
"""


LEASES = "1791625096 aa:bb:cc:00:00:01 192.168.1.10 Phone-A 01:aa\n1791625097 aa:bb:cc:00:00:09 192.168.1.11 * 01:bb\n"


def _rounds(parser, text):
    return [r for r in (parser.feed(line) for line in text.splitlines(keepends=True)) if r is not None]


def test_router_stream_parses_rounds_with_lease_names():
    """Удалённый скрипт только читает; парсер отдаёт один круг на #END с обоими интерфейсами, следующий круг с нуля, имена - из DHCP leases."""
    script = ssh.remote_script(["phy0-ap0", "phy1-ap0"], 0.5)
    assert script.count("station dump") == 2 and "sleep 0.5" in script
    assert not any(w in script for w in ("reboot", "wifi", "uci", "opkg", "restart", "rm "))
    parser = ssh.StreamParser()
    [first] = _rounds(parser, ROUND)
    assert sorted(s.iface for s in first) == ["phy0-ap0", "phy1-ap0"] and {s.name for s in first} == {None}
    assert parser.feed("#END") == []                                                              # следующий круг с нуля
    assert rs.parse_leases(LEASES) == {"aa:bb:cc:00:00:01": "Phone-A"}                             # «*» - имени нет
    [named] = _rounds(parser, ROUND.replace("#END\n", "#LEASES\n" + LEASES + "#END\n"))
    assert {s.name for s in named} == {"Phone-A", None}


class _Stdout:
    def __init__(self, text):
        self._lines = [l.encode() for l in text.splitlines(keepends=True)]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _Proc:
    def __init__(self, text):
        self.stdout = _Stdout(text)

    async def wait(self):
        return 255


def test_station_stream_reconnects_and_backs_off_without_ssh(capsys):
    """Обрыв - переподключение с паузой; нет ssh - одна строка лога на попытку и пауза 2-4-8 с."""
    spawned, pauses = [], []

    async def spawn(*args, **kw):
        spawned.append(args)
        return _Proc(ROUND)

    async def sleep(s):
        pauses.append(s)

    async def scenario():
        rounds = []
        async for stations in ssh.station_stream("root@router", ["phy0-ap0", "phy1-ap0"], 0.5, spawn, sleep):
            rounds.append(len(stations))
            if len(rounds) == 2:
                break
        return rounds
    assert asyncio.run(scenario()) == [2, 2]
    assert len(spawned) == 2 and spawned[0][0] == "ssh" and "root@router" in spawned[0]
    assert pauses == [ssh.RETRY_MIN_S] and "SAM_PRESENCE_ROUTER" in capsys.readouterr().out
    pauses = []

    async def no_ssh(*a, **k):
        raise FileNotFoundError("ssh")

    async def sleep_three_times(s):
        pauses.append(s)
        if len(pauses) == 3:
            raise asyncio.CancelledError

    async def missing():
        try:
            async for _ in ssh.station_stream("r", ["x"], 0.5, no_ssh, sleep_three_times):
                pass
        except asyncio.CancelledError:
            pass
    asyncio.run(missing())
    assert pauses == [2.0, 4.0, 8.0] and capsys.readouterr().out.count("ssh не запустился") == 3


IDLE_TICK_S = 3600.0          # тик хаба длиннее сценария: кадры не считаются, проверяется только потребление потока
GUARD_S = 30.0                # предохранитель от зависания, не замер: событие приходит, как только поток отдан


def _hub():
    now = [100.0]
    return ph.PresenceHub(ph.MODE_RSSI, pr.DEMO_LAYOUT, clock=lambda: now[0]), now


def _station(level):
    return rs.Station("s1", "phy1-ap0", level, 100)


CALM, SHAKING = [-60.0] * 40, [-60.0 + (i % 2) * 8 for i in range(25)]     # спокойная комната, потом кто-то прошёл между роутером и устройством


def test_rssi_hub_shows_signal_activity_and_no_people():
    """Хаб в rssi показывает активность сигнала с возрастом и не рисует людей: из feed_stations и из потока роутера; без потока цикл живёт."""
    hub, now = _hub()
    assert hub.snapshot()["signal"] == {"activity": 0.0, "stations": 0, "active": 0, "age_s": None}
    for level in CALM + SHAKING:
        hub.feed_stations([_station(level)], now=100.0)
    now[0] = 103.0
    snap = hub.step()
    assert snap["people"] == [] and snap["mode"] == "rssi"
    assert snap["signal"]["activity"] > 0 and snap["signal"]["age_s"] == 3.0

    async def scenario():
        drained = asyncio.Event()

        async def router():
            for level in CALM + SHAKING:
                yield [_station(level)]
            drained.set()                                               # поток исчерпан: всё отдано хабу

        streamed = ph.PresenceHub(ph.MODE_RSSI, pr.DEMO_LAYOUT, IDLE_TICK_S, clock=lambda: 100.0, stations=router)
        task = asyncio.create_task(streamed.run())
        await asyncio.wait_for(drained.wait(), GUARD_S)
        await asyncio.sleep(0)
        alive = not task.done()
        task.cancel()
        return streamed.snapshot()["signal"]["activity"], alive
    activity, alive = asyncio.run(scenario())
    assert activity > 0 and alive                                       # поток кончился, цикл хаба продолжает работать


def test_device_book_persists_and_reads_old_files(tmp_path):
    """Книга устройств ведётся и сохраняется, битый файл - None, старый формат с удалёнными полями читается."""
    path = tmp_path / "devices.json"
    hub = ph.PresenceHub(ph.MODE_RSSI, pr.DEMO_LAYOUT, clock=lambda: 50.0, devices_file=path)
    hub.feed_stations([rs.Station("d1", "phy1-ap0", -50.0, 100, "Phone")], now=50.0)
    assert hub.snapshot()["devices"][0]["name"] == "Phone" and hub.device_events[0]["event"] == "appeared"
    assert ph.load_devices(path).devices["d1"].name == "Phone"
    path.write_text("не json", encoding="utf-8")
    assert ph.load_devices(path) is None
    old = ('[{"sid": "a", "name": "X", "iface": "phy0-ap0", "first_seen": 1.0, "last_seen": 2.0, "last_signal_db": -50.0, "present": false, '
           '"usual_db": -50.0, "off_count": 0, "anchor_db": -70.0, "displaced": true, "gap_count": 0}]')
    assert list(pdv.DeviceBook.from_json(old).devices) == ["a"]


def test_heat_map_counts_owned_presence_and_cools_with_time():
    """Устройство без хозяина карту не греет, карта остывает с полураспадом, трек греет там, где стоял дольше и свежее."""
    layout = {**pr.EMPTY_LAYOUT, "router": {"x": 6.0, "z": 4.0, "h": 1.5}}
    hub = ph.PresenceHub(ph.MODE_RSSI, layout, clock=lambda: 0.0)
    for i in range(70):
        hub.feed_stations([rs.Station("t1", "phy0", -45.0 + (i % 2) * 9 * (i > 40), 100, "Tablet")], now=float(i))
        hub.step(float(i))
    assert hub.heat.counts == {}

    cooling = ph.PresenceHub(ph.MODE_RSSI, pr.DEMO_LAYOUT, clock=lambda: 0.0, heat_half_life_s=100.0)
    cooling.heat.add(1.0, 1.0, 8.0)
    cooling.step(100.0)
    assert cooling.heat.counts[cooling.heat.cell_of(1.0, 1.0)] == pytest.approx(4.0)

    track = [{"t": 0.0, "x": 1.0, "z": 1.0}, {"t": 100.0, "x": 1.0, "z": 1.0}, {"t": 110.0, "x": 3.0, "z": 1.0}]
    now_fresh = pr.heat_from_track(track, 110.0, 7200.0)
    stay, walk = now_fresh.counts[now_fresh.cell_of(1.0, 1.0)], now_fresh.counts[now_fresh.cell_of(2.5, 1.0)]
    assert stay > 90 and walk < 5                                        # стоял сто секунд, прошёл за десять
    old = pr.heat_from_track(track, 110.0 + 7200.0, 7200.0)
    assert old.counts[old.cell_of(1.0, 1.0)] == pytest.approx(stay / 2, rel=0.05)


WALKS = [{"device": "s24", "points": [{"t": 0.0, "x": 1.0, "z": 1.0, "label": "a"}, {"t": 60.0, "x": 1.0, "z": 1.0, "label": "b"}]}]
BOX = {"walls": [{"x1": 0, "z1": 0, "x2": 5, "z2": 0, "t": 0.2, "h": 2.5}, {"x1": 5, "z1": 0, "x2": 5, "z2": 5, "t": 0.2, "h": 2.5},
                 {"x1": 5, "z1": 5, "x2": 0, "z2": 5, "t": 0.2, "h": 2.5}, {"x1": 0, "z1": 5, "x2": 0, "z2": 0, "t": 0.2, "h": 2.5}],
       "objects": [], "rooms": [{"id": "h", "title": "x", "x": 0, "z": 0, "w": 5, "d": 5}]}


def hub_with_walks(tmp_path, clock=lambda: 100.0):
    walks = tmp_path / "walks.json"
    walks.write_text(json.dumps(WALKS))
    return ph.PresenceHub(ph.MODE_RSSI, BOX, clock=clock, walks_file=walks, heat_file=tmp_path / "heat.json")


def test_clearing_heat_hides_recorded_walks_across_restart(tmp_path):
    """Тепло страницы - только сохранённые координаты; очистка прячет записанные прогулки и после рестарта, новые прогулки видны."""
    hub = hub_with_walks(tmp_path)
    assert hub.heat_payload(100.0)["cells"] == [] and len(hub.walks()) == 1
    hub.clear_heat()
    assert hub.walks() == []
    again = hub_with_walks(tmp_path, clock=lambda: 200.0)
    assert again.walks() == []
    (tmp_path / "walks.json").write_text(json.dumps(WALKS + [{"device": "s24", "points": [{"t": 150.0, "x": 2.0, "z": 2.0}, {"t": 180.0, "x": 3.0, "z": 2.0}]}]))
    assert len(again.walks()) == 1            # прогулка, записанная после очистки, показывается
