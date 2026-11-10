"""Building-type default loss model + automatic antenna placement.

Hand anchors used below:
- Loss model: PL = 20log10(f) + N*log10(d) + clutter*d - 28. At 1960 MHz with
  N = 30 and no clutter, PL(10 m) = 65.85 + 30 - 28 = 67.85 dB; building type
  retail_mall (N = 22) gives 59.85 dB at the same distance (8 dB less).
- Placement: counts are not hand-derivable, so the tests assert properties:
  meets the target on the same grid predict_floor uses, scales with area and
  target, is deterministic, and leaves a model the BOM generator accepts.
"""

import copy

import pytest

from romtool.autoplace import AutoplaceError, autoplace_floor
from romtool.bom import generate_bom
from romtool.cascade import solve_cascade
from romtool.config import load_config, with_overrides
from romtool.lossmodel import LossModelError, resolve_loss_model
from romtool.predict import PredictError, predict_floor

SRC = {"subtype": "direct", "injection_dbm": 15.0}
REP = {"max_dl_out_dbm": 33.0}
THR = -60.0  # composite-domain threshold that makes small floors need >1 antenna


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def make_model(w_px, h_px, building_type=None, floor_bt=None, walls=None, **floor_extra):
    floor = {"id": "f1", "name": "F1", "level": 0, "ppm": 10.0, "w_px": w_px,
             "h_px": h_px, "walls": list(walls or []), "elements": []}
    floor.update(floor_extra)
    if floor_bt:
        floor["building_type"] = floor_bt
    model = {"environment": "office", "floors": [floor]}
    if building_type:
        model["building_type"] = building_type
    return model


def request(**kw):
    req = {"bands": ["B2_1900"], "threshold_dbm": THR, "source": SRC,
           "repeater": REP, "step_px": 4}
    req.update(kw)
    return req


def positions(res):
    return [(a["x_px"], a["y_px"]) for a in res["antennas"]]


# ------------------------------------------------------------ loss model

def test_building_type_config_has_grades_and_references(cfg):
    types = cfg["building_types"]
    assert set(types) == {"office", "office_open", "office_standard", "office_dense",
                          "hospital", "warehouse_open", "school_campus",
                          "hotel_residential", "parking_garage", "stadium_venue",
                          "retail_mall"}
    for tid, t in types.items():
        assert t["n_grade"] in ("P", "E"), tid
        assert t["clutter_grade"] in ("P", "C", "E"), tid
        if t["clutter_grade"] == "C":   # company-calibrated against ANS designs
            assert "calibrated" in t["clutter_ref"], tid
        assert t["n_ref"] and t["clutter_ref"], tid
        if t["n_grade"] == "P":
            assert "P.1238" in t["n_ref"], tid
        else:
            assert t["n_ref"].startswith("estimate"), tid
    # the three published rows match the environment block they were taken from
    assert types["office"]["n"] == cfg["environments"]["office"]["n"]
    assert types["retail_mall"]["n"] == cfg["environments"]["commercial"]["n"]
    assert types["hotel_residential"]["n"] == cfg["environments"]["residential"]["n"]


def _band(cfg, bid="B2_1900"):
    return next(b for b in cfg["bands"] if b["id"] == bid)


def test_selection_order_walls_then_building_type_then_environment(cfg):
    band = _band(cfg)
    wall = {"id": "w", "x1": 0, "y1": 0, "x2": 5, "y2": 5, "material": "drywall"}

    m = make_model(100, 100)
    lm = resolve_loss_model(m, cfg, m["floors"][0], band, "office")
    assert (lm["source"], lm["n"], lm["n_source"]) == ("environment", 30.0, "environment")

    m = make_model(100, 100, building_type="retail_mall")
    lm = resolve_loss_model(m, cfg, m["floors"][0], band, "office")
    assert (lm["source"], lm["n"], lm["n_source"]) == ("building_type", 22.0, "building_type")
    assert lm["grade_n"] == "P"

    # floor value beats project value
    m = make_model(100, 100, building_type="retail_mall", floor_bt="hospital")
    lm = resolve_loss_model(m, cfg, m["floors"][0], band, "office")
    assert lm["building_type"] == "hospital" and lm["grade_n"] == "E"

    # drawn walls: environment N, building type ignored
    m = make_model(100, 100, building_type="retail_mall", walls=[wall])
    lm = resolve_loss_model(m, cfg, m["floors"][0], band, "office")
    assert (lm["source"], lm["n"]) == ("walls_drawn", 30.0)

    # per-band override keyed by building type
    band2 = dict(band, n_exponent_overrides={"retail_mall": 25.0})
    m = make_model(100, 100, building_type="retail_mall")
    lm = resolve_loss_model(m, cfg, m["floors"][0], band2, "office")
    assert (lm["n"], lm["n_source"]) == (25.0, "building_type_band_override")


