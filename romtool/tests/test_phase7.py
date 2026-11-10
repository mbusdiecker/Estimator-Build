"""Phase E4 (v0.7.0) - intelligence + RF link + actuals loop.

Hand anchors against the shipped config values:
- sanity gates: labor-share windows per archetype (config/sanity.json),
  crew-days/weeks ceil consistency, E-density (stadium fixture: 24 E of 26
  labor lines = 92.3077%), unit-cost outlier ratio == the line's stacked
  factor (roof rope x exterior x night = 3.014375 > 3.0), missing-scope
  checklists (stadium fixture misses I and J), quote expiry pass-through.
- actuals drift math: E01 card 1.8/3.0 min per FT (mid 2.4 -> 0.04 h/ft);
  rows (100 ft / 4.8 h -> 1.2), (50 / 1.8 -> 0.9), (25 / 1.2 -> 1.2):
  qty-weighted mean 7.8/175/0.04 = 1.114286 (drift 11.4286%, watch),
  qty-weighted median 1.2 -> propose low 2.16 / high 3.6; a 3.0 median
  clamps into the 0.5..2.0 guard window.
- override precedence: company override E01 2.16/3.6 -> engine base
  minutes 2.88/ft, provenance stamped, drift loops to 0% tight.
- rf link: seeded example project hand counts (2+2 antennas, 7/8 trunk
  17.0 m, lmr400 17.4 m incl. a 4.2 m cross-floor riser, 3 splitters,
  booster 10 A DC load -> O-zone O01 x1 / O02 x2 / O04 x1).
- ai endpoints: fully mocked gateway, 503 degradation with documented
  schema, 502 on malformed gateway bodies, overlay validation on
  scenario-suggest. NO test touches the network.
"""

import copy
import json
import os
import zipfile

import pytest

from fastapi.testclient import TestClient

from romtool import ai as ai_mod
from romtool import actuals as actuals_mod
from romtool.config import load_config
from romtool.estimate import compute_estimate, load_labor_catalog
from romtool.rflink import RfLinkError, draft_from_model, load_rflink_config
from romtool.sanity import (SanityConfigError, load_checklists,
                            load_sanity_config, run_sanity, sanity_export_rows)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "stadium_estimate.json")
EXAMPLE_MODEL = None


def _load_stadium():
    with open(FIXTURE, "rb") as fh:
        return json.loads(fh.read())


def _recap(d=None):
    return compute_estimate(d if d is not None else _load_stadium())


def _example_model():
    global EXAMPLE_MODEL
    if EXAMPLE_MODEL is None:
        from romtool.example_project import build_example_model
        EXAMPLE_MODEL = build_example_model()
    return EXAMPLE_MODEL


# ------------------------------------------------------------------ config

def test_sanity_config_shape_and_windows():
    cfg = load_sanity_config()
    assert cfg["version"] == "0.7.0"
    assert cfg["severity"]["missing_scope"] == "hard"
    assert cfg["severity"]["crew_duration"] == "hard"
    assert cfg["severity"]["e_density"] == "warning"
    assert cfg["severity"]["unit_cost_outlier"] == "warning"
    assert set(cfg["labor_share_by_archetype"]) == {
        "stadium", "hospital", "warehouse", "tunnel", "hotel", "office"}
    expected_zones = set("ABCDEFGHIJKLMNO")
    for arch, spec in cfg["labor_share_by_archetype"].items():
        assert set(spec["bands"]) == expected_zones
        for z, b in spec["bands"].items():
            assert 0.0 <= b["low"] <= b["high"] <= 1.0
    uco = cfg["unit_cost_outlier"]
    assert uco["ratio_low"] == pytest.approx(0.4)
    assert uco["ratio_high"] == pytest.approx(3.0)
    assert cfg["e_density_warn_pct"] == 50.0
    assert cfg["crew"]["max_plausible_hours_per_crew_week"] == 300.0


def test_checklists_cover_all_six_archetypes():
    cl = load_checklists()
    assert cl["version"] == "0.7.0"
    assert set(cl["archetypes"]) == {
        "stadium", "hospital", "warehouse", "tunnel", "hotel", "office"}
    for arch, entry in cl["archetypes"].items():
        assert entry["label"]
        zones = entry["zones"]
        assert zones, arch
        for z in zones:
            assert "zone_prefix" in z and "caption" in z
    # stadium expects the erces/dc/testing core
    zset = {z["zone_prefix"] for z in cl["archetypes"]["stadium"]["zones"]}
    assert {"A", "G", "O", "H"} <= zset


def test_sanity_config_bad_window_rejected(tmp_path):
    cfg = load_sanity_config()
    cfg["labor_share_by_archetype"]["office"]["bands"]["E"]["high"] = 1.5
    p = tmp_path / "bad_sanity.json"
    p.write_text(json.dumps(cfg))
    with pytest.raises(SanityConfigError, match="invalid"):
        load_sanity_config(str(p))


# ---------------------------------------------------- labor share gate

def test_labor_share_hand_anchor_two_zone_estimate():
    """Wrapped hours hand math (default crew 4+1, N01 0.125 + N02 0.115 ->
    x1.24; derived wrap lines no-stack):
      E06 x1   45 min  -> 0.75 x 1.24 = 0.93 h
      B01 x1  180 min  -> 3.00 x 1.24 = 3.72 h
      N03 x1 (1 crew x 1 location) 120-240 mid 180 -> 3.00 h
      N04 x1 (ceil(4.65/40) = 1 crew-day) mid 22.5 min -> 0.375 h
      total 8.025 h -> E 0.115889 / B 0.463551 / N 0.420560
    office windows: E 0.25-0.70 (low), B 0-0.12 (high), N 0-0.06 (high)."""
    d = {"name": "share", "locations": [
        {"id": "f1", "name": "n", "lines": [{"catalog_id": "E06", "qty": 1},
                                            {"catalog_id": "B01", "qty": 1}]}]}
    recap = _recap(d)
    cfg = load_sanity_config()
    from romtool.sanity import labor_share_gate
    block, findings = labor_share_gate(recap, "office", cfg)
    shares = {e["zone"]: e["share"] for e in block["zones"]}
    assert shares["E"] == pytest.approx(0.93 / 8.025, abs=1e-6)
    assert shares["B"] == pytest.approx(3.72 / 8.025, abs=1e-6)
    assert shares["N"] == pytest.approx(3.375 / 8.025, abs=1e-6)
    zones = {f["zone"]: f["code"] for f in findings}
    assert zones == {"E": "LABOR_SHARE_OUT_OF_BAND",
                     "B": "LABOR_SHARE_OUT_OF_BAND",
                     "N": "LABOR_SHARE_OUT_OF_BAND"}


def test_labor_share_within_band_and_unbanded_zone():
    """B01 x3: B 11.16 + E 0.93 direct = 12.09 -> 1 crew-day;
    N03 3.0 + N04 0.375 -> total 15.465 h; B 0.7215 / E 0.0601 / N 0.2183
    - all three outside the office windows."""
    d = {"name": "share2", "locations": [
        {"id": "f1", "name": "n",
         "lines": [{"catalog_id": "E06", "qty": 1},
                   {"catalog_id": "B01", "qty": 3}]}]}
    recap = _recap(d)
    from romtool.sanity import labor_share_gate
    block, findings = labor_share_gate(recap, "office", load_sanity_config())
    shares = {e["zone"]: e["share"] for e in block["zones"]}
    assert shares["B"] == pytest.approx(11.16 / 15.465, abs=1e-6)
    assert shares["E"] == pytest.approx(0.93 / 15.465, abs=1e-6)
    assert {(f["zone"], f["code"]) for f in findings} == {
        ("E", "LABOR_SHARE_OUT_OF_BAND"), ("B", "LABOR_SHARE_OUT_OF_BAND"),
        ("N", "LABOR_SHARE_OUT_OF_BAND")}


