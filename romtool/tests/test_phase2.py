"""Phase 2: legacy LMR + FirstNet bands, P.1238 low-band overrides, source
subtypes (P25 direct inject) and NFPA critical-area polygon KPI.

Hand-checked numbers reuse the phase-1 synthetic room (tests/test_kpi.py):
3x3 grid at 1 px/m, antenna port tuned so exactly {(0,0),(1,0),(0,1)} reach
the -94.8 dBm threshold (33.33 % whole-floor coverage).
"""

import math
import struct

import pytest

from app.routes import __version__
from romtool.cascade import build_graph, cable_loss_db, solve_cascade
from romtool.config import load_config, with_overrides
from romtool.geometry import point_in_polygon, polygon_membership
from romtool.physics import itu_p1238_pl_db
from romtool.predict import predict_floor

from test_cascade import test_cfg as base_cfg
from test_kpi import (EIRP, THRESH, make_room, predict_room,
                      repeater_gain_for_target, room_with_antenna_at_exact_port)

EXPECTED_BAND_IDS = [
    "B12_700", "B5_850", "B2_1900", "B66_2100",
    "B14_FIRSTNET", "VHF_150", "UHF_450", "PS700", "PS800",
    # phase-3 additions (5G NR), appended to the end of the default set
    "N71_600", "N41_2500", "N77_CBAND"]


def phase2_cfg(extra_band=None):
    cfg = base_cfg()
    cfg["itu_p1238_valid_mhz"] = [300.0, 3000.0]
    cfg["environments"]["residential"] = {"n": 28.0}
    cfg["bands"].append({
        "id": "VHF_TEST", "label": "VHF test 154 MHz", "freq_mhz": 154.0,
        "ambient_dbm": -85.0,
        "n_exponent_overrides": {"office": 34.0, "residential": 32.0},
        "wall_loss_scale_db": 0.0,
    })
    if extra_band:
        cfg["bands"].append(extra_band)
    return cfg


# ------------------------------------------------------------------ bands/config

def test_default_config_carries_phase2_bands():
    cfg = load_config()
    assert [b["id"] for b in cfg["bands"]] == EXPECTED_BAND_IDS
    freq = {b["id"]: b["freq_mhz"] for b in cfg["bands"]}
    assert freq["VHF_150"] == 154.0
    assert freq["UHF_450"] == 453.0
    assert freq["PS700"] == 772.0
    assert freq["PS800"] == 860.0
    assert freq["B14_FIRSTNET"] == 763.0
    # low-band overrides only on the sub-300 MHz bands
    assert "n_exponent_overrides" in next(b for b in cfg["bands"] if b["id"] == "VHF_150")
    assert "n_exponent_overrides" not in next(b for b in cfg["bands"] if b["id"] == "PS700")
    assert cfg["source"]["subtypes"]["direct"]["injection_dbm"] == 5.0


def test_example_model_has_critical_area_and_solves_all_bands():
    from romtool.example_project import build_example_model

    model = build_example_model()
    f1 = model["floors"][0]
    assert len(f1["critical_areas"]) == 1
    assert len(f1["critical_areas"][0]["points"]) == 4
    cfg = load_config()
    solve = solve_cascade(model, cfg)
    assert solve["errors"] == []
    assert set(solve["bands"]) == set(EXPECTED_BAND_IDS)
    # the example polygon covers grid points: predict must evaluate tier 2
    pr = predict_floor(model, cfg, "exf1", "B14_FIRSTNET", step_px=2)
    tier2 = [p for p in pr["kpi"]["presets"] if p.get("tier") == 2][0]
    assert tier2["scope"] == "critical_areas"
    assert tier2["n_points"] > 0
    assert len(tier2["per_polygon"]) == 1


# ------------------------------------------------------------------ P25 direct inject