def test_unknown_building_type_rejected(cfg):
    m = make_model(100, 100, building_type="moon_base")
    with pytest.raises(LossModelError):
        resolve_loss_model(m, cfg, m["floors"][0], _band(cfg), "office")
    with pytest.raises(AutoplaceError):
        autoplace_floor(m, cfg, "f1", request())


def test_office_subtypes_select_their_clutter(cfg):
    band = _band(cfg)
    expected = {  # building type: (N, clutter dB/m, clutter grade)
        "office": (30.0, 0.3, "C"),
        "office_open": (30.0, 0.1, "E"),
        "office_standard": (30.0, 0.3, "C"),
        "office_dense": (30.0, 0.6, "E"),
    }
    for bt, (n, clutter, grade) in expected.items():
        m = make_model(100, 100, building_type=bt)
        lm = resolve_loss_model(m, cfg, m["floors"][0], band, "office")
        assert (lm["source"], lm["building_type"]) == ("building_type", bt)
        assert (lm["n"], lm["clutter_db_per_m"], lm["grade_clutter"]) == (n, clutter, grade), bt
        assert lm["grade_n"] == "P" and lm["clutter_source"] == "building_type", bt
    # open and dense start from the clutter_map classes; standard keeps the office value
    classes = cfg["clutter_map"]["classes"]
    bts = cfg["building_types"]
    assert bts["office_open"]["clutter_db_per_m"] == classes["open"]["db_per_m"]
    assert bts["office_dense"]["clutter_db_per_m"] == classes["dense_rooms"]["db_per_m"]
    assert bts["office_standard"]["clutter_ref"] == bts["office"]["clutter_ref"]


SYS_CFG = {"building_types": {"office": {
    "clutter_by_system": {"das": 0.5, "dran": 0.9},
    "clutter_by_system_grade": "E",
    "clutter_by_system_ref": "test value only, not calibrated"}}}


def test_system_type_precedence_request_over_floor_over_project(cfg):
    c = with_overrides(cfg, SYS_CFG)
    band = _band(c)
    m = make_model(100, 100, building_type="office")
    m["system_type"] = "das"
    lm = resolve_loss_model(m, c, m["floors"][0], band, "office")
    assert (lm["clutter_db_per_m"], lm["clutter_source"], lm["system_type"]) == (
        0.5, "building_type_system", "das")
    assert lm["grade_clutter"] == "E"

    m["floors"][0]["system_type"] = "dran"          # floor beats project
    lm = resolve_loss_model(m, c, m["floors"][0], band, "office")
    assert (lm["clutter_db_per_m"], lm["system_type"]) == (0.9, "dran")

    # request beats floor
    lm = resolve_loss_model(m, c, m["floors"][0], band, "office", system_type="das")
    assert (lm["clutter_db_per_m"], lm["system_type"]) == (0.5, "das")


def test_system_type_falls_back_when_building_type_has_no_value(cfg):
    c = with_overrides(cfg, SYS_CFG)
    band = _band(c)
    m = make_model(100, 100, building_type="office")
    m["system_type"] = "bda"                        # office has no bda value
    lm = resolve_loss_model(m, c, m["floors"][0], band, "office")
    assert (lm["clutter_db_per_m"], lm["clutter_source"]) == (0.3, "building_type")
    assert lm["grade_clutter"] == "C"

    m = make_model(100, 100, building_type="retail_mall")   # no clutter_by_system block
    m["system_type"] = "das"
    lm = resolve_loss_model(m, c, m["floors"][0], band, "office")
    assert (lm["clutter_db_per_m"], lm["clutter_source"]) == (0.0, "building_type")


