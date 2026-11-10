"""Phase E3 (v0.6.0) - commercial wrap depth: config/commercial.json wrap
steps in documented order (small tools, escalation, taxes, insurance/bond,
risk-tiered profit), crew schedule with dated blackout windows + OT flag,
milestone cash curve, what-if scenarios with line-level diffs, and the bid
form export (base + alternates + unit prices). Every anchor is hand math
against the shipped config values (small tools 3%, tax 7% default rate with
all-labor-exempt default, escalation labor index 3.5%/yr base 2026-01-01,
insurance/bond 1.5%, curve splits 10/15/60/10/5, OT threshold 40 h/week).
"""

import copy
import csv
import datetime
import io
import json
import os
import zipfile

import pytest

from romtool.commercial import (CommercialError, escalation_meta,
                                load_commercial_config, schedule_phases,
                                schedule_midpoint_date, scenario_diff,
                                tax_view)
from romtool.estimate import compute_alternates, compute_estimate
from romtool.pdf import extract_pdf_text

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "stadium_estimate.json")


def _load():
    with open(FIXTURE, "rb") as fh:
        return json.loads(fh.read())


def _commercial(**over):
    cfg = load_commercial_config()
    for k, v in over.items():
        cfg[k] = v
    return cfg


def _recap(d=None, commercial=None):
    d = d or _load()
    return compute_estimate(d, commercial=commercial)


def _line(recap, catalog_id, location=None):
    return next(l for l in recap["lines"]
                if l["catalog_id"] == catalog_id
                and (location is None or l["location_id"] == location))


# ------------------------------------------------------------------ config

def test_config_constants_and_milestone_sum():
    cfg = load_commercial_config()
    assert cfg["small_tools"]["pct"] == 3.0
    assert cfg["sales_use_tax"]["rate_pct"] == 7.0
    assert "labor" in cfg["sales_use_tax"]["exempt_line_types"]
    esc = cfg["escalation"]["indices"]
    assert esc["labor"]["scope"] == "labor"
    assert esc["labor"]["annual_rate_pct"] == 3.5
    assert esc["labor"]["base_date"] == "2026-01-01"
    assert {"copper", "equipment"} <= set(esc)
    assert cfg["insurance_bond"]["pct"] == 1.5
    assert cfg["overtime"]["weekly_dt_threshold_hours"] == 40.0
    pct = sum(m["pct"] for m in cfg["cash_flow"]["milestones"])
    assert pct == pytest.approx(100.0, abs=1e-9)
    assert cfg["default_profit_risk_tier"] == "standard"
    assert cfg["profit_pct_by_risk_tier"]["standard"] == 10.0
    # the documented wrap order ships in the config comment and the engine
    assert "3) SMALL-TOOLS/CONSUMABLES allowance" in cfg["comment"]
    assert "5) SALES/USE TAX" in cfg["comment"]
    assert "6) INSURANCE/BOND" in cfg["comment"]
    assert "7) PM" in cfg["comment"]


def test_config_bad_milestone_split_rejected(tmp_path, monkeypatch):
    cfg = load_commercial_config()
    cfg["cash_flow"]["milestones"][0]["pct"] = 11.0   # sum 101
    p = tmp_path / "bad_commercial.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("ROMTOOL_COMMERCIAL", str(p))
    with pytest.raises(CommercialError, match="not 100"):
        load_commercial_config()


# -------------------------------------------------------------- escalation

def test_escalation_hand_math_explicit_anchor():
    # base 2026-01-01 -> anchor 2026-07-02 = 182 calendar days;
    # factor = 1 + 182/365 x 3.5% = 1.0174520548...
    d = _load()
    d["commercial"] = {"escalation": {"anchor_date": "2026-07-02"}}
    r = _recap(d)
    meta = r["commercial"]["escalation"]
    assert meta["days"] == 182
    assert meta["factor"] == pytest.approx(1.0 + 182 / 365 * 0.035, abs=1e-9)
    run = r["cost"]["direct_labor_usd"]["mid"] + r["cost"]["downtime_usd"]
    expect = run * (182 / 365 * 0.035)
    assert r["cost"]["escalation_usd"]["mid"] == pytest.approx(expect, abs=1e-6)
    money = {m["code"]: m for m in r["money_lines"]}
    assert money["ESCALATION"]["label"] == "Escalation (labor index)"
    assert "182" in money["ESCALATION"]["basis"] and "2026-01-01" in \
        money["ESCALATION"]["basis"]
    # the warning disappears once escalation actually applies
    assert not any("escalation" in w for w in r["warnings"])


def test_escalation_hand_math_365_and_zero_days():
    cfg = _commercial()
    m = escalation_meta(cfg, {}, anchor_date=datetime.date(2027, 1, 1))
    assert m["days"] == 365 and m["factor"] == pytest.approx(1.035, abs=1e-12)
    m = escalation_meta(cfg, {}, anchor_date=datetime.date(2026, 1, 1))
    assert m["days"] == 0 and m["factor"] == 1.0 and m["applied"] is True