def test_direct_inject_source_hand_math():
    """SRC(direct, injection 10 dBm) --2 m @20 dB/100m--> antenna (2 dBi, cap 60):
    10 - 0.4 = 9.6 dBm in, port 9.6, EIRP 11.6."""
    cfg = base_cfg()
    model = {
        "environment": "office",
        "floors": [{
            "id": "L0", "name": "G", "level": 0, "ppm": 1.0, "w_px": 3, "h_px": 3,
            "walls": [],
            "elements": [
                {"id": "src", "type": "source", "label": "SRC", "subtype": "direct",
                 "injection_dbm": 10.0, "x": 2, "y": 2},
                {"id": "a1", "type": "antenna", "label": "A1", "antenna_id": "omni_test",
                 "x": 0, "y": 0, "gain_db": 2.0, "max_port_dbm": 60.0},
                {"id": "r1", "type": "cable_run", "from_id": "src", "to_id": "a1",
                 "cable_id": "testc", "length_m": 2.0},
            ],
        }],
    }
    g = build_graph(model, cfg)
    assert g["errors"] == [], g["errors"]
    solve = solve_cascade(model, cfg)
    assert solve["errors"] == []
    src = [n for n in solve["bands"]["T2G"]["nodes"] if n["type"] == "source"][0]
    assert src["subtype"] == "direct" and src["out_dbm"] == pytest.approx(10.0)
    ant = solve["bands"]["T2G"]["antennas"][0]
    assert ant["port_dbm"] == pytest.approx(9.6)
    assert ant["eirp_dbm"] == pytest.approx(11.6)
    # default subtype (off_air) keeps phase-1 arithmetic
    model["floors"][0]["elements"][0] = {
        "id": "src", "type": "source", "label": "SRC", "subtype": "off_air",
        "donor_gain_db": 11.0, "feeder_loss_db": 0.0, "x": 2, "y": 2}
    solve2 = solve_cascade(model, cfg)
    ant2 = solve2["bands"]["T2G"]["antennas"][0]
    assert ant2["port_dbm"] == pytest.approx(-85.0 + 11.0 - 0.4)


def test_unknown_source_subtype_rejected():
    cfg = base_cfg()
    model = make_room([])  # empty antenna list, add a bad source below
    model["floors"][0]["elements"][0]["subtype"] = "puck"
    g = build_graph(model, cfg)
    assert any("unknown subtype 'puck'" in e for e in g["errors"])


# ------------------------------------------------------------------ low-band overrides

def test_p1238_validity_warning_and_silencing():
    cfg = phase2_cfg()
    model = room_with_antenna_at_exact_port(EIRP)
    pr = predict_room(model, cfg, "L0", "VHF_TEST")
    assert any("outside the ITU-R P.1238 valid range" in w for w in pr["model_warnings"])
    cfg2 = phase2_cfg()
    cfg2["itu_p1238_valid_mhz"] = [100.0, 3000.0]
    pr2 = predict_room(model, cfg2, "L0", "VHF_TEST")
    assert pr2["model_warnings"] == []
    # in-range band (2 GHz) never warns
    pr3 = predict_room(model, cfg2, "L0", "T2G")
    assert pr3["model_warnings"] == []


def test_band_n_override_used_and_reported():
    cfg = phase2_cfg()
    model = room_with_antenna_at_exact_port(EIRP)
    pr = predict_room(model, cfg, "L0", "VHF_TEST")
    assert pr["n_exponent"] == 34.0
    assert pr["n_exponent_source"] == "band_override"
    # hand math: PL = 20log10(154) + 34*log10(1) - 28 (1 m, no walls)
    pl_1m = 20 * math.log10(154.0) - 28.0
    assert pr["values_dbm"][1] == pytest.approx(EIRP - pl_1m, abs=0.02)
    # LTE-style band without overrides falls back to the environment N
    pr_t2g = predict_room(model, cfg, "L0", "T2G")
    assert pr_t2g["n_exponent_source"] == "environment"
    assert pr_t2g["n_exponent"] == 30.0
    # non-office environment picks its own override
    cfg_res = with_overrides(cfg, {"environments": {"office": {"n": 30.0}}})
    model_res = room_with_antenna_at_exact_port(EIRP)
    model_res["environment"] = "residential"
    pr_res = predict_room(model_res, cfg_res, "L0", "VHF_TEST")
    assert pr_res["n_exponent"] == 32.0  # residential override for VHF_TEST
    assert pr_res["n_exponent_source"] == "band_override"


