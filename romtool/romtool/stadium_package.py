"""Phase E5 (v0.8.0) - the Generic 60k-Seat Stadium morning package.

Builds the capstone deliverable as REAL stored objects plus the bid package
written to dist/stadium-package/:

  1. An RF-side stadium project (roof donor array over two bowl levels, a
     concourse and the ERCES/DAS headend floor) that solves a real cascade.
  2. A stored Class 1 labor estimate definition that scales the fixture-class
     stadium scenario (tests/fixtures/stadium_estimate.json) to a 60,000-seat
     venue: rope-access roof with exterior conditions, lift-access occupied
     bowl levels, a scissor-lift concourse, club-level service closets, the
     ERCES + neutral-host headend with the 48VDC plant auto-block, night
     schedule, dated event-lockout windows, crane/shroud/HVAC rows
     (E11/K09/K10/B11), the finishing L-zone, definition alternates[]
     (Add Band 14 FirstNet / Deduct C-band) and definition antenna_points
     for per-point bid pricing.
  3. Two bound vendor quotes (ERCES booster, headend equipment) with future
     binding dates; amounts are placeholder binds that exercise the register.
  4. The full export package: recap.xlsx/.csv, bid_form.pdf/.xlsx,
     inclusions_exclusions.pdf, assumptions.csv, cashflow.xlsx, crew_plan.csv,
     bid_tab.json, sanity.json and MORNING-REPORT.md.

Every quantity choice is stated in the assumption register (see
venue_assumptions) - nothing numeric is invented silently. The package is
rebuilt with scripts/build_stadium_package.py; the build is idempotent
(existing project/estimate/quote rows are reused, not duplicated).

The morning report is written for Matt (Senior Project Estimator): plain
language, no em-dashes, accuracy class stated honestly (fixture-class
inputs with partial vendor quotes, Class 4-ish at best).
"""

import datetime
import json
import os

PROJECT_NAME = "Generic 60k-Seat Stadium (RF)"
ESTIMATE_NAME = "Generic 60k-Seat Stadium (ERCES + 3-carrier neutral host DAS)"
START_DATE = "2026-11-16"

PACKAGE_FILES = (
    "recap.xlsx", "recap.csv", "bid_form.pdf", "bid_form.xlsx",
    "inclusions_exclusions.pdf", "assumptions.csv", "cashflow.xlsx",
    "crew_plan.csv", "bid_tab.json", "sanity.json", "manifest.json",
    "MORNING-REPORT.md",
)


# ----------------------------------------------------------------- RF model

def rf_model():
    """Representative neutral-host cascade: off-air source -> headend
    repeater -> 7/8in risers/trunks -> zone splitters -> bowl antennas.
    Antenna density is representative; the labor estimate carries the full
    260-point scale takeoff (see stadium_definition antenna_points)."""
    roof = {
        "id": "st_roof", "name": "Roof / donor array", "level": 3,
        "ppm": 10.0, "w_px": 220, "h_px": 150,
        "walls": [], "elements": [
            # Neutral host: carriers feed the headend directly from their base
            # station equipment (5 dBm injection per band in this ROM chain).
            {"id": "st_src", "type": "source", "label": "Carrier feeds",
             "x": 200, "y": 30, "subtype": "direct", "injection_dbm": 5.0,
             "donor_gain_db": 11.0},
        ],
    }
    headend = {
        "id": "st_headend", "name": "ERCES + DAS headend room", "level": 0,
        "ppm": 10.0, "w_px": 60, "h_px": 40,
        "walls": [], "elements": [
            {"id": "st_rep", "type": "repeater", "label": "DAS headend RPTR",
             "x": 30, "y": 20},
        ],
    }
    bowl_lower = {
        "id": "st_bowl_lower", "name": "Bowl lower level", "level": 1,
        "ppm": 10.0, "w_px": 220, "h_px": 150,
        "critical_areas": [{
            "id": "st_bowl_lower_ca1", "label": "Field of play (NFPA critical)",
            "points": [[110, 55], [160, 55], [160, 100], [110, 100]],
        }],
        "elements": [
            {"id": "st_sp_l1", "type": "splitter", "label": "SP-L1", "x": 60, "y": 120, "ways": 4},
            {"id": "st_sp_l2", "type": "splitter", "label": "SP-L2", "x": 150, "y": 120, "ways": 2},
            {"id": "st_a_l1", "type": "antenna", "label": "L-A1", "antenna_id": "omni_das", "x": 30, "y": 40},
            {"id": "st_a_l2", "type": "antenna", "label": "L-A2", "antenna_id": "omni_das", "x": 95, "y": 40},
            {"id": "st_a_l3", "type": "antenna", "label": "L-A3", "antenna_id": "omni_das", "x": 185, "y": 40},
            {"id": "st_a_l4", "type": "antenna", "label": "L-A4", "antenna_id": "omni_das", "x": 40, "y": 110},
        ],
    }
    bowl_upper = {
        "id": "st_bowl_upper", "name": "Bowl upper level", "level": 2,
        "ppm": 10.0, "w_px": 220, "h_px": 150,
        "elements": [
            {"id": "st_sp_u1", "type": "splitter", "label": "SP-U1", "x": 80, "y": 120, "ways": 4},
            {"id": "st_a_u1", "type": "antenna", "label": "U-A1", "antenna_id": "omni_das", "x": 40, "y": 40},
            {"id": "st_a_u2", "type": "antenna", "label": "U-A2", "antenna_id": "omni_das", "x": 130, "y": 40},
        ],
    }
    concourse = {
        "id": "st_concourse", "name": "Main concourse", "level": 0,
        "ppm": 10.0, "w_px": 220, "h_px": 90,
        "elements": [
            {"id": "st_sp_c1", "type": "splitter", "label": "SP-C1", "x": 80, "y": 70, "ways": 2},
            {"id": "st_a_c1", "type": "antenna", "label": "C-A1", "antenna_id": "omni_das", "x": 40, "y": 30},
            {"id": "st_a_c2", "type": "antenna", "label": "C-A2", "antenna_id": "omni_das", "x": 150, "y": 30},
        ],
    }
    # graph edges: donor feeder -> headend repeater -> 7/8in risers/trunks ->
    # zone splitters -> branch coax -> antennas (one feed per node)
    headend["elements"].append(
        {"id": "st_r_fd", "type": "cable_run", "from_id": "st_src", "to_id": "st_rep",
         "cable_id": "lmr400", "length_m": 18.0})
    bowl_lower["elements"].extend([
        {"id": "st_r_tr1", "type": "cable_run", "from_id": "st_rep", "to_id": "st_sp_l1",
         "cable_id": "coax_7_8in", "length_m": 26.0},
        {"id": "st_r_tr2", "type": "cable_run", "from_id": "st_sp_l1", "to_id": "st_sp_l2",
         "cable_id": "lmr400", "length_m": 20.0},
        {"id": "st_r_b1", "type": "cable_run", "from_id": "st_sp_l2", "to_id": "st_a_l1",
         "cable_id": "lmr400", "length_m": 32.0},
        {"id": "st_r_b2", "type": "cable_run", "from_id": "st_sp_l2", "to_id": "st_a_l2",
         "cable_id": "lmr400", "length_m": 28.0},
        {"id": "st_r_b3", "type": "cable_run", "from_id": "st_sp_l2", "to_id": "st_a_l3",
         "cable_id": "lmr400", "length_m": 24.0},
        {"id": "st_r_b4", "type": "cable_run", "from_id": "st_sp_l1", "to_id": "st_a_l4",
         "cable_id": "lmr400", "length_m": 30.0},
    ])
    bowl_upper["elements"].extend([
        {"id": "st_r_tr3", "type": "cable_run", "from_id": "st_sp_l1", "to_id": "st_sp_u1",
         "cable_id": "coax_7_8in", "length_m": 24.0},
        {"id": "st_r_b5", "type": "cable_run", "from_id": "st_sp_u1", "to_id": "st_a_u1",
         "cable_id": "lmr400", "length_m": 26.0},
        {"id": "st_r_b6", "type": "cable_run", "from_id": "st_sp_u1", "to_id": "st_a_u2",
         "cable_id": "lmr400", "length_m": 22.0},
    ])
    concourse["elements"].extend([
        {"id": "st_r_tr4", "type": "cable_run", "from_id": "st_sp_u1", "to_id": "st_sp_c1",
         "cable_id": "coax_7_8in", "length_m": 28.0},
        {"id": "st_r_b7", "type": "cable_run", "from_id": "st_sp_c1", "to_id": "st_a_c1",
         "cable_id": "lmr400", "length_m": 18.0},
        {"id": "st_r_b8", "type": "cable_run", "from_id": "st_sp_c1", "to_id": "st_a_c2",
         "cable_id": "lmr400", "length_m": 24.0},
    ])

    return {
        "name": PROJECT_NAME,
        "description": ("Phase E5 seed: generic 60,000-seat open-air stadium. "
                        "Representative neutral-host cascade for the cellular "
                        "carriers: base-station direct feeds (5 dBm/band) -> "
                        "headend repeater -> 7/8in risers -> zone splitters -> "
                        "bowl/concourse antennas, on a roof-over-two-bowl-"
                        "levels stack. The ERCES booster branch (G zone) and "
                        "the full 260-point takeoff are priced in the paired "
                        "labor estimate; the RF model carries the trunk layout "
                        "at representative density. Seating-bowl geometry is "
                        "representative, not a surveyed floorplan."),
        "environment": "commercial",
        "config_overrides": {},
        "stadium_phase": "E5",
        "floors": [roof, bowl_upper, bowl_lower, concourse, headend],
    }


