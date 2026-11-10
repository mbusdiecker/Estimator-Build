"""Phase 4: Class 1 labor estimate engine.

Worked stadium scenario (tests/fixtures/stadium_estimate.json): rope-access
roof, lift-access occupied bowl, occupied+constrained closet, ERCES headend
with the auto-sized 48VDC power plant, night differential, M05 travel moves,
M01 lockout day, finishing rows and the new-row set (E11 shrouds, K10 crane
lifts, B11 headend HVAC). All factor-stack math is asserted against
independent hand formulas, plus the wrap math, $ chain, API CRUD smoke,
CSV/XLSX export smoke and a <5 s benchmark.
"""

import copy
import csv
import importlib.util
import io
import json
import math
import os
import time
import xml.etree.ElementTree as ET
import zipfile

import pytest

from romtool.estimate import (CONDITION_FLAGS, EstimateError, compute_estimate,
                              load_estimating_config, load_labor_catalog)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixtures", "stadium_estimate.json")
CATALOG = os.path.join(REPO, "config", "labor_units.json")
ESTIMATING = os.path.join(REPO, "config", "estimating.json")
CSV_SRC = "/root/assistant/research/das-build-unit-catalog-v2.csv"

# factor mids straight off config/estimating.json (asserted exactly below)
F_OCC, F_EXT, F_CON = 1.375, 1.30, 1.30
F_LIFT, F_ROPE = 1.375, 1.75
F_NIGHT = 1.325
N01 = (1 / 4) * 0.5            # foremen 1 / techs 4, productive haircut 0.5
N02 = 0.115                    # mid of 8-15%
SUP = 1 + N01 + N02            # 1.24 supervision multiplier on task hours
TECH, FORE = 85.0, 105.0


@pytest.fixture(scope="module")
def stadium():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def recap(stadium):
    return compute_estimate(stadium)


def line(rec, cid, loc=None):
    out = [l for l in rec["lines"] if l["catalog_id"] == cid
           and (loc is None or l["location_id"] == loc)]
    assert len(out) == 1, "expected exactly one line %s (loc=%r)" % (cid, loc)
    return out[0]


def _anchors(rec):
    """Hand-computed mid-band line hours, keyed (catalog_id). Ids are unique
    across locations in the fixture."""
    return {
        "A01": 1.5 * F_EXT * F_ROPE * F_NIGHT * SUP,
        "A04": 2 * 2.25 * F_EXT * F_ROPE * F_NIGHT * SUP,
        "A09": 6 * 0.25 * F_EXT * F_ROPE * F_NIGHT * SUP,
        "K04": 2 * 3.0 * F_EXT * F_NIGHT * SUP,          # K zone: no rope factor
        "K10": 2 * 3.0 * F_EXT * F_NIGHT * SUP,          # new row, same rule
        "E06": 40 * 0.75 * F_OCC * F_LIFT * F_NIGHT * SUP,
        "E03": 80 * 0.625 * F_OCC * F_LIFT * F_NIGHT * SUP,
        "E01": 1200 * (2.4 / 60) * F_OCC * F_LIFT * F_NIGHT * SUP,
        "E11": 8 * 0.5 * F_OCC * F_LIFT * F_NIGHT * SUP,  # new row (shrouds)
        "K01": 4 * 0.75 * F_OCC * F_NIGHT * SUP,          # K zone: no lift factor
        "K02": 12 * 0.25 * F_OCC * F_NIGHT * SUP,
        "L02": 60 * (2 / 60) * F_OCC * F_LIFT * F_NIGHT * SUP,  # finishing rows
        "C01": 2.0 * F_OCC * F_CON * F_NIGHT * SUP,
        "C07": 3 * 1.5 * F_OCC * F_CON * F_NIGHT * SUP,
        "C08": 12 * (20 / 60) * F_OCC * F_CON * F_NIGHT * SUP,
        "B03": 1.5 * F_NIGHT * SUP,                       # night-only, ground
        "B05": 1.5 * F_NIGHT * SUP,
        "B11": 4.5 * F_NIGHT * SUP,                       # new row (headend HVAC)
        "H07": 7.0 * F_NIGHT * SUP,
        "G03": 3.0 * F_NIGHT * SUP,
        "O01": 3.0 * F_NIGHT * SUP,                       # power-plant derived
        "O02": 2 * 0.625 * F_NIGHT * SUP,
        "O04": 3 * 2.75 * F_NIGHT * SUP,
        "M05": 3 * 0.375,         # derived raw: no night premium
        "N03": 4 * 3.0,           # 4 locations x 1 crew x 3 h mid
        "N04": 15 * 0.375,        # 15 crew-days x 22.5 min mid
    }