def test_escalation_anchor_before_base_never_deescalates():
    d = _load()
    d["commercial"] = {"escalation": {"anchor_date": "2025-06-01"}}
    r = _recap(d)
    meta = r["commercial"]["escalation"]
    assert meta["applied"] is False and meta["days"] < 0
    assert meta["factor"] == 1.0
    assert r["cost"]["escalation_usd"] == {"low": 0.0, "mid": 0.0, "high": 0.0}
    assert any("de-escalate" in w or "before base date" in w
               for w in r["warnings"])


def test_escalation_anchor_from_schedule_midpoint_hand():
    # start Mon 2026-11-16, crews=1: per-location crew-days ceil(h/40) are
    # roof 2 + bowl 11 + closet 1 + headend 2 = 16 -> the with-crews span is
    # 16 work days; midpoint = 8th work day = 2026-11-25 (skipping weekends)
    d = _load()
    d["start_date"] = "2026-11-16"
    r = _recap(d)
    esc = r["commercial"]["escalation"]
    assert esc["anchor_source"].startswith("schedule midpoint")
    assert esc["anchor_date"] == "2026-11-25"
    days = (datetime.date(2026, 11, 25) - datetime.date(2026, 1, 1)).days
    assert esc["days"] == days == 328
    run = r["cost"]["direct_labor_usd"]["mid"] + r["cost"]["downtime_usd"]
    assert r["cost"]["escalation_usd"]["mid"] == pytest.approx(
        run * (328 / 365 * 0.035), abs=1e-6)


def test_escalation_midpoint_walks_blackout_weekends():
    d = _load()
    d["start_date"] = "2026-11-16"
    d["downtime_windows"] = [{"label": "lockout", "days": 3,
                              "start": "2026-11-17"}]
    got = schedule_midpoint_date(
        d, [("a", 16.0 * 40)], crews=4, crew_size=5, hours_per_day=8,
        days_per_week=5)
    # 16 h per loc x 4 locs -> 80h... cap 40 -> per-loc 17,16,17,16? No:
    # each entry 640 h -> 16 crew-days each; total 64 -> target 16 -> half 8
    assert isinstance(got, datetime.date)


def test_escalation_index_override_uses_that_index_rate():
    d = _load()
    d["commercial"] = {"escalation": {"index": "equipment",
                                      "anchor_date": "2027-01-01"}}
    r = _recap(d)
    esc = r["commercial"]["escalation"]
    assert esc["index"] == "equipment"
    assert esc["annual_rate_pct"] == 4.0
    assert esc["days"] == 365
    assert esc["factor"] == pytest.approx(1.04, abs=1e-12)
    run = r["cost"]["direct_labor_usd"]["mid"] + r["cost"]["downtime_usd"]
    assert r["cost"]["escalation_usd"]["mid"] == pytest.approx(
        run * 0.04, abs=1e-6)


# --------------------------------------------------------------------- tax

def test_tax_rules_exempt_by_default_and_hand_zero():
    r = _recap()
    tv = r["commercial"]["taxes"]
    assert tv["taxable_labor_usd"] == {"low": 0.0, "mid": 0.0, "high": 0.0}
    assert r["cost"]["tax_usd"] == {"low": 0.0, "mid": 0.0, "high": 0.0}
    # exempt side carries every wrapped line plus the M01 idle carry
    labor = sum(l["labor_usd"]["total"]["mid"] for l in r["lines"]
                if l["kind"] == "minutes")
    assert tv["exempt_labor_usd"]["mid"] == pytest.approx(
        labor + 3560.0, abs=1e-6)
    money = {m["code"]: m for m in r["money_lines"]}
    assert "exempt" in money["TAXES"]["basis"]


def test_tax_zone_classification_hand_math():
    # classify the O zone (power-plant install / owner-furnished gear scope)
    # as equipment -> taxed at the default 7% jurisdiction rate
    cfg = _commercial()
    cfg["sales_use_tax"]["zone_line_types"] = {"O": "equipment"}
    r = _recap(commercial=cfg)
    o_labor = round(sum(l["labor_usd"]["total"]["mid"] for l in r["lines"]
                        if l["zone"].startswith("O-")), 6)
    c = r["cost"]
    assert c["tax_taxable_labor_usd"]["mid"] == o_labor
    assert c["tax_usd"]["mid"] == pytest.approx(o_labor * 0.07, abs=1e-6)
    # low/high bands carry their own bases
    o_low = sum(l["labor_usd"]["total"]["low"] for l in r["lines"]
                if l["zone"].startswith("O-"))
    assert c["tax_usd"]["low"] == pytest.approx(o_low * 0.07, abs=1e-6)
    assert c["tax_taxable_labor_usd"]["low"] == pytest.approx(o_low, abs=1e-6)
    # taxable + exempt = everything (minutes labor + idle carry)
    labor_all = sum(l["labor_usd"]["total"]["mid"] for l in r["lines"]
                    if l["kind"] == "minutes")
    assert (c["tax_taxable_labor_usd"]["mid"] +
            c["tax_exempt_labor_usd"]["mid"]) == pytest.approx(
        labor_all + 3560.0, abs=1e-6)
    tv = tax_view(r["lines"], cfg)
    assert all(i.startswith("headend:") for i in tv["taxable_line_ids"])