def test_labor_share_stadium_out_of_band_E_warning():
    recap = _recap()
    d = _load_stadium()
    report = run_sanity(recap, d, [], archetype="stadium")
    e_zone = next(z for z in report["gates"]["labor_share"]["zones"]
                  if z["zone"] == "E")
    assert e_zone["status"] == "out_of_band"
    band = report["gates"]["labor_share"]["zones"]
    assert e_zone["share"] > e_zone["band_high"]
    # warning severity: labor_share findings never hard
    assert all(f["gate"] != "labor_share" for f in report["hard_findings"])
    assert any(f["code"] == "LABOR_SHARE_OUT_OF_BAND"
               for f in report["warnings"])


# ---------------------------------------------- crew duration consistency

def test_crew_math_consistent_on_stadium():
    recap = _recap()
    from romtool.sanity import crew_duration_gate
    block, findings = crew_duration_gate(recap, load_sanity_config())
    assert block["consistent"] is True
    assert block["crew_days_expected"] == recap["totals"]["crew_days"]
    assert block["crew_weeks_expected"] == recap["totals"]["crew_weeks"]
    assert findings == []


def test_crew_math_doctored_is_hard_finding():
    recap = _recap()
    recap2 = copy.deepcopy(recap)
    recap2["totals"]["crew_days"] = recap2["totals"]["crew_days"] + 1
    from romtool.sanity import crew_duration_gate
    block, findings = crew_duration_gate(recap2, load_sanity_config())
    assert block["consistent"] is False
    assert [f["code"] for f in findings] == ["CREW_MATH_INCONSISTENT"]
    cfg = load_sanity_config()
    assert cfg["severity"]["crew_duration"] == "hard"


def test_crew_week_plausibility_warning():
    recap = _recap()
    recap["totals"]["hours_per_crew_week"] = 500.0
    from romtool.sanity import crew_duration_gate
    block, findings = crew_duration_gate(recap, load_sanity_config())
    codes = [f["code"] for f in findings]
    assert codes == ["CREW_WEEK_PLAUSIBILITY"]
    assert findings[0]["message"].startswith("implied average load 500.0 h")


# ------------------------------------------------------------- e-density

def test_e_density_stadium_hand_count():
    """Stadium fixture: 26 labor (minutes-kind) lines, 24 E + 2 G ->
    92.3077% over the 50% threshold (warning, not hard)."""
    recap = _recap()
    from romtool.sanity import e_density_gate
    block, findings = e_density_gate(recap, load_sanity_config())
    assert block["labor_lines"] == 26
    assert block["by_quality"] == {"E": 24, "G": 2}
    assert block["e_lines"] == 24
    assert block["e_pct"] == pytest.approx(92.3077, abs=1e-3)
    assert block["threshold_pct"] == 50.0
    assert [f["code"] for f in findings] == ["E_DENSITY_HIGH"]


def test_e_density_threshold_boundary():
    """E06 (E) + derived N03/N04 (E) + H09 (G): 3 of 4 labor lines E ->
    75% exactly; the finding fires strictly above the threshold."""
    d = {"name": "few", "locations": [
        {"id": "f1", "name": "n",
         "lines": [{"catalog_id": "H09", "qty": 1},
                   {"catalog_id": "E06", "qty": 1}]}]}
    recap = _recap(d)
    from romtool.sanity import e_density_gate
    cfg = load_sanity_config()
    block, findings = e_density_gate(recap, cfg)
    assert block["labor_lines"] == 4
    assert block["e_pct"] == pytest.approx(75.0)
    assert findings and findings[0]["code"] == "E_DENSITY_HIGH"
    cfg2 = {**cfg, "e_density_warn_pct": 75.0}
    block2, findings2 = e_density_gate(recap, cfg2)
    assert findings2 == []          # at-threshold is not above-threshold


# -------------------------------------------------- unit-cost outliers

def test_unit_cost_outlier_ratio_equals_stacked_factor():
    """Rope + occupied + constrained + night on E06 qty 1: stacked mid
    1.375 x 1.3 x 1.325 x 1.75 = 4.144766 > ratio_high 3.0 -> finding; the
    reported ratio equals the line's stacked factor (same rate mix)."""
    d = {"name": "stacked", "schedule": {"night": True}, "crew":
         {"crews": 1, "techs": 4, "foremen": 1},
         "locations": [{"id": "f1", "name": "n", "access": "rope",
                        "conditions": ["occupied", "constrained_access"],
                        "lines": [{"catalog_id": "E06", "qty": 1}]}]}
    recap = _recap(d)
    line = next(l for l in recap["lines"] if l["catalog_id"] == "E06")
    assert line["stacked_factor"]["mid"] == pytest.approx(4.144766, abs=1e-5)
    from romtool.sanity import unit_cost_outlier_gate
    block, findings = unit_cost_outlier_gate(recap, load_sanity_config(),
                                             load_labor_catalog(), {})
    assert [f["code"] for f in findings] == ["UNIT_COST_OUTLIER"]
    f = findings[0]
    assert f["side"] == "over"
    assert f["ratio"] == pytest.approx(4.144766, abs=1e-4)
    assert f["line_id"] == "f1:E06"


def test_unit_cost_outlier_stadium_roof_lines():
    """Stadium roof: rope 1.75 x exterior 1.3 x night 1.325 = 3.014375 -
    just past the 3.0 band: the verify-the-stack warning fires on the
    3 A-zone roof lines (not on K-adder rows, which skip the access
    factor)."""
    recap = _recap()
    d = _load_stadium()
    report = run_sanity(recap, d, [], archetype="stadium")
    outs = [f for f in report["warnings"] if f["code"] == "UNIT_COST_OUTLIER"]
    assert sorted(f["catalog_id"] for f in outs) == ["A01", "A04", "A09"]
    a01 = next(f for f in outs if f["catalog_id"] == "A01")
    assert a01["ratio"] == pytest.approx(3.014375, abs=1e-6)
    # K rows never take the access factor -> ratio 1.7225, no finding
    assert "K04" not in {f["catalog_id"] for f in outs}


def test_unit_cost_outlier_under_side_via_config():
    d = {"name": "u", "locations": [
        {"id": "f1", "name": "n", "lines": [{"catalog_id": "E06", "qty": 1}]}]}
    recap = _recap(d)
    cfg = load_sanity_config()
    cfg["unit_cost_outlier"] = {"ratio_low": 5.0, "ratio_high": 9.0}
    from romtool.sanity import unit_cost_outlier_gate
    block, findings = unit_cost_outlier_gate(recap, cfg,
                                             load_labor_catalog(), {})
    assert all(f["side"] == "under" for f in findings)
    ratios = {f["line_id"]: f["ratio"] for f in findings}
    no_stack_ratio = 85.0 / 110.2           # derived wrap lines bill tech only
    assert ratios["f1:E06"] == pytest.approx(1.0, abs=1e-6)  # stacked 1.0x
    assert ratios["N03"] == pytest.approx(no_stack_ratio, abs=1e-6)
    assert ratios["N04"] == pytest.approx(no_stack_ratio, abs=1e-6)