def test_unknown_system_type_rejected(cfg):
    m = make_model(100, 100, building_type="office")
    m["system_type"] = "wifi"
    with pytest.raises(LossModelError):
        resolve_loss_model(m, cfg, m["floors"][0], _band(cfg), "office")
    with pytest.raises(AutoplaceError):
        autoplace_floor(make_model(100, 100, building_type="office"), cfg, "f1",
                        request(system_type="wifi"))


def test_system_type_absent_keeps_building_type_clutter(cfg):
    c = with_overrides(cfg, SYS_CFG)
    m = make_model(100, 100, building_type="office")
    lm = resolve_loss_model(m, c, m["floors"][0], _band(c), "office")
    base = resolve_loss_model(m, cfg, m["floors"][0], _band(cfg), "office")
    assert (lm["clutter_db_per_m"], lm["clutter_source"], lm["system_type"]) == (
        0.3, "building_type", None)
    assert lm["clutter_db_per_m"] == base["clutter_db_per_m"]


def test_autoplace_request_system_type_reaches_loss_model(cfg):
    c = with_overrides(cfg, SYS_CFG)
    res = autoplace_floor(make_model(100, 100, building_type="office"), c, "f1",
                          request(system_type="das"))
    info = res["loss_model"]["B2_1900"]
    assert (info["clutter_db_per_m"], info["clutter_source"], info["system_type"]) == (
        0.5, "building_type_system", "das")


def _one_antenna_model(building_type=None, walls=None):
    m = make_model(200, 100, building_type=building_type, walls=walls)
    m["floors"][0]["elements"] = [
        {"id": "src", "type": "source", "label": "S", "x": 1, "y": 1, "subtype": "direct",
         "injection_dbm": 0.0},
        {"id": "a1", "type": "antenna", "label": "A1", "antenna_id": "omni_das", "x": 0, "y": 0,
         "gain_db": 0.0, "max_port_dbm": 60.0},
        {"id": "r1", "type": "cable_run", "from_id": "src", "to_id": "a1",
         "cable_id": "lmr400", "length_m": 0.0},
    ]
    return m


def test_building_type_changes_predict_by_hand_math(cfg):
    base = _one_antenna_model()
    sol = solve_cascade(base, cfg)
    eirp = sol["bands"]["B2_1900"]["antennas"][0]["eirp_dbm"]
    assert eirp == pytest.approx(0.0)
    d_px = 100  # grid point (100, 0) is 10 m from the antenna at ppm 10
    idx = d_px  # row 0, col 100 at step 1
    pr0 = predict_floor(base, cfg, "f1", "B2_1900", step_px=1)
    # default environment N = 30: RSSI = 0 - (65.85 + 30 - 28) = -67.85
    assert pr0["values_dbm"][idx] == pytest.approx(-67.8, abs=0.11)
    assert pr0["loss_model"]["source"] == "environment"

    mall = _one_antenna_model(building_type="retail_mall")
    pr1 = predict_floor(mall, cfg, "f1", "B2_1900", step_px=1)
    assert pr1["n_exponent"] == 22.0 and pr1["n_exponent_source"] == "building_type"
    assert pr1["loss_model"]["building_type"] == "retail_mall"
    assert pr1["values_dbm"][idx] == pytest.approx(-59.8, abs=0.11)  # 8 dB less loss

    # same floor with a wall drawn far from the path: environment N again
    walled = _one_antenna_model(building_type="retail_mall", walls=[
        {"id": "w", "x1": 190, "y1": 0, "x2": 190, "y2": 90, "material": "drywall"}])
    pr2 = predict_floor(walled, cfg, "f1", "B2_1900", step_px=1)
    assert pr2["n_exponent"] == 30.0 and pr2["loss_model"]["source"] == "walls_drawn"
    assert pr2["values_dbm"][idx] == pytest.approx(pr0["values_dbm"][idx], abs=0.01)


