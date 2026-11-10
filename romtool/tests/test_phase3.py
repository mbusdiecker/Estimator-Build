"""Phase 3: 5G NR bands (n71 600 / n41 2500 / n77 C-band), NR SS-RSRP KPI
presets, and the multiband composite / best-band view.

Hand-checked numbers reuse the phase-1/2 synthetic 3x3 room
(tests/test_kpi.py): 1 px = 1 m, office N=30, no walls,
P1238 PL(d) = 20log10(f_MHz) + 30log10(d_m) - 28.

  PL(1 m) anchors (dB):  n71 634.5 -> 28.0486 | n41 2593.0 -> 40.2761
                         n77 3840.0 -> 43.6866 | n78 3350.0 -> 42.5009
  PL(0.01 m) @2000:      20log10(2000) - 60 - 28 = -21.9794 dB

P.2040-style wall scaling from the 2000 MHz tabulation (loss * log10(f)/log10(2000)):
  concrete: 15.5178 dB @2600, 16.2873 dB @3840
  metal   : 31.0355 dB @2600, 32.5747 dB @3840
"""

import math
import struct

import pytest

from romtool.cascade import solve_cascade
from romtool.composite import composite_heatmap_view, predict_floor_multiband
from romtool.config import load_config, with_overrides
from romtool.physics import itu_p1238_pl_db
from romtool.predict import PredictError, predict_floor

from test_cascade import test_cfg as base_cfg
from test_kpi import (make_room, predict_room, room_with_antenna_at_exact_port)

NR_BAND_IDS = ["N71_600", "N41_2500", "N77_CBAND"]

NR_FREQS = {"N71_600": 634.5, "N41_2500": 2593.0, "N77_CBAND": 3840.0}

PL_2G_1M = itu_p1238_pl_db(2000.0, 1.0, 30.0, 0, 15.4)  # 38.0206 dB
PL_2G_001M = itu_p1238_pl_db(2000.0, 0.01, 30.0, 0, 15.4)  # -21.9794 dB


def nr_band(bid="NR2G", freq=2000.0, tech="5G NR", **extra):
    band = {"id": bid, "label": "NR test %s" % bid, "freq_mhz": freq,
            "ambient_dbm": -85.0, "technology": tech}
    band.update(extra)
    return band


def phase3_cfg(nr_bands=(nr_band("NR2G", 2000.0),), nr_preset=True, extra=None):
    """base_cfg + NR test bands + the SS-RSRP preset (mirrors default.json)."""
    cfg = base_cfg()
    for b in nr_bands:
        cfg["bands"].append(b)
    if nr_preset:
        cfg["kpi_presets"]["nr_ssrsp"] = {
            "name": "5G NR SS-RSRP",
            "technology": "5G NR",
            "threshold_dbm": -95.0,
            "min_area_pct": 95.0,
            "class_table": [
                {"name": "excellent", "min_dbm": -80.0},
                {"name": "good", "min_dbm": -90.0},
                {"name": "fair", "min_dbm": -100.0},
                {"name": "edge", "min_dbm": -110.0},
            ],
        }
    if extra:
        extra(cfg)
    return cfg


# ------------------------------------------------------------------- config data