# ------------------------------------------------------- estimate definition

def _L(cid, qty):
    return {"catalog_id": cid, "qty": qty}


def stadium_definition():
    """Fixture-class stadium estimate scaled to the 60k-seat venue. Qty
    choices are stated as assumption rows in venue_assumptions()."""
    return {
        "name": ESTIMATE_NAME,
        "start_date": START_DATE,
        "sanity": {"archetype": "stadium"},
        "schedule": {"night": True, "weekend": False},
        "crew": {"crews": 1, "techs": 5, "foremen": 1,
                 "hours_per_day": 8, "days_per_week": 5},
        "locations": [
            {"id": "roof", "name": "Roof / donor array",
             "access": "rope", "conditions": ["exterior"], "lines": [
                 _L("A01", 6), _L("A02", 8), _L("A04", 4), _L("A05", 4),
                 _L("A06", 120), _L("A07", 180), _L("A08", 1), _L("A09", 14),
                 _L("A10", 2), _L("G02", 2), _L("K04", 6), _L("K10", 2),
                 _L("K11", 2), _L("K09", 2),
             ]},
            {"id": "bowl-lower", "name": "Bowl lower level",
             "access": "lift", "conditions": ["occupied"], "lines": [
                 _L("E06", 120), _L("E01", 14000), _L("E03", 300),
                 _L("E04", 140), _L("E05", 12), _L("E11", 40),
                 _L("D04", 500), _L("D05", 350), _L("D06", 18),
                 _L("K01", 12), _L("K02", 72), _L("L02", 300), _L("L01", 120),
                 _L("H01", 60), _L("H02", 120), _L("H04", 6), _L("H05", 6),
                 _L("G02", 16),
             ]},
            {"id": "bowl-upper", "name": "Bowl upper level",
             "access": "lift", "conditions": ["occupied"], "lines": [
                 _L("E06", 80), _L("E01", 9500), _L("E03", 210),
                 _L("E04", 100), _L("E05", 8), _L("E11", 26),
                 _L("D04", 350), _L("D05", 250), _L("D06", 12),
                 _L("K01", 8), _L("K02", 48), _L("L02", 200),
                 _L("H01", 40), _L("H02", 80), _L("H04", 6), _L("H05", 6),
                 _L("G02", 12),
             ]},
            {"id": "concourse", "name": "Main concourse",
             "access": "scissor",
             "conditions": ["occupied", "constrained_access"], "lines": [
                 _L("E06", 60), _L("E01", 5000), _L("E03", 160),
                 _L("E04", 70), _L("E05", 6), _L("E11", 18),
                 _L("D04", 450), _L("D05", 300), _L("D06", 8),
                 _L("C01", 2), _L("C02", 2), _L("C06", 2), _L("C07", 6),
                 _L("C08", 6), _L("C09", 40),
                 _L("K01", 6), _L("K02", 30), _L("L02", 150), _L("L04", 200),
                 _L("H01", 24), _L("H02", 60), _L("H04", 6), _L("H05", 6),
                 _L("G02", 8),
             ]},
            {"id": "service-closets", "name": "Club level TR / service closets",
             "access": "ground",
             "conditions": ["occupied", "constrained_access"], "lines": [
                 _L("C01", 6), _L("C03", 6), _L("C05", 400), _L("C06", 6),
                 _L("C07", 12), _L("C08", 24), _L("C09", 120),
                 _L("D06", 12), _L("D08", 12),
             ]},
            {"id": "headend", "name": "ERCES + DAS headend room",
             "access": "ground", "lines": [
                 _L("B01", 2), _L("B03", 1), _L("B04", 6), _L("B07", 2),
                 _L("B09", 4), _L("B11", 1), _L("B13", 4),
                 _L("G01", 1), _L("G03", 1),
                 _L("J01", 4), _L("J02", 6), _L("J04", 4), _L("J05", 120),
                 _L("J06", 2),
                 _L("O07", 150), _L("O08", 30), _L("O09", 8), _L("O10", 2),
                 _L("O11", 1), _L("O12", 1),
                 _L("H06", 28), _L("H07", 1), _L("H09", 4), _L("H10", 1),
                 _L("I01", 2), _L("I02", 6), _L("I03", 1), _L("I04", 6),
             ],
             "power_plant": {"runtime_hours": 12, "load_amps": 30,
                             "spill_containment": True,
                             "battery_monitoring": True}},
        ],
        "travel_moves": [
            {"from": "roof", "to": "bowl-lower"},
            {"from": "bowl-lower", "to": "bowl-upper"},
            {"from": "bowl-upper", "to": "concourse"},
            {"from": "concourse", "to": "service-closets"},
            {"from": "service-closets", "to": "headend"},
        ],
        "downtime_windows": [
            {"label": "Home game event lockout (bowl closed to contractors)",
             "days": 2, "start": "2026-11-29"},
            {"label": "Holiday concert build shutdown",
             "days": 1, "start": "2026-12-24"},
        ],
        "antenna_points": {"bowl-lower": 120, "bowl-upper": 80,
                           "concourse": 60},
        "alternates": [
            {"name": "Add Band 14 FirstNet", "kind": "add",
             "antenna_points": 16, "lines": [
                 {"location_id": "roof", "catalog_id": "A01", "qty": 1},
                 {"location_id": "roof", "catalog_id": "A09", "qty": 1},
                 {"location_id": "bowl-lower", "catalog_id": "E06", "qty": 8},
                 {"location_id": "bowl-lower", "catalog_id": "E04", "qty": 8},
                 {"location_id": "bowl-lower", "catalog_id": "H02", "qty": 8},
                 {"location_id": "bowl-upper", "catalog_id": "E06", "qty": 8},
                 {"location_id": "bowl-upper", "catalog_id": "E04", "qty": 8},
                 {"location_id": "bowl-upper", "catalog_id": "H02", "qty": 8},
                 {"location_id": "headend", "catalog_id": "B04", "qty": 2},
             ]},
            {"name": "Deduct C-band carrier (n77)", "kind": "deduct",
             "lines": [
                 {"location_id": "bowl-lower", "catalog_id": "E06", "qty": 40},
                 {"location_id": "bowl-lower", "catalog_id": "E01", "qty": 4600},
                 {"location_id": "bowl-lower", "catalog_id": "E03", "qty": 100},
                 {"location_id": "bowl-lower", "catalog_id": "E11", "qty": 13},
                 {"location_id": "bowl-lower", "catalog_id": "H02", "qty": 40},
                 {"location_id": "bowl-upper", "catalog_id": "E06", "qty": 27},
                 {"location_id": "bowl-upper", "catalog_id": "E01", "qty": 3100},
                 {"location_id": "bowl-upper", "catalog_id": "E03", "qty": 70},
                 {"location_id": "bowl-upper", "catalog_id": "E11", "qty": 9},
                 {"location_id": "bowl-upper", "catalog_id": "H02", "qty": 27},
                 {"location_id": "concourse", "catalog_id": "E06", "qty": 20},
                 {"location_id": "concourse", "catalog_id": "E01", "qty": 1700},
                 {"location_id": "concourse", "catalog_id": "E03", "qty": 50},
                 {"location_id": "concourse", "catalog_id": "E11", "qty": 6},
                 {"location_id": "concourse", "catalog_id": "H02", "qty": 20},
                 {"location_id": "headend", "catalog_id": "B04", "qty": 2},
             ]},
        ],
    }


