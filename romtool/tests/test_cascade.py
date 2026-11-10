"""Cascade arithmetic hand-example: every number below is checked against
pencil math at exact config anchors (f=2000 MHz, cable exactly 20.0 dB/100m).

Chain:  SRC (off-air) --1.0 m testc--> REP --2.0 m testc--> SP(2-way il 3.2)
        --1.0 m testc--> A1     (omni, 2 dBi)
        --1.5 m testc--> A2

Hand math:
  source out  = -85 + 11 - 0                                   = -74.0 dBm
  feeder loss 1.0 m @20 dB/100m                               =  0.2 dB
  repeater in = -74.0 - 0.2                                    = -74.2 dBm
  repeater out = min(-74.2 + 25, 33)                           = -49.2 dBm
  trunk loss 2.0 m                                            =  0.4 dB
  splitter in  = -49.2 - 0.4                                   = -49.6 dBm
  per branch  = -49.6 - 3.2                                    = -52.8 dBm
  A1 in = -52.8 - 0.2 = -53.0 dBm, port = -53.0, EIRP = -51.0 dBm
  A2 in = -52.8 - 0.3 = -53.1 dBm, EIRP = -51.1 dBm
"""

import math

import pytest

from romtool.cascade import build_graph, cable_loss_db, solve_cascade


def test_cfg():
    return {
        "bands": [{"id": "T2G", "label": "test 2 GHz", "freq_mhz": 2000.0, "ambient_dbm": -85.0}],
        "cables": {"testc": {"label": "test cable", "loss_db_per_100m_ghz": {"2.0": 20.0}}},
        "parts": {
            "splitters": {
                "2way": {"label": "Splitter, 2-way", "il_db": 3.2},
                "3way": {"label": "Splitter, 3-way", "il_db": 5.4},
                "4way": {"label": "Splitter, 4-way", "il_db": 7.6},
            },
            "tappers": {"default": {"label": "Tap", "ways": 2, "through_il_db": 0.5, "tap_loss_db": 9.0}},
            "couplers": {"default": {"label": "Coupler", "through_il_db": 0.4, "coupled_loss_db": 8.0}},
        },
        "antennas": {"omni_test": {"label": "Omni test", "gain_db": 2.0, "max_port_dbm": 20.0}},
        "repeater": {"label": "REP", "dl_gain_db": 25.0, "max_dl_out_dbm": 33.0, "ports": 4},
        "source": {"label": "SRC", "subtype_default": "off_air", "donor_gain_db": 11.0, "feeder_loss_db": 0.0},
        "environments": {"office": {"n": 30.0}},
        "default_environment": "office",
        "floor_loss_db_per_floor": 15.4,
        "wall_loss_ref_mhz": 2000.0,
        "wall_materials": {"concrete": {"label": "Concrete", "loss_db": 15.0}},
        "kpi_presets": {
            "single_tier": {"name": "Single tier", "threshold_dbm": -95.0, "min_area_pct": 95.0},
            "nfpa_two_tier": [
                {"tier": 1, "name": "general", "threshold_dbm": -95.0, "min_area_pct": 95.0},
                {"tier": 2, "name": "critical-area", "threshold_dbm": -95.0, "min_area_pct": 99.0},
            ],
        },
        "report": {"color_scale": [{"dbm": -55, "rgb": [0, 200, 0]}, {"dbm": -125, "rgb": [30, 30, 30]}]},
    }


def chain_model(extra=None, ways=2):
    els = [
        {"id": "src", "type": "source", "label": "SRC", "x": 90, "y": 50},
        {"id": "rep", "type": "repeater", "label": "REP", "x": 80, "y": 50},
        {"id": "sp", "type": "splitter", "label": "SP", "x": 50, "y": 50, "ways": ways},
        {"id": "a1", "type": "antenna", "label": "A1", "antenna_id": "omni_test", "x": 10, "y": 10},
        {"id": "a2", "type": "antenna", "label": "A2", "antenna_id": "omni_test", "x": 10, "y": 90},
        {"id": "r_fd", "type": "cable_run", "from_id": "src", "to_id": "rep",
         "cable_id": "testc", "length_m": 1.0},
        {"id": "r_tr", "type": "cable_run", "from_id": "rep", "to_id": "sp",
         "cable_id": "testc", "length_m": 2.0},
        {"id": "r_b1", "type": "cable_run", "from_id": "sp", "to_id": "a1",
         "cable_id": "testc", "length_m": 1.0},
        {"id": "r_b2", "type": "cable_run", "from_id": "sp", "to_id": "a2",
         "cable_id": "testc", "length_m": 1.5},
    ]
    model = {"environment": "office", "floors": [
        {"id": "f1", "name": "Ground", "level": 0, "ppm": 10.0,
         "w_px": 100, "h_px": 100, "walls": [], "elements": els}
    ]}
    if extra:
        extra(model, els)
    return model