# --------------------------------------------------------- catalog store

def test_catalog_store_shape_and_regen_script():
    with open(CATALOG, "r", encoding="utf-8") as fh:
        cat = json.load(fh)
    assert cat["row_count"] == len(cat["units"]) == 134
    assert len(cat["zones"]) == 15
    assert cat["zones"][0] == "A-ROOFTOP-ENTRY" and cat["zones"][-1] == "O-DC-POWER-PLANT"
    ids = [u["id"] for u in cat["units"]]
    assert len(set(ids)) == 134
    assert {u["kind"] for u in cat["units"]} == {"minutes", "factor", "quote"}
    factor_ids = {u["id"] for u in cat["units"] if u["kind"] == "factor"}
    assert factor_ids == {"K03", "K05", "K06", "K14", "M02", "M03", "M04",
                          "M07", "N01", "N02", "N05"}
    for u in cat["units"]:
        if u["kind"] == "factor":
            assert u["minutes_low"] is None and u["minutes_high"] is None
    by_id = {u["id"]: u for u in cat["units"]}
    for probe, kind in (("E11", "minutes"), ("E12", "minutes"), ("E13", "minutes"),
                        ("B11", "minutes"), ("K10", "minutes"), ("K13", "minutes"),
                        ("K09", "quote"), ("K12", "quote"), ("O04", "minutes"),
                        ("O13", "quote"), ("M01", "quote"), ("B08", "quote")):
        assert by_id[probe]["kind"] == kind, probe
    assert by_id["O04"]["unit"] == "STRING"

    # the checked-in JSON must equal what the regen script produces from the
    # catalog CSV (regenerable; refresh the store after catalog edits)
    spec = importlib.util.spec_from_file_location(
        "build_labor_units", os.path.join(REPO, "scripts", "build_labor_units.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not os.path.exists(CSV_SRC):
        pytest.skip("catalog CSV not present at %s" % CSV_SRC)
    assert json.load(open(CATALOG, encoding="utf-8")) == mod.build(CSV_SRC)


def test_estimating_config_is_the_single_source():
    est = load_estimating_config()
    assert est["rate_card"]["tech_rate"] == 85.0
    assert est["rate_card"]["foreman_rate"] == 105.0
    assert est["pm_pct"] == 10.0 and est["overhead_pct"] == 10.0
    assert est["profit_pct"] == 10.0
    assert est["contingency_pct_by_quality"] == {"P": 3.0, "E": 7.0, "G": 10.0}
    assert est["factors"]["occupied"]["low"] == 1.25
    assert est["factors"]["occupied"]["high"] == 1.50
    assert est["access_factors"]["rope"] == {"row": "K05", "low": 1.50,
                                             "high": 2.00,
                                             "note": est["access_factors"]["rope"]["note"]}
    assert est["access_factors"]["ground"]["row"] is None
    assert est["foreman_productive_haircut"] == 0.5
    assert est["supervision_pct"]["low"] == 0.08 and est["supervision_pct"]["high"] == 0.15
    assert est["power_plant"]["battery_string_ah"] == 120.0
    assert est["power_plant"]["rectifier_size_amps"] == 50.0
    assert est["power_plant"]["rows"]["strings"] == "O04"


# ------------------------------------------------- hand math on the scenario

def test_line_factor_stack_hand_math(recap):
    exp = _anchors(recap)
    for cid, want in exp.items():
        ln = line(recap, cid)
        assert ln["hours"]["mid"] == pytest.approx(want, abs=1e-6), cid
        if cid not in ("M05", "N03", "N04", "M01"):
            assert ln["kind"] == "minutes"


def test_factor_stacking_order_and_visibility(recap):
    a01 = line(recap, "A01")
    stack = a01["factors"]
    # ordered stack: condition -> access -> schedule, then supervision block
    assert [c["key"] for c in stack["condition"]] == ["exterior"]
    assert stack["condition"][0]["row"] == "M04"
    assert stack["access"] == {"row": "K05", "low": 1.50, "high": 2.00, "value": 1.75}
    assert [c["key"] for c in stack["schedule"]] == ["night"]
    assert stack["schedule"][0]["row"] == "M02"
    assert a01["supervision"]["n01_foreman_ratio"] == pytest.approx(N01)
    assert a01["supervision"]["n01_row"] == "N01"
    assert a01["supervision"]["n02_row"] == "N02" and a01["supervision"]["n02_pct"] == 0.115
    # manual stage check: base x condition x access x schedule = task hours
    task = a01["base_minutes"]["mid"] * F_EXT * F_ROPE * F_NIGHT / 60.0
    assert a01["task_hours"]["mid"] == pytest.approx(task, abs=1e-6)
    assert a01["hours"]["mid"] == pytest.approx(task * SUP, abs=1e-6)
    # K-zone rows are the access adders: no access factor, but conditions/schedule stay
    for cid in ("K04", "K10", "K01", "K02"):
        assert line(recap, cid)["factors"]["access"] is None, cid
    assert line(recap, "K04")["factors"]["condition"][0]["key"] == "exterior"
    # canonical condition ordering regardless of definition order
    legend = recap["factor_legend"]["stack_order"]
    assert legend == ["base_minutes", "condition", "access", "schedule",
                      "supervision_N01_N02", "wrap_and_contingency"]
    from romtool.estimate import _condition_stack
    est = load_estimating_config()
    keys = [c["key"] for c in _condition_stack(
        ["constrained_access", "occupied"], est)]
    assert keys == ["occupied", "constrained_access"]


def test_wrap_math_and_crew_stats(recap):
    exp = _anchors(recap)
    # M05: 3 travel moves, base minutes, NO stacking (derived flag + raw hours)
    m05 = line(recap, "M05")
    assert m05["qty"] == 3 and m05["derived"] is True
    assert m05["hours"]["mid"] == pytest.approx(exp["M05"], abs=1e-6)
    assert m05["factors"]["schedule"] == [] and m05["factors"]["access"] is None
    assert m05["supervision"]["n02_pct"] == 0.0
    # N03 orientation: crews x locations = 1 crew x 4 phases
    n03 = line(recap, "N03")
    assert n03["qty"] == 4 and n03["hours"]["mid"] == pytest.approx(12.0, abs=1e-6)
    # N04 cleanup: ceil(direct wrapped hours / crew-day capacity)
    direct_mid = sum(e for cid, e in exp.items() if cid not in ("M05", "N03", "N04"))
    capacity_day = 1 * (4 + 1) * 8  # crews x crew size x hours/day = 40 h
    crew_days = math.ceil(direct_mid / capacity_day)
    assert crew_days == 15
    n04 = line(recap, "N04")
    assert n04["qty"] == 15
    assert n04["hours"]["mid"] == pytest.approx(15 * 0.375, abs=1e-6)
    assert n04["derivation"].startswith("ceil(")
    t = recap["totals"]
    assert t["total_crew_hours_mid"] == pytest.approx(sum(exp.values()), abs=1e-6)
    assert t["crew_days"] == 15 and t["crew_weeks"] == 3
    cap_week = capacity_day * 5  # 200 crew-hours/week
    assert t["hours_per_crew_week"] == pytest.approx(sum(exp.values()) / 3, abs=1e-5)
    assert recap["crew"]["capacity_per_crew_week_h"] == cap_week


def test_zone_and_location_rollups(recap):
    exp = _anchors(recap)
    zones = {z["key"]: z for z in recap["zone_rollups"]}
    assert zones["A-ROOFTOP-ENTRY"]["hours"]["mid"] == pytest.approx(
        exp["A01"] + exp["A04"] + exp["A09"], abs=1e-6)
    assert zones["K-ACCESS-ADDERS"]["hours"]["mid"] == pytest.approx(
        exp["K04"] + exp["K10"] + exp["K01"] + exp["K02"], abs=1e-6)
    assert zones["O-DC-POWER-PLANT"]["hours"]["mid"] == pytest.approx(
        exp["O01"] + exp["O02"] + exp["O04"], abs=1e-6)
    assert zones["B-HEADEND"]["hours"]["mid"] == pytest.approx(
        exp["B03"] + exp["B05"] + exp["B11"], abs=1e-6)
    assert zones["M-DOWNTIME-LOGISTICS"]["cost_carry_usd"] == pytest.approx(3560.0)
    # every rollup equals the sum of its own lines
    for z in recap["zone_rollups"]:
        ln_hours = sum(l["hours"]["mid"] for l in recap["lines"]
                       if l["zone"] == z["key"])
        assert z["hours"]["mid"] == pytest.approx(ln_hours, abs=1e-6), z["key"]
    locs = {l["key"]: l for l in recap["location_rollups"]}
    assert locs["headend"]["hours"]["mid"] == pytest.approx(
        exp["B03"] + exp["B11"] + exp["B05"] + exp["H07"] + exp["G03"]
        + exp["O01"] + exp["O02"] + exp["O04"], abs=1e-6)


def test_cost_chain_and_money_rollup(recap):
    exp = _anchors(recap)
    c = recap["cost"]
    # labor: stacked lines at 85 + 0.125*105 + 0.115*105 = 110.2 $/task-hour;
    # wrap lines bill raw hours at the tech rate (85)
    stacked = {k: v for k, v in exp.items() if k not in ("M05", "N03", "N04")}
    labor = sum(v / SUP * 110.2 for v in stacked.values()) + \
        (exp["M05"] + exp["N03"] + exp["N04"]) * TECH
    assert c["direct_labor_usd"]["mid"] == pytest.approx(labor, abs=1e-3)
    # M01 lockout: blended rate = (4*85 + 1*105) / 5 = 89 $/h;
    # idle crew-day = 5 crew x 8 h x 89 = 3560 USD, 1 blacked-out day
    carry = [l for l in recap["lines"] if l["kind"] == "cost_carry"]
    assert len(carry) == 1 and carry[0]["qty"] == 1 and carry[0]["hours"]["mid"] == 0.0
    # blended = (4 techs x $85 + 1 foreman x $105) / 5 = $89/h;
    # an idle crew-day = 5 crew x 8 h x $89 = $3,560, one blacked-out day
    assert carry[0]["idle_crew_day_usd"] == pytest.approx(3560.0)
    assert "89.00" in carry[0]["reference"]
    assert carry[0]["cost_carry_usd"]["mid"] == pytest.approx(3560.0)
    assert c["downtime_usd"] == pytest.approx(3560.0)
    # v0.6 wrap chain (config/commercial.json), documented order:
    # small tools 3% x (labor + downtime); escalation labor index skipped
    # (no anchor date on the fixture -> factor 1, warning); taxes 0% of the
    # taxable base (every stadium zone classifies as exempt labor);
    # insurance/bond 1.5% x running subtotal through taxes; PM 10% x that
    # same running subtotal; OH 10%; profit 10% (standard risk tier).
    run = labor + 3560.0
    st = 0.03 * run
    esc = 0.0
    tax = 0.0
    ins = 0.015 * (run + st + esc + tax)
    pm = 0.10 * (run + st + esc + tax + ins)
    assert c["small_tools_usd"]["mid"] == pytest.approx(st, abs=1e-3)
    assert c["escalation_usd"]["mid"] == pytest.approx(esc, abs=1e-9)
    assert c["tax_usd"]["mid"] == pytest.approx(tax, abs=1e-9)
    assert c["tax_taxable_labor_usd"]["mid"] == pytest.approx(0.0, abs=1e-9)
    assert c["insurance_bond_usd"]["mid"] == pytest.approx(ins, abs=1e-3)
    assert c["pm_usd"]["mid"] == pytest.approx(pm, abs=1e-3)
    oh = 0.10 * (run + st + esc + tax + ins + pm)
    assert c["overhead_usd"]["mid"] == pytest.approx(oh, abs=1e-3)
    prof = 0.10 * (run + st + esc + tax + ins + pm + oh)
    assert c["profit_usd"]["mid"] == pytest.approx(prof, abs=1e-3)
    assert c["profit_risk_tier"] == "standard"
    subtotal = run + st + esc + tax + ins + pm + oh + prof
    assert c["subtotal_usd"]["mid"] == pytest.approx(subtotal, abs=1e-3)
    conting = sum(l["labor_usd"]["total"]["mid"] * l["contingency_pct"] / 100.0
                  for l in recap["lines"] if l["kind"] == "minutes")
    assert c["contingency_usd"]["mid"] == pytest.approx(conting, abs=1e-3)
    assert c["grand_total_usd"]["mid"] == pytest.approx(subtotal + conting, abs=1e-3)
    money = {m["code"]: m for m in recap["money_lines"]}
    assert money["GRAND_TOTAL"]["amount"]["mid"] == pytest.approx(
        c["grand_total_usd"]["mid"], abs=1e-3)
    # documented wrap order, verbatim sequence of the money lines
    assert [m["code"] for m in recap["money_lines"]] == [
        "DIRECT_LABOR", "DOWNTIME_M01", "SMALL_TOOLS", "ESCALATION", "TAXES",
        "INSURANCE_BOND", "PM_I05", "OVERHEAD", "PROFIT", "SUBTOTAL",
        "CONTINGENCY_N05", "GRAND_TOTAL"]
    # recap commercial provenance mirrors the config constants + the order
    assert recap["commercial"]["wrap_order"] == [
        "direct_labor_incl_supervision", "downtime_m01_idle_carry",
        "small_tools_allowance", "escalation", "sales_use_tax",
        "insurance_bond", "pm", "overhead", "profit",
        "line_risk_contingency_n05", "grand_total"]
    assert recap["commercial"]["small_tools_pct"] == 3.0
    assert recap["commercial"]["insurance_bond_pct"] == 1.5
    assert recap["commercial"]["escalation"]["applied"] is False


def test_line_risk_contingency_by_quality(recap):
    a01, g03 = line(recap, "A01"), line(recap, "G03")
    assert a01["contingency_pct"] == 7.0      # E rows: placeholder-heavy
    assert g03["contingency_pct"] == 10.0     # G rows: practice/guess anchors
    assert a01["contingency_usd"]["mid"] == pytest.approx(
        a01["labor_usd"]["total"]["mid"] * 0.07, abs=1e-6)
    k10 = line(recap, "K10")
    assert k10["contingency_pct"] == 7.0
    assert line(recap, "M05")["contingency_usd"]["mid"] == pytest.approx(
        (1.125 * TECH) * 0.07, abs=1e-6)


def test_power_plant_helper(recap):
    O02 = line(recap, "O02")
    o04 = line(recap, "O04")
    assert O02["qty"] == 2        # N+1: ceil(30A / 50A) = 1 + 1 spare
    assert o04["qty"] == 3        # Ah = 12 h x 30 A = 360; ceil(360/120) = 3
    assert "360" in o04["derivation"] and "Ah" in o04["derivation"]
    assert O02["derived"] is True and o04["derived"] is True
    # strings are hand-checked above in test_line_factor_stack_hand_math (O04)
    # block lines stack like any other location line (supervision present)
    assert o04["supervision"]["n02_pct"] == 0.115
    # helper warns below the IFC 12-hr minimum and honors opt-ins / pinning
    d = copy.deepcopy(_load_fixture())
    d["locations"][3]["power_plant"] = {"runtime_hours": 6, "load_amps": 30,
                                        "spill_containment": True,
                                        "rectifier_modules": 3}
    r2 = compute_estimate(d)
    assert any("IFC" in w and "12" in w for w in r2["warnings"])
    assert line(r2, "O02")["qty"] == 3            # pinned count wins
    assert line(r2, "O04")["qty"] == 2            # Ah = 6x30 = 180 -> ceil(180/120) = 2
    assert line(r2, "O05")["qty"] == 2            # per string opt-in


def _load_fixture():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_definition_can_be_priced_from_config_not_code(stadium):
    # (a) factor values come from config: flatten occupied -> task hours drop
    est = load_estimating_config()
    mod = copy.deepcopy(est)
    mod["factors"]["occupied"]["low"] = 1.0
    mod["factors"]["occupied"]["high"] = 1.0
    r2 = compute_estimate(stadium, estimating=mod)
    c01 = line(r2, "C01")
    assert c01["hours"]["mid"] == pytest.approx(
        2.0 * 1.0 * F_CON * F_NIGHT * SUP, abs=1e-6)
    # (b) rate card + pm_pct travel with the definition via rate_overrides
    d = copy.deepcopy(stadium)
    d["rate_overrides"] = {"rate_card": {"tech_rate": 100.0}, "pm_pct": 20.0}
    r3 = compute_estimate(d)
    b03 = line(r3, "B03")
    assert b03["task_hours"]["mid"] == pytest.approx(1.5 * F_NIGHT, abs=1e-6)
    assert b03["labor_usd"]["tech"]["mid"] == pytest.approx(1.5 * F_NIGHT * 100.0,
                                                            abs=1e-6)
    assert r3["cost"]["pm_pct"] == 20.0


# ------------------------------------------------------------ validation

def test_validation_errors_are_collective_and_clear():
    def _min(**over):
        d = _load_fixture()
        d["locations"] = [copy.deepcopy(d["locations"][1])]
        d["locations"][0]["lines"] = [{"catalog_id": "E06", "qty": 1}]
        d["travel_moves"] = []   # single-location minimal defs
        d.update(over)
        return d

    with pytest.raises(EstimateError) as exc:
        compute_estimate(_min(locations=[]))
    assert any("locations" in e for e in exc.value.errors)

    with pytest.raises(EstimateError) as exc:
        d = _min()
        d["locations"][0]["lines"].append({"catalog_id": "ZZZ", "qty": 1})
        compute_estimate(d)
    assert any("ZZZ" in e for e in exc.value.errors)

    with pytest.raises(EstimateError) as exc:  # FACTOR rows are not line items
        d = _min()
        d["locations"][0]["lines"][0] = {"catalog_id": "K03", "qty": 1}
        compute_estimate(d)
    assert any("FACTOR" in e for e in exc.value.errors)

    d = _min()
    d["locations"][0]["access"] = "helicopter"
    with pytest.raises(EstimateError):
        compute_estimate(d)

    d = _min()
    d["locations"][0]["conditions"] = ["haunted"]
    with pytest.raises(EstimateError):
        compute_estimate(d)

    with pytest.raises(EstimateError):
        compute_estimate(_min(travel_moves=[{"from": "bowl-lower", "to": "mars"}]))

    with pytest.raises(EstimateError):
        compute_estimate(_min(downtime_windows=[{"days": 0}]))

    with pytest.raises(EstimateError):
        d = _min()
        d["locations"][0]["lines"] = [{"catalog_id": "E06", "qty": -4}]
        compute_estimate(d)

    with pytest.raises(EstimateError):
        d = _min()
        d["locations"][0]["lines"] = [{"catalog_id": "E06", "qty": 1},
                                      {"catalog_id": "E06", "qty": 2}]
        compute_estimate(d)

    with pytest.raises(EstimateError):
        d = _min()
        d["locations"][0]["power_plant"] = {"load_amps": 30}
        d["locations"][0]["lines"].append({"catalog_id": "O04", "qty": 2})
        compute_estimate(d)   # O04 would be double-counted vs the auto-derivation


def test_power_plant_superseded_note_data_present():
    """O-zone supersedes B05/B06 at detail level; B05/B06 stay usable."""
    cat = {u["id"]: u for u in load_labor_catalog()["units"]}
    assert cat["O01"]["item"].lower().find("supersedes") >= 0 or \
        "Supersedes B05/B06" in cat["O01"]["notes"] + cat["O01"]["reference"]
    assert cat["B05"]["kind"] == "minutes" and cat["B05"]["minutes_low"] == 60.0


# ------------------------------------------------------------ smoke: API

def _post(client, url):
    with open(FIXTURE, "rb") as fh:
        return client.post(url, content=fh.read(),
                           headers={"Content-Type": "application/json"})


def test_estimate_api_full_cycle(client, recap):
    r = _post(client, "/estimates")
    assert r.status_code == 200, r.text
    est = r.json()["estimate"]
    assert est["name"] == "Metropolitan Stadium ERCES/DAS build (ROM)"
    got = est["recap"]
    assert got["totals"]["total_crew_hours_mid"] == pytest.approx(
        recap["totals"]["total_crew_hours_mid"], abs=1e-6)
    assert got["cost"]["grand_total_usd"]["mid"] == pytest.approx(
        recap["cost"]["grand_total_usd"]["mid"], abs=1e-3)
    eid = est["id"]

    one = client.get("/estimates/%d" % eid)
    assert one.status_code == 200
    assert one.json()["estimate"]["recap"] == got

    listing = client.get("/estimates").json()["estimates"]
    assert any(e["id"] == eid and e["lines"] == len(recap["lines"])
               and abs(e["grand_total_mid"] - got["cost"]["grand_total_usd"]["mid"]) < 1e-3
               for e in listing)

    dry = client.post("/estimates/compute", json=_load_fixture())
    assert dry.status_code == 200
    assert dry.json()["recap"]["totals"] == got["totals"]
    assert dry.json()["recap"]["cost"] == got["cost"]

    csv_r = client.get("/exports/estimates/%d/recap.csv" % eid)
    assert csv_r.status_code == 200
    assert "text/csv" in csv_r.headers["content-type"]
    raw = list(csv.reader(io.StringIO(csv_r.text)))
    assert raw[0][:4] == ["location", "zone", "catalog_id", "item"]
    a01 = next(rw for rw in raw if rw[2] == "A01")
    assert a01[6] == "E"
    assert a01[10] == "exterior=1.3" and a01[11] == "K05=1.75" \
        and a01[12] == "night=1.325"
    # money rollup is its own section with its own header row
    hdr_i = next(i for i, rw in enumerate(raw) if rw[:2] == ["section", "code"])
    money = list(csv.DictReader(io.StringIO(
        "\n".join(",".join(rw) for rw in raw[hdr_i:]))))
    grand = next(m for m in money if m["code"] == "GRAND_TOTAL")
    assert float(grand["amount_mid"]) == pytest.approx(
        got["cost"]["grand_total_usd"]["mid"], abs=1e-3)
    assert next(m for m in money if m["code"] == "DIRECT_LABOR") is not None

    xl = client.get("/exports/estimates/%d/recap.xlsx" % eid)
    assert xl.status_code == 200
    assert xl.headers["content-type"].endswith("spreadsheetml.sheet")
    assert xl.content[:2] == b"PK"
    zf = zipfile.ZipFile(io.BytesIO(xl.content))
    assert "xl/workbook.xml" in zf.namelist()
    sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    texts = [t.text or "" for t in ET.fromstring(sheet).iter()
             if t.tag.endswith("}t")]
    for probe in ("A01", "E11", "K10", "O04", "GRAND_TOTAL", "DIRECT_LABOR",
                  "Bowl lower level", "CONTINGENCY_N05", "foreman $105/h",
                  "base minutes x condition x access x schedule"):
        assert any(probe in tx for tx in texts), probe

    gone = client.delete("/estimates/%d" % eid)
    assert gone.status_code == 200
    assert client.get("/estimates/%d" % eid).status_code == 404
    assert client.get("/exports/estimates/%d/recap.xlsx" % eid).status_code == 404


def test_estimate_api_rejects_bad_definitions(client):
    bad = _load_fixture()
    bad["locations"][0]["lines"].append({"catalog_id": "NOPE", "qty": 1})
    r = client.post("/estimates", json=bad)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"]["errors"], list)
    assert any("NOPE" in e for e in r.json()["detail"]["errors"])

    fac = _load_fixture()
    fac["locations"][0]["lines"] = [{"catalog_id": "M03", "qty": 1}]
    r = client.post("/estimates", json=fac)
    assert r.status_code == 422
    assert any("FACTOR" in e for e in r.json()["detail"]["errors"])


# ------------------------------------------------------------ smoke: benchmark

def test_benchmark_engine_csv_xlsx_under_5s(recap):
    from romtool.estimate import recap_csv
    from romtool.xlsx import encode_xlsx
    t0 = time.perf_counter()
    n = 10
    for _ in range(n):
        r = compute_estimate(_load_fixture())
        csv_text = recap_csv(r)
        blob = encode_xlsx([{"name": "Recap", "rows": [
            ["a", "b"], [1, 2.5, "K10"]]}])
    dt = (time.perf_counter() - t0) / n
    print("\nbench stadium compute+csv+xlsx: %.4f s/run" % dt)
    assert dt < 5.0
    assert "GRAND_TOTAL" in csv_text and blob[:2] == b"PK"


# ------------------------------------------------------------ smoke: docs

def test_help_documents_estimate_schema(client):
    body = client.get("/help").text
    for probe in ("Class 1 labor estimate", "POST /estimates", "/estimates/compute",
                  "travel_moves", "downtime_windows", "power_plant",
                  "access", "rate_overrides", "recap.xlsx", "recap.csv",
                  "cost-carry", "FACTOR", "supersedes", "B05/B06",
                  "load_amps", "N+1"):
        assert probe in body, "missing on /help: %r" % probe


def test_how_it_works_documents_factor_mechanics(client):
    est = load_estimating_config()
    body = client.get("/how-it-works").text
    for probe in ("Class 1 estimate math", "M03", "M04", "M07", "K03", "K05",
                  "K06", "N01", "N02", "N05", "I05",
                  "1.375", "1.325", "1.24",         # mids + supervision multiplier
                  "0.5",                            # foreman productive haircut
                  "3%", "7%", "10%",                # line-risk contingency bands
                  "89", "3,560",                    # blended rate + idle crew-day
                  "base minutes", "task hours",
                  "K-Access-ADDERS", "crew-week"):
        assert probe in body, "missing on /how-it-works: %r" % probe
    # the factor table is pinned to the shipped config values
    for value in (est["factors"]["occupied"]["low"], est["factors"]["occupied"]["high"],
                  est["access_factors"]["rope"]["low"], est["access_factors"]["rope"]["high"],
                  est["supervision_pct"]["low"], est["supervision_pct"]["high"],
                  est["rate_card"]["tech_rate"], est["rate_card"]["foreman_rate"],
                  est["power_plant"]["battery_string_ah"]):
        assert ("%g" % value) in body, value