def test_default_config_carries_phase3_nr_bands():
    cfg = load_config()
    ids = [b["id"] for b in cfg["bands"]]
    assert ids[:9] == [  # phase-1/2 set untouched and still first
        "B12_700", "B5_850", "B2_1900", "B66_2100",
        "B14_FIRSTNET", "VHF_150", "UHF_450", "PS700", "PS800"]
    assert ids[9:] == NR_BAND_IDS
    freq = {b["id"]: b["freq_mhz"] for b in cfg["bands"]}
    assert freq["N71_600"] == pytest.approx(634.5)   # n71 DL 617-652 mid
    assert freq["N41_2500"] == pytest.approx(2593.0)  # n41 2496-2690 mid
    assert freq["N77_CBAND"] == pytest.approx(3840.0)  # C-band 3.7-3.98 mid
    for bid in NR_BAND_IDS:
        band = next(b for b in cfg["bands"] if b["id"] == bid)
        assert band["technology"] == "5G NR"
        assert band["ambient_dbm"] == -85.0
    # n78 is a documented config option, not a shipped preset
    assert "N78_3300" not in ids
    assert "n78" in cfg["comment"]
    # validity window widened so C-band (3.84 GHz) is inside P.1238
    assert cfg["itu_p1238_valid_mhz"] == pytest.approx([300.0, 6000.0])
    # cable anchors extended past 2 GHz so n41/n77 runs interpolate
    for cable in cfg["cables"].values():
        anchors = cable["loss_db_per_100m_ghz"]
        assert "3.0" in anchors and "4.0" in anchors
    nrp = cfg["kpi_presets"]["nr_ssrsp"]
    assert nrp["threshold_dbm"] == -95.0 and nrp["min_area_pct"] == 95.0
    assert nrp["technology"] == "5G NR"
    assert [c["min_dbm"] for c in nrp["class_table"]] == [-80.0, -90.0, -100.0, -110.0]


def test_n78_3350_works_as_config_option():
    """Documented phase-3 path: n78 3.3 GHz (or any added NR band) is a plain
    config/data entry with zero code changes."""
    cfg = phase3_cfg(
        nr_bands=(nr_band("N78_3300", 3350.0), nr_band("NR2G", 2000.0)),
        nr_preset=True)
    cfg["itu_p1238_valid_mhz"] = [300.0, 6000.0]
    model = room_with_antenna_at_exact_port(-60.0)
    pr = predict_room(model, cfg, "L0", "N78_3300")
    # hand math: PL(1 m) = 20log10(3350) - 28 = 42.5009 dB
    assert pr["values_dbm"][1] == pytest.approx(-60.0 - 42.5009, abs=0.02)
    assert [p["preset"] for p in pr["kpi"]["presets"]] == [
        "single_tier", "nfpa_two_tier", "nfpa_two_tier", "nr_ssrsp"]
    assert pr["band"]["technology"] == "5G NR"
    solve = solve_cascade(model, cfg)
    assert solve["errors"] == []
    assert set(solve["bands"]) == {"T2G", "NR2G", "N78_3300"}


# ------------------------------------------------------------------- SS-RSRP preset

def test_nr_band_gets_ssrsp_preset_lte_does_not():
    cfg = phase3_cfg()
    model = room_with_antenna_at_exact_port(-90.0)
    pr = predict_room(model, cfg, "L0", "NR2G")
    presets = pr["kpi"]["presets"]
    assert [p["preset"] for p in presets] == [
        "single_tier", "nfpa_two_tier", "nfpa_two_tier", "nr_ssrsp"]
    nrp = presets[-1]
    assert nrp["threshold_dbm"] == -95.0
    assert nrp["min_area_pct"] == 95.0
    assert nrp["scope"] == "whole_floor"
    assert nrp["ss_rsrp_offset_db"] == 0.0
    assert set(nrp["rsrp_class_pct"]) == {"excellent", "good", "fair", "edge", "none"}
    # 1-m point sits at -90 - 38.0206 = -128.0206 -> all non-coverage:
    # single tier (-95): only (0,0) (=-90 + 21.9794) covers -> 11.11 %
    assert nrp["covered_pct"] == pytest.approx(100.0 / 9, abs=0.02)
    assert pr["kpi"]["presets"][0]["covered_pct"] == pytest.approx(100.0 / 9, abs=0.02)
    # same model, non-NR band: no nr_ssrsp entry ever
    pr_lte = predict_room(model, cfg, "L0", "T2G")
    assert [p["preset"] for p in pr_lte["kpi"]["presets"]] == [
        "single_tier", "nfpa_two_tier", "nfpa_two_tier"]