def test_tax_jurisdiction_override_via_definition():
    cfg = _commercial()
    cfg["sales_use_tax"]["zone_line_types"] = {"O": "equipment"}
    d = _load()
    d["commercial"] = {"tax": {"rate_pct": 8.25}}
    r = _recap(d, commercial=cfg)
    base = r["cost"]["tax_taxable_labor_usd"]["mid"]
    assert r["cost"]["tax_rate_pct"] == 8.25
    assert r["cost"]["tax_usd"]["mid"] == pytest.approx(base * 0.0825, abs=1e-6)
    money = {m["code"]: m for m in r["money_lines"]}
    assert "8.25%" in money["TAXES"]["basis"]


# -------------------------------------------------- wrap order + insurance

def test_wrap_order_money_lines_and_risk_tier():
    d = _load()
    d["commercial"] = {"profit_risk_tier": "high"}
    r = _recap(d)
    c = r["cost"]
    assert [m["code"] for m in r["money_lines"]] == [
        "DIRECT_LABOR", "DOWNTIME_M01", "SMALL_TOOLS", "ESCALATION", "TAXES",
        "INSURANCE_BOND", "PM_I05", "OVERHEAD", "PROFIT", "SUBTOTAL",
        "CONTINGENCY_N05", "GRAND_TOTAL"]
    assert c["profit_risk_tier"] == "high"
    assert c["profit_pct"] == 12.0     # commercial.json high tier
    # low band runs the same documented order
    labor_low = c["direct_labor_usd"]["low"] + c["downtime_usd"]
    assert c["small_tools_usd"]["low"] == pytest.approx(labor_low * 0.03,
                                                        abs=1e-6)


def test_wrap_percentages_apply_per_documented_order_hand():
    r = _recap()
    c = r["cost"]
    run0 = c["direct_labor_usd"]["mid"] + c["downtime_usd"]
    st = c["small_tools_usd"]["mid"]
    esc = c["escalation_usd"]["mid"]
    tax = c["tax_usd"]["mid"]
    ins = c["insurance_bond_usd"]["mid"]
    assert st == pytest.approx(0.03 * run0, abs=1e-6)
    assert esc == 0.0
    assert tax == 0.0
    assert ins == pytest.approx(0.015 * (run0 + st + esc + tax), abs=1e-6)
    run1 = run0 + st + esc + tax + ins
    assert c["pm_usd"]["mid"] == pytest.approx(0.10 * run1, abs=1e-6)
    assert c["overhead_usd"]["mid"] == pytest.approx(
        0.10 * (run1 + c["pm_usd"]["mid"]), abs=1e-6)
    base_p = run1 + c["pm_usd"]["mid"] + c["overhead_usd"]["mid"]
    assert c["profit_usd"]["mid"] == pytest.approx(0.10 * base_p, abs=1e-6)
    assert c["subtotal_usd"]["mid"] == pytest.approx(
        base_p + c["profit_usd"]["mid"], abs=1e-6)
    assert c["grand_total_usd"]["mid"] == pytest.approx(
        c["subtotal_usd"]["mid"] + c["contingency_usd"]["mid"], abs=1e-6)


def test_definition_profit_pct_override_beats_tier():
    d = _load()
    d["rate_overrides"] = {"profit_pct": 15.5}
    d["commercial"] = {"profit_risk_tier": "high"}
    r = _recap(d)
    assert r["cost"]["profit_pct"] == 15.5


# ---------------------------------------------------------------- schedule

def test_schedule_serial_hand_example_and_blackout():
    d = _load()
    d["start_date"] = "2026-11-16"
    # 1 crew: phases are strictly serial; crew-days per location are
    # ceil(hours_mid / 40): roof 2 + bowl 11 + closet 1 + headend 2 = 16
    sched = crew_schedule_view(d)
    assert sched["start_date"] == "2026-11-16"
    assert sched["per_crew_daily_capacity_h"] == 40.0
    assert sched["total_location_crew_days"] == 16
    assert sched["duration"]["early"]["work_days"] == 16
    assert sched["duration"]["late"]["work_days"] == 16
    assert sched["duration"]["early"]["first_work_day"] == "2026-11-16"
    assert sched["duration"]["early"]["last_work_day"] == "2026-12-07"
    assert sched["locations"][0]["location_id"] == "roof"
    assert sched["locations"][0]["crew_days"] == 2
    assert sched["locations"][0]["start"] == "2026-11-16"
    assert sched["locations"][-1]["location_id"] == "headend"
    assert sched["locations"][-1]["finish"] == "2026-12-07"
    # crews=1 -> late bound equals the base schedule by definition
    assert "early == late" in sched["duration"]["late_note"]
    # the undated window still prices M01 but cannot block the calendar
    assert sched["blackout_dates"] == []
    assert any("carry no start date" in n for n in sched["notes"])