def test_band_wall_loss_scale_db_adds_exactly_to_crossings():
    cfg = base_cfg()
    band = {"id": "SC", "label": "scale test", "freq_mhz": 2000.0, "ambient_dbm": -85.0}
    cfg["bands"].append(band)
    model = room_with_antenna_at_exact_port(EIRP)
    model["floors"][0]["walls"] = [
        {"id": "w", "x1": 1, "y1": -1, "x2": 1, "y2": 4, "material": "concrete"}]
    scaled0 = predict_room(model, cfg, "L0", "SC")
    cfg6 = with_overrides(cfg, {"bands": [dict(band, wall_loss_scale_db=6.0)]})
    scaled6 = predict_room(model, cfg6, "L0", "SC")
    for i, (a, b) in enumerate(zip(scaled0["values_dbm"], scaled6["values_dbm"])):
        x, y = i % 3, i // 3
        crossing = x == 2  # ray antenna(0,0)->(x,y) strictly crosses the x=1 wall
        expect = 6.0 if crossing else 0.0
        assert (a - b) == pytest.approx(expect, abs=0.11), (x, y, a, b)
    assert scaled6["wall_loss_scale_db"] == 6.0


# ------------------------------------------------------------------ polygon geometry

SQUARE = [(-0.5, -0.5), (1.5, -0.5), (1.5, 1.5), (-0.5, 1.5)]
L_SHAPE = [(-0.5, -0.5), (1.5, -0.5), (1.5, 0.5), (0.5, 0.5), (0.5, 1.5), (-0.5, 1.5)]


def test_point_in_polygon_basic():
    assert point_in_polygon(0, 0, SQUARE)
    assert point_in_polygon(1, 1, SQUARE)
    assert not point_in_polygon(2, 2, SQUARE)
    assert point_in_polygon(0.4, 0.3, L_SHAPE)
    assert point_in_polygon(1, 1, L_SHAPE) is False  # the notch
    assert not point_in_polygon(-1, 0, SQUARE)  # degenerate <3 pts
    assert point_in_polygon(0, 0, SQUARE)  # back to sanity after degenerate call
    assert not point_in_polygon(0, 0, [(0, 0), (1, 1)])


def test_polygon_membership_counts_exact():
    members, per = polygon_membership(3, 3, 1, [L_SHAPE])
    # 3x3 grid points (0..2)^2: L_SHAPE covers (0,0),(1,0),(0,1) exactly
    idx_covered = {0, 1, 3}
    assert [i for i, m in enumerate(members) if m] == sorted(idx_covered)
    assert len(per) == 1 and per[0] == members
    # overlapping polygons union without double counting
    p1 = [(0.5, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5)]  # (1,1)
    p2 = [(0.9, 0.9), (2.5, 0.9), (2.5, 2.5), (0.9, 2.5)]  # (1,1),(2,1),(1,2),(2,2)
    members2, per2 = polygon_membership(3, 3, 1, [p1, p2])
    assert sum(members2) == 4
    assert sum(per2[0]) == 1 and sum(per2[1]) == 4


# ------------------------------------------------------------------ NFPA tier KPI

def test_nfpa_tier2_evaluates_inside_polygon():
    cfg = phase2_cfg()
    model = room_with_antenna_at_exact_port(EIRP)
    model["floors"][0]["critical_areas"] = [
        {"id": "ca1", "label": "IT room", "points": [[x, y] for x, y in L_SHAPE]},
    ]
    pr = predict_room(model, cfg, "L0", "T2G")
    tiers = [p for p in pr["kpi"]["presets"] if p["preset"] == "nfpa_two_tier"]
    t1, t2 = tiers[0], tiers[1]
    # tier 1: whole floor, 33.33 % -> fail at 95
    assert t1["scope"] == "whole_floor"
    assert t1["covered_pct"] == pytest.approx(100 * 3 / 9, abs=0.05)
    assert t1["pass"] is False
    # tier 2: union inside the polygon = exactly the 3 covered points
    assert t2["scope"] == "critical_areas"
    assert t2["n_points"] == 3
    assert t2["covered_pct"] == 100.0
    assert t2["pass"] is True
    assert t2["per_polygon"][0]["pass"] is True
    assert t2["per_polygon"][0]["n_points"] == 3
    assert pr["kpi"]["critical_areas"]["pass_union"] is True
    assert pr["kpi"]["critical_areas"]["polygons"][0]["covered_pct"] == 100.0


def test_nfpa_tier2_fails_on_uncovered_polygon():
    cfg = phase2_cfg()
    model = room_with_antenna_at_exact_port(EIRP)
    model["floors"][0]["critical_areas"] = [
        {"id": "bad", "label": "far corner", "points": [[1.5, 1.5], [2.5, 1.5], [2.5, 2.5], [1.5, 2.5]]},
    ]
    pr = predict_room(model, cfg, "L0", "T2G")
    t2 = [p for p in pr["kpi"]["presets"] if p.get("tier") == 2][0]
    assert t2["scope"] == "critical_areas"
    assert t2["n_points"] == 1 and t2["covered_pct"] == 0.0
    assert t2["pass"] is False