def test_rsrp_class_percentages_hand_math():
    """EIRP = -60 dBm @2 GHz NR band, no walls. Hand math per grid point
    (index = row*3+col), PL = 20log10(2000) + 30log10(d) - 28:
      (0,0) d=0.01: RSSI = -60 - (-21.9794)  = -38.0206 -> excellent (>= -80)
      (1,0)/(0,1) d=1:    -98.0206 -> fair (-100 .. -90)
      (1,1) d=1.414:      -102.5360 -> edge (-110 .. -100)
      (2,0)/(0,2) d=2:    -107.0516 -> edge
      (2,1)/(1,2) d=2.236:-108.5052 -> edge
      (2,2) d=2.828:      -111.5670 -> none (< -110)
    => class % = 11.11 / 0 / 22.22 / 55.56 / 11.11."""
    cfg = phase3_cfg(nr_bands=(nr_band("NR2G", 2000.0),))
    model = room_with_antenna_at_exact_port(-60.0)
    pr = predict_room(model, cfg, "L0", "NR2G")
    nrp = pr["kpi"]["presets"][-1]
    pct = nrp["rsrp_class_pct"]
    assert pct["excellent"] == pytest.approx(100.0 / 9, abs=0.02)
    assert pct["good"] == 0.0
    assert pct["fair"] == pytest.approx(200.0 / 9, abs=0.02)
    assert pct["edge"] == pytest.approx(500.0 / 9, abs=0.02)
    assert pct["none"] == pytest.approx(100.0 / 9, abs=0.02)
    assert sum(pct.values()) == pytest.approx(100.0, abs=0.05)
    # spot check two grid values from the docstring hand math (values are
    # rounded to 0.1 dB, hence abs=0.11)
    assert pr["values_dbm"][1] == pytest.approx(-98.0206, abs=0.11)
    assert pr["values_dbm"][8] == pytest.approx(-111.5670, abs=0.11)


def test_ssrsp_offset_db_converts_rssi_to_rsrp():
    """Per-band ss_rsrp_offset_db (e.g. -10log10(#RE) of the carrier) applies
    to the SS-RSRP estimate only (single/NFPA tiers stay on composite RSSI).
    With offset -10 the 1-m points read -108.0206 and the diagonal
    (1,1) reads -112.5360. Against the preset threshold -95 the SS-RSRP
    coverage is then (0,0) only; the raw single tier also covers (0,0) only,
    but its value set is unshifted:
      single tier raw: (0,0) -38.0206 >= -95 -> 1/9
      nr preset RSRP : (0,0) -48.0206 >= -95 -> 1/9
    classes on RSRP: excellent 1/9 (-48.0206), edge 2/9 (1-m points
    -108.0206 >= -110), none 6/9."""
    cfg = phase3_cfg(nr_bands=(nr_band("NR2G", 2000.0, ss_rsrp_offset_db=-10.0),))
    model = room_with_antenna_at_exact_port(-60.0)
    pr = predict_room(model, cfg, "L0", "NR2G")
    nrp = pr["kpi"]["presets"][-1]
    assert nrp["ss_rsrp_offset_db"] == -10.0
    assert nrp["covered_pct"] == pytest.approx(100.0 / 9, abs=0.02)
    pct = nrp["rsrp_class_pct"]
    assert pct["excellent"] == pytest.approx(100.0 / 9, abs=0.02)  # -48.0206
    assert pct["good"] == 0.0
    assert pct["fair"] == 0.0
    assert pct["edge"] == pytest.approx(200.0 / 9, abs=0.02)  # two 1-m points
    assert pct["none"] == pytest.approx(600.0 / 9, abs=0.02)
    # unshifted tiers: single-tier coverage is (0,0) only at -95 raw
    assert pr["kpi"]["presets"][0]["covered_pct"] == pytest.approx(100.0 / 9, abs=0.02)
    # and the shifted threshold arithmetic is exact: RSRP(1,0) = -108.0206
    assert pr["values_dbm"][1] - 10.0 == pytest.approx(-108.0206, abs=0.11)