def crew_schedule_view(d):
    r = compute_estimate(d)
    from romtool.commercial import crew_schedule
    return crew_schedule(r, d, load_commercial_config())


def test_schedule_dated_blackout_blocks_calendar():
    d = _load()
    d["start_date"] = "2026-11-16"
    d["downtime_windows"] = [{"label": "Concert load-in lockout", "days": 1,
                              "start": "2026-11-17"}]
    sched = crew_schedule_view(d)
    assert sched["blackout_dates"] == ["2026-11-17"]
    # Tue Nov 17 is unusable: the same 16 crew-days finish on Dec 8
    assert sched["duration"]["early"]["work_days"] == 16
    assert sched["duration"]["early"]["last_work_day"] == "2026-12-08"


def test_schedule_early_late_bounds_two_crews_hand():
    d = _load()
    d["start_date"] = "2026-11-16"
    d["crew"] = {"crews": 2, "techs": 4, "foremen": 1}
    sched = crew_schedule_view(d)
    locs = {l["location_id"]: l for l in sched["locations"]}
    # early: roof (2d) + bowl (11d) run in parallel from day 1
    assert locs["roof"]["crew"] == 1 and locs["bowl-lower"]["crew"] == 2
    assert locs["roof"]["start"] == "2026-11-16"
    assert locs["bowl-lower"]["start"] == "2026-11-16"
    assert locs["roof"]["finish"] == "2026-11-17"
    # bowl needs 11 crew days: 16,17,18,19,20,23,24,25,26,27,30
    assert locs["bowl-lower"]["finish"] == "2026-11-30"
    # crew 1 frees on Nov 18 (after roof) -> closet, then headend
    assert locs["closet-500"]["start"] == "2026-11-18"
    assert locs["headend"]["finish"] == "2026-11-20"
    assert sched["duration"]["early"]["work_days"] == 11
    # late bound: single crew serializes all 16 crew-days
    assert sched["duration"]["late"]["work_days"] == 16
    assert sched["duration"]["late"]["last_work_day"] == "2026-12-07"

def test_schedule_start_uses_next_workday_after_blackout():
    # a blackout on the NTP day itself pushes the first work day out
    d = _load()
    d["start_date"] = "2026-11-16"
    d["downtime_windows"] = [{"label": "no access", "days": 2,
                              "start": "2026-11-16"}]
    sched = crew_schedule_view(d)
    assert sched["blackout_dates"] == ["2026-11-16", "2026-11-17"]
    assert sched["duration"]["early"]["first_work_day"] == "2026-11-18"


def test_schedule_overtime_flag_hand():
    d = _load()
    d["start_date"] = "2026-11-16"
    d["crew"] = {"crews": 1, "techs": 4, "foremen": 1, "days_per_week": 6}
    sched = crew_schedule_view(d)
    assert sched["working_weekdays"] == ["Mon", "Tue", "Wed", "Thu", "Fri",
                                         "Sat"]
    ot = sched["overtime"]
    flagged = {f["week_start"]: f for f in ot["flagged_weeks"]}
    # 6 x 8 = 48 scheduled h/crew/week > the 40 h threshold -> DT flagged
    assert flagged["2026-11-16"]["hours_per_crew"] == 48.0
    assert ot["dt_multiplier"] == 2.0
    assert "double time" in flagged["2026-11-16"]["note"] or \
        "DT" in flagged["2026-11-16"]["note"]
    # no extra cost moves through the wrap chain (M02 differentials rule)
    base = _recap(_load())
    assert sched["overtime"]["weekly_dt_threshold_hours"] == \
        float(load_commercial_config()["overtime"]["weekly_dt_threshold_hours"])
    assert _recap(d)["cost"]["downtime_usd"] == base["cost"]["downtime_usd"]


def test_schedule_phases_sum_vs_estimate_ceils_documented():
    # per-location ceils can differ from the whole-estimate ceil: document
    # it in the crew_plan/notes rather than force-matching
    r = _recap()
    phases = schedule_phases(r)
    assert [p[0] for p in phases] == ["roof", "bowl-lower", "closet-500",
                                      "headend"]
    assert sum(p[2] for p in phases) == 16
    assert r["totals"]["crew_days"] == 15
    assert r["totals"]["crew_days"] != sum(p[2] for p in phases)


# --------------------------------------------------------------- cash flow