# ------------------------------------------------ missing scope checklist

def test_missing_scope_synthetic_office():
    d = {"name": "p", "locations": [
        {"id": "f1", "name": "n", "lines": [{"catalog_id": "E06", "qty": 2}]}]}
    recap = _recap(d)
    from romtool.sanity import missing_scope_gate
    block, findings = missing_scope_gate(d, recap, "office",
                                         load_checklists())
    # office checklist: B C D E H I J -> only E present
    assert [m["zone_prefix"] for m in block["missing"]] == [
        "B", "C", "D", "H", "I", "J"]
    assert [m["zone_prefix"] for m in block["present"]] == ["E"]
    assert {f["code"] for f in findings} == {"MISSING_SCOPE"}
    assert findings[0]["zone"] == "B"


def test_missing_scope_stadium_fixture_misses_I_J():
    recap = _recap()
    d = _load_stadium()
    report = run_sanity(recap, d, [], archetype="stadium")
    ms = report["gates"]["missing_scope"]
    assert [m["zone_prefix"] for m in ms["missing"]] == ["I", "J"]
    assert [m["zone_prefix"] for m in ms["present"]] == \
        ["A", "C", "E", "G", "H", "K", "O"]
    hard = [f for f in report["hard_findings"] if f["code"] == "MISSING_SCOPE"]
    assert sorted(f["zone"] for f in hard) == ["I", "J"]


def test_missing_scope_hard_findings_block_approval_flag():
    d = {"name": "p", "locations": [
        {"id": "f1", "name": "n", "lines": [{"catalog_id": "E06", "qty": 2}]}],
        "sanity": {"archetype": "stadium"}}
    recap = _recap(d)
    report = run_sanity(recap, d, [], archetype=None)
    assert report["archetype"] == "stadium"
    assert report["blocks_approval"] is True
    assert report["counts"]["hard"] >= 1


# ---------------------------------------------------- quote expiry pass-through

def test_quote_expiry_gate_passes_through_governance_math():
    import datetime
    from romtool.sanity import quote_expiry_gate
    today = datetime.date(2026, 11, 10)
    quotes = [
        {"id": 1, "vendor": "A", "scope": "s", "amount": 1000.0,
         "binding_until": "2026-11-01", "status": "received"},
        {"id": 2, "vendor": "B", "scope": "s", "amount": 2000.0,
         "binding_until": "2026-11-20", "status": "bound"},
    ]
    block, findings = quote_expiry_gate(quotes, today)
    assert block["total"] == 2
    assert sorted(f["code"] for f in findings) == \
        ["QUOTE_BINDING_SOON", "QUOTE_EXPIRED"]   # 10 days < 14 warn threshold
    assert findings[-1]["gate"] == "quote_expiry"
    assert all(f["message"] for f in findings)


def test_run_sanity_no_archetype_skips_share_and_scope():
    recap = _recap()
    d = _load_stadium()
    report = run_sanity(recap, d, [])
    assert report["archetype"] is None
    assert report["gates"]["labor_share"]["status"] == "skipped"
    assert report["gates"]["missing_scope"]["status"] == "skipped"
    assert report["gates"]["crew_duration"]["status"] == "ok"


# ---------------------------------------------------------- exports rows

def test_sanity_export_rows_shape():
    d = {"name": "p", "locations": [
        {"id": "f1", "name": "n", "lines": [{"catalog_id": "E06", "qty": 2}]}],
        "sanity": {"archetype": "stadium"}}
    report = run_sanity(_recap(d), d, [])
    rows = sanity_export_rows(report)
    assert rows[0][0] == "sanity_summary"
    assert "blocks_approval=yes" in rows[0]
    codes = {r[0] for r in rows[1:]}
    assert "sanity_hard" in codes
    assert any(r[1] == "MISSING_SCOPE" for r in rows if r[0] == "sanity_hard")


# --------------------------------------------------------- drift math

def test_drift_report_hand_anchor():
    cat = load_labor_catalog()
    e01 = next(r for r in cat["units"] if r["id"] == "E01")
    assert (e01["minutes_low"], e01["minutes_high"]) == (1.8, 3.0)
    mid_hpf = 2.4 / 60.0          # 0.04 norm h per ft
    rows = [
        {"id": 1, "catalog_id": "E01", "qty": 100, "hours": 4.8, "date": "2026-11-01"},
        {"id": 2, "catalog_id": "E01", "qty": 50, "hours": 1.8, "date": "2026-11-02"},
        {"id": 3, "catalog_id": "E01", "qty": 25, "hours": 1.2, "date": "2026-11-03"},
    ]
    drift = actuals_mod.drift_report(rows, cat)
    card = drift["cards"][0]
    assert card["catalog_id"] == "E01"
    assert card["norm_minutes"] == {"low": 1.8, "mid": 2.4, "high": 3.0}
    ratios = [e["ratio"] for e in card["rows"]]
    assert ratios == [pytest.approx(1.2), pytest.approx(0.9),
                      pytest.approx(1.2)]
    # qty-weighted mean: (4.8 + 1.8 + 1.2) / 175 ft = 0.0445714 h/ft -> /0.04
    assert card["ratio_weighted_mean"] == pytest.approx(1.114286, abs=1e-5)
    # qty-weighted median: [(0.9, w50), (1.2, w100), (1.2, w25)]; half 87.5
    assert card["ratio_weighted_median"] == pytest.approx(1.2)
    assert card["drift_pct"] == pytest.approx(11.4286, abs=1e-3)
    assert card["quality_flag"] == "watch"


def test_drift_report_single_row_and_flags():
    cat = load_labor_catalog()
    rows = [{"id": 1, "catalog_id": "E01", "qty": 10, "hours": 0.4,
             "date": "2026-11-01"}]          # exactly on norm -> ratio 1.0
    drift = actuals_mod.drift_report(rows, cat)
    card = drift["cards"][0]
    assert card["n_rows"] == 1
    assert card["ratio_weighted_median"] == pytest.approx(1.0)
    assert card["drift_pct"] == pytest.approx(0.0, abs=1e-9)
    assert card["quality_flag"] == "tight"
    assert drift["unresolved_ids"] == []
    # unknown card id -> surfaced, never crashes
    drift2 = actuals_mod.drift_report(rows + \
        [{"catalog_id": "ZZZ", "qty": 1, "hours": 1, "date": "2026-11-01"}], cat)
    assert drift2["unresolved_ids"] == ["ZZZ"]


def test_weighted_median_definition():
    wm = actuals_mod.weighted_median
    assert wm([(3.0, 1)]) == 3.0
    assert wm([(1.0, 10), (5.0, 10)]) == 1.0      # smallest at the half-cross
    assert wm([(0.9, 50), (1.2, 100), (1.2, 25)]) == 1.2
    assert wm([(1.2, 1), (1.2, 1), (3.0, 1)]) == 1.2