def test_nr_preset_pass_flag_and_threshold_override():
    cfg = phase3_cfg(nr_bands=(nr_band("NR2G", 2000.0),))
    hot = with_overrides(cfg, {"kpi_presets": {"nr_ssrsp": {
        "threshold_dbm": -150.0, "min_area_pct": 50.0}}})
    pr = predict_room(room_with_antenna_at_exact_port(-60.0), hot, "L0", "NR2G")
    nrp = pr["kpi"]["presets"][-1]
    assert nrp["covered_pct"] == 100.0 and nrp["pass"] is True
    # non-match technology: band without "5G NR" tech is never NR-evaluated
    cfg_off = phase3_cfg(nr_bands=(nr_band("NR2G", 2000.0, tech="LTE"),))
    pr_off = predict_room(room_with_antenna_at_exact_port(-60.0), cfg_off, "L0", "NR2G")
    assert [p["preset"] for p in pr_off["kpi"]["presets"]].count("nr_ssrsp") == 0


def test_nr_bands_inside_p1238_window_no_warning_upper_limit():
    cfg = phase3_cfg(
        nr_bands=(nr_band("N77_C", 3840.0), nr_band("X7G", 7000.0)),
        nr_preset=True)
    cfg["itu_p1238_valid_mhz"] = [300.0, 6000.0]
    model = room_with_antenna_at_exact_port(-60.0)
    pr_c = predict_room(model, cfg, "L0", "N77_C")
    assert pr_c["model_warnings"] == []
    pr_x = predict_room(model, cfg, "L0", "X7G")
    assert any("outside the ITU-R P.1238 valid range" in w
               for w in pr_x["model_warnings"])


# ------------------------------------------------------------------- n41/n77 hand math

def test_n41_n77_wall_scaling_hand_math():
    """Concrete wall x=1 (tabulated 15 dB @2 GHz), antenna at (0,0):
    scaled loss 15*log10(f)/log10(2000): 15.5178 dB @2600, 16.2873 dB @3840.
    Only the x=2 column rays strictly cross the wall."""
    cfg = phase3_cfg(nr_bands=(nr_band("FA1", 2600.0), nr_band("FB1", 3840.0)),
                     nr_preset=False)
    model = room_with_antenna_at_exact_port(-60.0)
    model["floors"][0]["walls"] = [
        {"id": "w", "x1": 1, "y1": -1, "x2": 1, "y2": 4, "material": "concrete"}]
    loss_fa = 15.0 * math.log10(2600.0) / math.log10(2000.0)
    loss_fb = 15.0 * math.log10(3840.0) / math.log10(2000.0)
    solve = solve_cascade(model, cfg)
    pr = predict_floor(model, cfg, "L0", "FA1", solve_result=solve)
    pr_fb = predict_floor(model, cfg, "L0", "FB1", solve_result=solve)
    for i in range(9):
        x, y = i % 3, i // 3
        d = math.hypot(x, y) if (x or y) else 0.01
        pl_fa = 20 * math.log10(2600.0) + 30 * math.log10(d) - 28
        pl_fb = 20 * math.log10(3840.0) + 30 * math.log10(d) - 28
        assert pr["values_dbm"][i] == pytest.approx(
            -60.0 - pl_fa - (loss_fa if x == 2 else 0.0), abs=0.11), (x, y)
        assert pr_fb["values_dbm"][i] == pytest.approx(
            -60.0 - pl_fb - (loss_fb if x == 2 else 0.0), abs=0.11), (x, y)
        # crossing-column delta carries exactly the extra scaled wall dB
        # (values are rounded to 0.1 dB each, hence abs=0.21)
        assert (pr["values_dbm"][i] - pr_fb["values_dbm"][i]) == pytest.approx(
            (20 * math.log10(3840.0) - 20 * math.log10(2600.0))
            + (loss_fb - loss_fa) * (1 if x == 2 else 0), abs=0.21)