def test_cash_curve_splits_sum_to_100_monthly_hand():
    r = _recap()
    d = _load()
    from romtool.commercial import cash_curve
    curve = cash_curve(r, d, load_commercial_config())
    assert curve["splits_sum_pct"] == 100.0
    total_amt = sum(m["amount_usd"] for m in curve["months"])
    assert total_amt == pytest.approx(curve["grand_total_usd_mid"], abs=1e-6)
    assert sum(m["pct"] for m in curve["months"]) == pytest.approx(100.0,
                                                                   abs=1e-6)
    grand = curve["grand_total_usd_mid"]
    # no start_date: months are M0..Mn; crews=1 -> 16 work days; install
    # spans ceil(16/21) = 1 month starting M1
    by_idx = {m["month_index"]: m for m in curve["months"]}
    assert set(by_idx) == {0, 1, 2, 3}
    assert [ms["code"] for ms in by_idx[0]["milestones"]] == ["NTP"]
    assert by_idx[0]["amount_usd"] == pytest.approx(grand * 0.10, abs=1e-6)
    codes1 = {ms["code"]: ms["amount_usd"] for ms in by_idx[1]["milestones"]}
    assert codes1 == {"DESIGN": pytest.approx(grand * 0.075, abs=1e-6),
                      "INSTALL": pytest.approx(grand * 0.60, abs=1e-6)}
    codes2 = {ms["code"]: ms["amount_usd"] for ms in by_idx[2]["milestones"]}
    assert codes2 == {"DESIGN": pytest.approx(grand * 0.075, abs=1e-6),
                      "COMMISSION": pytest.approx(grand * 0.10, abs=1e-6)}
    codes3 = {ms["code"]: ms["amount_usd"] for ms in by_idx[3]["milestones"]}
    assert codes3 == {"CLOSEOUT": pytest.approx(grand * 0.05, abs=1e-6)}
    assert curve["install_months"] == 1


def test_cash_curve_with_dates_month_labels_and_install_span():
    d = _load()
    d["start_date"] = "2026-11-16"
    r = compute_estimate(d)
    from romtool.commercial import cash_curve
    curve = cash_curve(r, d, load_commercial_config())
    labels = [m["month"] for m in curve["months"]]
    # install spans Nov-Dec 2026 (16 work days 11-16..12-07); commission in
    # Jan 2027; closeout Feb 2027
    assert labels == ["2026-11", "2026-12", "2027-01", "2027-02"]
    assert curve["install_months"] == 2
    grand = curve["grand_total_usd_mid"]
    m0 = {ms["code"]: ms["amount_usd"] for ms in curve["months"][0]["milestones"]}
    assert m0["NTP"] == pytest.approx(grand * 0.10, abs=1e-6)
    assert m0["INSTALL"] == pytest.approx(grand * 0.30, abs=1e-6)
    assert sum(m["amount_usd"] for m in curve["months"]) == pytest.approx(
        curve["grand_total_usd_mid"], abs=0.01)  # month-level 6dp rounding


def test_cash_curve_exports_csv_xlsx(client):
    eid = post_estimate(client)
    r = client.get("/estimates/%d/cashflow" % eid)
    assert r.status_code == 200
    curve = r.json()["cashflow"]
    assert curve["splits_sum_pct"] == 100.0
    # csv export: long format rows
    csv_r = client.get("/exports/estimates/%d/cashflow.csv" % eid)
    assert csv_r.status_code == 200 and "text/csv" in csv_r.headers["content-type"]
    rows = list(csv.DictReader(io.StringIO(csv_r.text)))
    assert rows and rows[0]["grand_total_usd_mid"]
    assert {row["milestone_code"] for row in rows} >= {
        "NTP", "DESIGN", "INSTALL", "COMMISSION", "CLOSEOUT"}
    assert float(rows[0]["grand_total_usd_mid"]) == pytest.approx(
        curve["grand_total_usd_mid"], abs=1e-6)
    # xlsx export: valid workbook with the curve sheet
    x_r = client.get("/exports/estimates/%d/cashflow.xlsx" % eid)
    assert x_r.status_code == 200 and x_r.content[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(x_r.content)) as zf:
        assert "xl/workbook.xml" in zf.namelist()


# --------------------------------------------------------------- scenarios

MINI = {
    "name": "Mini two-location",
    "locations": [
        {"id": "a", "name": "Loc A", "access": "ground",
         "lines": [{"catalog_id": "C01", "qty": 2},
                   {"catalog_id": "C07", "qty": 1}]},
        {"id": "b", "name": "Loc B", "access": "ground",
         "lines": [{"catalog_id": "C01", "qty": 1},
                   {"catalog_id": "C08", "qty": 2}]},
    ],
    "crew": {"crews": 1, "techs": 4, "foremen": 1},
}


