"""Phase E5 (v0.8.0) - Generic 60k-Seat Stadium capstone coverage.

Hand anchors against the shipped values (romtool/stadium_package.py):
- definition scales the fixture-class stadium to a 60k-seat venue: 260 DAS
  antenna points (120 lower / 80 upper / 60 concourse), ~28,500 ft heliax,
  12-hr/30 A ERCES plant (O04 x3 strings, O02 x2 N+1 rectifiers), E11/K09/
  K10/B11 crane/shroud/HVAC rows, L-zone finishing, two dated lockouts
  (3 M01 idle days), alternates Add Band 14 FirstNet (+) and Deduct C-band
  (negative credit), per-location antenna_points, sanity.archetype stadium.
- sanity on the stored estimate: zero hard findings (stadium checklist fully
  covered, crew gate consistent), unit-cost outliers only on the roof
  rope-stack lines (1.75 x 1.3 x 1.325 = 3.014375 > 3.0, intentional),
  E-density warning present (fixture-class catalog is E-heavy).
- crew-duration gate basis regression: the gate recomputes crew math on the
  ENGINE basis (location minutes lines only; derived M05/N03/N04 wrap lines
  are excluded) - the E4 gate counted them and could flag good recaps.
- package: every file exists and is non-trivial, PDF magic bytes present,
  xlsx files are zips, MORNING-REPORT.md carries the accuracy-class
  statement with no em-dashes, manifest serves on GET /reports/stadium.
"""

import csv
import io
import json
import os

import pytest

from fastapi.testclient import TestClient

from romtool import stadium_package as sp
from romtool.estimate import (compute_alternates, compute_estimate,
                              load_labor_catalog)
from romtool.sanity import load_checklists, run_sanity

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "stadium_estimate.json")

PDF_MAGIC = b"%PDF-"
XLSX_MAGIC = b"PK"


# --------------------------------------------------------- definition (pure)

@pytest.fixture(scope="module")
def stadium_recap():
    return compute_estimate(sp.stadium_definition(), catalog=load_labor_catalog())


def test_definition_scales_fixture_class(stadium_recap):
    d = sp.stadium_definition()
    recap = stadium_recap
    # shape: fixture-class locations scaled up, archetype + dates set
    assert d["sanity"] == {"archetype": "stadium"}
    assert d["start_date"] == "2026-11-16"
    assert d["schedule"]["night"] is True
    locs = {l["id"] for l in d["locations"]}
    assert {"roof", "bowl-lower", "bowl-upper", "concourse",
            "service-closets", "headend"} <= locs
    # crane / shroud / HVAC rows present
    ids = {ln["catalog_id"] for loc in d["locations"] for ln in loc["lines"]}
    assert {"E11", "K09", "K10", "B11"} <= ids
    assert any(ln["catalog_id"].startswith("L") for loc in d["locations"]
               for ln in loc["lines"])
    # antenna points and unit-price basis
    pts = d["antenna_points"]
    assert sum(pts.values()) == 260
    # night windows + event lockouts: priced M01 carry for all 3 days
    windows = recap["lines"]
    carry = [l for l in windows if l["kind"] == "cost_carry"]
    assert sum(l["qty"] for l in carry) == 3
    assert recap["cost"]["downtime_usd"] > 0
    # ERCES + DC plant block auto-sized (12h x 30A -> 3 strings, N+1 = 2)
    ozones = {(l["catalog_id"], l["qty"]) for l in recap["lines"]
              if l["zone"].startswith("O-")}
    assert ("O04", 3) in ozones and ("O02", 2) in ozones and ("O01", 1) in ozones
    # grand total and crew-hours are positive, crew plan plausible
    assert recap["cost"]["grand_total_usd"]["mid"] > 0
    assert recap["totals"]["total_crew_hours_mid"] > 0
    assert recap["totals"]["crew_weeks"] > 0


def test_alternates_priced_by_engine(stadium_recap):
    definition = sp.stadium_definition()
    alts = compute_alternates(definition, catalog=load_labor_catalog())
    by_name = {a["name"]: a for a in alts}
    first = by_name["Add Band 14 FirstNet"]
    cband = by_name["Deduct C-band carrier (n77)"]
    assert first["kind"] == "add" and first["amount_usd_mid"] > 0
    assert cband["kind"] == "deduct" and cband["amount_usd_mid"] < 0
    # the add alternate carries its own per-point unit price (16 antennas)
    assert first["antenna_points"] == 16
    assert first["unit_price_per_antenna_point"] > 0
    assert (first["amount_usd_mid"] / first["antenna_points"]
            == pytest.approx(first["unit_price_per_antenna_point"], rel=1e-9))