# ------------------------------------------------------------------- composite view

def comp_band(bid, freq, tech="5G NR", **extra):
    band = {"id": bid, "label": "band %s" % bid, "freq_mhz": freq,
            "ambient_dbm": -85.0, "technology": tech}
    band.update(extra)
    return band


def comp_cfg(bands, walls_material=None, nr_preset=True):
    cfg = base_cfg()
    if walls_material:
        cfg["wall_materials"][walls_material] = {"label": walls_material, "loss_db": 30.0}
    for b in bands:
        cfg["bands"].append(b)
    if nr_preset:
        cfg["kpi_presets"]["nr_ssrsp"] = {
            "name": "5G NR SS-RSRP",
            "technology": "5G NR",
            "threshold_dbm": -95.0,
            "min_area_pct": 95.0,
            "class_table": [
                {"name": "excellent", "min_dbm": -80.0},
                {"name": "good", "min_dbm": -90.0},
                {"name": "fair", "min_dbm": -100.0},
                {"name": "edge", "min_dbm": -110.0},
            ],
        }
    return cfg


def room_for_bands(eirp_by_band, walls=None):
    """One antenna at (0,0); per-band repeater gains deliver each EIRP."""
    model = make_room([("A1", (0, 0), "L0")])
    rep = model["floors"][0]["elements"][1]
    rep["max_dl_out_dbm"] = 60.0
    rep["dl_gain_db_per_band"] = {bid: e + 74.0 for bid, e in eirp_by_band.items()}
    if walls:
        model["floors"][0]["walls"] = walls
    return model


def multiband(model, cfg, bids):
    solve = solve_cascade(model, cfg)
    assert solve["errors"] == [], solve["errors"]
    return predict_floor_multiband(model, cfg, "L0", bids, solve_result=solve)


def test_composite_no_walls_best_band_hand_math():
    """No walls: at every point value_A - value_B = (Ea - Eb)
    + 20log10(fB/fA). EA1@2600 = -60, FB1@3840 = 5 dB hotter:
    delta = -5 + 3.3872 = -1.6128 -> FB1 (n77) serves all 9 points."""
    cfg = comp_cfg([comp_band("EA1", 2600.0), comp_band("FB1", 3840.0)])
    model = room_for_bands({"EA1": -60.0, "FB1": -55.0})
    solve = solve_cascade(model, cfg)
    multi = predict_floor_multiband(model, cfg, "L0", ["EA1", "FB1"],
                                    solve_result=solve)
    va = predict_floor(model, cfg, "L0", "EA1", solve_result=solve)["values_dbm"]
    vb = predict_floor(model, cfg, "L0", "FB1", solve_result=solve)["values_dbm"]
    assert set(multi["best_band"]) == {"FB1"}
    assert set(multi["best_server"]) == {"A1"}
    delta = -5.0 + 20 * math.log10(3840.0 / 2600.0)
    for i in range(9):
        assert va[i] - vb[i] == pytest.approx(delta, abs=0.05)
        assert multi["values_dbm"][i] == pytest.approx(max(va[i], vb[i]), abs=1e-9)
        assert multi["best_band"][i] == "FB1"
        assert multi["best_server"][i] == "A1"
    union = multi["union_kpi"]
    # both thresholds -95: A covers (0,0) (-60 + 19.7005 = -40.2995) only;
    # B covers (0,0) (-55 + 16.3135 = -38.6865) but (1,0) = -98.6866 < -95
    # -> union is 1/9
    assert union["n_covered"] == 1
    assert union["covered_pct"] == pytest.approx(100.0 / 9, abs=0.05)


