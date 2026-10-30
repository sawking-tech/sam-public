"""Мини-сеть как свидетельство потока (SAM-118): множитель только в клетках областей сети, без флага поток прежний, признаки опроса как в обучении.
План и веса выдуманные: квартира 6 x 4 м из tests/fakes_presence.flow_layout, левая половина - «комната A», правой области у сети нет на плане."""
import json

import numpy as np
import pytest

from radar import presence as pr
from radar import presence_flow_live as pfl
from radar import presence_hub as ph
from radar import presence_link as plk
from radar import presence_nn as pnn
from radar import presence_rssi as rs
from radar.presence_random import Randomness
from tests.fakes_presence import (FLOW_IFACE as IFACE, FLOW_MODEL as MODEL, FLOW_NAME as NAME, FLOW_PLACES as PLACES, FLOW_START as START,
                                  FLOW_TEST_WINDOW as TEST_WINDOW, flow_layout as layout, isolated_hub_env, load_script, write_layout)

ROOMS = [{"title": "комната A", "x": 0.0, "z": 0.0, "w": 3.0, "d": 4.0}]
NORM = pnn.Norm(db_zero=-60.0, db_scale=10.0, db_missing=-75.0, age_cap_s=5.0, rate_log_scale=7.0)
WINDOW_TICKS = 4
NN_PART = pnn.NnFactor.__name__.lower()          # имя вклада сети в срезах детального вида потока
POLLS = 12


def fake_model(bias=(3.0, 0.0)) -> pnn.GruModel:
    """GRU на 2 скрытых с нулевыми весами: ответ задаёт смещение головы - область A почти наверняка."""
    h, f = 2, 8
    w = {"w_ih": np.zeros((3 * h, f)), "w_hh": np.zeros((3 * h, h)), "b_ih": np.zeros(3 * h), "b_hh": np.zeros(3 * h),
         "a_w": np.zeros((2, h)), "a_b": np.array(bias)}
    return pnn.GruModel(w, ["комната A", "комната B"], [0.5, 0.5], WINDOW_TICKS, NORM, {"spots": [], "corridor": []})


def save_model(folder, model, iface=IFACE):
    np.savez(folder / f"gru-{iface}.npz", **model.w)
    (folder / f"gru-{iface}.json").write_text(json.dumps({"iface": iface, "areas": model.areas, "priors": [0.5, 0.5], "window_ticks": WINDOW_TICKS,
                                                          "norm": NORM.__dict__, "classes": {"spots": [], "corridor": []}}), encoding="utf-8")


def make_flow():
    return pfl.LiveFlow(layout(), MODEL, {}, {"place_xy": PLACES}, randomness=Randomness(1), window_slices=TEST_WINDOW)


def last_parts(flow):
    return set(flow.state(detail=True)["devices"][0]["slices"][-1]["parts"])


def test_nn_factor_raises_only_cells_of_the_likely_area():
    flow = make_flow()
    bundle = pnn.NnBundle({IFACE: fake_model()}, ROOMS, weight=0.2)
    probs = bundle.model(IFACE).probabilities(np.zeros((WINDOW_TICKS, 8)))
    field = bundle.factor_for(IFACE, flow.world.points, lambda: probs).log_field(None)
    left, far_right = flow.world.points[:, 0] < 3.0, flow.world.points[:, 0] > 3.0 + pnn.DOORWAY_REACH_M
    assert probs[0] > 0.9 and (field[left] > 0).all() and (field[far_right] == 0).all()     # у «комнаты B» нет клеток на плане - множитель 1; у стены клетки отходят соседней комнате
    assert pnn.NnFactor(lambda: None, np.array([0.5, 0.5]), np.zeros(3, dtype=int)).log_field(None) is None


def mass_in_room_a(flow):
    state = flow.state()
    return sum(p for (x, _), p in zip(state["cells"], state["devices"][0]["p"]) if x < ROOMS[0]["w"])


def built_hub(tmp_path, nn_dir=None):
    """Хаб из окружения с потоком; сеть - за флагом и только если в папке есть веса."""
    tmp_path.mkdir(exist_ok=True)
    models = tmp_path / "radio_model.json"
    models.write_text(json.dumps({"models": MODEL}), encoding="utf-8")
    plan = write_layout(tmp_path, {**layout(), "rooms": ROOMS})
    env = {ph.FLAG_ENV: ph.MODE_RSSI, ph.FLOW_ENV: "1", pr.LAYOUT_ENV: str(plan), pfl.RADIO_MODEL_ENV: str(models)}
    if nn_dir is not None:
        env.update({pnn.NN_ENV: "1", pnn.NN_DIR_ENV: str(nn_dir)})
    return ph.build(isolated_hub_env(tmp_path, **env))


@pytest.mark.parametrize("with_weights", [False, True], ids=["flag-without-weights", "flag-with-weights"])
def test_flow_carries_the_net_only_with_flag_and_models(tmp_path, with_weights):
    # без флага, и с флагом, но без весов в папке, - окно потока несёт только кольцо
    nets = tmp_path / "nn"
    nets.mkdir()
    if with_weights:
        save_model(nets, fake_model())
    plain = built_hub(tmp_path / "plain")
    flagged = built_hub(tmp_path, nets)
    for k in range(POLLS):
        for hub in (plain, flagged):
            hub.feed_stations([rs.Station("abc", IFACE, -50.0, 100, NAME)], START + k * 0.5)
        if k == 0:
            assert last_parts(flagged.flow) == {"rings"}                     # один опрос - окна признаков ещё нет, сеть молчит
    assert last_parts(plain.flow) == {"rings"}
    if not with_weights:
        assert last_parts(flagged.flow) == {"rings"}
        return
    # с сетью окно получает её множитель, как только набралось окно опросов, и он тянет массу в «комнату A»
    assert last_parts(flagged.flow) == {"rings", NN_PART}
    assert mass_in_room_a(flagged.flow) > mass_in_room_a(plain.flow)


def test_nn_weights_load_back_with_band_and_areas(tmp_path):
    model = fake_model()
    save_model(tmp_path, model)
    bundle = pnn.NnBundle.load(tmp_path, ROOMS, 0.2)
    assert list(bundle.models) == [IFACE] and bundle.model(IFACE).areas == model.areas and bundle.model("phy0-ap0") is None


def test_poll_features_match_training_features():
    train = load_script("radar_nn_train")
    link = plk.LinkFeatures(avg_chains=(-52.0, -55.0), ack_last_db=-50.0, ack_avg_db=-52.0, tx=plk.LinkRate(mbit=400.0), rx=plk.LinkRate(mbit=120.0))
    feats = pnn.Features(NORM, 2)
    feats.push(-53.0, 300, IFACE, link)
    tick = {"phone-a": {"db": -53.0, "fresh": True, "age_ms": 300, "if": IFACE, "ant_diff": 3.0, "ack0": -50.0, "ack1": -52.0, "tx_rate": 400.0, "rx_rate": 120.0}}
    expected = train.tick_features([tick])[0, :train.FEATURES_PER_PHONE]
    assert np.allclose(feats.rows[-1], expected)