def test_tier2_falls_back_to_whole_floor_without_polygons():
    cfg = phase2_cfg()
    model = room_with_antenna_at_exact_port(EIRP)
    pr = predict_room(model, cfg, "L0", "T2G")
    t2 = [p for p in pr["kpi"]["presets"] if p.get("tier") == 2][0]
    assert t2["scope"] == "whole_floor"
    assert "no critical-area polygons" in t2["note"]
    assert pr["kpi"]["critical_areas"]["evaluated"] is False
    # explicit scope override on the tier wins over the default
    cfg2 = with_overrides(cfg, {"kpi_presets": {"nfpa_two_tier": [
        {"tier": 1, "name": "general", "threshold_dbm": -95.0, "min_area_pct": 95.0},
        {"tier": 2, "name": "critical-area", "threshold_dbm": -95.0,
         "min_area_pct": 99.0, "scope": "whole_floor"}]}})
    model["floors"][0]["critical_areas"] = [
        {"id": "ca", "label": "x", "points": [[x, y] for x, y in L_SHAPE]}]
    pr2 = predict_room(model, cfg2, "L0", "T2G")
    t2b = [p for p in pr2["kpi"]["presets"] if p.get("tier") == 2][0]
    assert t2b["scope"] == "whole_floor"


def test_tier2_union_across_overlapping_polygons():
    """A covers only the covered point (0,0); B covers the whole floor and
    fully contains A -> union must count (0,0) once (9 points, not 10)."""
    cfg = phase2_cfg()
    model = room_with_antenna_at_exact_port(EIRP)
    model["floors"][0]["critical_areas"] = [
        {"id": "ca1", "label": "A", "points": [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]]},
        {"id": "ca2", "label": "B", "points": [[-5, -5], [5, -5], [5, 5], [-5, 5]]},
    ]
    pr = predict_room(model, cfg, "L0", "T2G")
    t2 = [p for p in pr["kpi"]["presets"] if p.get("tier") == 2][0]
    assert t2["n_points"] == 9  # (0,0) counted once; A inside B no double count
    assert len(t2["per_polygon"]) == 2
    a, b = t2["per_polygon"]
    assert a["n_points"] == 1 and a["covered_pct"] == 100.0 and a["pass"] is True
    assert b["n_points"] == 9
    assert b["covered_pct"] == pytest.approx(100 * 3 / 9, abs=0.05)
    assert b["pass"] is False
    # union: 3/9 covered -> 33.3 % < 99 % -> FAIL
    assert t2["covered_pct"] == pytest.approx(100 * 3 / 9, abs=0.05)
    assert t2["pass"] is False


# ------------------------------------------------------------------ API: critical areas + NFPA report

def _api_room(client, name="PS Room"):
    """Build the synthetic room through the REST API with the default config.

    Repeater gain tuned so antenna port receives EIRP - antenna gain; the
    3x3 grid then covers exactly {(0,0),(1,0),(0,1)} at -94.8 dBm on B12_700.
    """
    p = client.post("/projects", json={"name": name}).json()
    pid = p["id"]
    f = client.post("/projects/%d/floors" % pid, json={
        "name": "Ground", "level": 0, "ppm": 1.0, "w_px": 3, "h_px": 3}).json()["floor"]
    pl_1m = itu_p1238_pl_db(751.0, 1.0, 30.0, 0, 15.4)
    port = THRESH + pl_1m + 0.01 - 2.0  # omni_das gain = 2 dBi
    src = client.post("/projects/%d/floors/%s/elements" % (pid, f["id"]), json={
        "type": "source", "label": "SRC", "subtype": "off_air",
        "donor_gain_db": 11.0, "feeder_loss_db": 0.0, "x": 2, "y": 2}).json()["element"]
    rep = client.post("/projects/%d/floors/%s/elements" % (pid, f["id"]), json={
        "type": "repeater", "label": "REP", "x": 2, "y": 2,
        "dl_gain_db": repeater_gain_for_target(port), "max_dl_out_dbm": 60.0}).json()["element"]
    ant = client.post("/projects/%d/floors/%s/elements" % (pid, f["id"]), json={
        "type": "antenna", "label": "A1", "antenna_id": "omni_das",
        "x": 0, "y": 0, "gain_db": 2.0, "max_port_dbm": 60.0}).json()["element"]
    client.post("/projects/%d/floors/%s/elements" % (pid, f["id"]), json={
        "type": "cable_run", "from_id": src["id"], "to_id": rep["id"],
        "cable_id": "lmr400", "length_m": 0.0})
    client.post("/projects/%d/floors/%s/elements" % (pid, f["id"]), json={
        "type": "cable_run", "from_id": rep["id"], "to_id": ant["id"],
        "cable_id": "lmr400", "length_m": 0.0})
    return pid, f["id"]