def ant(solve, band="T2G", label=None):
    for a in solve["bands"][band]["antennas"]:
        if label is None or a["label"] == label:
            return a
    raise AssertionError("antenna not found")


def test_hand_example_port_powers():
    solve = solve_cascade(chain_model(), test_cfg())
    assert solve["errors"] == []
    a1 = ant(solve)  # first antenna = A1
    assert a1["in_dbm"] == pytest.approx(-53.0, abs=1e-9)
    assert a1["port_dbm"] == pytest.approx(-53.0, abs=1e-9)
    assert a1["eirp_dbm"] == pytest.approx(-51.0, abs=1e-9)
    assert a1["capped"] is False
    a2 = ant(solve, label="A2")
    assert a2["in_dbm"] == pytest.approx(-53.1, abs=1e-9)
    assert a2["eirp_dbm"] == pytest.approx(-51.1, abs=1e-9)


def test_hand_example_node_trace():
    solve = solve_cascade(chain_model(), test_cfg())
    band = solve["bands"]["T2G"]
    nodes = {n["element_id"]: n for n in band["nodes"]}
    edges = {e["element_id"]: e for e in band["edges"]}
    assert nodes["src"]["out_dbm"] == pytest.approx(-74.0, abs=1e-9)
    assert edges["r_fd"]["loss_db"] == pytest.approx(0.2, abs=1e-9)
    assert nodes["rep"]["in_dbm"] == pytest.approx(-74.2, abs=1e-9)
    assert nodes["rep"]["out_dbm"] == pytest.approx(-49.2, abs=1e-9)  # no clamp (33 max)
    assert edges["r_tr"]["loss_db"] == pytest.approx(0.4, abs=1e-9)
    assert nodes["sp"]["in_dbm"] == pytest.approx(-49.6, abs=1e-9)
    assert nodes["sp"]["out_dbm"] == pytest.approx(-52.8, abs=1e-9)


def test_below_zero_budget_warns():
    # SPEC: loss budget < 0 dBm out => warn. Chain outputs are all negative here.
    solve = solve_cascade(chain_model(), test_cfg())
    warns = solve["bands"]["T2G"]["warnings"]
    assert any("A1" in w and "below the 0 dBm budget" in w for w in warns)
    assert any("A2" in w for w in warns)


def test_donor_ambient_override():
    def override(model, els):
        model["floors"][0]["elements"][0]["ambient_dbm"] = {"T2G": -70.0}
    solve = solve_cascade(chain_model(extra=override), test_cfg())
    a1 = ant(solve)
    # source out = -70 + 11 = -59; rep in = -59.2; rep out = -34.2; SP in -34.6;
    # branch -37.8; A1 in -38.0 -> EIRP -36.0
    assert a1["in_dbm"] == pytest.approx(-38.0, abs=1e-9)
    assert a1["eirp_dbm"] == pytest.approx(-36.0, abs=1e-9)


def test_donor_feeder_loss_param():
    def override(model, els):
        model["floors"][0]["elements"][0]["feeder_loss_db"] = 3.0
    solve = solve_cascade(chain_model(extra=override), test_cfg())
    a1 = ant(solve)
    # every level shifts by -3 dB vs the hand example
    assert a1["in_dbm"] == pytest.approx(-56.0, abs=1e-9)


def test_repeater_clamp_warns():
    def override(model, els):
        for el in els:
            if el["id"] == "rep":
                el["max_dl_out_dbm"] = -80.0
    solve = solve_cascade(chain_model(extra=override), test_cfg())
    band = solve["bands"]["T2G"]
    nodes = {n["element_id"]: n for n in band["nodes"]}
    assert nodes["rep"]["out_dbm"] == pytest.approx(-80.0, abs=1e-9)
    assert any("clamped" in w for w in band["warnings"])
    # clamp propagates: A1 in = -80 - 0.4 - 3.2 - 0.2 = -83.8
    assert ant(solve)["in_dbm"] == pytest.approx(-83.8, abs=1e-9)


