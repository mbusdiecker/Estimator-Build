"""KPI % on a synthetic room, with exact hand-checked geometry.

Synthetic room: 3x3 px floor, ppm = 1 px/m, step 1 px -> 9 grid points at
(x, y) in {0,1,2}^2 (meters). Antenna at a known corner, band 2000 MHz,
office N=30, no walls, same floor.

P1238 PL(d) = 20log10(2000) + 30 log10(d) - 28
  PL(1 m)     = 66.0206 - 28        = 38.0206 dB
  PL(1.414 m) = +30log10(1.414)     = 42.5257 dB
  PL(2 m)     = +30log10(2)         = 47.0516 dB

Choose EIRP so the 1-m RSSI sits exactly 0.01 dB above threshold T = -94.8 dBm:
  point (0,0)  (d clamped to 0.01 m) -> covered
  points (1,0) and (0,1) (d = 1 m)   -> covered (RSSI = T + 0.01 dB)
  (1,1) d=1.414  -> -99.32 dBm      -> NOT covered
  (2,0)/(0,2) d=2 -> -103.83        -> NOT covered
  (2,1)/(1,2) d=2.236 -> -105.4     -> NOT covered
  (2,2) d=2.828       -> -109.6     -> NOT covered
=> covered = 3 / 9 = 33.33 % exactly.
"""

import math

import pytest

from romtool.cascade import solve_cascade
from romtool.config import with_overrides
from romtool.physics import itu_p1238_pl_db
from romtool.predict import PredictError, predict_floor

from test_cascade import test_cfg as base_cfg

PL_1M = itu_p1238_pl_db(2000.0, 1.0, 30.0, 0, 15.4)  # 38.0206 dB
PL_001M = itu_p1238_pl_db(2000.0, 0.01, 30.0, 0, 15.4)
THRESH = -94.8
EIRP = THRESH + PL_1M + 0.01  # 1-m RSSI sits 0.01 dB above the threshold


def make_room(ant_specs):
    """ant_specs: list of (element_id, coords, floor_id).

    The chain is a straight source -> repeater -> (zero-length runs) -> N
    antennas. The repeater's DL gain is tuned per test so each antenna port
    receives exactly a target dBm (zero-length runs, gain 0 antennas).
    """
    els = [
        {"id": "src", "type": "source", "label": "SRC", "subtype": "off_air",
         "donor_gain_db": 11.0, "feeder_loss_db": 0.0, "x": 2, "y": 2},
        {"id": "rep", "type": "repeater", "label": "REP", "x": 2, "y": 2},
        {"id": "r1", "type": "cable_run", "from_id": "src", "to_id": "rep",
         "cable_id": "testc", "length_m": 0.0},
    ]
    floors = [
        {"id": "L0", "name": "Ground", "level": 0, "ppm": 1.0, "w_px": 3, "h_px": 3,
         "walls": [], "elements": els},
        {"id": "L1", "name": "Above", "level": 1, "ppm": 1.0, "w_px": 3, "h_px": 3,
         "walls": [], "elements": []},
    ]
    for eid, (x, y), flid in ant_specs:
        floors[0 if flid == "L0" else 1]["elements"].append({
            "id": eid, "type": "antenna", "label": eid, "antenna_id": "omni_test",
            "x": x, "y": y, "gain_db": 0.0, "max_port_dbm": 60.0,
        })
        els.append({"id": "r_" + eid, "type": "cable_run", "from_id": "rep",
                    "to_id": eid, "cable_id": "testc", "length_m": 0.0})
    return {"environment": "office", "floors": floors}


def repeater_gain_for_target(target_port_dbm):
    # rep in = -85 ambient + 11 donor - 0 feeder = -74.0; no trunk losses
    return target_port_dbm + 74.0


def room_with_antenna_at_exact_port(port_dbm, x=0, y=0, floor_of="L0"):
    model = make_room([("A1", (x, y), floor_of)])
    rep = model["floors"][0]["elements"][1]
    rep["dl_gain_db"] = repeater_gain_for_target(port_dbm)
    rep["max_dl_out_dbm"] = 60.0
    return model


def predict_room(model, cfg, floor_id="L0", band="T2G", step=1):
    solve = solve_cascade(model, cfg)
    if solve["errors"]:
        raise AssertionError("solve errors: %s" % solve["errors"])
    return predict_floor(model, cfg, floor_id, band, step_px=step, solve_result=solve)