def test_default_behavior_unchanged_without_building_type(cfg):
    m = _one_antenna_model()
    pr = predict_floor(m, cfg, "f1", "B2_1900", step_px=2)
    assert pr["n_exponent"] == 30.0 and pr["n_exponent_source"] == "environment"
    # a building type with the same N as the environment gives identical values
    # pin clutter to 0 so only N is compared (office clutter is company-calibrated in config)
    c0 = with_overrides(cfg, {"building_types": {"office": {"clutter_db_per_m": 0.0}}})
    m2 = _one_antenna_model(building_type="office")
    pr2 = predict_floor(m2, c0, "f1", "B2_1900", step_px=2)
    assert pr2["values_dbm"] == pr["values_dbm"]


def test_clutter_term_subtracts_per_metre(cfg):
    c0 = with_overrides(cfg, {"building_types": {"office": {"clutter_db_per_m": 0.0}}})
    c2 = with_overrides(cfg, {"building_types": {"office": {"clutter_db_per_m": 0.5}}})
    m = _one_antenna_model(building_type="office")
    base = predict_floor(m, c0, "f1", "B2_1900", step_px=1)
    cl = predict_floor(m, c2, "f1", "B2_1900", step_px=1)
    # 10 m away: 0.5 dB/m * 10 m = 5 dB lower
    assert base["values_dbm"][100] - cl["values_dbm"][100] == pytest.approx(5.0, abs=0.11)


def test_unknown_building_type_in_predict(cfg):
    m = _one_antenna_model(building_type="moon_base")
    with pytest.raises(PredictError):
        predict_floor(m, cfg, "f1", "B2_1900", step_px=2)


# ------------------------------------------------------------- autoplace

def count_covered(model, cfg, band, step, thr):
    pr = predict_floor(model, cfg, "f1", band, step_px=step)
    vals = pr["values_dbm"]
    return 100.0 * sum(1 for v in vals if v is not None and v >= thr - 1e-9) / len(vals)


def test_rectangle_meets_target_and_matches_predict(cfg):
    m = make_model(600, 400, building_type="office")
    res = autoplace_floor(m, cfg, "f1", request())
    assert res["target_met"] is True
    assert 2 <= res["antenna_count"] <= 12
    row = res["achieved"]["B2_1900"][0]
    assert row["covered_pct"] >= 95.0 and row["pass"]
    # same grid as predict_floor: the numbers agree exactly
    assert res["grid"]["verify_step_px"] == 4
    assert count_covered(m, cfg, "B2_1900", 4, THR) == pytest.approx(row["covered_pct"], abs=0.01)
    # chain solves cleanly and the BOM falls out of the model
    sol = solve_cascade(m, cfg)
    assert sol["errors"] == []
    bom = generate_bom(m, cfg)["items"]
    ants = [i for i in bom if i["category"] == "antenna"]
    assert sum(i["qty"] for i in ants) == res["antenna_count"]
    assert {"source", "repeater", "cable", "connectors"} <= {i["category"] for i in bom}
    assert res["iterations"]["outer"] >= 1
    assert not [w for w in res["warnings"] if "below" in w]


def test_larger_floor_needs_more_antennas(cfg):
    small = autoplace_floor(make_model(400, 300, building_type="office"), cfg, "f1", request())
    big = autoplace_floor(make_model(800, 600, building_type="office"), cfg, "f1", request())
    assert big["target_met"] and small["target_met"]
    assert big["antenna_count"] > small["antenna_count"]


def test_tighter_target_needs_more_antennas(cfg):
    loose = autoplace_floor(make_model(600, 400, building_type="office"), cfg, "f1",
                            request(coverage_target_pct=80.0))
    tight = autoplace_floor(make_model(600, 400, building_type="office"), cfg, "f1",
                            request(coverage_target_pct=99.5))
    higher = autoplace_floor(make_model(600, 400, building_type="office"), cfg, "f1",
                             request(threshold_dbm=THR + 6.0))
    assert tight["antenna_count"] > loose["antenna_count"]
    assert higher["antenna_count"] > loose["antenna_count"]
    assert tight["achieved"]["B2_1900"][0]["covered_pct"] >= 99.5