def test_antenna_port_cap():
    def override(model, els):
        for el in els:
            if el["id"] == "a1":
                el["max_port_dbm"] = -60.0
    solve = solve_cascade(chain_model(extra=override), test_cfg())
    a1 = ant(solve, label="A1")
    assert a1["port_dbm"] == pytest.approx(-60.0, abs=1e-9)
    assert a1["capped"] is True
    assert any("capped" in w for w in solve["bands"]["T2G"]["warnings"])


def test_tapper_and_coupler_roles():
    def extra(model, els):
        els[:] = [e for e in els if e["id"] not in ("sp", "r_tr", "r_b1", "r_b2")]
        els.extend([
            {"id": "tp", "type": "tapper", "label": "TP", "x": 50, "y": 50},
            {"id": "a3", "type": "antenna", "label": "A3", "antenna_id": "omni_test", "x": 30, "y": 30},
            {"id": "r_t1", "type": "cable_run", "from_id": "rep", "to_id": "tp",
             "cable_id": "testc", "length_m": 0.0, "port": "through"},
            {"id": "r_t2", "type": "cable_run", "from_id": "tp", "to_id": "a1",
             "cable_id": "testc", "length_m": 0.0, "port": "tap"},
            {"id": "r_t3", "type": "cable_run", "from_id": "tp", "to_id": "a2",
             "cable_id": "testc", "length_m": 0.0, "port": "tap"},
            {"id": "cp", "type": "coupler", "label": "CP", "x": 70, "y": 80},
            {"id": "a4", "type": "antenna", "label": "A4", "antenna_id": "omni_test", "x": 90, "y": 30},
            {"id": "r_c1", "type": "cable_run", "from_id": "tp", "to_id": "cp",
             "cable_id": "testc", "length_m": 0.0, "port": "through"},
            {"id": "r_c2", "type": "cable_run", "from_id": "cp", "to_id": "a3",
             "cable_id": "testc", "length_m": 0.0, "port": "coupled"},
            {"id": "r_c3", "type": "cable_run", "from_id": "cp", "to_id": "a4",
             "cable_id": "testc", "length_m": 0.0, "port": "through"},
        ])
    solve = solve_cascade(chain_model(extra=extra), test_cfg())
    assert solve["errors"] == []
    ports = {a["label"]: a["in_dbm"] for a in solve["bands"]["T2G"]["antennas"]}
    # rep out -74.2+25 = -49.2; TP in -49.2: tap branches -49.2-9 = -58.2 (A1, A2)
    assert ports["A1"] == pytest.approx(-58.2, abs=1e-9)
    assert ports["A2"] == pytest.approx(-58.2, abs=1e-9)
    # TP through to CP: -49.2-0.5 = -49.7; CP coupled -49.7-8 = -57.7 (A3)
    assert ports["A3"] == pytest.approx(-57.7, abs=1e-9)
    # CP through -49.7-0.4 = -50.1 (A4)
    assert ports["A4"] == pytest.approx(-50.1, abs=1e-9)


def test_splitter_ways_3way_il():
    def extra(model, els):
        els[:] = [e for e in els if e["id"] not in ("a2", "r_b2")]
        els.extend([
            {"id": "a2", "type": "antenna", "label": "A2", "antenna_id": "omni_test", "x": 10, "y": 90},
            {"id": "a3", "type": "antenna", "label": "A3", "antenna_id": "omni_test", "x": 90, "y": 90},
            {"id": "r_b2b", "type": "cable_run", "from_id": "sp", "to_id": "a2",
             "cable_id": "testc", "length_m": 0.5},
            {"id": "r_b3", "type": "cable_run", "from_id": "sp", "to_id": "a3",
             "cable_id": "testc", "length_m": 0.5},
        ])
    solve = solve_cascade(chain_model(extra=extra, ways=3), test_cfg())
    assert solve["errors"] == [], solve["errors"]
    ports = {a["label"]: a["in_dbm"] for a in solve["bands"]["T2G"]["antennas"]}
    # SP in -49.6, 3-way il 5.4 -> branch -55.0; A1 -55.2 (1.0 m run); A2/A3 -55.1 (0.5 m runs)
    assert ports["A1"] == pytest.approx(-55.2, abs=1e-9)
    assert ports["A2"] == pytest.approx(-55.1, abs=1e-9)
    assert ports["A3"] == pytest.approx(-55.1, abs=1e-9)