def test_stadium_sanity_zero_hard_and_all_zones_in_band(stadium_recap):
    definition = sp.stadium_definition()
    report = run_sanity(stadium_recap, definition, [],
                        today=__import__("datetime").date(2026, 11, 9))
    assert report["hard_findings"] == []
    assert report["archetype"] == "stadium"
    codes = [f["code"] for f in report["warnings"]]
    assert not any(c == "LABOR_SHARE_OUT_OF_BAND" for c in codes)
    assert "E_DENSITY_HIGH" in codes  # honest: E-heavy fixture-class catalog
    # the only unit-cost outliers are the intentional roof rope-stack lines
    outlier_lines = {f["line_id"] for f in report["warnings"]
                     if f["code"] == "UNIT_COST_OUTLIER"}
    assert outlier_lines and all(k.startswith("roof:") for k in outlier_lines)
    for f in report["warnings"]:
        if f["code"] == "UNIT_COST_OUTLIER":
            assert f["ratio"] == pytest.approx(3.014375)
    assert report["gates"]["missing_scope"]["missing"] == []


def test_crew_gate_uses_engine_basis_regression(stadium_recap):
    """E5 fix: the sanity crew gate recomputes crew_days from location lines
    only (engine basis); counting derived M05/N03/N04 wrap lines double-counts
    and produced a false CREW_MATH_INCONSISTENT hard finding at scale."""
    recap = stadium_recap
    report = run_sanity(recap, sp.stadium_definition(), [],
                        today=__import__("datetime").date(2026, 11, 9))
    crew = report["gates"]["crew_duration"]
    assert crew["consistent"] is True
    assert crew["crew_days_expected"] == recap["totals"]["crew_days"]
    assert crew["crew_weeks_expected"] == recap["totals"]["crew_weeks"]
    # the naive (pre-fix) basis would have disagreed at this size:
    naive_direct = sum(l["hours"]["mid"] for l in recap["lines"]
                       if l["kind"] == "minutes")
    cap_day = crew["capacity_per_crew_day_h"]
    import math
    assert math.ceil(naive_direct / cap_day) != crew["crew_days_expected"]
    # and the shipped fixture stays consistent under the corrected gate
    with open(FIXTURE) as fh:
        fixture = json.load(fh)
    fixture_recap = compute_estimate(fixture, catalog=load_labor_catalog())
    fixture_report = run_sanity(fixture_recap, fixture, [],
                                today=__import__("datetime").date(2026, 11, 9))
    assert not any(f["code"] == "CREW_MATH_INCONSISTENT"
                   for f in fixture_report["hard_findings"])


def test_venue_assumptions_documented():
    rows = sp.venue_assumptions()
    assert len(rows) == 12
    texts = " ".join(t for t, _ in rows)
    for token in ("60,000-seat", "three cellular carriers", "12-hour",
                  "260 DAS points", "Night windows", "Event lockouts",
                  "K09", "2026-11-16", "placeholder binds"):
        assert token in texts
    for text, review_date in rows:
        assert review_date == "2027-01-15"
        assert text.strip()


# ------------------------------------------------------- end-to-end via API

@pytest.fixture(scope="module")
def stadium(client):
    """Build through the module once per module against the ephemeral API DB."""
    return sp.build_stadium(package_dir=None, actor="e5-tests")


def test_objects_and_quotes_real(stadium, client):
    assert stadium["rf_cascade_errors"] == []
    assert stadium["estimate_id"] > 0 and stadium["project_id"] > 0
    assert len(stadium["quote_ids"]) == 2
    est = client.get("/estimates/%d" % stadium["estimate_id"]).json()["estimate"]
    assert est["state"] == "review" and est["revision"] == 1
    assert est["definition"]["sanity"]["archetype"] == "stadium"
    quotes = client.get("/estimates/%d/quotes" % stadium["estimate_id"]).json()
    assert len(quotes["quotes"]) == 2
    assert all(q["status"] == "bound" for q in quotes["quotes"])
    assert quotes["warnings"] == []  # binding dates in 2027, no expiry noise
    # two bound quotes group in the bid tab against their linked lines
    tab = client.get("/estimates/%d/bid_tab" % stadium["estimate_id"]).json()
    scopes = [g["scope"] for g in tab["groups"]]
    assert "headend:G01" in scopes
    assert "headend:B01,headend:B04,headend:B07,headend:B11,headend:B13" in scopes
    regs = client.get("/estimates/%d/assumptions" % stadium["estimate_id"]).json()
    assert regs["summary"]["total"] == stadium["assumption_count"]
    assert regs["summary"]["open"] == stadium["assumption_count"]