def test_deterministic_and_idempotent(cfg):
    a = autoplace_floor(make_model(600, 400, building_type="office"), cfg, "f1", request())
    b = autoplace_floor(make_model(600, 400, building_type="office"), cfg, "f1", request())
    assert positions(a) == positions(b)
    assert a["antenna_count"] == b["antenna_count"]
    assert a["achieved"] == b["achieved"]
    # rerun on the same model replaces the earlier auto elements
    m = make_model(600, 400, building_type="office")
    r1 = autoplace_floor(m, cfg, "f1", request())
    n_el = len(m["floors"][0]["elements"])
    r2 = autoplace_floor(m, cfg, "f1", request())
    assert len(m["floors"][0]["elements"]) == n_el
    assert positions(r1) == positions(r2)


def test_building_type_changes_count(cfg):
    office = autoplace_floor(make_model(600, 400, building_type="office"), cfg, "f1", request())
    mall = autoplace_floor(make_model(600, 400, building_type="retail_mall"), cfg, "f1", request())
    assert mall["antenna_count"] < office["antenna_count"]
    assert mall["loss_model"]["B2_1900"]["building_type"] == "retail_mall"


def test_walls_drawn_use_walls_not_building_type(cfg):
    wall = {"id": "w1", "x1": 200, "y1": 0, "x2": 200, "y2": 300, "material": "concrete"}
    m = make_model(400, 300, building_type="retail_mall", walls=[wall])
    res = autoplace_floor(m, cfg, "f1", request(step_px=6))
    assert res["loss_model"]["B2_1900"]["source"] == "walls_drawn"
    assert res["target_met"]


def test_nfpa_tiers_with_critical_area(cfg):
    ca = [{"id": "ca1", "label": "IT", "points": [[20, 20], [120, 20], [120, 100], [20, 100]]}]
    m = make_model(400, 300, building_type="office", critical_areas=ca)
    res = autoplace_floor(m, cfg, "f1", {"bands": ["PS800"], "source": SRC,
                                          "repeater": REP, "step_px": 4})
    rows = res["achieved"]["PS800"]
    assert res["targets"]["nfpa_bands"] == ["PS800"]
    assert [r["tier"] for r in rows] == [1, 2]
    assert rows[1]["scope"] == "critical_areas" and rows[1]["min_area_pct"] == 99.0
    assert all(r["pass"] for r in rows) and res["target_met"]
    # predict's NFPA presets agree
    pr = predict_floor(m, cfg, "f1", "PS800", step_px=4)
    nfpa = [p for p in pr["kpi"]["presets"] if p["preset"] == "nfpa_two_tier"]
    assert all(p["pass"] for p in nfpa)


def test_existing_antennas_are_kept_and_counted(cfg):
    m = make_model(600, 400, building_type="office")
    first = autoplace_floor(m, cfg, "f1", request())
    # freeze the result as "manual": drop the auto flag from everything
    for el in m["floors"][0]["elements"]:
        el.pop("auto", None)
    again = autoplace_floor(m, cfg, "f1", request(source=SRC))
    # the frozen antennas already meet the target: nothing new is needed
    assert again["fixed_antennas_counted"] == first["antenna_count"]
    assert again["total_antennas_on_floor"] >= first["antenna_count"]
    assert again["target_met"]


def test_infeasible_budget_reports_blocker(cfg):
    m = make_model(600, 400, building_type="office")
    with pytest.raises(AutoplaceError) as ei:
        autoplace_floor(m, cfg, "f1", request(threshold_dbm=-20.0))
    assert "coverage radius" in str(ei.value)


def test_bad_requests(cfg):
    m = make_model(400, 300)
    with pytest.raises(AutoplaceError):
        autoplace_floor(copy.deepcopy(m), cfg, "f1", {"bands": []})
    with pytest.raises(AutoplaceError):
        autoplace_floor(copy.deepcopy(m), cfg, "f1", request(bands=["NOPE"]))
    with pytest.raises(AutoplaceError):
        autoplace_floor(copy.deepcopy(m), cfg, "f1", request(coverage_target_pct=0))
    with pytest.raises(AutoplaceError):
        autoplace_floor(copy.deepcopy(m), cfg, "nope", request())
    with pytest.raises(AutoplaceError):
        autoplace_floor(copy.deepcopy(m), cfg, "f1", request(source={"bogus": 1}))


