"""Sanity gates (phase E4, v0.7.0).

Rule-driven pre-sign-off checks over a computed recap + its definition +
the quote register. Pure functions over recap/definition/config dicts --
no SQL; app/db.py owns storage, app/routes.py owns endpoint wiring.

Gates (thresholds and severities in config/sanity.json):

1. LABOR SHARE by venue archetype: share of wrapped task+foreman+super
   hours (mid band) per catalog zone letter prefix vs a per-archetype
   plausibility window. Zones without a band entry report informationally.

2. HOURS -> CREW-DAYS/WEEKS consistency: the recap's totals.crew_days /
   crew_weeks must equal a ceil recomputation from the stored lines (same
   basis as the engine: location minutes lines only, estimate-level wrap
   lines excluded) and crew plan (HARD finding when the math disagrees -- a doctored or
   broken-engine recap must not reach approval), plus a plausibility
   warning when implied hours per crew-week exceed a sustainable load.

3. E-DENSITY: share of labor lines whose unit card is provenance E
   (estimated/placeholder) -> warning pointing at the assumption register.

4. UNIT-COST OUTLIERS: per line, wrapped labor $ mid / qty vs the norm-card
   unit cost (minutes mid / 60 x (tech_rate + (N01 ratio + N02 pct) x
   foreman_rate)). With the default crew plan the ratio equals the line's
   stacked factor, so rope x exterior x night stacks flag for verification;
   K-adder rows exclude the access factor and do not self-flag.

5. QUOTE EXPIRY: reuses romtool/governance.py quote date math (binding_until
   inclusive); every expiry warning surfaces, codes preserved.

6. MISSING SCOPE: config/checklists.json per venue archetype -- expected
   catalog zone prefixes (+ optional any-of catalog ids). Missing entries
   are HARD findings.

The estimate's venue archetype comes from definition.sanity.archetype (any
key of labor_share_by_archetype) or ?archetype= override; with no archetype
the labor-share and missing-scope gates report skipped and never block.
run_sanity() returns the report; a non-empty hard_findings list blocks the
approved transition (config/governance.json sanity_blocks_approval).
"""

import json
import math
import os

from romtool.estimate import (catalog_index, load_estimating_config,
                              load_labor_catalog)
from romtool.governance import load_governance_config, quote_warnings

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_MODULE_DIR)
DEFAULT_SANITY_PATH = os.path.join(_REPO_ROOT, "config", "sanity.json")
DEFAULT_CHECKLISTS_PATH = os.path.join(_REPO_ROOT, "config", "checklists.json")


class SanityConfigError(ValueError):
    """Malformed sanity/checklist config; .errors is a list."""


# ------------------------------------------------------------------ config