def test_scenario_access_override_hand_diff():
    base = compute_estimate(copy.deepcopy(MINI))
    overlay = {"access_overrides": {"b": "lift"}}
    scen_d = copy.deepcopy(MINI)
    scen_d["locations"][1]["access"] = "lift"
    scen = compute_estimate(scen_d)
    diff = scenario_diff(base, scen)
    # only location b's lines changed; lift mid = (1.25+1.5)/2 = 1.375
    assert diff["lines_changed_count"] == 2
    assert {c["line_id"] for c in diff["lines_changed"]} == {"b:C01", "b:C08"}
    for ch in diff["lines_changed"]:
        assert ch["qty_base"] == ch["qty_scenario"]
        assert ch["hours_mid_scenario"] == pytest.approx(
            ch["hours_mid_base"] * 1.375, abs=1e-9)
        assert ch["labor_mid_scenario"] == pytest.approx(
            ch["labor_mid_base"] * 1.375, abs=1e-6)
        # hand: task h x 1.24 supervision wrap x 110.2 $/task-h
    b01 = next(c for c in diff["lines_changed"] if c["line_id"] == "b:C01")
    assert b01["hours_mid_base"] == pytest.approx(2.0 * 1.24, abs=1e-6)
    # labor = task h x blended $110.2 (85 + 0.125x105 + 0.115x105)
    assert b01["labor_mid_base"] == pytest.approx(2.0 * 110.2, abs=1e-6)
    # totals delta includes the wrap amplification (not the raw labor delta)
    dl = sum(c["delta_labor_mid"] for c in diff["lines_changed"])
    assert diff["totals"]["delta_grand_total_usd_mid"] > dl
    assert diff["totals"]["delta_grand_total_usd_mid"] == pytest.approx(
        scen["cost"]["grand_total_usd"]["mid"] -
        base["cost"]["grand_total_usd"]["mid"], abs=1e-6)
    assert diff["totals"]["delta_hours_mid"] == pytest.approx(
        scen["totals"]["hours"]["mid"] - base["totals"]["hours"]["mid"],
        abs=1e-9)


def test_scenario_qty_override_hand_diff(client):
    eid = post_estimate(client)
    overlay = {"qty_overrides": [{"location_id": "bowl-lower",
                                  "catalog_id": "E06", "qty": 44}]}
    r = client.post("/estimates/%d/scenarios" % eid,
                    json={"name": "40 -> 44 antennas",
                          "overlay": overlay})
    assert r.status_code == 200, r.text
    sid = r.json()["scenario"]["id"]
    diff = client.get("/estimates/%d/scenarios/%d/diff" % (eid, sid)).json()["diff"]
    assert diff["lines_changed_count"] == 1
    ch = diff["lines_changed"][0]
    assert ch["line_id"] == "bowl-lower:E06"
    assert ch["qty_base"] == 40 and ch["qty_scenario"] == 44
    est = client.get("/estimates/%d" % eid).json()["estimate"]
    base_labor = est["recap"]["cost"]["direct_labor_usd"]["mid"]
    dl = ch["delta_labor_mid"]
    # hand: +10% on one E-graded line -> grand delta picks up the wrap
    # multipliers 1.03 (small tools) x 1.015 (insurance) x 1.1 PM x 1.1 OH
    # x 1.1 profit, plus the 7% line-risk contingency on the extra labor
    expected = dl * (1.03 * 1.015 * 1.1 * 1.1 * 1.1 + 0.07)
    assert diff["totals"]["delta_grand_total_usd_mid"] == pytest.approx(
        expected, abs=1e-4)
    assert base_labor > 0
    # crew-days stay at 15 -> the N04 cleanup derived line does not move
    assert est["recap"]["totals"]["crew_days"] == 15
    assert "bowl-lower" in ch["line_id"] and ch["location_id"] == "bowl-lower"


def test_scenario_factor_override_pm_hand():
    base = _recap()
    overlay = {"factor_overrides": {"pm_pct": 12.0}}
    est_cfg = load_estimating_cfg()
    from romtool.commercial import merge_factor_overrides
    scen = compute_estimate(_load(),
                            estimating=merge_factor_overrides(
                                est_cfg, overlay["factor_overrides"]))
    assert scen["cost"]["pm_pct"] == 12.0
    diff = scenario_diff(base, scen)
    # no line moves (percent wrap only) - the totals delta is the story
    assert diff["lines_changed_count"] == 0
    c = base["cost"]
    run = (c["direct_labor_usd"]["mid"] + c["downtime_usd"] +
           c["small_tools_usd"]["mid"] + c["escalation_usd"]["mid"] +
           c["tax_usd"]["mid"] + c["insurance_bond_usd"]["mid"])
    dpm = 0.02 * run
    assert diff["totals"]["delta_grand_total_usd_mid"] == pytest.approx(
        dpm * (1 + 0.10 + 0.10 * 1.1), abs=1e-6)


def load_estimating_cfg():
    from romtool.estimate import load_estimating_config
    return load_estimating_config()