def test_propose_refit_hand_anchor_and_guard():
    cat = load_labor_catalog()
    e01 = next(r for r in cat["units"] if r["id"] == "E01")
    rows = [
        {"catalog_id": "E01", "qty": 100, "hours": 4.8, "date": "2026-11-01"},
        {"catalog_id": "E01", "qty": 50, "hours": 1.8, "date": "2026-11-02"},
        {"catalog_id": "E01", "qty": 25, "hours": 1.2, "date": "2026-11-03"},
    ]
    cfg = actuals_mod.load_actuals_config()
    assert cfg["min_rows_for_refit_proposal"] == 2
    assert (cfg["guard_ratio_low"], cfg["guard_ratio_high"]) == (0.5, 2.0)
    drift = actuals_mod.drift_report(rows, cat, cfg)
    prop = actuals_mod.propose_refit(drift, cat, cfg)
    p = prop["proposals"][0]
    assert p["catalog_id"] == "E01"
    assert p["median_ratio"] == pytest.approx(1.2)
    # norm low/high scaled by the median ratio, unclamped at 1.2
    assert p["proposed_low"] == pytest.approx(e01["minutes_low"] * 1.2)  # 2.16
    assert p["proposed_high"] == pytest.approx(e01["minutes_high"] * 1.2)  # 3.6
    assert p["proposed_mid"] == pytest.approx(2.88)
    assert p["clamped"] is False
    assert prop["never_auto_applies"] is True


def test_propose_refit_clamps_outlier_median():
    cat = load_labor_catalog()
    # two rows both at 3.0x of the E01 norm card -> median 3.0 clamps to 2.0
    rows = [
        {"catalog_id": "E01", "qty": 60, "hours": 60 * 2.4 / 60 * 3.0,
         "date": "2026-11-01"},
        {"catalog_id": "E01", "qty": 40, "hours": 40 * 2.4 / 60 * 3.0,
         "date": "2026-11-02"},
    ]
    drift = actuals_mod.drift_report(rows, cat)
    assert drift["cards"][0]["ratio_weighted_median"] == pytest.approx(3.0)
    prop = actuals_mod.propose_refit(drift, cat)
    p = prop["proposals"][0]
    assert p["clamped"] is True
    assert p["median_ratio"] == pytest.approx(3.0)
    assert p["proposed_low"] == pytest.approx(1.8 * 2.0)   # guard 2.0
    assert p["proposed_high"] == pytest.approx(3.0 * 2.0)
    assert "clamped" in p["note"]


def test_propose_refit_min_rows_gate():
    cat = load_labor_catalog()
    cfg = actuals_mod.load_actuals_config()
    drift = actuals_mod.drift_report(
        [{"catalog_id": "E01", "qty": 10, "hours": 0.5, "date": "2026-11-01"}],
        cat, cfg)
    prop = actuals_mod.propose_refit(drift, cat, cfg)
    assert prop["proposals"] == []
    assert prop["skipped"][0]["catalog_id"] == "E01"
    assert "needs >= 2" in prop["skipped"][0]["reason"]


def test_validate_actual_rows_errors():
    with pytest.raises(actuals_mod.ActualsError) as exc:
        actuals_mod.validate_actual_rows([])
    assert "rows[]" in str(exc.value)
    with pytest.raises(actuals_mod.ActualsError) as exc:
        actuals_mod.validate_actual_rows([
            {"catalog_id": "NOPE", "qty": 1, "hours": 1, "date": "2026-11-01"},
            {"catalog_id": "E01", "qty": -1, "hours": 0, "date": "not-a-date"},
            {"catalog_id": "K03", "qty": 1, "hours": 1, "date": "2026-11-01"},
        ])
    msgs = exc.value.errors
    assert any("NOPE" in m for m in msgs)
    assert any("qty" in m for m in msgs)
    assert any("hours" in m for m in msgs)
    assert any("date" in m for m in msgs)
    assert any("K03" in m and "factor row" in m for m in msgs)
    ok = actuals_mod.validate_actual_rows(
        [{"catalog_id": "E01", "qty": 10, "hours": 0.4, "cost": 50.5,
          "date": "2026-11-01T10:00:00", "location_id": " f1 ",
          "note": " crew A "}])
    assert ok == [{"catalog_id": "E01", "location_id": "f1", "qty": 10.0,
                   "hours": 0.4, "cost": 50.5, "date": "2026-11-01",
                   "note": "crew A"}]


def test_refit_payload_validation_and_apply(tmp_path):
    cat = load_labor_catalog()
    path = tmp_path / "company_labor_overrides.json"
    os.environ["ROMTOOL_COMPANY_OVERRIDES"] = str(path)
    try:
        # invalid payloads -> ActualsError
        with pytest.raises(actuals_mod.ActualsError):
            actuals_mod.apply_refit_to_file(
                {"catalog_id": "ZZZ", "low": 1, "high": 2}, {"catalog": cat},
                path=str(path))
        with pytest.raises(actuals_mod.ActualsError) as exc:
            actuals_mod.apply_refit_to_file(
                {"catalog_id": "E01", "low": 5, "high": 1}, {"catalog": cat},
                path=str(path))
        assert any("high" in m for m in exc.value.errors)
        layer = actuals_mod.apply_refit_to_file(
            {"catalog_id": "E01", "low": 2.16, "high": 3.6,
             "note": "field refit"},
            {"estimate_id": 7, "n_rows": 3, "median_ratio": 1.2,
             "actor": "tester", "revision": 1, "catalog": cat},
            path=str(path), now_iso="2026-11-10T00:00:00Z")
        assert layer["applied"]["catalog_id"] == "E01"
        assert layer["applied"]["low"] == 2.16
        assert layer["applied"]["source"]["estimate_id"] == 7
        assert layer["history"][0]["catalog_id"] == "E01"
        assert layer["history"][0]["refit_ratio"] == pytest.approx(1.2)
        # second apply keeps the prior card + appends history
        layer2 = actuals_mod.apply_refit_to_file(
            {"catalog_id": "E01", "low": 2.0, "high": 3.4, "note": "re-refit"},
            {"estimate_id": 7, "n_rows": 3, "median_ratio": 1.05,
             "actor": "tester", "revision": 2, "catalog": cat},
            path=str(path), now_iso="2026-11-11T00:00:00Z")
        assert layer2["applied"]["prior"]["low"] == 2.16
        assert layer2["applied"]["prior"]["updated_at"] == "2026-11-10T00:00:00Z"
        assert len(layer2["history"]) == 2
    finally:
        os.environ.pop("ROMTOOL_COMPANY_OVERRIDES", None)