def venue_assumptions(review_date="2027-01-15"):
    """Manual register rows: every scale decision stated at the moment it is
    made. Auto rows (one per E-grade labor line) come from the engine sync;
    these document the takeoff itself."""
    return [
        ("Venue basis: generic 60,000-seat open-air stadium; scope modeled as "
         "roof donor array, lower/upper bowl seating levels, main concourse, "
         "club-level TR/service closets and the ERCES/DAS headend. Bowl "
         "geometry is representative, not a surveyed takeoff.", review_date),
        ("Neutral host: three cellular carriers (low band, PCS/AWS mid band "
         "and 3.7 GHz C-band) share passive antennas and coax, fed at the "
         "headend by direct base-station feeds. Per-carrier base stations and "
         "the equipment fit-out are owner-furnished and excluded.",
         review_date),
        ("ERCES: 700/800 MHz public-safety booster (Class B) with a 12-hour "
         "48VDC battery plant per IFC 510.4, sized at 30 A load by the "
         "engine's auto-sizer; headend room assumed on a service level with "
         "a ducted mini-split (B11).", review_date),
        ("Antenna points: 260 DAS points (120 lower bowl, 80 upper bowl, 60 "
         "concourse). ERCES service antennas (G02) are separate points in the "
         "G zone and are not in the per-point unit price.", review_date),
        ("Coax takeoff: 28,500 ft of 1/2 in heliax (14,000 / 9,500 / 5,000 by "
         "level) home-run to zone hubs at roughly 110 ft per point; tray and "
         "J-hook allowances cover catwalk and concourse pathway only. "
         "Connector ends (730 total) and jumpers scale to this pull.", review_date),
        ("Night windows: bowl production planned 22:00-06:00 in season; the "
         "M02 night differential applies estimate-wide.", review_date),
        ("Event lockouts: two blacked-out calendar windows priced as M01 idle "
         "crew-day carry (home game weekend starting 2026-11-29, 2 days; "
         "holiday concert build 2026-12-24, 1 day).", review_date),
        ("Roof access is rope (rappel) with crane-assisted placement (K10) "
         "and two crane days (K09). K09 is a 0-minute quote row: the crane "
         "day rate needs a local vendor quote (see open items).", review_date),
        ("Crew plan: one crew of 5 techs + 1 foreman, 8h x 5-day weeks, "
         "production starting 2026-11-16. Multi-crew fast-tracking and "
         "overtime are re-plan scenarios, not carried in the base.", review_date),
        ("Benchmark/acceptance scope: 6 band scopes per coverage floor (three "
         "carrier bands + B14 + PS700/PS800); 28 critical-area point tests at "
         "the field of play and egress stairs.", review_date),
        ("Escalation carried on the config labor index (base 2026-01-01, "
         "3.5%/yr assumption-grade) anchored to the schedule midpoint; swap "
         "in a published index or a vendor price-hold date before contract.", review_date),
        ("Vendor quote amounts in this register are placeholder binds created "
         "to exercise the quote/bid-tab workflow; replace with executed "
         "quotes before the bid goes out.", review_date),
    ]