def test_stored_estimate_stats_and_sanity(stadium, client):
    est = client.get("/estimates/%d" % stadium["estimate_id"]).json()["estimate"]
    assert stadium["crew_hours_mid"] == pytest.approx(
        est["recap"]["totals"]["total_crew_hours_mid"])
    assert stadium["grand_total_mid"] == pytest.approx(
        est["recap"]["cost"]["grand_total_usd"]["mid"])
    assert stadium["antenna_points"] == 260
    assert stadium["unit_price_per_point"] == pytest.approx(
        est["recap"]["cost"]["grand_total_usd"]["mid"] / 260.0)
    sanity = client.get("/estimates/%d/sanity" % stadium["estimate_id"]).json()
    assert sanity["archetype"] == "stadium"
    assert sanity["counts"]["hard"] == 0
    assert sanity["counts"]["warnings"] == stadium["sanity"]["warnings"] > 0
    # alternates priced from the API definition match the module pricing
    alts = stadium["alternates"]
    assert [a["amount_usd_mid"] > 0 for a in alts if a["kind"] == "add"]
    assert [a["amount_usd_mid"] < 0 for a in alts if a["kind"] == "deduct"]
    # the schedule and cashflow endpoints run on this estimate
    sched = client.get("/estimates/%d/schedule" % stadium["estimate_id"]).json()
    assert sched["blackout_dates"] == ["2026-11-29", "2026-11-30", "2026-12-24"]
    assert sched["duration"]["early"]["work_days"] > 180
    curve = client.get("/estimates/%d/cashflow" % stadium["estimate_id"]).json()
    assert curve["cashflow"]["months"][0]["milestones"]


def test_approval_not_blocked_by_sanity(stadium, client):
    """The E4 guard's positive case: no hard findings on the stadium, so the
    recomputed sanity report does not block a review -> approved transition."""
    eid = stadium["estimate_id"]
    est_obj = client.get("/estimates/%d" % eid).json()["estimate"]
    report = run_sanity(est_obj["recap"], est_obj["definition"], [])
    assert report["blocks_approval"] is False
    assert report["counts"]["hard"] == 0


# --------------------------------------------------------------- package

@pytest.fixture(scope="module")
def pkg(client, stadium, tmp_path_factory):
    d = tmp_path_factory.mktemp("e5-pkg")
    est = client.get("/estimates/%d" % stadium["estimate_id"]).json()["estimate"]
    class _E(dict):
        pass
    # rebuild the summary from the module flow for the package build
    summary = dict(stadium)
    summary["package_dir"] = None
    out = sp.build_package(est, summary, str(d),
                           today=__import__("datetime").date(2026, 11, 10))
    return str(d), out, summary


def test_package_files_exist_and_non_trivial(pkg):
    d, out, _ = pkg
    for name in sp.PACKAGE_FILES:
        p = os.path.join(d, name)
        assert os.path.exists(p), name
        assert os.path.getsize(p) > 300, name  # non-trivial
    assert out["files"]["MORNING-REPORT.md"] > 3000


def test_package_pdf_and_xlsx_magic(pkg):
    d, out, _ = pkg
    for name in ("bid_form.pdf", "inclusions_exclusions.pdf"):
        with open(os.path.join(d, name), "rb") as fh:
            assert fh.read(5) == PDF_MAGIC, name
    for name in ("recap.xlsx", "bid_form.xlsx", "cashflow.xlsx"):
        with open(os.path.join(d, name), "rb") as fh:
            assert fh.read(2) == XLSX_MAGIC, name