def test_override_precedence_engine_prefers_company_card(tmp_path):
    path = tmp_path / "company_labor_overrides.json"
    path.write_text(json.dumps({"version": "0.7.0", "overrides": {
        "E01": {"low": 2.16, "high": 3.6, "note": "company refit",
                "updated_at": "2026-11-10T00:00:00Z"}}}))
    os.environ["ROMTOOL_COMPANY_OVERRIDES"] = str(path)
    try:
        cat = load_labor_catalog()
        e01 = next(r for r in cat["units"] if r["id"] == "E01")
        # engine prefers the override card
        assert (e01["minutes_low"], e01["minutes_high"]) == (2.16, 3.6)
        assert e01["company_override"]["prior_minutes"] == {"low": 1.8,
                                                            "high": 3.0}
        base_path = os.path.join(os.path.dirname(HERE), "config",
                                 "labor_units.json")
        with open(base_path, "r", encoding="utf-8") as fh:
            base = json.load(fh)
        base_e01 = next(r for r in base["units"] if r["id"] == "E01")
        assert (base_e01["minutes_low"], base_e01["minutes_high"]) == (1.8, 3.0)
        # hand math: 100 ft x 2.16 min = 360 min low / 600 min high
        recap = compute_estimate({"name": "ov", "locations": [
            {"id": "f1", "name": "n",
             "lines": [{"catalog_id": "E01", "qty": 100}]}]})
        line = next(l for l in recap["lines"] if l["catalog_id"] == "E01")
        assert line["base_minutes"]["low"] == pytest.approx(216.0)
        assert line["base_minutes"]["high"] == pytest.approx(360.0)
        assert line["base_minutes"]["mid"] == pytest.approx(288.0)
        assert line["company_override"]["note"] == "company refit"
        assert recap["company_override_catalog_ids"] == ["E01"]
        # the loop closes: the earlier 1.2x actuals now drift 0% (tight)
        rows = [{"catalog_id": "E01", "qty": 100, "hours": 4.8,
                 "date": "2026-11-01"}]
        drift = actuals_mod.drift_report(rows, load_labor_catalog())
        assert drift["cards"][0]["drift_pct"] == pytest.approx(0.0, abs=1e-9)
        assert drift["cards"][0]["quality_flag"] == "tight"
        assert drift["cards"][0]["company_override"] is True
    finally:
        os.environ.pop("ROMTOOL_COMPANY_OVERRIDES", None)


# ------------------------------------------------------------- RF link

def test_rflink_draft_from_example_project_hand_counts():
    model = _example_model()
    draft = draft_from_model(model, load_config(), catalog=load_labor_catalog())
    basis, defn = draft["basis"], draft["definition"]
    assert basis["antennas_per_floor"] == {"exf1": 2, "exf2": 2}
    assert basis["antennas_total"] == 4
    assert basis["hub_floors"] == ["exf1"]
    assert basis["headend_floors"] == ["exf1"]
    assert basis["cable_meters_by_id"] == {"coax_7_8in": 17.0, "lmr400": 17.4}
    f1 = next(l for l in defn["locations"] if l["id"] == "floor_exf1")
    f2 = next(l for l in defn["locations"] if l["id"] == "floor_exf2")
    qty = lambda loc, cid: next((l["qty"] for l in loc["lines"]
                                 if l["catalog_id"] == cid), None)

    # antenna points per floor + bid unit-price basis
    assert defn["antenna_points"] == {"floor_exf1": 2.0, "floor_exf2": 2.0}
    assert qty(f1, "E06") == 2 and qty(f2, "E06") == 2
    # splitters + hub closet rows on the hub floor
    assert qty(f1, "E05") == 3
    assert {c for c in ("C01", "C02", "C06", "C07") if qty(f1, c) is not None} \
        == {"C01", "C02", "C06", "C07"}
    assert qty(f1, "C07") == 2
    # headend rows + power feed
    assert {c: qty(f1, c) for c in ("B01", "B03", "B09", "A01", "A02", "A04",
                                    "J01", "J02", "J04")} == {
        "B01": 1.0, "B03": 1.0, "B09": 1.0, "A01": 1.0, "A02": 1.0,
        "A04": 1.0, "J01": 1.0, "J02": 2.0, "J04": 1.0}
    # coax: 7/8 trunk 17.0 m + lmr400 horizontal 13.2 m -> E01 99.08 ft
    # (17.0 + 13.2) x 3.28084; riser b4/b5 lmr400 4.2 m -> E02 13.78 ft
    assert qty(f1, "E01") == pytest.approx(round(30.2 * 3.28084, 2))
    assert qty(f1, "E01") == pytest.approx(99.08)
    assert qty(f1, "E02") == pytest.approx(13.78)
    assert qty(f1, "E10") == pytest.approx(13.78)      # riser fire wrap
    assert qty(f1, "D05") == pytest.approx(22.57, abs=1e-9)  # 112.86 ft / 5
    assert qty(f1, "E03") == 16.0                      # 8 runs x both ends
    assert qty(f1, "E04") == 5.0                       # 2 antennas + 3 taps
    assert qty(f2, "E04") == 2.0
    # L-zone allowances: 4 tiles/antenna; patches per penetration qty
    assert qty(f1, "L02") == 8.0 and qty(f2, "L02") == 8.0
    assert qty(f1, "L03") == 3.0                        # A04 1 + C07 2
    # testing: per floor x band
    assert qty(f1, "H04") == 12 and qty(f2, "H04") == 12
    assert qty(f1, "H05") == 12 and qty(f2, "H05") == 12
    assert qty(f1, "H01") == 8.0                         # sweep per run
    assert qty(f1, "H02") == 2.0 and qty(f2, "H02") == 2.0
    assert qty(f2, "E03") is None                        # connectors ride owner
    # estimate-level engineering + testing rows on the first location
    eng = {c: qty(f1, c) for c in ("I01", "I02", "I03", "I06", "I05",
                                   "H06", "H07", "H09", "H10")}
    assert eng == {"I01": 1.0, "I02": 2.0, "I03": 1.0, "I06": 1.0,
                   "I05": 1.0, "H06": 1.0, "H07": 1.0, "H09": 12.0,
                   "H10": 1.0}
    # booster DC load block -> engine auto-sizer source
    assert f1["power_plant"] == {"load_amps": 10.0, "runtime_hours": 12.0}
    assert "power_plant" not in f2


def test_rflink_draft_computes_and_sizes_the_o_zone():
    draft = draft_from_model(_example_model(), load_config(),
                             catalog=load_labor_catalog())
    recap = compute_estimate(draft["definition"])
    o_lines = {l["catalog_id"]: l["qty"] for l in recap["lines"]
               if l["zone"].startswith("O-")}
    # Ah = 12 h x 10 A = 120; strings ceil(120/120) = 1; rects ceil(10/50)+1
    assert o_lines == {"O01": 1.0, "O02": 2.0, "O04": 1.0}
    assert recap["company_override_catalog_ids"] == []


def test_rflink_riser_detection_cross_floor_run():
    model = _example_model()
    model["floors"][0]["elements"].append({
        "id": "x_r", "type": "cable_run", "from_id": "ex_sp2",
        "to_id": "ex_a3", "cable_id": "coax_1_2in", "length_m": 3.0})
    draft = draft_from_model(model, load_config(), catalog=load_labor_catalog())
    f1 = next(l for l in draft["definition"]["locations"]
              if l["id"] == "floor_exf1")
    qty = lambda cid: next((l["qty"] for l in f1["lines"]
                            if l["catalog_id"] == cid), None)
    assert qty("E02") == pytest.approx(round((4.2 + 3.0) * 3.28084, 2))
    assert qty("E10") == pytest.approx(round((4.2 + 3.0) * 3.28084, 2))


def test_rflink_unknown_cable_rejected_and_empty_project():
    model = copy.deepcopy(_example_model())
    run_el = next(el for fl in model["floors"] for el in fl["elements"]
                  if el.get("type") == "cable_run")
    run_el["cable_id"] = "bad_cable"
    with pytest.raises(RfLinkError, match="bad_cable"):
        draft_from_model(model, load_config(), catalog=load_labor_catalog())
    empty = {"name": "x", "floors": [{"id": "f", "name": "n", "level": 0,
                                      "elements": []}]}
    with pytest.raises(RfLinkError, match="no antenna"):
        draft_from_model(empty, load_config())