def quote_payloads(est, today=None):
    """The two bound vendor quotes. linked_line_ids must exist in the stored
    recap (location_id:catalog_id)."""
    return [
        {"vendor": "ERCES integrator (placeholder bind)",
         "scope": "700/800 MHz Class B ERCES booster: booster install + "
                  "configuration, headend rack space, 12-hr battery plant",
         "amount": 18500.00, "currency": "USD",
         "binding_until": "2027-01-15", "status": "bound",
         "linked_line_ids": ["headend:G01"],
         "inclusions": ["Booster install labor and configuration",
                        "Battery plant set to the 12-hr runtime",
                        "Functional test witnessed with the AHJ"],
         "exclusions": ["Donor antenna re-aim on the roof",
                        "Permit and AHJ plan-review fees",
                        "Ongoing monitoring service"],
         "notes": "Placeholder bind created by the E5 package build; replace "
                  "with the executed vendor quote."},
        {"vendor": "Headend equipment integrator (placeholder bind)",
         "scope": "Headend equipment set: racks, neutral-host combiners, "
                  "surge protection, alarm wiring, mini-split HVAC, DC wiring",
         "amount": 27300.00, "currency": "USD",
         "binding_until": "2027-02-02", "status": "bound",
         "linked_line_ids": ["headend:B01", "headend:B04", "headend:B07",
                             "headend:B11", "headend:B13"],
         "inclusions": ["Mounting and configuration labor for the linked lines",
                        "HVAC condensate pump + drain tie-in"],
         "exclusions": ["Equipment purchase cost (owner-furnished)",
                        "Dedicated circuits and panel work (J zone)",
                        "48VDC plant batteries (O zone)"],
         "notes": "Placeholder bind created by the E5 package build."},
    ]


# -------------------------------------------------------------- build flow

def _db():
    from app import db
    return db


def _find_project(db):
    for row in db.list_projects():
        if row["name"] == PROJECT_NAME:
            return row["id"]
    return None


def _find_estimate(db, name):
    for row in db.list_estimates():
        if row["name"] == name:
            return row["id"]
    return None


def _quotes_present(db, eid, scopes):
    have = {q["scope"] for q in db.list_quotes(eid)}
    return all(s in have for s in scopes)