def test_composite_best_band_flips_across_wall_hand_math():
    """Metal wall (30 dB @2 GHz tabulated) on x=1. EA1 2600 @Ea = -60,
    FB1 3840 @Eb = Ea + 4 = -56.
    A - B = (Ea - Eb) + 20log10(fB/fA) + (wallB - wallA)
          non-cross:  -4 + 3.3872              = -0.6128  -> FB1 serves
          x=2 column: -4 + 3.3872 + 1.5391     = +0.9263  -> EA1 serves
    (n77's extra wall loss flips the serving band behind the wall)."""
    wall = [{"id": "w", "x1": 1, "y1": -1, "x2": 1, "y2": 4, "material": "metal"}]
    cfg = comp_cfg([comp_band("EA1", 2600.0), comp_band("FB1", 3840.0)],
                   walls_material="metal")
    model = room_for_bands({"EA1": -60.0, "FB1": -56.0}, walls=wall)
    multi = multiband(model, cfg, ["EA1", "FB1"])
    for i in range(9):
        x, y = i % 3, i // 3
        serving = "EA1" if x == 2 else "FB1"
        assert multi["best_band"][i] == serving, (x, y, multi["best_band"][i])
        assert multi["best_server"][i] == "A1"
    # hand-checked composite values at (1,0) and (2,0):
    wall_fa = 30.0 * math.log10(2600.0) / math.log10(2000.0)
    wall_fb = 30.0 * math.log10(3840.0) / math.log10(2000.0)
    assert multi["values_dbm"][1] == pytest.approx(-56.0 - 43.6866, abs=0.11)  # FB1
    assert multi["values_dbm"][2] == pytest.approx(
        -60.0 - (20 * math.log10(2600.0) + 9.0309 - 28.0) - wall_fa, abs=0.11)  # EA1
    # per band grids keep their hand values on the crossing column
    va = multiband(model, cfg, ["EA1"])["values_dbm"]
    vb = multiband(model, cfg, ["FB1"])["values_dbm"]
    assert va[2] - vb[2] == pytest.approx(0.9263, abs=0.21)
    assert va[0] - vb[0] == pytest.approx(-0.6128, abs=0.21)
    # union KPI at -95: EA1 covers (0,0) (-60 + 19.7005 = -40.2995) and
    # FB1 covers (0,0) (-56 + 16.3135 = -39.6865); every other point is
    # below -95 on both bands -> union = 1/9
    assert multi["union_kpi"]["n_covered"] == 1
    assert multi["union_kpi"]["covered_pct"] == pytest.approx(100.0 / 9, abs=0.05)


def test_composite_union_kpi_hand_math():
    """No walls: EA1 2600 @ -56 covers only (0,0) ((0,0): -56 + 19.7005 =
    -36.2995 >= -95; (1,0): -56 - 40.2995 = -96.2995 < -95, margin 1.3 dB).
    FB1 3840 @ -9 covers all 9 (worst: (2,2) = -9 - 57.2328 = -66.2328).
    Union (OR) = 100 %; per-band KPIs stay A=11.11 % / B=100 %."""
    cfg = comp_cfg([comp_band("EA1", 2600.0), comp_band("FB1", 3840.0)])
    model = room_for_bands({"EA1": -56.0, "FB1": -9.0})
    multi = multiband(model, cfg, ["EA1", "FB1"])
    pb = {entry["band"]["id"]: entry for entry in multi["per_band"]}
    assert pb["EA1"]["kpi"]["covered_pct"] == pytest.approx(100.0 / 9, abs=0.05)
    assert pb["FB1"]["kpi"]["covered_pct"] == 100.0
    assert multi["union_kpi"]["covered_pct"] == 100.0
    assert multi["union_kpi"]["n_covered"] == 9
    assert multi["union_kpi"]["pass"] is True
    assert multi["union_kpi"]["threshold_dbm"] == -95.0  # uniform thresholds
    # per_band entries expose the NR SS-RSRP blocks (technology opt-in)
    assert pb["EA1"]["kpi"]["presets"][-1]["preset"] == "nr_ssrsp"