def test_outline_limits_the_area(cfg):
    ol = [[0, 0], [400, 0], [400, 100], [0, 100]]
    full = make_model(600, 400, building_type="office")
    part = make_model(600, 400, building_type="office", outline=ol)
    a = autoplace_floor(full, cfg, "f1", request())
    b = autoplace_floor(part, cfg, "f1", request())
    assert b["targets"]["outline"] == "polygon"
    assert b["grid"]["n_points"] < a["grid"]["n_points"]
    assert b["antenna_count"] <= a["antenna_count"]


# ------------------------------------------------------------------- API

def test_api_autoplace_then_bom_and_predict(client):
    r = client.post("/projects", json={"name": "AP test", "building_type": "office"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert r.json()["model"]["building_type"] == "office"
    fid = client.post("/projects/%d/floors" % pid,
                      json={"name": "L1", "ppm": 10, "w_px": 600, "h_px": 400}).json()["floor"]["id"]

    # validation of the new fields
    assert client.patch("/projects/%d" % pid, json={"building_type": "moon_base"}).status_code == 422
    assert client.patch("/projects/%d/floors/%s" % (pid, fid),
                        json={"building_type": "hospital"}).json()["floor"]["building_type"] == "hospital"
    assert client.patch("/projects/%d/floors/%s" % (pid, fid),
                        json={"building_type": None}).status_code == 200

    body = {"bands": ["B2_1900"], "threshold_dbm": THR, "source": SRC,
            "repeater": REP, "step_px": 4}
    dry = client.post("/projects/%d/floors/%s/autoplace" % (pid, fid), json=dict(body, dry_run=True))
    assert dry.status_code == 200, dry.text
    assert dry.json()["saved"] is False
    assert client.get("/projects/%d/bom" % pid).json()["items"] == []

    res = client.post("/projects/%d/floors/%s/autoplace" % (pid, fid), json=body)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["saved"] and out["target_met"] and out["antenna_count"] >= 2
    assert out["antenna_count"] == dry.json()["antenna_count"]  # deterministic

    bom = client.get("/projects/%d/bom" % pid).json()["items"]
    assert sum(i["qty"] for i in bom if i["category"] == "antenna") == out["antenna_count"]
    assert bom == out["bom"]

    pr = client.post("/projects/%d/floors/%s/predict?band=B2_1900&step=4" % (pid, fid)).json()
    vals = pr["values_dbm"]
    pct = 100.0 * sum(1 for v in vals if v is not None and v >= THR - 1e-9) / len(vals)
    assert pct >= 95.0
    assert pr["loss_model"]["building_type"] == "office"

    # errors map to 422
    assert client.post("/projects/%d/floors/%s/autoplace" % (pid, fid),
                       json={"bands": ["NOPE"]}).status_code == 422
    assert client.post("/projects/%d/floors/nope/autoplace" % pid,
                       json=body).status_code == 404


def test_api_system_type_validation(client):
    r = client.post("/projects", json={"name": "SYS test", "system_type": "das"})
    assert r.status_code == 200, r.text
    assert r.json()["model"]["system_type"] == "das"
    pid = r.json()["id"]
    assert client.patch("/projects/%d" % pid, json={"system_type": "wifi"}).status_code == 422
    fid = client.post("/projects/%d/floors" % pid,
                      json={"name": "L1", "ppm": 10, "w_px": 100, "h_px": 100}).json()["floor"]["id"]
    assert client.patch("/projects/%d/floors/%s" % (pid, fid),
                        json={"system_type": "wifi"}).status_code == 422
    assert client.patch("/projects/%d/floors/%s" % (pid, fid),
                        json={"system_type": "dran"}).json()["floor"]["system_type"] == "dran"
    assert client.patch("/projects/%d/floors/%s" % (pid, fid),
                        json={"system_type": None}).status_code == 200
    body = {"bands": ["B2_1900"], "threshold_dbm": THR, "source": SRC,
            "repeater": REP, "step_px": 4, "system_type": "wifi"}
    assert client.post("/projects/%d/floors/%s/autoplace" % (pid, fid),
                       json=body).status_code == 422