def test_rflink_draft_endpoint_draft_then_store(client):
    r = client.post("/projects/1/draft-estimate")
    assert r.status_code == 200
    body = r.json()
    assert body["stored"] is False
    assert body["project_id"] == 1
    defn = body["definition"]
    cat_ids = {u["id"] for u in load_labor_catalog()["units"]}
    n_lines = 0
    for loc in defn["locations"]:
        assert loc["lines"]
        for ln in loc["lines"]:
            n_lines += 1
            assert ln["catalog_id"] in cat_ids
    assert n_lines > 30 and len(defn["locations"]) >= 2
    assert body["basis"]["antennas_total"] == 4

    before = {e["id"] for e in client.get("/estimates").json()["estimates"]}
    r2 = client.post("/projects/1/draft-estimate?store=true")
    assert r2.status_code == 200
    est = r2.json()["estimate"]
    assert est["state"] == "draft" and est["revision"] == 1
    assert r2.json()["draft_basis"]["antennas_total"] == 4
    assert est["id"] not in before
    after = {e["id"] for e in client.get("/estimates").json()["estimates"]}
    assert after - before == {est["id"]}
    # stored estimate carries real O-zone lines from the booster load block
    o_lines = [l for l in est["recap"]["lines"] if l["zone"].startswith("O-")]
    assert {(l["catalog_id"], l["qty"]) for l in o_lines} == {
        ("O01", 1.0), ("O02", 2.0), ("O04", 1.0)}


def test_rflink_draft_endpoint_empty_project_422(client):
    proj = client.post("/projects", json={"name": "bare"}).json()
    r = client.post("/projects/%s/draft-estimate" % proj["id"])
    assert r.status_code == 422
    assert "no floors" in r.json()["detail"]["errors"][0]


# ----------------------------------------------------------- AI endpoints

AI_CFG_STUB = {
    "version": "0.7.0", "base_url": "http://ai-gw.test/v1",
    "model": "test-model", "api_key_env": None, "timeout_seconds": 5,
    "temperature": 0.1, "max_tokens": 64,
    "prompts": {"system": "strict json", "rfp_scan_instruction": "scan",
                "scenario_suggest_instruction": "scen",
                "bid_memo_instruction": "memo"},
}


def _write_ai_cfg(tmp_path, **over):
    data = copy.deepcopy(AI_CFG_STUB)
    data.update(over)
    p = tmp_path / "ai.json"
    p.write_text(json.dumps(data))
    os.environ["ROMTOOL_AI"] = str(p)
    return p


GATEWAY_OK = {"choices": [{"message": {"content": ""}}]}


def _gw(content_obj=None, raw=None):
    body = raw if raw is not None else json.dumps(content_obj)
    return {"choices": [{"message": {"content": body}}]}


def test_ai_degrades_503_without_config(client, tmp_path, monkeypatch):
    monkeypatch.setenv("ROMTOOL_AI", str(tmp_path / "missing.json"))
    for url, payload in (
            ("/ai/rfp-scan", {"text": "RFP"}),
            ("/ai/scenario-suggest", {"recap": {"name": "r", "totals": {}}}),
            ("/ai/bid-memo", {"recap": {"name": "r", "totals": {}}})):
        r = client.post(url, json=payload)
        assert r.status_code == 503
        body = r.json()
        assert body["detail"] == "ai gateway unreachable"
        assert body["schema"]["request"] and body["schema"]["response"]


def test_ai_degrades_503_without_api_key(client, tmp_path, monkeypatch):
    _write_ai_cfg(tmp_path, api_key_env="ROMTOOL_AI_TEST_KEY")
    monkeypatch.delenv("ROMTOOL_AI_TEST_KEY", raising=False)
    r = client.post("/ai/rfp-scan", json={"text": "RFP"})
    assert r.status_code == 503
    assert "api key env var" in r.json()["reason"]


def test_ai_unreachable_gateway_is_503(client, tmp_path, monkeypatch):
    _write_ai_cfg(tmp_path)

    def boom(url, payload, api_key=None, timeout=30.0):
        raise ai_mod.AiUnavailable("connection refused")

    monkeypatch.setattr(ai_mod, "_http_post_json", boom)
    r = client.post("/ai/scenario-suggest",
                    json={"recap": {"name": "r", "totals": {}}})
    assert r.status_code == 503
    assert r.json()["detail"] == "ai gateway unreachable"


def test_ai_rfp_scan_contract(client, tmp_path, monkeypatch):
    _write_ai_cfg(tmp_path)
    seen = {}

    def fake(url, payload, api_key=None, timeout=30.0):
        seen["url"], seen["payload"], seen["key"] = url, payload, api_key
        return _gw({"requirements": [
                        {"id": "R1", "category": "erces",
                         "text": "ERCES battery runtime 12h",
                         "source_excerpt": "12-hour backup"}],
                    "scope_matrix": [{"requirement_id": "R1", "in_scope": True,
                                      "catalog_zones": ["G", "O"],
                                      "note": "size the plant"}],
                    "gaps": [{"id": "G1", "text": "carrier count",
                              "reason": "unspecified"}],
                    "assumptions": ["3 carriers assumed"]})

    monkeypatch.setattr(ai_mod, "_http_post_json", fake)
    r = client.post("/ai/rfp-scan", json={"text": "RFP excerpt ..."})
    assert r.status_code == 200
    out = r.json()
    assert out["result"]["gaps"][0]["id"] == "G1"
    assert out["meta"]["model"] == "test-model"
    assert out["schema"]["response"]["scope_matrix"]
    # transport contract: chat/completions, model+messages+temperature
    assert seen["url"] == "http://ai-gw.test/v1/chat/completions"
    assert seen["payload"]["model"] == "test-model"
    assert seen["payload"]["temperature"] == 0.1
    body_txt = seen["payload"]["messages"][-1]["content"]
    assert "RFP excerpt" in body_txt
    # 422 on empty text
    r = client.post("/ai/rfp-scan", json={"text": "  "})
    assert r.status_code == 422


def test_ai_malformed_gateway_bodies_are_502(client, tmp_path, monkeypatch):
    _write_ai_cfg(tmp_path)
    for bad in (_gw(raw="not json"), _gw({"wrong": []}),
                _gw(raw="```json{\"requirements\":[]}```")):
        monkeypatch.setattr(ai_mod, "_http_post_json", lambda *a, **k: bad)
        r = client.post("/ai/rfp-scan", json={"text": "x"})
        assert r.status_code == 502, bad
        assert r.json()["detail"].startswith("ai gateway")