def test_composite_values_equal_elementwise_max_of_single_band_predicts():
    cfg = comp_cfg([comp_band("EA1", 2600.0), comp_band("FB1", 3840.0)])
    model = room_for_bands({"EA1": -55.0, "FB1": -55.0})
    solve = solve_cascade(model, cfg)
    va = predict_floor(model, cfg, "L0", "EA1", solve_result=solve)["values_dbm"]
    vb = predict_floor(model, cfg, "L0", "FB1", solve_result=solve)["values_dbm"]
    multi = predict_floor_multiband(model, cfg, "L0", ["EA1", "FB1"],
                                    solve_result=solve)
    for i in range(9):
        expected = va[i] if (vb[i] is None or va[i] >= vb[i]) else vb[i]
        assert multi["values_dbm"][i] == pytest.approx(expected, abs=1e-9)
        assert multi["best_band"][i] == ("EA1" if expected == va[i] else "FB1")


def test_composite_single_band_equals_plain_predict_and_dedupes():
    cfg = comp_cfg([comp_band("EA1", 2600.0)])
    model = room_for_bands({"EA1": -60.0})
    solve = solve_cascade(model, cfg)
    single = predict_floor(model, cfg, "L0", "EA1", solve_result=solve)
    multi = predict_floor_multiband(model, cfg, "L0", ["EA1", "EA1"],
                                    solve_result=solve)
    assert multi["bands"] == ["EA1"]
    assert multi["values_dbm"] == single["values_dbm"]
    assert multi["best_server"] == single["best_server"]


def test_composite_input_validation():
    cfg = comp_cfg([comp_band("EA1", 2600.0), comp_band("FB1", 3840.0)])
    model = room_for_bands({"EA1": -60.0})
    solve = solve_cascade(model, cfg)
    with pytest.raises(PredictError):
        predict_floor_multiband(model, cfg, "L0", [], solve_result=solve)
    with pytest.raises(PredictError):
        predict_floor_multiband(model, cfg, "L0", ["EA1", "ZZZ"],
                                solve_result=solve)
    with pytest.raises(PredictError):
        predict_floor_multiband(model, cfg, "nope", ["EA1"], solve_result=solve)
    # comma-joined string selection is the documented convenience form
    multi = predict_floor_multiband(model, cfg, "L0", "EA1,FB1",
                                    solve_result=solve)
    assert multi["bands"] == ["EA1", "FB1"]


def test_composite_heatmap_view_shapes_for_renderer():
    cfg = comp_cfg([comp_band("EA1", 2600.0), comp_band("FB1", 3840.0)])
    model = room_for_bands({"EA1": -60.0, "FB1": -55.0})
    multi = multiband(model, cfg, ["EA1", "FB1"])
    view = composite_heatmap_view(multi)
    assert view["band"]["id"] == "MULTI"
    assert view["kpi"]["covered_pct"] == multi["union_kpi"]["covered_pct"]
    assert view["kpi"]["presets"][0]["pass"] == multi["union_kpi"]["pass"]
    assert view["grid"] == multi["grid"]
    assert len(view["values_dbm"]) == 9
    assert view["floor_geom"]["id"] == "L0"


# ------------------------------------------------------------------- composite API