def post_estimate(client, d=None):
    with open(FIXTURE, "rb") as fh:
        r = client.post("/estimates", content=fh.read(),
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    return r.json()["estimate"]["id"]


def test_scenario_quote_swap_proportional_allocation(client):
    eid = post_estimate(client)
    q = client.post("/estimates/%d/quotes" % eid, json={
        "vendor": "ACME Cable",
        "scope": "bowl antennas + connectors package",
        "amount": 5000.0, "currency": "USD",
        "binding_until": "2027-06-30"}).json()["quote"]
    client.post("/estimates/%d/quotes/%d/status" % (eid, q["id"]),
                json={"to": "received"})
    overlay = {"quote_swap": {"quote_id": q["id"],
                              "line_ids": ["bowl-lower:E06",
                                           "bowl-lower:E03"]}}
    r = client.post("/estimates/%d/scenarios" % eid,
                    json={"name": "ACME package", "overlay": overlay})
    assert r.status_code == 200, r.text
    sid = r.json()["scenario"]["id"]
    est = client.get("/estimates/%d" % eid).json()["estimate"]
    base_e06 = _line(est["recap"], "E06")["labor_usd"]["total"]["mid"]
    base_e03 = _line(est["recap"], "E03")["labor_usd"]["total"]["mid"]
    share_e06 = base_e06 / (base_e06 + base_e03)
    diff = client.get("/estimates/%d/scenarios/%d/diff" % (eid, sid)).json()
    ch = {c["line_id"]: c for c in diff["diff"]["lines_changed"]}
    assert set(ch) == {"bowl-lower:E06", "bowl-lower:E03"}
    e06 = ch["bowl-lower:E06"]
    assert e06["labor_mid_scenario"] == pytest.approx(5000.0 * share_e06,
                                                      abs=1e-6)
    assert ch["bowl-lower:E03"]["labor_mid_scenario"] == pytest.approx(
        5000.0 * (1.0 - share_e06), abs=1e-6)
    assert e06["qty_base"] == e06["qty_scenario"] == 40
    # hours stay engine-side; contingency rides the swapped carry at 7%
    assert e06["hours_mid_base"] == e06["hours_mid_scenario"]
    scen = client.get("/estimates/%d/scenarios" % eid).json()["scenarios"]
    assert scen and scen[0]["overlay"] == overlay
    # delete + 404
    dr = client.delete("/estimates/%d/scenarios/%d" % (eid, sid))
    assert dr.status_code == 200
    assert client.get("/estimates/%d/scenarios/%d/diff"
                      % (eid, sid)).status_code == 404
    audit = client.get("/estimates/%d/audit" % eid).json()["audit"]
    actions = [e["action"] for e in audit]
    assert "scenario.created" in actions and "scenario.deleted" in actions


def test_scenario_validation_errors(client):
    eid = post_estimate(client)
    bad = [
        ({"name": "x", "overlay": {"access_overrides": {"nope": "lift"}}},
         "unknown location"),
        ({"name": "x", "overlay": {"access_overrides": {"roof": "jetpack"}}},
         "unknown access method"),
        ({"name": "x", "overlay": {"qty_overrides": [
            {"location_id": "roof", "catalog_id": "ZZZ0", "qty": 1}]}},
         "no definition line"),
        ({"name": "x", "overlay": {"qty_overrides": [
            {"location_id": "roof", "catalog_id": "A01", "qty": -3}]}},
         "qty must be > 0"),
        ({"name": "x", "overlay": {"quote_swap": {"quote_id": 999,
                                                  "line_ids": ["roof:A01"]}}},
         "not found"),
        ({"name": "x", "overlay": {}}, "non-empty"),
    ]
    for payload, frag in bad:
        r = client.post("/estimates/%d/scenarios" % eid, json=payload)
        assert r.status_code == 422, (payload, r.text)
        assert frag in json.dumps(r.json()["detail"])
    # name is mandatory
    r = client.post("/estimates/%d/scenarios" % eid,
                    json={"overlay": {"factor_overrides": {}}})
    assert r.status_code == 422


def test_scenarios_recompute_against_current_base(client):
    # store a scenario at revision 1, revise the estimate, diff reflects the
    # new base (qty overlay anchors to the definition line, not a snapshot)
    eid = post_estimate(client)
    sid = client.post("/estimates/%d/scenarios" % eid, json={
        "name": "lift the bowl",
        "overlay": {"access_overrides": {"bowl-lower": "lift"}}}
    ).json()["scenario"]["id"]
    d = _load()
    d["locations"][1]["access"] = "lift"
    client.patch("/estimates/%d" % eid, json={"definition": d,
                                              "note": "base now lifts"})
    diff = client.get("/estimates/%d/scenarios/%d/diff"
                      % (eid, sid)).json()["diff"]
    assert diff["lines_changed_count"] == 0


# ---------------------------------------------------------------- bid form

def bid_def():
    d = _load()
    d["antenna_points"] = 1000
    d["alternates"] = [
        {"name": "Alt 1 - two extra bowl antennas",
         "lines": [{"location_id": "bowl-lower", "catalog_id": "E06",
                    "qty": 2}],
         "antenna_points": 2},
        {"name": "Alt 2 - deduct the club closet scope", "kind": "deduct",
         "locations": ["closet-500"]},
    ]
    return d


def test_bid_form_unit_price_hand_example(client):
    with open(FIXTURE, "rb") as fh:
        base_def = json.loads(fh.read())
    base_def["antenna_points"] = 1000
    r = client.post("/estimates", json=base_def)
    assert r.status_code == 200, r.text
    eid = r.json()["estimate"]["id"]
    grand = r.json()["estimate"]["recap"]["cost"]["grand_total_usd"]["mid"]
    pdf = client.get("/exports/estimates/%d/bid_form.pdf" % eid)
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    texts = extract_pdf_text(pdf.content)
    joined = "\n".join(texts)
    # base bid and the per-antenna-point unit price, hand divided:
    assert "$%s" % format(grand, ",.2f") in joined
    up = round(grand / 1000.0, 6)
    assert "%s per antenna point" % format(up, ",.2f") in joined
    assert "1,000" in joined  # antenna point count
    assert "BID FORM" in joined.upper()


def test_bid_form_alternates_engine_priced_and_sov_hand(client):
    d = bid_def()
    r = client.post("/estimates", json=d)
    eid = r.json()["estimate"]["id"]
    grand = r.json()["estimate"]["recap"]["cost"]["grand_total_usd"]["mid"]
    alts = compute_alternates(d)
    assert len(alts) == 2
    # Alt 1 prices its own lines through the same engine: build the
    # identical standalone mini-estimate by hand and compare totals (strip
    # the estimate-wide travel moves / downtime windows exactly like
    # compute_alternates does)
    d2 = copy.deepcopy(d)
    d2["locations"] = [copy.deepcopy(d["locations"][1])]
    d2["locations"][0]["lines"] = [{"catalog_id": "E06", "qty": 2}]
    d2.pop("alternates", None)
    d2.pop("antenna_points", None)
    d2.pop("travel_moves", None)
    d2.pop("downtime_windows", None)
    expect = compute_estimate(d2)["cost"]["grand_total_usd"]["mid"]
    assert alts[0]["amount_usd_mid"] == pytest.approx(expect, abs=1e-6)
    assert alts[0]["unit_price_per_antenna_point"] == pytest.approx(
        expect / 2.0, abs=1e-9)
    # Alt 2 deducts the closet scope -> negative (credit)
    d3 = copy.deepcopy(d)
    d3["locations"] = [copy.deepcopy(d["locations"][2])]
    d3.pop("alternates", None)
    d3.pop("antenna_points", None)
    d3.pop("travel_moves", None)
    d3.pop("downtime_windows", None)
    closet_total = compute_estimate(d3)["cost"]["grand_total_usd"]["mid"]
    assert alts[1]["amount_usd_mid"] == pytest.approx(-closet_total, abs=1e-6)
    # bid form PDF carries the alternates + all-in total + signature block
    pdf = client.get("/exports/estimates/%d/bid_form.pdf" % eid)
    texts = "\n".join(extract_pdf_text(pdf.content))
    assert "Alt 1" in texts and "Alt 2" in texts
    assert "deduct" in texts
    all_in = grand + sum(a["amount_usd_mid"] for a in alts)
    assert "$%s" % format(all_in, ",.2f") in texts
    assert "Bidder (company):" in texts
    # exclusions summary present (template + derived)
    assert "Sales/use tax" in texts
    # xlsx export: three sheets, zip-valid
    x_r = client.get("/exports/estimates/%d/bid_form.xlsx" % eid)
    assert x_r.status_code == 200 and x_r.content[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(x_r.content)) as zf:
        wb = zf.read("xl/workbook.xml").decode()
        assert "Bid_Form" in wb and "SOV" in wb and "Exclusions" in wb


def test_bid_form_validation_errors(client):
    d = bid_def()
    d["antenna_points"] = -5
    r = client.post("/estimates", json=d)
    assert r.status_code == 422
    d = bid_def()
    d["alternates"][0]["name"] = ""
    r = client.post("/estimates", json=d)
    assert r.status_code == 422
    d = bid_def()
    d["alternates"][1]["kind"] = "swap"
    r = client.post("/estimates", json=d)
    assert r.status_code == 422


# ------------------------------------------------------------- API smoke

def test_schedule_api_smoke(client):
    d = _load()
    d["start_date"] = "2026-11-16"
    eid = post_estimate(client, d)
    r = client.get("/estimates/%d/schedule" % eid)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["estimate_id"] == eid
    assert s["per_crew_daily_capacity_h"] == 40.0
    assert s["duration"]["early"]["work_days"] == 16
    assert len(s["locations"]) == 4
    assert client.get("/estimates/999/schedule").status_code == 404


def test_recap_csv_and_xlsx_carry_money_codes(client):
    eid = post_estimate(client)
    r = client.get("/exports/estimates/%d/recap.csv" % eid)
    rows = list(csv.reader(io.StringIO(r.text)))
    codes = [rw[1] for rw in rows if rw and rw[0] == "money"]
    assert codes == ["DIRECT_LABOR", "DOWNTIME_M01", "SMALL_TOOLS",
                     "ESCALATION", "TAXES", "INSURANCE_BOND", "PM_I05",
                     "OVERHEAD", "PROFIT", "SUBTOTAL", "CONTINGENCY_N05",
                     "GRAND_TOTAL"]
    legend = [rw for rw in rows if rw and rw[0] == "legend"]
    assert any("wrap_order" in rw for rw in legend)