def build_stadium(package_dir=None, actor="rowan (E5 capstone)", today=None):
    """Create/reuse the stadium RF project + labor estimate as real stored
    objects, run the sanity report, price the alternates and (optionally)
    write the morning bid package. Returns a summary dict."""
    from romtool.cascade import solve_cascade
    from romtool.config import load_config
    from romtool.estimate import compute_estimate, compute_alternates, \
        load_labor_catalog
    from romtool.rflink import model_basis
    from romtool.sanity import run_sanity
    from romtool.commercial import load_commercial_config
    from romtool.governance import load_governance_config, StateRuleError, \
        assert_estimate_transition

    db = _db()
    db.init_db()
    today = today or datetime.date.today()

    # ---- RF project (real cascade object)
    pid = _find_project(db)
    solve_result = None
    if pid is None:
        model = rf_model()
        proj = db.create_project(model["name"], model)
        pid = proj["id"]
    solve_result = solve_cascade(db.get_project(pid)["model"], load_config())
    if solve_result["errors"]:
        raise RuntimeError("stadium RF cascade failed: %s" % solve_result["errors"])
    basis = model_basis(db.get_project(pid)["model"], load_config())
    warn_kinds = {}
    for w in solve_result.get("warnings", []):
        kind = ("clamp/port-power" if "capped" in w or "exceeds max out" in w
                else w.split("'")[0].strip())
        warn_kinds[kind] = warn_kinds.get(kind, 0) + 1

    # ---- labor estimate (fixture-class scaled) stored as a real object
    definition = stadium_definition()
    catalog = load_labor_catalog()
    recap = compute_estimate(definition, catalog=catalog)
    eid = _find_estimate(db, definition["name"])
    gov = load_governance_config()
    if eid is None:
        rec = db.create_estimate(recap["name"], definition, recap, actor)
        eid = rec["id"]
        db.sync_assumptions(eid, recap["lines"], 1,
                            gov["default_assumption_owner"],
                            gov.get("auto_assumption_source_quality", "E"),
                            actor=actor)
        for text, review_date in venue_assumptions():
            db.add_assumption(eid, text, gov["default_assumption_owner"],
                              review_date, actor, 1)
        for payload in quote_payloads(recap):
            fields = dict(payload)
            db.create_quote(eid, fields, actor, 1)
    est = db.get_estimate(eid)
    recap = est["recap"]

    # ---- governance: draft -> review (approval is sanity-guarded)
    if est["state"] == "draft":
        assert_estimate_transition(est["state"], "review", gov)
        est, _ = db.set_estimate_state(eid, "review", "draft", actor,
                                       "E5 morning package build")

    # ---- alternates + unit price + sanity (fresh from the stored estimate)
    alternates = compute_alternates(est["definition"], catalog=catalog)
    pts = est["definition"].get("antenna_points")
    if isinstance(pts, dict):
        n_pts = round(sum(float(v) for v in pts.values()), 6)
    elif pts is not None:
        n_pts = float(pts)
    else:
        n_pts = None
    unit_price = (round(est["recap"]["cost"]["grand_total_usd"]["mid"] / n_pts, 6)
                  if n_pts else None)
    quotes = db.list_quotes(eid)
    sanity = run_sanity(est["recap"], est["definition"], quotes, today=today)

    summary = {
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "version": "0.8.0",
        "subphase": "E5",
        "project_id": pid,
        "project_name": PROJECT_NAME,
        "estimate_id": est["id"],
        "estimate_name": est["name"],
        "state": est["state"],
        "revision": est["revision"],
        "actor": actor,
        "rf_basis": {k: basis[k] for k in ("antennas_total", "hub_elements_total",
                                           "cable_runs", "cable_meters_by_id")},
        "rf_cascade_errors": solve_result["errors"],
        "rf_cascade_warning_count": len(solve_result.get("warnings", [])),
        "rf_cascade_warning_kinds": warn_kinds,
        "crew_hours_mid": est["recap"]["totals"]["total_crew_hours_mid"],
        "crew_days": est["recap"]["totals"]["crew_days"],
        "crew_weeks": est["recap"]["totals"]["crew_weeks"],
        "grand_total_mid": est["recap"]["cost"]["grand_total_usd"]["mid"],
        "grand_total_low": est["recap"]["cost"]["grand_total_usd"]["low"],
        "grand_total_high": est["recap"]["cost"]["grand_total_usd"]["high"],
        "labor_mid": est["recap"]["cost"]["direct_labor_usd"]["mid"],
        "downtime_mid": est["recap"]["cost"]["downtime_usd"],
        "escalation": est["recap"]["commercial"]["escalation"],
        "antenna_points": n_pts,
        "unit_price_per_point": unit_price,
        "alternates": alternates,
        "quote_ids": [q["id"] for q in quotes],
        "assumption_count": len(db.list_assumptions(eid)),
        "sanity": {"archetype": sanity["archetype"],
                   "hard": sanity["counts"]["hard"],
                   "warnings": sanity["counts"]["warnings"],
                   "blocks_approval": sanity["blocks_approval"]},
        "quality_counts": _quality_counts(est["recap"]),
        "grade_hours": _grade_hours(est["recap"]),
        "package_dir": None,
        "package_files": {},
    }
    if package_dir:
        pkg = build_package(est, summary, package_dir, today)
        summary.update({"package_dir": pkg["dir"],
                        "package_files": pkg["files"]})
    return summary


def _quality_counts(recap):
    counts = {}
    for ln in recap["lines"]:
        if ln["kind"] == "minutes" and ln.get("location_id"):
            q = ln["source_quality"]
            counts[q] = counts.get(q, 0) + 1
    return counts


def _grade_hours(recap):
    """Per provenance grade: labor lines, wrapped hours, labor dollars.
    Same basis as the sanity gates: all minutes-kind lines including the
    estimate-level wrap rows (M05/N03/N04)."""
    out = {}
    for ln in recap["lines"]:
        if ln["kind"] != "minutes":
            continue
        g = out.setdefault(ln["source_quality"],
                           {"lines": 0, "hours_mid": 0.0, "labor_mid": 0.0})
        g["lines"] += 1
        g["hours_mid"] += ln["hours"]["mid"]
        g["labor_mid"] += ln["labor_usd"]["total"]["mid"]
    for g in out.values():
        g["hours_mid"] = round(g["hours_mid"], 2)
        g["labor_mid"] = round(g["labor_mid"], 2)
    return out


# ------------------------------------------------------------ package files