def test_api_critical_area_crud(client):
    pid, fid = _api_room(client, "CA CRUD")
    r = client.post("/projects/%d/floors/%s/critical_areas" % (pid, fid),
                    json={"label": "IT room", "points": [[x, y] for x, y in L_SHAPE]})
    assert r.status_code == 200, r.text
    ca = r.json()["critical_areas"][0]
    assert ca["label"] == "IT room" and len(ca["points"]) == 6

    # list
    lst = client.get("/projects/%d/floors/%s/critical_areas" % (pid, fid)).json()
    assert len(lst["critical_areas"]) == 1

    # patch points + label
    r2 = client.patch("/projects/%d/critical_areas/%s" % (pid, ca["id"]),
                      json={"label": "IT room v2", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]})
    assert r2.status_code == 200
    assert r2.json()["critical_area"]["label"] == "IT room v2"
    assert len(r2.json()["critical_area"]["points"]) == 4

    # invalid input -> 422; unknown ids -> 404
    assert client.post("/projects/%d/floors/%s/critical_areas" % (pid, fid),
                       json={"points": [[0, 0], [1, 1]]}).status_code == 422
    assert client.post("/projects/%d/floors/nope/critical_areas" % pid,
                       json={"points": [[0, 0], [1, 1], [1, 0]]}).status_code == 404
    assert client.patch("/projects/%d/critical_areas/none" % pid,
                        json={"label": "x"}).status_code == 404
    # batch create
    rb = client.post("/projects/%d/floors/%s/critical_areas" % (pid, fid), json={
        "critical_areas": [
            {"label": "B", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]},
            {"label": "C", "points": [[2, 2], [2.5, 2], [2.5, 2.5], [2, 2.5]]}]})
    assert rb.status_code == 200 and len(rb.json()["critical_areas"]) == 2
    d = rb.json()["critical_areas"]
    # delete
    assert client.delete("/projects/%d/critical_areas/%s" % (pid, d[0]["id"])).status_code == 200
    assert client.delete("/projects/%d/critical_areas/%s" % (pid, d[0]["id"])).status_code == 404
    assert len(client.get("/projects/%d/floors/%s/critical_areas" % (pid, fid))
               .json()["critical_areas"]) == 2  # IT room v2 + C
    # critical areas never touch the BOM
    bom = client.get("/projects/%d/bom" % pid).json()
    assert not any("critical" in it["item"].lower() for it in bom["items"])