def test_ai_scenario_suggest_validates_overlays(client, tmp_path,
                                                monkeypatch):
    est = client.post("/estimates", json={"name": "ai-s", "locations": [
        {"id": "f1", "name": "n",
         "lines": [{"catalog_id": "E06", "qty": 2}]}]}).json()["estimate"]
    _write_ai_cfg(tmp_path)
    good = _gw({"suggestions": [
        {"name": "rope f1", "rationale": "access heavy",
         "overlay": {"access_overrides": {"f1": "rope"}},
         "impact_hint": "hours up"}]})
    monkeypatch.setattr(ai_mod, "_http_post_json", lambda *a, **k: good)
    r = client.post("/ai/scenario-suggest", json={"estimate_id": est["id"]})
    assert r.status_code == 200
    assert r.json()["result"]["suggestions"][0]["overlay"]["access_overrides"]
    bad = _gw({"suggestions": [
        {"name": "bad", "rationale": "x",
         "overlay": {"access_overrides": {"ghost-loc": "rope"}}}]})
    monkeypatch.setattr(ai_mod, "_http_post_json", lambda *a, **k: bad)
    r = client.post("/ai/scenario-suggest", json={"estimate_id": est["id"]})
    assert r.status_code == 422
    assert "overlay invalid" in json.dumps(r.json()["detail"])


def test_ai_bid_memo_includes_assumption_register(client, tmp_path,
                                                  monkeypatch):
    est = client.post("/estimates", json={"name": "ai-m", "locations": [
        {"id": "f1", "name": "n",
         "lines": [{"catalog_id": "E06", "qty": 2}]}]}).json()["estimate"]
    n_assum = client.get("/estimates/%d/assumptions" % est["id"]).json()[
        "summary"]["total"]
    assert n_assum >= 1
    _write_ai_cfg(tmp_path)
    seen = {}

    def fake(url, payload, api_key=None, timeout=30.0):
        seen["messages"] = payload["messages"]
        return _gw({"memo": "MEMO BODY", "highlights": ["total"]})

    monkeypatch.setattr(ai_mod, "_http_post_json", fake)
    r = client.post("/ai/bid-memo", json={"estimate_id": est["id"]})
    assert r.status_code == 200
    assert r.json()["result"]["memo"] == "MEMO BODY"
    assert r.json()["assumptions_included"] == n_assum
    user_txt = seen["messages"][-1]["content"]
    assert "assumptions" in json.loads(user_txt)


def test_ai_transport_sends_bearer_key_and_requires_200(client, tmp_path,
                                                       monkeypatch):
    _write_ai_cfg(tmp_path, api_key_env="ROMTOOL_AI_TEST_KEY")
    monkeypatch.setenv("ROMTOOL_AI_TEST_KEY", "sk-test")
    seen = {}

    def fake(url, payload, api_key=None, timeout=30.0):
        seen["api_key"] = api_key
        return _gw({"requirements": [], "scope_matrix": [], "gaps": []})

    monkeypatch.setattr(ai_mod, "_http_post_json", fake)
    r = client.post("/ai/rfp-scan", json={"text": "x"})
    assert r.status_code == 200
    assert seen["api_key"] == "sk-test"


# ------------------------------------------------- sanity API + approval guard

def _make_est(client, defn):
    r = client.post("/estimates", json=defn)
    assert r.status_code == 200, r.text
    return r.json()["estimate"]


PARTIAL_OFFICE = {"name": "partial office", "locations": [
    {"id": "f1", "name": "n", "lines": [{"catalog_id": "E06", "qty": 2}]}],
    "sanity": {"archetype": "office"}}


def test_sanity_endpoint_shape_and_archetypes(client):
    est = _make_est(client, copy.deepcopy(PARTIAL_OFFICE))
    eid = est["id"]
    r = client.get("/estimates/%d/sanity" % eid)
    assert r.status_code == 200
    s = r.json()
    assert s["archetype"] == "office"
    assert s["archetype_source"] == "definition"
    assert set(s["gates"]) == {"labor_share", "crew_duration", "e_density",
                               "unit_cost_outlier", "quote_expiry",
                               "missing_scope"}
    assert s["blocks_approval"] is True
    assert {f["code"] for f in s["hard_findings"]} == {"MISSING_SCOPE"}
    # query override wins over the definition
    r2 = client.get("/estimates/%d/sanity?archetype=warehouse" % eid)
    s2 = r2.json()
    assert s2["archetype"] == "warehouse"
    assert s2["archetype_source"] == "query_override"
    assert s2["counts"]["hard"] != {} and \
        [m["zone_prefix"] for m in s2["gates"]["missing_scope"]["missing"]] == \
        ["B", "C", "D", "H", "K"]
    # no archetype at all -> gates skipped, nothing hard
    d2 = copy.deepcopy(PARTIAL_OFFICE)
    d2.pop("sanity")
    est2 = _make_est(client, d2)
    s3 = client.get("/estimates/%d/sanity" % est2["id"]).json()
    assert s3["archetype"] is None
    assert s3["gates"]["labor_share"]["status"] == "skipped"
    assert s3["blocks_approval"] is False