def test_package_contents_real(pkg, stadium):
    d, out, summary = pkg
    with open(os.path.join(d, "recap.csv"), newline="") as fh:
        rows = list(csv.reader(fh))
    flat = [" ".join(r) for r in rows if r]
    assert any("GRAND_TOTAL" in r for r in flat)
    assert any(stadium["estimate_name"] in r or "sanity_summary" in r
               for r in flat)
    sanity = json.load(open(os.path.join(d, "sanity.json")))
    assert sanity["estimate_id"] == stadium["estimate_id"]
    assert sanity["counts"]["hard"] == 0
    assert sanity["archetype"] == "stadium"
    manifest = json.load(open(os.path.join(d, "manifest.json")))
    assert manifest["estimate_id"] == stadium["estimate_id"]
    assert manifest["project_id"] == stadium["project_id"]
    # files map lists every generated file; the manifest itself carries
    # its own byte size at the top level (no self-reference in files)
    assert set(manifest["files"]) == set(sp.PACKAGE_FILES) - {"manifest.json"}
    # the manifest itself is on disk and non-trivial
    assert os.path.getsize(os.path.join(d, "manifest.json")) > 300
    assert manifest["unit_price_per_point"] > 0
    tab = json.load(open(os.path.join(d, "bid_tab.json")))
    assert len(tab["bid_tab"]["groups"]) == 2
    with open(os.path.join(d, "crew_plan.csv"), newline="") as fh:
        plan = list(csv.reader(fh))
    locs = [r[0] for r in plan if r and r[0] in
            ("roof", "bowl-lower", "bowl-upper", "concourse",
             "service-closets", "headend")]
    assert locs == ["roof", "bowl-lower", "bowl-upper", "concourse",
                    "service-closets", "headend"]
    with open(os.path.join(d, "assumptions.csv"), newline="") as fh:
        arows = list(csv.DictReader(fh))
    assert len(arows) == stadium["assumption_count"]
    kinds = {r["origin"] for r in arows}
    assert kinds == {"auto", "manual"}


def test_morning_report_professional_tone(pkg, stadium):
    d, out, summary = pkg
    text = open(os.path.join(d, "MORNING-REPORT.md"), encoding="utf-8").read()
    assert "Class 4" in text and "Class 3" in text  # accuracy stated honestly
    assert "no hard findings" in text.lower()
    assert "Open items" in text
    assert str(stadium["estimate_id"]) in text
    assert "$" in text and "per antenna point" in text
    assert "E1" in text and "E5" in text  # what shipped overnight
    assert "—" not in text, "em-dash in the report"
    for banned in ("here's what matters", "game changer", "let's dive in",
                   "It's not", "comprehensive", "seamless", "robust",
                   "underscore", "testament"):
        assert banned.lower() not in text.lower(), banned


# ------------------------------------------------------------------ route

def test_reports_stadium_route_404_then_manifest(client, stadium,
                                                 tmp_path,
                                                 monkeypatch):
    missing = tmp_path / "empty-pkg"
    missing.mkdir()
    monkeypatch.setenv("ROMTOOL_STADIUM_PACKAGE", str(missing))
    r = client.get("/reports/stadium")
    assert r.status_code == 404
    assert "rebuild" in r.json()["detail"]

    d = tmp_path / "pkg"
    d.mkdir()
    est = client.get("/estimates/%d" % stadium["estimate_id"]).json()["estimate"]
    summary = dict(stadium)
    sp.build_package(est, summary, str(d),
                     today=__import__("datetime").date(2026, 11, 10))
    monkeypatch.setenv("ROMTOOL_STADIUM_PACKAGE", str(d))
    r = client.get("/reports/stadium")
    assert r.status_code == 200
    body = r.json()
    assert body["package_dir"] == str(d)
    manifest = body["manifest"]
    assert manifest["estimate_id"] == stadium["estimate_id"]
    assert manifest["estimate_name"] == stadium["estimate_name"]
    assert set(manifest["files"]) == set(sp.PACKAGE_FILES) - {"manifest.json"}
    assert manifest["api"]["sanity"].endswith("/sanity")


def test_health_version_bumped(client):
    body = client.get("/health").json()
    assert body["version"] == "0.8.0"
    assert body["subphase"] == "E5"


def test_rebuild_is_idempotent(client, stadium, tmp_path_factory):
    again = sp.build_stadium(package_dir=None, actor="e5-tests")
    assert again["project_id"] == stadium["project_id"]
    assert again["estimate_id"] == stadium["estimate_id"]
    assert again["quote_ids"] == stadium["quote_ids"]
    projs = [p["name"] for p in client.get("/projects").json()["projects"]]
    assert projs.count(sp.PROJECT_NAME) == 1
    ests = [e["name"] for e in client.get("/estimates").json()["estimates"]]
    assert ests.count(sp.ESTIMATE_NAME) == 1