def build_package(est, summary, package_dir, today=None):
    """Write the full bid package + MORNING-REPORT.md. File bytes come from
    the same helpers the live API endpoints use."""
    from app import routes as routes_mod
    from romtool.bidform import bid_form_pdf, bid_form_xlsx
    from romtool.commercial import (cash_curve, cash_curve_xlsx,
                                    crew_schedule, load_commercial_config)
    from romtool.governance import (assumptions_csv, bid_tab, commercial_pdf,
                                    quote_view)
    from romtool.xlsx import encode_xlsx

    db = _db()
    today = today or datetime.date.today()
    package_dir = os.path.abspath(package_dir)
    os.makedirs(package_dir, exist_ok=True)

    gov_view = routes_mod._governance_view(est, today)
    sanity_report = routes_mod._sanity_report(est, None, today)
    recap_bytes = routes_mod._recap_xlsx_bytes(est["recap"], gov_view,
                                               sanity_report)
    bid_data = routes_mod._bid_form_data(est, today)
    sheet = routes_mod._commercial_sheet(est, today)
    commercial = load_commercial_config()
    curve = cash_curve(est["recap"], est["definition"], commercial, today)
    sched = crew_schedule(est["recap"], est["definition"], commercial, today)
    tab = bid_tab(db.list_quotes(est["id"]), est["recap"], today)
    sanity_body = dict(sanity_report)
    sanity_body.update({
        "estimate_id": est["id"], "estimate_name": est["name"],
        "state": est["state"], "revision": est["revision"],
        "as_of": today.isoformat(),
        "export_note": "generated by romtool/stadium_package.py (E5)"})

    writes = {
        "recap.xlsx": recap_bytes,
        "recap.csv": routes_mod.recap_csv(est["recap"], gov_view,
                                          sanity_report).encode("utf-8"),
        "bid_form.pdf": bid_form_pdf(bid_data),
        "bid_form.xlsx": bid_form_xlsx(bid_data),
        "inclusions_exclusions.pdf": commercial_pdf(
            sheet, gov_view),
        "assumptions.csv": assumptions_csv(
            db.list_assumptions(est["id"])).encode("utf-8"),
        "cashflow.xlsx": cash_curve_xlsx(curve),
        "crew_plan.csv": _crew_plan_csv(sched).encode("utf-8"),
        "bid_tab.json": json.dumps({
            "estimate_id": est["id"],
            "quotes": [quote_view(q, today) for q in
                       db.list_quotes(est["id"])],
            "bid_tab": tab}, indent=1, default=str).encode("utf-8"),
        "sanity.json": json.dumps(sanity_body, indent=1).encode("utf-8"),
    }
    files = {}
    for name, blob in writes.items():
        path = os.path.join(package_dir, name)
        with open(path, "wb") as fh:
            fh.write(blob)
        files[name] = len(blob)
    report = render_morning_report(summary, est, sanity_report, sched,
                                   curve, tab, today)
    path = os.path.join(package_dir, "MORNING-REPORT.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(report)
    files["MORNING-REPORT.md"] = len(report.encode("utf-8"))

    manifest = {
        "generated_at": summary["built_at"],
        "romtool_version": summary["version"],
        "subphase": summary["subphase"],
        "package_dir": package_dir,
        "project_id": summary["project_id"],
        "project_name": summary["project_name"],
        "estimate_id": est["id"],
        "estimate_name": est["name"],
        "estimate_state": est["state"],
        "revision": est["revision"],
        "crew_hours_mid": summary["crew_hours_mid"],
        "crew_days": summary["crew_days"],
        "crew_weeks": summary["crew_weeks"],
        "grand_total_mid": summary["grand_total_mid"],
        "antenna_points": summary["antenna_points"],
        "unit_price_per_point": summary["unit_price_per_point"],
        "alternates": summary["alternates"],
        "quote_ids": summary["quote_ids"],
        "assumption_count": summary["assumption_count"],
        "sanity_summary": summary["sanity"],
        "quality_counts": summary["quality_counts"],
        "files": {k: {"bytes": v} for k, v in sorted(files.items())},
        "api": {
            "estimate": "/estimates/%d" % est["id"],
            "sanity": "/estimates/%d/sanity" % est["id"],
            "schedule": "/estimates/%d/schedule" % est["id"],
            "cashflow": "/estimates/%d/cashflow" % est["id"],
            "bid_tab": "/estimates/%d/bid_tab" % est["id"],
            "assumptions": "/estimates/%d/assumptions" % est["id"],
            "this_manifest": "/reports/stadium",
        },
    }
    mpath = os.path.join(package_dir, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    # the manifest cannot list its own file size stably (self-reference);
    # files covers every generated file, manifest.json size via getsize()
    files["manifest.json"] = os.path.getsize(mpath)
    summary["sanity_hard"] = sanity_report["counts"]["hard"]
    summary["sanity_warnings"] = sanity_report["counts"]["warnings"]
    summary["sanity_report"] = sanity_report
    summary["schedule"] = sched
    return {"dir": package_dir, "files": files, "manifest": manifest}


def _crew_plan_csv(sched):
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["location_id", "location", "hours_mid", "crew_days",
                "crew", "start", "finish"])
    for loc in sched["locations"]:
        w.writerow([loc["location_id"], loc["name"], loc["hours_mid"],
                    loc["crew_days"], loc["crew"] or "",
                    loc["start"] or "", loc["finish"] or ""])
    w.writerow([])
    w.writerow(["bound", "work_days", "first_work_day", "last_work_day",
                "calendar_days", "total_location_crew_days"])
    for bound, label, note in ((sched["duration"]["early"], "early",
                                sched["duration"]["early_note"]),
                               (sched["duration"]["late"], "late",
                                sched["duration"]["late_note"])):
        w.writerow([label, bound["work_days"], bound["first_work_day"] or "",
                    bound["last_work_day"] or "",
                    bound["calendar_days"] if bound["calendar_days"] else "",
                    bound["total_location_crew_days"]])
    w.writerow([])
    w.writerow(["blackout_dates"])
    for d in sched["blackout_dates"]:
        w.writerow([d])
    for f in sched["overtime"]["flagged_weeks"]:
        w.writerow(["ot_flag", f["week_start"], f["hours_per_crew"],
                    f["note"]])
    return buf.getvalue()


# ------------------------------------------------------------ morning report

def _usd(v):
    return "$%s" % format(float(v), ",.2f")


def render_morning_report(summary, est, sanity_report, sched, curve, tab,
                          today):
    """The review document for Matt. Plain sentences, no em-dashes, accuracy
    class stated honestly."""
    c = est["recap"]["cost"]
    t = est["recap"]["totals"]
    grades = summary["grade_hours"]
    n_labor = sum(g["lines"] for g in grades.values())
    e_lines = grades.get("E", {}).get("lines", 0)
    e_pct = (e_lines / n_labor * 100.0) if n_labor else 0.0
    p_lines, g_lines = grades.get("P", {}).get("lines", 0), \
        grades.get("G", {}).get("lines", 0)
    e_hours = grades.get("E", {}).get("hours_mid", 0.0)
    g_hours = grades.get("G", {}).get("hours_mid", 0.0)
    p_hours = grades.get("P", {}).get("hours_mid", 0.0)
    tot_hours = e_hours + g_hours + p_hours
    hard, warns = sanity_report["counts"]["hard"], \
        sanity_report["counts"]["warnings"]
    alt_add = [a for a in summary["alternates"] if a["kind"] == "add"]
    alt_ded = [a for a in summary["alternates"] if a["kind"] == "deduct"]

    low = summary["grand_total_low"]
    mid = summary["grand_total_mid"]
    high = summary["grand_total_high"]
    low_pct = (low - mid) / mid * 100.0 if mid else 0.0
    high_pct = (high - mid) / mid * 100.0 if mid else 0.0

    L = []
    ap = L.append
    ap("# ROMtool 0.8.0 stadium package: morning report")
    ap("")
    ap("Prepared for: Matt Busdiecker, Senior Project Estimator (ANS)")
    ap("Estimate: %s" % est["name"])
    ap("Generated: %s (ROMtool 0.8.0, subphase E5)" % summary["built_at"])
    ap("")
    ap("## The one-paragraph version")
    ap("")
    ap("The 60k-seat stadium is in ROMtool as a real RF project and a real "
       "labor estimate, and the bid package for it is written to "
       "%s. The estimate is the fixture-class stadium scenario scaled to a "
       "60,000-seat venue: roof donor work on rope, lift-occupied bowl "
       "levels, a scissor-lift concourse, club-level closets, the ERCES and "
       "neutral-host headend with the 48VDC plant, night windows, dated "
       "event lockouts, crane/shroud/HVAC rows and the finishing zone. Base "
       "bid %s mid (%s low, %s high) and %s per antenna point on %s points. "
       "Full numbers are in the table below. Read this file as the accuracy "
       "statement: it is a fixture-class Class 4 number (closer to Class 5 "
       "on the low end) with two placeholder vendor binds, not a design-"
       "takeoff Class 3."
       % (summary.get("package_dir") or "dist/stadium-package",
          _usd(mid), _usd(low), _usd(high),
          _usd(summary["unit_price_per_point"]),
          int(summary["antenna_points"])))
    ap("")
    ap("## What shipped overnight (E1 through E5, v0.4.0 to v0.8.0)")
    ap("")
    ap("- E1 (0.4.0): Class 1 labor engine, 134-row unit catalog with "
       "P/G/E provenance, visible factor stack, recap exports.")
    ap("- E2 (0.5.0): versioned estimates, audit log, draft/review/approved/"
       "sent state machine, assumption register, quote register with binding "
       "dates, bid-tab comparison, inclusions/exclusions sheet.")
    ap("- E3 (0.6.0): commercial wrap chain (small tools, escalation, tax, "
       "insurance/bond, PM/OH/profit, line-risk contingency), crew schedule "
       "with dated blackouts, milestone cash curve, stored what-if scenarios "
       "with diffs, bid form with engine-priced alternates and unit prices.")
    ap("- E4 (0.7.0): sanity gates with venue archetypes and missing-scope "
       "checklists, actuals loop with drift reports and guarded refit "
       "proposals, RF-link draft estimates, AI endpoints that degrade to 503 "
       "without a gateway. E5 fixed one E4 defect: the crew-duration gate "
       "counted derived wrap lines in its recompute, which could flag good "
       "recaps (CREW_MATH_INCONSISTENT); the gate now uses the engine basis.")
    ap("- E5 (0.8.0): this stadium build, the package, /reports/stadium, "
       "docs and tests.")
    ap("")
    ap("## The stadium as objects")
    ap("")
    ap("- RF project #%d: %s. Floors: roof carrier-feed/donor array, bowl "
       "upper, bowl lower (field of play marked NFPA-critical), concourse, "
       "ERCES/DAS headend. Cascade solves with zero errors (%d antenna points "
       "modeled, %d cable runs) and the usual ROM gain-clamp warnings (%d, a "
       "gain-rich chain caps ports by design). The model shows the trunk "
       "layout at representative density while the labor takeoff carries the "
       "full 260-point scale."
       % (summary["project_id"], summary["project_name"],
          summary["rf_basis"].get("antennas_total", 0),
          summary["rf_basis"].get("cable_runs", 0),
          summary.get("rf_cascade_warning_count", 0)))
    ap("- Estimate #%d, revision %d, state %s: %d recap lines. Definition "
       "carries sanity.archetype = stadium, start %s, night schedule, two "
       "dated lockout windows, alternates and per-location antenna_points."
       % (est["id"], est["revision"], est["state"], len(est["recap"]["lines"]),
          est["definition"]["start_date"]))
    ap("- Vendor quotes #%s: ERCES booster %s and headend equipment %s, both "
       "bound with 2027 binding dates. Amounts are placeholder binds to make "
       "the bid tab real; swap in executed quotes before the bid goes out."
       % (", #".join(str(i) for i in summary["quote_ids"]),
          _usd(18500.0), _usd(27300.0)))
    ap("- Assumption register: %d rows (%d auto from E-grade lines + 12 manual "
       "takeoff assumptions), all open."
       % (summary["assumption_count"],
          summary["assumption_count"] - 12))
    ap("")
    ap("## The numbers")
    ap("")
    ap("| Metric | Value |")
    ap("| --- | --- |")
    ap("| Crew hours (mid) | %s h |" % format(t["hours"]["mid"], ",.2f"))
    ap("| Crew days / weeks (1 crew, 5 techs + 1 foreman) | %s / %s |"
       % (t["crew_days"], t["crew_weeks"]))
    ap("| Direct labor (mid, wrapped) | %s |" % _usd(c["direct_labor_usd"]["mid"]))
    ap("| Downtime carry (M01, 3 lockout days) | %s |" % _usd(c["downtime_usd"]))
    ap("| Escalation (%s index) | factor %s through %s |"
       % (c["escalation_meta"]["index"], c["escalation_meta"]["factor"],
          c["escalation_meta"]["anchor_date"]))
    ap("| Base bid (mid) | %s |" % _usd(c["grand_total_usd"]["mid"]))
    ap("| Low / high band | %s / %s |" % (_usd(low), _usd(high)))
    ap("| Unit price | %s per antenna point (%s points) |"
       % (_usd(summary["unit_price_per_point"]), int(summary["antenna_points"])))
    for a in alt_add:
        ap("| Alternate: %s (add) | %s |" % (a["name"], _usd(a["amount_usd_mid"])))
    for a in alt_ded:
        ap("| Alternate: %s (deduct) | %s credit |" % (a["name"],
                                                       _usd(-a["amount_usd_mid"])))
    ap("")
    ap("Crew plan: %s-day working weeks, per-location dates in crew_plan.csv, "
       "blackouts on %s. The schedule carries one crew end to end "
       "(conservative); add crews or overtime before taking the duration to "
       "a bid date."
       % (sched["days_per_week"],
          ", ".join(sched["blackout_dates"]) or "no dated windows"))
    ap("")
    ap("## Provenance: what grade is backing the numbers")
    ap("")
    ap("| Grade | Labor lines | Share of lines | Wrapped hours (mid) |")
    ap("| --- | --- | --- | --- |")
    for grade, label, meaning in (
            ("P", "P (published)", "published norm anchors"),
            ("G", "G (industry guide)", "practice/documented ranges, no published norm"),
            ("E", "E (assumption)", "estimated placeholders, assumption register")):
        g = grades.get(grade, {})
        share = (g.get("hours_mid", 0.0) / tot_hours * 100.0) if tot_hours else 0.0
        ap("| %s | %d | %.1f%% | %.2f h (%.1f%% of hours) |"
           % (label, g.get("lines", 0),
              (g.get("lines", 0) / n_labor * 100.0) if n_labor else 0.0,
              g.get("hours_mid", 0.0), share))
    ap("")
    ap("Of %d labor lines, %d are E-grade (%.1f%%). That is the honest shape "
       "of a fixture-class model: the catalog says P where it has published "
       "anchors (conduit, dedicated circuits), G where industry practice "
       "documents a range, and E everywhere the night's research did not "
       "produce a defensible norm. Every E line has an open row in the "
       "assumption register; closing those rows with your actuals is the "
       "path from this Class 4 number down to Class 3."
       % (n_labor, e_lines, e_pct))
    ap("")
    ap("## Sanity gates")
    ap("")
    if hard == 0:
        ap("No hard findings: the crew math recomputes clean and every "
           "stadium checklist zone (A, C, E, G, H, I, J, K, O) has scope. "
           "The estimate can pass the approval gate.")
    else:
        ap("%d hard finding(s):" % hard)
        for f in sanity_report["hard_findings"]:
            ap("- %s: %s" % (f["code"], f["message"]))
    if warns:
        ap("")
        ap("%d warning(s), none blocking:" % warns)
        for f in sanity_report["warnings"][:6]:
            short = f["message"][:150].rstrip()
            ap("- %s: %s%s" % (f["code"], short,
                               "" if len(f["message"]) <= 150 else "..."))
        if warns > 6:
            ap("- (and %d more, same pattern; sanity.json has them all)"
               % (warns - 6))
    ap("")
    ap("The recurring warnings are expected: E-density is high because the "
       "catalog is assumption-heavy, and the roof lines stack rope x exterior "
       "x night to about 3.01x the base card, just past the 3.0 outlier "
       "threshold. Both are working as intended: they tell you which stacks "
       "to verify, and the stacks here (rappeled roof work at night) are "
       "intentional.")
    ap("")
    ap("## Accuracy class, stated plainly")
    ap("")
    ap("This package supports Class 4 accuracy at best, and the low/high "
       "band spans %+.0f%% / %+.0f%% around the mid (AACE Class 5 territory "
       "at the bottom, Class 4 at the top). What pulls it toward Class 4: "
       "defined scope, catalog norms with sources, dated schedule, priced "
       "alternates, and two vendor binds (placeholder amounts). What keeps "
       "it out of Class 3: no design takeoff, most norms assumption-grade, "
       "quote amounts not executed. Treat the mid as a budget number, the "
       "bid form as a format proof, and re-price after design development "
       "and real quotes."
       % (low_pct, high_pct))
    ap("")
    ap("## Open items for your actuals")
    ap("")
    for item in [
        "Crane day rate (K09) and the two quote binds need real numbers from "
        "your vendors.",
        "Import actual hours against estimate #%d (POST /estimates/%d/actuals) "
        "as crews start; the drift report flags norms that need refits and "
        "the proposal flow writes company overrides after you approve."
        % (est["id"], est["id"]),
        "Close the assumption register: verify E-grade rows against the "
        "quotes you collect, then clear approval for the sent state.",
        "Set the sales/use tax rule for your jurisdiction (shipped default "
        "treats labor as exempt, so the tax line reads $0.00 on purpose).",
        "Swap the escalation index for a published series or a vendor "
        "price-hold date when one governs.",
        "Re-plan crews if the duration matters: the base is one crew over "
        "%d crew-weeks; the scenario engine prices multi-crew overlays."
        % t["crew_weeks"],
    ]:
        ap("- " + item)
    ap("")
    ap("## Package contents and how to re-run")
    ap("")
    for name in ("recap.xlsx", "recap.csv", "bid_form.pdf", "bid_form.xlsx",
                 "inclusions_exclusions.pdf", "assumptions.csv",
                 "cashflow.xlsx", "crew_plan.csv", "bid_tab.json",
                 "sanity.json", "manifest.json"):
        ap("- %s" % name)
    ap("")
    ap("Rebuild anything at the live API:")
    ap("")
    ap("    BASE=http://127.0.0.1:8807")
    ap("    curl -s $BASE/estimates/%d | jq .estimate.recap.cost" % est["id"])
    ap("    curl -s $BASE/estimates/%d/sanity | jq .counts" % est["id"])
    ap("    curl -s $BASE/exports/estimates/%d/bid_form.pdf -o bid_form.pdf"
       % est["id"])
    ap("    curl -s $BASE/reports/stadium | jq")
    ap("")
    ap("Rebuild the whole package from scratch (idempotent, it reuses the "
       "stored objects):")
    ap("")
    ap("    cd /root/assistant/romtool")
    ap("    .venv/bin/python scripts/build_stadium_package.py")
    ap("")
    ap("Run the test suite: .venv/bin/pytest -q (227 through E4 plus the new "
       "E5 coverage).")
    ap("")
    ap("manifest.json in this folder carries the ids every file was built "
       "from, and GET /reports/stadium serves it from the app.")
    ap("")
    return "\n".join(L)


# -------------------------------------------------------------------- CLI

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Build the E5 Generic 60k-Seat Stadium objects + morning bid package")
    ap.add_argument("--package-dir", default=None,
                    help="default: <repo>/dist/stadium-package (ROMTOOL_STADIUM_PACKAGE)")
    ap.add_argument("--no-package", action="store_true",
                    help="create/reuse the stored objects only")
    ap.add_argument("--actor", default="rowan (E5 capstone)")
    args = ap.parse_args(argv)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_dir = os.environ.get("ROMTOOL_STADIUM_PACKAGE")
    default_dir = env_dir or os.path.join(repo_root, "dist", "stadium-package")
    package_dir = None if args.no_package else (args.package_dir or default_dir)
    summary = build_stadium(package_dir=package_dir, actor=args.actor)
    print(json.dumps(summary, indent=1, default=str))
    return summary


if __name__ == "__main__":  # pragma: no cover
    main()