def load_sanity_config(path=None):
    p = path or os.environ.get("ROMTOOL_SANITY") or DEFAULT_SANITY_PATH
    with open(p, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    validate_sanity_config(cfg)
    return cfg


def load_checklists(path=None):
    p = path or os.environ.get("ROMTOOL_CHECKLISTS") or DEFAULT_CHECKLISTS_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_sanity_config(cfg):
    errors = []
    sev = cfg.get("severity") or {}
    for gate in ("labor_share", "crew_duration", "e_density",
                 "unit_cost_outlier", "quote_expiry", "missing_scope"):
        if sev.get(gate) not in ("hard", "warning"):
            errors.append("severity.%s must be 'hard' or 'warning'" % gate)
    bands_by_arch = cfg.get("labor_share_by_archetype") or {}
    if not bands_by_arch:
        errors.append("labor_share_by_archetype missing")
    for arch, spec in bands_by_arch.items():
        bands = (spec or {}).get("bands") or {}
        if not bands:
            errors.append("labor_share_by_archetype.%s.bands missing" % arch)
        for z, b in bands.items():
            lo, hi = float(b.get("low", -1)), float(b.get("high", -1))
            if not (0.0 <= lo <= hi <= 1.0):
                errors.append("labor_share_by_archetype.%s band %r invalid"
                              " (need 0 <= low <= high <= 1)" % (arch, z))
    if not (0.0 < float(cfg.get("e_density_warn_pct", 0)) <= 100.0):
        errors.append("e_density_warn_pct must be in (0, 100]")
    uco = cfg.get("unit_cost_outlier") or {}
    if float(uco.get("ratio_low", -1)) <= 0 or float(uco.get("ratio_high", 0)) <= 1:
        errors.append("unit_cost_outlier needs ratio_low > 0 and ratio_high > 1")
    crew = cfg.get("crew") or {}
    if float(crew.get("max_plausible_hours_per_crew_week", 0)) <= 0:
        errors.append("crew.max_plausible_hours_per_crew_week must be > 0")
    if errors:
        raise SanityConfigError(errors)


def archetype_of(definition, override=None):
    """Effective archetype: ?archetype= override wins, then definition
    sanity.archetype, else None (share/missing-scope gates skip)."""
    if override:
        return str(override).strip().lower()
    d = definition or {}
    return (d.get("sanity") or {}).get("archetype") or None


def _zone_prefix(zone):
    return str(zone or "").split("-")[0].upper()


# ------------------------------------------------------------------- gates

def labor_share_gate(recap, archetype, sanity):
    """Zone-share windows for the archetype; unbanded zones informational."""
    spec = sanity["labor_share_by_archetype"].get(archetype)
    if spec is None:
        return None, "archetype %r has no labor_share bands" % archetype
    bands = spec["bands"]
    labor_lines = [l for l in recap.get("lines", []) if l.get("kind") == "minutes"]
    total = sum(l["hours"]["mid"] for l in labor_lines)
    by_zone = {}
    for l in labor_lines:
        by_zone.setdefault(_zone_prefix(l.get("zone")), []).append(l)
    entries, findings = [], []
    for z in sorted(by_zone):
        hours = sum(l["hours"]["mid"] for l in by_zone[z])
        share = hours / total if total > 0 else 0.0
        band = bands.get(z)
        entry = {"zone": z, "zone_name": by_zone[z][0].get("zone"),
                 "hours_mid": round(hours, 6), "share": round(share, 6),
                 "n_lines": len(by_zone[z])}
        if band:
            entry["band_low"], entry["band_high"] = band["low"], band["high"]
            if share < band["low"] - 1e-9 or share > band["high"] + 1e-9:
                entry["status"] = "out_of_band"
                findings.append({
                    "gate": "labor_share", "code": "LABOR_SHARE_OUT_OF_BAND",
                    "zone": z,
                    "message": "labor share %0.1f%% in zone %s is outside the"
                               " %s window %.0f%%-%.0f%% (%0.2f wrapped h of"
                               " %0.2f h): verify the takeoff or re-band the"
                               " archetype before sign-off"
                               % (share * 100.0, z, archetype,
                                  band["low"] * 100.0, band["high"] * 100.0,
                                  hours, total)})
            else:
                entry["status"] = "ok"
        else:
            entry["status"] = "unbanded"
        entries.append(entry)
    missing = sorted(set(bands) - set(by_zone))
    return {"zones": entries, "total_hours_mid": round(total, 6),
            "banded_zones_missing_from_estimate": missing}, findings


def crew_duration_gate(recap, sanity):
    """Recompute crew-days/weeks from the stored lines; hard finding on
    mismatch + plausibility warning on implied weekly load."""
    crew = recap.get("crew") or {}
    # direct-hours basis must match the engine: the engine's crew_days derives
    # from LOCATION lines only (minutes-kind rows that carry a location) and
    # EXCLUDES the derived estimate-level wrap lines (M05 travel, N03
    # orientation, N04 cleanup per crew-day, no location_id), which themselves
    # are sized off crew_days. Counting them here double-counted and produced
    # false CREW_MATH_INCONSISTENT hard findings on large estimates.
    lines = recap.get("lines", [])
    direct = [l for l in lines
              if l.get("kind") == "minutes" and l.get("location_id") is not None]
    hours_all = {b: round(sum(l["hours"][b] for l in lines), 6)
                 for b in ("low", "mid", "high")}
    hours_direct = {b: round(sum(l["hours"][b] for l in direct), 6)
                    for b in ("low", "mid", "high")}
    cap_day = float(crew.get("capacity_per_crew_day_h") or 0.0)
    cap_week = float(crew.get("capacity_per_crew_week_h") or 0.0)
    exp_days = int(math.ceil(hours_direct["mid"] / cap_day)) if cap_day > 0 else 0
    exp_weeks = int(math.ceil(hours_all["mid"] / cap_week)) if cap_week > 0 else 0
    got_days = recap.get("totals", {}).get("crew_days")
    got_weeks = recap.get("totals", {}).get("crew_weeks")
    findings = []
    ok = True
    if got_days != exp_days or got_weeks != exp_weeks:
        ok = False
        findings.append({
            "gate": "crew_duration", "code": "CREW_MATH_INCONSISTENT",
            "message": "crew math inconsistent: totals say crew_days=%s"
                       " crew_weeks=%s but the lines+crew plan recompute to"
                       " crew_days=%d (ceil direct %0.2f h / %0.2f h crew-day"
                       " capacity) and crew_weeks=%d (ceil %0.2f h / %0.2f h"
                       " crew-week capacity) - do not approve until the recap"
                       " is recomputed"
                       % (got_days, got_weeks, exp_days, hours_direct["mid"],
                          cap_day, exp_weeks, hours_all["mid"], cap_week)})
    hpw = recap.get("totals", {}).get("hours_per_crew_week") or 0.0
    max_hpw = float(sanity["crew"]["max_plausible_hours_per_crew_week"])
    if exp_weeks > 0 and hpw > max_hpw + 1e-9:
        findings.append({
            "gate": "crew_duration", "code": "CREW_WEEK_PLAUSIBILITY",
            "message": "implied average load %0.1f h/crew-week exceeds the"
                       " plausible sustained maximum %.0f h - add crews or"
                       " re-plan the schedule" % (hpw, max_hpw)})
    return {"crew_days_expected": exp_days, "crew_days_reported": got_days,
            "crew_weeks_expected": exp_weeks, "crew_weeks_reported": got_weeks,
            "hours_all_lines_mid": hours_all["mid"],
            "hours_direct_mid": hours_direct["mid"],
            "capacity_per_crew_day_h": cap_day,
            "capacity_per_crew_week_h": cap_week,
            "hours_per_crew_week": hpw,
            "max_plausible_hours_per_crew_week": max_hpw,
            "consistent": ok}, findings


def e_density_gate(recap, sanity):
    """Share of labor lines on E-grade (estimated) unit cards."""
    labor_lines = [l for l in recap.get("lines", []) if l.get("kind") == "minutes"]
    counts = {}
    for l in labor_lines:
        q = l.get("source_quality")
        counts[q] = counts.get(q, 0) + 1
    n = len(labor_lines)
    n_e = counts.get("E", 0)
    pct = n_e / n * 100.0 if n else 0.0
    threshold = float(sanity["e_density_warn_pct"])
    findings = []
    if n and pct > threshold + 1e-9:
        findings.append({
            "gate": "e_density", "code": "E_DENSITY_HIGH",
            "message": "%d of %d labor lines (%0.1f%%) carry E-grade"
                       " (estimated/placeholder) unit cards, above the %.0f%%"
                       " threshold: work the assumption register (open rows"
                       " map 1:1 to these lines) before pricing sign-off"
                       % (n_e, n, pct, threshold)})
    return {"labor_lines": n, "by_quality": counts,
            "e_lines": n_e, "e_pct": round(pct, 4),
            "threshold_pct": threshold}, findings


def unit_cost_outlier_gate(recap, sanity, catalog, estimating):
    """Per-line unit cost vs the norm-card unit cost at the estimate's crew plan."""
    uco = sanity["unit_cost_outlier"]
    idx = catalog_index(catalog)["by_id"]
    crew = recap.get("crew") or {}
    rate_card = recap["cost"]["rate_card"]
    tech_rate = float(rate_card["tech_rate"])
    fore_rate = float(rate_card["foreman_rate"])
    n01 = float(crew.get("n01_foreman_ratio") or 0.0)
    n02 = float(crew.get("n02_supervision_pct") or 0.0)
    cost_per_task_hour = tech_rate + (n01 + n02) * fore_rate
    findings, checked = [], 0
    for l in recap.get("lines", []):
        if l.get("kind") != "minutes":
            continue
        qty = float(l.get("qty") or 0.0)
        row = idx.get(l.get("catalog_id"))
        if row is None or qty <= 0 or not row.get("minutes_high"):
            continue
        checked += 1
        mid_min = (float(row["minutes_low"]) + float(row["minutes_high"])) / 2.0
        norm_unit = mid_min / 60.0 * cost_per_task_hour
        unit = float(l["labor_usd"]["total"]["mid"]) / qty
        ratio = unit / norm_unit if norm_unit > 0 else 0.0
        if ratio < float(uco["ratio_low"]) - 1e-9:
            findings.append({
                "gate": "unit_cost_outlier", "code": "UNIT_COST_OUTLIER",
                "line_id": l.get("line_id"), "catalog_id": l.get("catalog_id"),
                "side": "under",
                "ratio": round(ratio, 6),
                "message": "unit cost $%0.4f/%s is %0.2fx the %s norm-card"
                           " unit cost $%0.4f (threshold low %.2f) - line is"
                           " carried BELOW the card: check qty/derivation"
                           % (unit, l.get("unit"), ratio, l.get("catalog_id"),
                              norm_unit, float(uco["ratio_low"]))})
        elif ratio > float(uco["ratio_high"]) + 1e-9:
            findings.append({
                "gate": "unit_cost_outlier", "code": "UNIT_COST_OUTLIER",
                "line_id": l.get("line_id"), "catalog_id": l.get("catalog_id"),
                "side": "over",
                "ratio": round(ratio, 6),
                "message": "unit cost $%0.4f/%s is %0.2fx the %s norm-card"
                           " unit cost $%0.4f (threshold high %.2f) - stacked"
                           " factors (access x condition x schedule) push this"
                           " past the card: verify the stack is intentional"
                           % (unit, l.get("unit"), ratio, l.get("catalog_id"),
                              norm_unit, float(uco["ratio_high"]))})
    return {"checked_lines": checked, "cost_per_task_hour_basis":
            round(cost_per_task_hour, 6),
            "ratio_low": float(uco["ratio_low"]),
            "ratio_high": float(uco["ratio_high"]),
            "n01_foreman_ratio": n01, "n02_supervision_pct": n02,
            "basis": "line wrapped labor $ mid / qty vs norm mid minutes/60 x"
                     " (tech + (N01+N02) x foreman rate)"}, findings


def missing_scope_gate(definition, recap, archetype, checklists):
    """Archetype checklist vs the estimate's zone/catalog presence."""
    entry = (checklists.get("archetypes") or {}).get(archetype)
    if entry is None:
        return None, "archetype %r has no checklist" % archetype
    labor_lines = [l for l in recap.get("lines", []) if l.get("kind") == "minutes"]
    zone_prefixes = {_zone_prefix(l.get("zone")) for l in labor_lines}
    catalog_ids = {l.get("catalog_id") for l in labor_lines}
    present, missing = [], []
    for item in entry.get("zones") or []:
        z = str(item.get("zone_prefix") or "").upper()
        want_ids = set(item.get("catalog_ids") or [])
        hit = z in zone_prefixes and (not want_ids or bool(want_ids & catalog_ids))
        rec = {"zone_prefix": z, "caption": item.get("caption"),
               "required_catalog_ids": sorted(want_ids) or None,
               "status": "present" if hit else "missing"}
        (present if hit else missing).append(rec)
    findings = [{
        "gate": "missing_scope", "code": "MISSING_SCOPE",
        "zone": m["zone_prefix"], "caption": m["caption"],
        "message": "%s checklist expects %s zone scope - %s - but this"
                   " estimate carries no %s-zone labor lines: confirm it is"
                   " genuinely out of scope or add the takeoff before"
                   " approval" % (archetype, m["zone_prefix"],
                                  m["caption"] or "expected scope",
                                  m["zone_prefix"])}
        for m in missing]
    return {"archetype": archetype, "label": entry.get("label"),
            "present": present, "missing": missing}, findings


def quote_expiry_gate(quotes, today, gov=None, sanity=None):
    """Governance quote date math surfaced as sanity warnings."""
    gov = gov or load_governance_config()
    warns = quote_warnings(quotes or [], today, gov)
    findings = [{**w, "gate": "quote_expiry"} for w in warns]
    summary = {"total": len(quotes or []), "warn_codes": sorted({
        w["code"] for w in warns})}
    return summary, findings


# ----------------------------------------------------------------- run

GATES = ("labor_share", "crew_duration", "e_density", "unit_cost_outlier",
         "quote_expiry", "missing_scope")


def run_sanity(recap, definition, quotes=None, archetype=None, today=None,
               catalog=None, estimating=None, sanity=None, checklists=None,
               gov=None):
    """Full sanity report. Raises SanityConfigError on bad config. Blocks
    approval = hard_findings non-empty."""
    sanity = sanity or load_sanity_config()
    checklists = checklists or load_checklists()
    gov = gov or load_governance_config()
    catalog = catalog if catalog is not None else load_labor_catalog()
    estimating = estimating or load_estimating_config()
    effective = archetype_of(definition, archetype)
    findings = []

    share_block, share_note = None, None
    if effective and effective in sanity["labor_share_by_archetype"]:
        share_block, f = labor_share_gate(recap, effective, sanity)
        findings.extend(f)
    else:
        share_note = ("skipped: no archetype" if not effective else
                      "skipped: archetype %r unknown (known: %s)"
                      % (effective, sorted(sanity["labor_share_by_archetype"])))

    crew_block, f = crew_duration_gate(recap, sanity)
    findings.extend(f)

    e_block, f = e_density_gate(recap, sanity)
    findings.extend(f)

    outlier_block, f = unit_cost_outlier_gate(recap, sanity, catalog, estimating)
    findings.extend(f)

    quote_block, f = quote_expiry_gate(quotes, today, gov, sanity)
    findings.extend(f)

    scope_block, scope_note = None, None
    if effective:
        scope_block, f = missing_scope_gate(definition, recap, effective,
                                            checklists)
        if scope_block is not None:
            findings.extend(f)
        else:
            scope_note = "skipped: archetype %r unknown (checklist data)"
    else:
        scope_note = "skipped: no archetype"

    severity = sanity["severity"]
    hard, warn = [], []
    for f in findings:
        (hard if severity.get(f["gate"], "warning") == "hard" else warn).append(f)
    hard.sort(key=lambda x: (x["gate"], x["code"], str(x.get("zone"))))

    return {
        "archetype": effective,
        "archetype_source": ("query_override" if archetype else
                             "definition" if effective else "none"),
        "hard_findings": hard,
        "warnings": warn,
        "gates": {
            "labor_share": {"status": ("skipped" if share_block is None else
                                       ("finding" if any(f["gate"] == "labor_share" for f in findings) else "ok")),
                            "note": share_note, **(share_block or {})},
            "crew_duration": {"status": "finding" if any(f["gate"] == "crew_duration" for f in findings) else "ok",
                              **crew_block},
            "e_density": {"status": "finding" if any(f["gate"] == "e_density" for f in findings) else "ok",
                          **e_block},
            "unit_cost_outlier": {"status": "finding" if any(f["gate"] == "unit_cost_outlier" for f in findings) else "ok",
                                  **outlier_block},
            "quote_expiry": {"status": "finding" if any(f["gate"] == "quote_expiry" for f in findings) else "ok",
                             **quote_block},
            "missing_scope": {"status": ("skipped" if scope_block is None else
                                         ("finding" if any(f["gate"] == "missing_scope" for f in findings) else "ok")),
                              "note": scope_note, **(scope_block or {})},
        },
        "counts": {"hard": len(hard), "warnings": len(warn)},
        "blocks_approval": bool(hard),
        "severity_policy": severity,
        "config_version": sanity.get("version"),
    }


# ------------------------------------------------------- recap export rows

def sanity_export_rows(report):
    """Recap CSV/XLSX rows for the sanity section (module shared with routes)."""
    rows = [["sanity_summary", "archetype=%s" % (report["archetype"] or "none"),
             "hard=%d" % report["counts"]["hard"],
             "warnings=%d" % report["counts"]["warnings"],
             "blocks_approval=%s" % ("yes" if report["blocks_approval"] else "no")]]
    for f in report["hard_findings"]:
        rows.append(["sanity_hard", f["code"], f.get("zone") or "", f["message"]])
    for f in report["warnings"]:
        rows.append(["sanity_warning", f["code"], f.get("zone") or "", f["message"]])
    if not report["hard_findings"] and not report["warnings"]:
        rows.append(["sanity_ok", "no findings", "", "all gates clear"])
    return rows