def test_sanity_blocks_and_releases_approval(client):
    est = _make_est(client, copy.deepcopy(PARTIAL_OFFICE))
    eid = est["id"]
    assert client.post("/estimates/%d/state" % eid,
                       json={"to": "review"}).status_code == 200
    r = client.post("/estimates/%d/state" % eid, json={"to": "approved"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "sanity hard findings block" in detail["error"]
    assert {f["code"] for f in detail["hard_findings"]} == {"MISSING_SCOPE"}
    assert detail["sanity_endpoint"] == "/estimates/%d/sanity" % eid
    # estimator completes scope in review; approval releases
    d2 = copy.deepcopy(PARTIAL_OFFICE)
    d2["locations"][0]["lines"] += [
        {"catalog_id": cid, "qty": 1} for cid in
        ("B01", "C01", "D01", "H04", "I02", "J01")]
    assert client.patch("/estimates/%d" % eid,
                        json={"definition": d2,
                              "note": "complete office scope"}).status_code == 200
    r2 = client.post("/estimates/%d/state" % eid, json={"to": "approved"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["estimate"]["state"] == "approved"


def test_sanity_warnings_never_block_approval(client):
    """E-heavy + no archetype: warnings only -> draft->review->approved."""
    d = {"name": "warn only", "locations": [
        {"id": "f1", "name": "n", "lines": [{"catalog_id": "E06", "qty": 2}]}]}
    est = _make_est(client, d)
    for step in ("review", "approved"):
        r = client.post("/estimates/%d/state" % est["id"], json={"to": step})
        assert r.status_code == 200, (step, r.text)


def test_quote_expiry_surfaces_in_sanity_report(client):
    est = _make_est(client, copy.deepcopy(PARTIAL_OFFICE))
    eid = est["id"]
    import datetime
    past = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    r = client.post("/estimates/%d/quotes" % eid, json={
        "vendor": "V", "scope": "labor supply", "amount": 5000.0,
        "binding_until": past, "status": "received"})
    assert r.status_code == 200
    s = client.get("/estimates/%d/sanity" % eid).json()
    assert s["gates"]["quote_expiry"]["status"] == "finding"
    codes = [f["code"] for f in s["warnings"] if f["gate"] == "quote_expiry"]
    assert codes == ["QUOTE_EXPIRED"]
    assert s["counts"]["warnings"] >= 1


def test_recap_exports_carry_sanity_section(client):
    est = _make_est(client, copy.deepcopy(PARTIAL_OFFICE))
    eid = est["id"]
    r = client.get("/exports/estimates/%d/recap.csv" % eid)
    assert r.status_code == 200
    sanity_lines = [ln for ln in r.text.splitlines()
                    if ln.startswith("sanity")]
    assert sanity_lines and sanity_lines[0].startswith("sanity_summary")
    assert 'blocks_approval=yes' in sanity_lines[0]
    assert any("MISSING_SCOPE" in ln for ln in sanity_lines)
    assert any("sanity_hard" in ln for ln in sanity_lines)
    r = client.get("/exports/estimates/%d/recap.xlsx" % eid)
    assert r.status_code == 200
    from romtool.xlsx import encode_xlsx  # noqa: F401 - writer referenced
    assert len(r.content) > 200


# --------------------------------------------------------- actuals API flow

def _E01_actual(qty, ratio, day):
    return {"catalog_id": "E01", "location_id": "f1",
            "qty": qty, "hours": round(qty * (2.4 / 60.0) * ratio, 6),
            "cost": 100.0, "date": day, "note": "field crew"}


def test_actuals_api_flow_import_drift_propose_apply(client, tmp_path,
                                                     monkeypatch):
    est = _make_est(client, {"name": "actuals flow", "locations": [
        {"id": "f1", "name": "n",
         "lines": [{"catalog_id": "E01", "qty": 100}]}]})
    eid = est["id"]
    ov_path = tmp_path / "company_labor_overrides.json"
    monkeypatch.setenv("ROMTOOL_COMPANY_OVERRIDES", str(ov_path))
    if ov_path.exists():
        ov_path.unlink()

    r = client.post("/estimates/%d/actuals" % eid, json={"rows": [
        _E01_actual(100, 1.2, "2026-11-01"),
        _E01_actual(50, 0.9, "2026-11-02")]})
    assert r.status_code == 200
    body = r.json()
    assert body["imported"] == 2
    assert body["drift"]["cards"][0]["n_rows"] == 2

    drift = client.get("/estimates/%d/actuals/drift" % eid).json()
    card = drift["cards"][0]
    # weighted mean: (4.8 + 1.8) / 150 ft / 0.04 h/ft = 1.1 -> drift 10.0%
    assert card["drift_pct"] == pytest.approx(10.0, abs=1e-9)
    assert card["quality_flag"] == "tight"
    rows = client.get("/estimates/%d/actuals" % eid).json()["rows"]
    assert [r["qty"] for r in rows] == [100.0, 50.0]
    assert rows[0]["cost"] == 100.0

    prop = client.post("/estimates/%d/actuals/propose-refit" % eid).json()
    assert prop["proposal"]["never_auto_applies"] is True
    proposal = prop["proposal"]["proposals"][0]
    assert proposal["catalog_id"] == "E01"
    # stored + audited, nothing applied
    proposals = client.get(
        "/estimates/%d/actuals/refit-proposals" % eid).json()["proposals"]
    assert proposals and proposals[0]["proposal"]["proposals"]
    assert ov_path.exists() is False          # propose NEVER writes the layer

    apply_body = {"catalog_id": "E01", "low": proposal["proposed_low"],
                  "high": proposal["proposed_high"],
                  "note": "company-approved refit"}
    r2 = client.post("/estimates/%d/actuals/apply-refit" % eid,
                     json=apply_body)
    assert r2.status_code == 200, r2.text
    applied = r2.json()["applied"]
    assert applied["source"]["estimate_id"] == eid
    data = json.loads(ov_path.read_text())
    assert data["overrides"]["E01"]["low"] == proposal["proposed_low"]
    assert data["history"][0]["estimate_id"] == eid

    # drift loops to on-norm + the engine now prefers the override card
    card2 = client.get("/estimates/%d/actuals/drift" % eid).json()["cards"][0]
    assert card2["company_override"] is True
    dr = actuals_mod.drift_report(client.get(
        "/estimates/%d/actuals" % eid).json()["rows"], load_labor_catalog())
    # after the refit the ratios are measured against the NEW card:
    # row 1 lands exactly on-norm (1.0), row 2 was 0.75x the new card
    assert dr["cards"][0]["ratio_weighted_median"] == pytest.approx(1.0)
    assert dr["cards"][0]["drift_pct"] == pytest.approx(-8.3333, abs=1e-3)
    assert dr["cards"][0]["quality_flag"] == "tight"
    est2 = client.get("/estimates/%d" % eid).json()["estimate"]
    # the STORED recap is history (pre-override); a fresh compute prefers it
    fresh = compute_estimate(est2["definition"])
    assert fresh["company_override_catalog_ids"] == ["E01"]
    line2 = next(l for l in fresh["lines"] if l["catalog_id"] == "E01")
    assert line2["base_minutes"]["mid"] == pytest.approx(288.0)
    audit = [e["action"] for e in
             client.get("/estimates/%d/audit" % eid).json()["audit"]]
    assert {"actuals.imported", "refit.proposed", "refit.applied"} <= set(audit)


def test_actuals_apply_refit_writes_provenance_and_engine_prefers(
        client, tmp_path, monkeypatch):
    est = _make_est(client, {"name": "ov flow", "locations": [
        {"id": "f1", "name": "n",
         "lines": [{"catalog_id": "E01", "qty": 100}]}]})
    ov_path = tmp_path / "company_labor_overrides.json"
    monkeypatch.setenv("ROMTOOL_COMPANY_OVERRIDES", str(ov_path))
    if ov_path.exists():
        ov_path.unlink()
    client.post("/estimates/%d/actuals" % est["id"], json={"rows": [
        _E01_actual(100, 1.2, "2026-11-01"), _E01_actual(50, 1.2, "2026-11-02")]})
    r = client.post("/estimates/%d/actuals/apply-refit" % est["id"],
                    json={"catalog_id": "E01", "low": 2.16, "high": 3.6,
                          "note": "hand-approved"})
    assert r.status_code == 200
    est2 = client.get("/estimates/%d" % est["id"]).json()["estimate"]
    line = next(l for l in est2["recap"]["lines"]
                if l["catalog_id"] == "E01")
    # the STORED recap predates the override; a fresh compute prefers it
    recap = compute_estimate(est2["definition"])
    line2 = next(l for l in recap["lines"] if l["catalog_id"] == "E01")
    assert line2["base_minutes"]["mid"] == pytest.approx(288.0)
    assert line2["company_override"]["note"] == "hand-approved"
    assert recap["company_override_catalog_ids"] == ["E01"]


def test_actuals_validation_422(client):
    est = _make_est(client, {"name": "bad rows", "locations": [
        {"id": "f1", "name": "n",
         "lines": [{"catalog_id": "E01", "qty": 100}]}]})
    r = client.post("/estimates/%d/actuals" % est["id"], json={"rows": [
        {"catalog_id": "E01", "qty": 0, "hours": 1, "date": "2026-11-01"}]})
    assert r.status_code == 422
    r = client.post("/estimates/%d/actuals" % est["id"], json={"rows": "x"})
    assert r.status_code == 422
    r = client.post("/estimates/%d/actuals" % est["id"], json={"rows": [
        {"catalog_id": "E01", "qty": 1, "hours": 1, "date": "bad"}]})
    assert r.status_code == 422
    assert any("date" in m for m in r.json()["detail"]["errors"])
    r = client.post("/estimates/%d/actuals/apply-refit" % est["id"],
                    json={"catalog_id": "E01", "low": 1, "high": 0.5})
    assert r.status_code == 422


def test_actuals_unknown_estimate_404(client):
    r = client.get("/estimates/99999/actuals")
    assert r.status_code == 404
    r = client.get("/estimates/99999/sanity")
    assert r.status_code == 404