def test_api_composite_endpoint_example_project(client):
    pid = next(p["id"] for p in client.get("/projects").json()["projects"]
               if p["example"])
    fid = client.get("/projects/%d" % pid).json()["model"]["floors"][0]["id"]
    url = ("/projects/%d/floors/%s/composite" % (pid, fid))
    r = client.get(url + "?bands=N71_600,N41_2500,N77_CBAND&step=4")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["view"] == "multiband_composite"
    assert body["bands"] == ["N71_600", "N41_2500", "N77_CBAND"]
    assert [e["band"]["id"] for e in body["per_band"]] == body["bands"]
    n = body["grid"]["n_points"]
    assert len(body["values_dbm"]) == n == len(body["best_band"]) == len(body["best_server"])
    for bb in body["best_band"]:
        assert bb in body["bands"]
    assert 0.0 <= body["union_kpi"]["covered_pct"] <= 100.0
    assert body["union_kpi"]["rule"].startswith("OR:")
    # each NR band carries its SS-RSRP block end to end
    for entry in body["per_band"]:
        assert entry["kpi"]["presets"][-1]["preset"] == "nr_ssrsp"
        assert entry["n_points"] == n
    # selection order preserved; duplicates collapse
    r2 = client.get(url + "?bands=N77_CBAND,N71_600,N77_CBAND&step=4")
    assert r2.status_code == 200
    assert r2.json()["bands"] == ["N77_CBAND", "N71_600"]
    assert [e["band"]["id"] for e in r2.json()["per_band"]] == ["N77_CBAND", "N71_600"]
    # POST alias
    rp = client.post(url + "?bands=N71_600&step=4")
    assert rp.status_code == 200 and rp.json()["bands"] == ["N71_600"]
    # mixed LTE + NR selection works
    rm = client.get(url + "?bands=B12_700,N71_600&step=4")
    assert rm.status_code == 200
    assert rm.json()["bands"] == ["B12_700", "N71_600"]


def test_api_composite_errors_and_png(client):
    pid = next(p["id"] for p in client.get("/projects").json()["projects"]
               if p["example"])
    fid = client.get("/projects/%d" % pid).json()["model"]["floors"][0]["id"]
    base = "/projects/%d/floors/%s/composite" % (pid, fid)
    r_bad = client.get(base + "?bands=ZZZ_900&step=4")
    assert r_bad.status_code == 400 and "unknown band" in r_bad.json()["detail"]
    assert "N71_600" in r_bad.json()["detail"]  # available list in the error
    assert client.get(base + "?step=4").status_code == 422  # missing bands
    assert client.get(base + "?bands=&step=4").status_code == 400
    assert client.get(base + "?bands=,,&step=4").status_code == 400
    r_nofloor = client.get("/projects/%d/floors/nope/composite?bands=N71_600" % pid)
    assert r_nofloor.status_code == 400  # unknown floor surfaces like predict's 400
    r_noproj = client.get("/projects/999999/floors/%s/composite?bands=N71_600" % fid)
    assert r_noproj.status_code == 404
    png = client.get(base + ".png?bands=N71_600,N41_2500&step=4")
    assert png.status_code == 200
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert "composite-" in png.headers["content-disposition"]
    w, h = struct.unpack(">II", png.content[16:24])
    assert w > 0 and h > 0


def test_api_nr_predict_carries_rsrp_on_example(client):
    pid = next(p["id"] for p in client.get("/projects").json()["projects"]
               if p["example"])
    fid = client.get("/projects/%d" % pid).json()["model"]["floors"][0]["id"]
    pr = client.post("/projects/%d/floors/%s/predict?band=N41_2500&step=4" % (pid, fid))
    assert pr.status_code == 200, pr.text
    body = pr.json()
    assert body["band"]["technology"] == "5G NR"
    assert body["n_exponent"] == 30.0
    nrp = [p for p in body["kpi"]["presets"] if p["preset"] == "nr_ssrsp"][0]
    assert nrp["threshold_dbm"] == -95.0
    assert set(nrp["rsrp_class_pct"]) == {"excellent", "good", "fair", "edge", "none"}
    # LTE band keeps its phase-1/2 shape (no NR block, no technology)
    pr_lte = client.post("/projects/%d/floors/%s/predict?band=B12_700&step=4" % (pid, fid))
    assert pr_lte.status_code == 200
    assert pr_lte.json()["band"]["technology"] is None
    assert all(p["preset"] != "nr_ssrsp" for p in pr_lte.json()["kpi"]["presets"])