def test_cable_loss_interpolation_log_domain():
    cfgc = {"loss_db_per_100m_ghz": {"0.7": 10.0, "2.0": 20.0}}
    # at exactly 2 GHz: 20.0
    assert cable_loss_db(cfgc, 100.0, 2000.0) == pytest.approx(20.0, abs=1e-12)
    # at 700: 10.0
    assert cable_loss_db(cfgc, 100.0, 700.0) == pytest.approx(10.0, abs=1e-12)
    # midway in log domain: geometric mean of anchors gives exactly 15.0
    mid = math.pow(700 * 2000.0, 0.5)
    assert cable_loss_db(cfgc, 100.0, mid) == pytest.approx(15.0, abs=1e-9)
    # clamp below first anchor
    assert cable_loss_db(cfgc, 100.0, 400.0) == pytest.approx(10.0, abs=1e-12)
    # 7 m at 751 MHz between 10 and 20 dB/100m
    f = 751.0
    lf = math.log10(f)
    lf0, lf1 = math.log10(700), math.log10(2000)
    per100 = 10 + 10 * (lf - lf0) / (lf1 - lf0)
    assert cable_loss_db(cfgc, 7.0, f) == pytest.approx(per100 * 0.07, abs=1e-9)


def test_graph_validation_errors():
    cfg = test_cfg()

    # orphan antenna: no feeding run
    def orphan(model, els):
        els[:] = [e for e in els if e["id"] not in ("r_b1",)]
    solve = solve_cascade(chain_model(extra=orphan), cfg)
    assert solve["errors"] and "no feeding cable run" in " ".join(solve["errors"])
    assert solve["bands"] == {}

    # two sources, one disconnected island
    def island(model, els):
        els.extend([
            {"id": "src2", "type": "source", "label": "SR2", "x": 5, "y": 5},
            {"id": "a5", "type": "antenna", "label": "A5", "antenna_id": "omni_test", "x": 2, "y": 2},
            {"id": "r_x", "type": "cable_run", "from_id": "src2", "to_id": "a5",
             "cable_id": "testc", "length_m": 1.0},
        ])
    g = build_graph(chain_model(extra=island), cfg)
    assert g["errors"] == []  # two healthy trees: allowed

    # fan-in violation: two runs feed the same splitter
    def two_feeds(model, els):
        els.extend([
            {"id": "r_dup", "type": "cable_run", "from_id": "rep", "to_id": "sp",
             "cable_id": "testc", "length_m": 1.0},
        ])
    g = build_graph(chain_model(extra=two_feeds), cfg)
    assert any("fed by 2 cable runs" in e for e in g["errors"])

    # repeater ports exceeded: repeater feeds two branch splitters with ports=1
    def over_ports(model, els):
        els[:] = [e for e in els if e["id"] not in ("a2", "r_b2")]
        els.extend([
            {"id": "sp2", "type": "splitter", "label": "SP2", "x": 70, "y": 70, "ways": 2},
            {"id": "a3", "type": "antenna", "label": "A3", "antenna_id": "omni_test", "x": 90, "y": 90},
            {"id": "r_sp2", "type": "cable_run", "from_id": "rep", "to_id": "sp2",
             "cable_id": "testc", "length_m": 1.0},
            {"id": "r_a3", "type": "cable_run", "from_id": "sp2", "to_id": "a3",
             "cable_id": "testc", "length_m": 1.0},
        ])
        for el in els:
            if el["id"] == "rep":
                el["ports"] = 1
    g = build_graph(chain_model(extra=over_ports), cfg)
    assert any("only 1 port" in e for e in g["errors"])

    # unknown cable id
    def badcable(model, els):
        for el in els:
            if el["id"] == "r_tr":
                el["cable_id"] = "nope"
    g = build_graph(chain_model(extra=badcable), cfg)
    assert any("unknown cable_id" in e for e in g["errors"])

    # no source at all
    def nosrc(model, els):
        els[:] = [e for e in els if e["id"] != "src"]
        els[0]["id"] = els[0]["id"]  # repeater now has no feeder
    solve = solve_cascade(chain_model(extra=nosrc), cfg)
    assert solve["errors"] and not solve["bands"]