@pytest.fixture()
def room_cfg():
    cfg = base_cfg()
    cfg["kpi_presets"]["single_tier"]["threshold_dbm"] = THRESH
    for tier in cfg["kpi_presets"]["nfpa_two_tier"]:
        tier["threshold_dbm"] = THRESH
    return cfg


def test_synthetic_room_kpi_exact_fraction(room_cfg):
    model = room_with_antenna_at_exact_port(EIRP)
    pr = predict_room(model, room_cfg)
    assert pr["grid"]["n_points"] == 9
    assert pr["kpi"]["covered_pct"] == pytest.approx(100 * 3 / 9, abs=0.02)
    assert pr["kpi"]["presets"][0]["pass"] is False  # 33.3% < 95%
    assert pr["kpi"]["presets"][1]["pass"] is False
    assert pr["kpi"]["presets"][2]["pass"] is False


def test_synthetic_room_values_match_hand_math(room_cfg):
    model = room_with_antenna_at_exact_port(EIRP)
    pr = predict_room(model, room_cfg)
    vals = pr["values_dbm"]  # index = row * 3 + col
    assert vals[0] == pytest.approx(EIRP - PL_001M, abs=0.02)  # (0,0): d clamped 0.01
    assert vals[1] == pytest.approx(THRESH + 0.01, abs=0.02)   # (1,0): d = 1
    assert vals[3] == pytest.approx(THRESH + 0.01, abs=0.02)   # (0,1): d = 1
    # (1,1) index 4: T + 0.01 - 30 log10(sqrt(2))
    assert vals[4] == pytest.approx(THRESH + 0.01 - 30 * math.log10(math.sqrt(2)), abs=0.02)
    assert set(pr["best_server"]) == {"A1"}


def test_composite_two_equal_antennas_plus_3db(room_cfg):
    """A1 at (0,0) and A2 at (2,0), equal port power: at grid point (1,0) both
    are exactly 1 m away -> composite = level + 3.0103 dB, best-server ties on
    a per-point basis."""
    port = EIRP
    model = make_room([("A1", (0, 0), "L0"), ("A2", (2, 0), "L0")])
    rep = model["floors"][0]["elements"][1]
    rep["dl_gain_db"] = repeater_gain_for_target(port)
    rep["max_dl_out_dbm"] = 60.0
    pr = predict_room(model, room_cfg)
    vals = pr["values_dbm"]
    composite_at_10 = 10 * math.log10(2 * 10 ** ((THRESH + 0.01) / 10.0))
    assert vals[1] == pytest.approx(composite_at_10, abs=0.05)
    assert pr["best_server"][0] == "A1" and pr["best_server"][2] == "A2"
    assert pr["best_server"][1] == "A1"  # exact tie resolves to the first antenna


def test_inter_floor_loss_is_exactly_floor_loss_per_floor(room_cfg):
    """Same coords one floor up: every value drops exactly 15.4 dB and no wall
    crossings apply between floors."""
    same = predict_room(room_with_antenna_at_exact_port(EIRP, floor_of="L0"), room_cfg)
    up = predict_room(room_with_antenna_at_exact_port(EIRP, floor_of="L1"), room_cfg)
    for a, b in zip(same["values_dbm"], up["values_dbm"]):
        assert (a - b) == pytest.approx(15.4, abs=0.02)
    assert up["best_server"][0] == "A1"


def test_kpi_passes_when_thresholds_relaxed(room_cfg):
    cfg = with_overrides(room_cfg, {"kpi_presets": {"single_tier": {"min_area_pct": 30.0}}})
    pr = predict_room(room_with_antenna_at_exact_port(EIRP), cfg)
    assert pr["kpi"]["covered_pct"] == pytest.approx(33.33, abs=0.05)
    assert pr["kpi"]["presets"][0]["pass"] is True


def test_full_coverage_when_hot(room_cfg):
    """EIRP strong enough that every point clears the threshold -> 100%."""
    hot = THRESH + itu_p1238_pl_db(2000.0, 2.8285, 30.0, 0, 15.4) + 0.01
    pr = predict_room(room_with_antenna_at_exact_port(hot), room_cfg)
    assert pr["kpi"]["covered_pct"] == pytest.approx(100.0, abs=0.01)
    assert pr["kpi"]["presets"][0]["pass"] is True
    assert pr["kpi"]["presets"][2]["pass"] is True


def test_predict_rejects_unknown_floor_and_band(room_cfg):
    model = room_with_antenna_at_exact_port(EIRP)
    with pytest.raises(PredictError):
        predict_floor(model, room_cfg, "nope", "T2G")
    with pytest.raises(PredictError):
        predict_floor(model, room_cfg, "L0", "B12_700")