def test_api_nfpa_report_end_to_end(client):
    pid, fid = _api_room(client, "NFPA E2E")
    ca = client.post("/projects/%d/floors/%s/critical_areas" % (pid, fid),
                     json={"label": "IT room", "points": [[x, y] for x, y in L_SHAPE]}
                     ).json()["critical_areas"][0]
    solve = client.post("/projects/%d/cascade/solve" % pid)
    assert solve.status_code == 200, solve.text
    assert solve.json()["errors"] == []

    pr = client.post("/projects/%d/floors/%s/predict?band=B12_700&step=1" % (pid, fid))
    assert pr.status_code == 200, pr.text
    body = pr.json()
    assert body["n_exponent_source"] == "environment"
    t2 = [p for p in body["kpi"]["presets"] if p.get("tier") == 2][0]
    assert t2["scope"] == "critical_areas" and t2["pass"] is True

    rep = client.get("/projects/%d/floors/%s/nfpa_report?band=B12_700&step=1" % (pid, fid))
    assert rep.status_code == 200, rep.text
    rr = rep.json()
    assert rr["project_id"] == pid and rr["floor"]["id"] == fid
    assert [t["tier"] for t in rr["tiers"]] == [1, 2]
    assert rr["tiers"][0]["scope"] == "whole_floor"
    assert rr["tiers"][0]["covered_pct"] == pytest.approx(100 * 3 / 9, abs=0.05)
    assert rr["tiers"][1]["scope"] == "critical_areas"
    assert rr["tiers"][1]["covered_pct"] == 100.0 and rr["tiers"][1]["pass"] is True
    assert rr["critical_areas"]["evaluated"] is True
    assert rr["critical_areas"]["defined"][0]["id"] == ca["id"]
    assert rr["critical_areas"]["covered_pct_union"] == 100.0
    assert rr["generated_with"] == "ROMtool " + __version__

    # attachment JSON copy
    js = client.get("/projects/%d/floors/%s/nfpa_report.json?band=B12_700&step=1" % (pid, fid))
    assert js.status_code == 200
    assert "nfpa-report-" in js.headers["content-disposition"]

    # PNGs render; same geometry (legend growth not needed at this floor size)
    hm = client.get("/projects/%d/floors/%s/heatmap.png?band=B12_700&step=1" % (pid, fid))
    nf = client.get("/projects/%d/floors/%s/nfpa_report.png?band=B12_700&step=1" % (pid, fid))
    for r in (hm, nf):
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    hm_w, hm_h = struct.unpack(">II", hm.content[16:24])
    nf_w, nf_h = struct.unpack(">II", nf.content[16:24])
    assert (hm_w, hm_h) == (nf_w, nf_h)
    # height = max(heatmap + badges, legend column need): tiny floor -> legend wins
    assert hm_h == 16 + 10 * 6 + 10  # 7 color stops need 86 px; badge stack only 56


def test_api_direct_source_and_subtype_validation(client):
    pid, fid = _api_room(client, "Direct Src")
    assert client.post("/projects/%d/floors/%s/elements" % (pid, fid),
                       json={"type": "source", "subtype": "puck"}).status_code == 422
    src = client.post("/projects/%d/floors/%s/elements" % (pid, fid), json={
        "type": "source", "label": "P25 feed", "subtype": "direct",
        "injection_dbm": 12.5, "x": 2, "y": 2})
    assert src.status_code == 200, src.text
    direct = src.json()["element"]
    # re-point the repeater's feed from the off-air SRC to the direct source,
    # then drop the now-unused off-air element
    elems = client.get("/projects/%d/floors/%s/elements" % (pid, fid)).json()["elements"]
    old_src = next(e for e in elems if e["type"] == "source" and e["label"] == "SRC")
    rep = next(e for e in elems if e["type"] == "repeater")
    feeder = next(e for e in elems
                  if e["type"] == "cable_run" and e["to_id"] == rep["id"])
    client.patch("/projects/%d/elements/%s" % (pid, feeder["id"]),
                 json={"from_id": direct["id"]})
    client.delete("/projects/%d/elements/%s" % (pid, old_src["id"]))
    solve = client.post("/projects/%d/cascade/solve" % pid)
    assert solve.status_code == 200, solve.text
    bands = solve.json()["bands"]["B12_700"]
    node = next(n for n in bands["nodes"] if n["type"] == "source")
    assert node["subtype"] == "direct"
    assert node["out_dbm"] == pytest.approx(12.5)
    assert node["detail"].startswith("direct inject")


def test_api_example_project_nfpa_and_lmr_bands(client):
    pid = next(p["id"] for p in client.get("/projects").json()["projects"] if p["example"])
    proj = client.get("/projects/%d" % pid).json()
    fid = proj["model"]["floors"][0]["id"]
    solve = client.post("/projects/%d/cascade/solve" % pid)
    assert solve.status_code == 200 and solve.json()["errors"] == []
    assert set(solve.json()["bands"]) == set(EXPECTED_BAND_IDS)
    # LMR band predict works + warns (VHF 154 below P.1238 validity)
    pr = client.post("/projects/%d/floors/%s/predict?band=VHF_150&step=2" % (pid, fid))
    assert pr.status_code == 200, pr.text
    body = pr.json()
    assert body["n_exponent_source"] == "band_override"
    assert any("P.1238" in w for w in body["model_warnings"])
    rep = client.get("/projects/%d/floors/%s/nfpa_report?band=PS800&step=2" % (pid, fid))
    assert rep.status_code == 200
    assert rep.json()["critical_areas"]["evaluated"] is True
    assert rep.json()["band"]["id"] == "PS800"
