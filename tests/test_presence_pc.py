"""Рабочий компьютер как свидетельство (SAM-121): игра без ввода ставит хозяина на диван и второго рядом, если он дома, ввод ставит за стол, выключенный молчит. Данные выдуманные."""
import pytest

from radar import presence_fuse as fuse
from radar import presence_hub as ph
from radar import presence_pc as ppc

NOW = 1_000_000.0
PLACES = {"desk": {"x": 1.0, "z": 1.0, "pose": "sit"}, "sofa": {"x": 3.0, "z": 2.0, "pose": "sit"}}
CONFIG = {"person": "player-a", "second": "player-b", "desk": "desk", "sofa": "sofa", "games": ["game.exe"], "idle_s": 120}


def _wifi(person: str, away: bool = False) -> fuse.Evidence:
    return fuse.Evidence(source="wifi", person=person, weight=0.5, t=NOW, away=away)


GAME = ppc.PcState(on=True, game=True, idle_s=300)
SOFA_A, SOFA_B = ("player-a", 3.0, 2.0, ppc.GAME_WEIGHT), ("player-b", 3.0, 2.0, ppc.PARTNER_WEIGHT)


@pytest.mark.parametrize("state, config, places, home_signs, expected", [
    # игра без ввода: хозяин на диване, второй рядом, если другой канал говорит, что он дома
    (GAME, CONFIG, PLACES, [_wifi("player-b")], [SOFA_A, SOFA_B]),
    # второй без подтверждения или со знаком «ушёл» отбрасывается
    (GAME, CONFIG, PLACES, [], [SOFA_A]),
    (GAME, CONFIG, PLACES, [_wifi("player-b", away=True)], [SOFA_A]),
    # недавний ввод - за столом, что бы ни шло
    (ppc.PcState(on=True, game=True, idle_s=5), CONFIG, PLACES, [], [("player-a", 1.0, 1.0, ppc.INPUT_WEIGHT)]),
    # простой без игры, выключенная машина, нет состояния, нет хозяина - молчит
    (ppc.PcState(on=True, game=False, idle_s=600), CONFIG, PLACES, [], []),
    (ppc.PcState(on=False, game=True, idle_s=0), CONFIG, PLACES, [], []),
    (None, CONFIG, PLACES, [], []),
    (ppc.PcState(True, True, 0), {}, PLACES, [], []),
    # без второго в настройке - только хозяин
    (ppc.PcState(True, True, 500), {**CONFIG, "second": None}, {"sofa": PLACES["sofa"]}, [], [SOFA_A]),
], ids=["game-partner-home", "game-partner-unknown", "game-partner-away", "input", "idle", "off", "no-state", "no-owner", "no-partner"])
def test_pc_state_puts_owner_and_partner(state, config, places, home_signs, expected):
    rows = ppc.evidence_from_state(state, config, places, NOW)
    kept = [e for e in ppc.drop_unsupported_partner(rows + home_signs, config.get("second")) if e.source == "pc"]
    assert [(e.person, e.x, e.z, e.weight) for e in kept] == expected
    assert all(e.pose == "sit" and e.t == NOW and not e.away for e in kept)


def test_pc_channel_reads_machine_at_most_once_per_poll():
    reads = []
    reader = lambda config: reads.append(1) or ppc.PcState(True, True, 400)
    channel = ppc.PcChannel(CONFIG, lambda: PLACES, reader, poll_s=10)
    assert channel.evidence(NOW) and channel.evidence(NOW + 3) and len(reads) == 1
    assert channel.evidence(NOW + 11) and len(reads) == 2
    assert ppc.PcChannel({}, lambda: PLACES, reader).evidence(NOW) == [] and len(reads) == 2


def test_windows_reader_reports_only_configured_games(monkeypatch):
    monkeypatch.setattr(ppc, "running_processes", lambda: {"launcher.exe", "game.exe"})
    monkeypatch.setattr(ppc, "windows_idle_s", lambda: 42.0)
    assert ppc.read_windows_state(CONFIG) == ppc.PcState(on=True, game=True, idle_s=42.0)
    assert ppc.read_windows_state({**CONFIG, "games": ["other.exe"]}).game is False and ppc.read_windows_state({}).game is False


def test_hub_places_pc_owner_and_not_an_absent_partner():
    layout = {"walls": [], "objects": [], "rooms": [], "places": [{"label": "desk", "x": 1.0, "z": 1.0}, {"label": "sofa", "x": 3.0, "z": 2.0}]}
    hub = ph.PresenceHub(ph.MODE_RSSI, layout, clock=lambda: NOW, pc=CONFIG, pc_reader=lambda config: ppc.PcState(True, True, 400),
                         channels=("pc",))
    assert "pc" in hub.enabled_channels
    rows = {r["person"]: r for r in hub.identities(NOW)}
    assert rows["player-a"]["sources"] == ["pc"] and "player-b" not in rows
    assert (rows["player-a"]["x"], rows["player-a"]["z"]) == pytest.approx((3.0, 2.0), abs=0.6)    # на диване, не за столом
