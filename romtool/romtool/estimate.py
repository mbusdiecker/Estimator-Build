"""Class 1 labor estimate engine (phase 4).

Turns an estimate definition (locations x catalog lines x access methods x
condition flags x schedule flags x travel/downtime/crew plan) into a full
recap: per-line minutes with the factor stack visible, zone rollups,
crew-hours, crew-week stats and the $ rollup (labor -> downtime carry ->
PM -> OH -> profit -> line-risk contingency -> total). Recap is stored in
SQLite alongside the definition (like projects) and exports to CSV/XLSX.

Every constant -- factor values, ratios, rate card, contingency bands --
lives in config/estimating.json; the catalog lives in config/labor_units.json
(generated from research/das-build-unit-catalog-v2.csv by
scripts/build_labor_units.py). Nothing numeric is hard-coded here.

Factor stacking order per line (multiplicative, recap shows every stage):

  base minutes (qty x unit minutes, low/mid/high, mid = (low+high)/2)
  x condition factors     M03 occupied / M04 exterior / M07 constrained
  x access factor         K03 lift/scissor / K05 rope / K06 ladder (ground=1)
  x schedule premium      M02 night / M02 weekend
  = task hours ("direct task hours", stage 4)
  + N01 foreman hours     task x (foremen/techs) x productive haircut
  + N02 super hours       task x supervision pct (mid of 8-15%)
  = line hours ("wrapped hours", billed labor)
  line $                  task @ tech_rate + foreman/super @ foreman_rate
  + line-risk contingency N05 (labor $ x pct by source_quality P/E/G)

$ chain order (v0.6.0 commercial wrap, phase E3 -- every step is applied in
exactly this documented order, mirrored in config/commercial.json and
/how-it-works section 11):

  1. DIRECT LABOR + SUPERVISION (wrapped line labor, incl. N01/N02)
  2. DOWNTIME        M01 idle crew-day carry (zero hours, schedule risk)
  3. SMALL TOOLS     pct x (labor + downtime)            commercial.json
  4. ESCALATION      (factor - 1) x (labor + downtime);  commercial.json
     factor = 1 + days(base_date -> anchor)/365 x annual_rate_pct/100,
     anchor = definition commercial.escalation.anchor_date, else the
     midpoint working day of the with-crews schedule (start_date), else
     the step is skipped with a warning (never de-escalates)
  5. TAXES           rate x taxable wrapped labor by line type rules
                                                           commercial.json
  6. INSURANCE/BOND  pct x running subtotal through taxes commercial.json
  7. PM (I05)        pm_pct x running subtotal through insurance
  8. OVERHEAD        overhead_pct x running subtotal through PM
  9. PROFIT          profit_pct x running subtotal through OH (or the
     commercial profit tier when the definition picks one)
 10. CONTINGENCY N05 per line by source quality on labor $, never marked
     up -> GRAND TOTAL = subtotal + contingency

Documented deviations (also on /how-it-works):
  - K-Access-ADDERS minute rows never take the access factor (they are the
    access adders); condition and schedule factors still apply.
  - Derived estimate-level logistics/wrap lines (M05 travel, N03 orientation,
    N04 cleanup) carry base minutes with no stacking; their quantities are
    computed from the crew plan (crew-days from mid-band direct hours).
  - Downtime windows (M01 rows) carry idle crew-day COST, zero hours:
    crew_size x hours_per_day x blended rate, per blacked-out day.
  - Location-level power_plant derived lines (O01/O02/O04 + optional O05/O06)
    stack like any other location line; their QUANTITIES are auto-sized
    (Ah = runtime_hours x load_amps; strings = ceil(Ah / string_Ah)).
"""

import copy
import csv
import io
import json
import math
import os

from romtool.commercial import (CommercialError, WRAP_ORDER,
                                escalation_meta, load_commercial_config,
                                parse_iso_date, schedule_midpoint_date,
                                wrap_steps)

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_MODULE_DIR)
DEFAULT_CATALOG_PATH = os.path.join(_REPO_ROOT, "config", "labor_units.json")
DEFAULT_ESTIMATING_PATH = os.path.join(_REPO_ROOT, "config", "estimating.json")

ACCESS_METHODS = ("ground", "ladder", "lift", "scissor", "rope")
CONDITION_FLAGS = ("occupied", "exterior", "constrained_access")
SCHEDULE_FLAGS = ("night", "weekend")
QUALITIES = ("P", "E", "G")

# estimate-level pseudo-location for derived logistics/wrap lines
GLOBAL_LOCATION_ID = "(estimate-wide)"


class EstimateError(ValueError):
    """Definition rejected; .errors is a list of messages (API maps to 422)."""

    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# ---------------------------------------------------------------- loaders

def _env(path_var, default):
    return os.environ.get(path_var) or default


def load_labor_catalog(path=None):
    """Load the labor-unit store (config/labor_units.json) as a fresh dict,
    with the company override layer (config/company_labor_overrides.json,
    phase E4 actuals loop) applied on top: overridden rows carry the
    override low/high minutes plus a company_override provenance block.
    The engine prefers the override card."""
    p = path or _env("ROMTOOL_LABOR_UNITS", DEFAULT_CATALOG_PATH)
    with open(p, "r", encoding="utf-8") as fh:
        catalog = json.load(fh)
    overrides = load_company_labor_overrides()
    if overrides:
        catalog = apply_company_overrides(catalog, overrides)
    return catalog


DEFAULT_COMPANY_OVERRIDES_PATH = os.path.join(
    _REPO_ROOT, "config", "company_labor_overrides.json")


def load_company_labor_overrides(path=None):
    """Company labor-norm override layer, or {} when absent/empty."""
    p = path or _env("ROMTOOL_COMPANY_OVERRIDES", DEFAULT_COMPANY_OVERRIDES_PATH)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except (ValueError, OSError):
        return {}
    return data.get("overrides") or {}


def apply_company_overrides(catalog, overrides):
    """Apply {catalog_id: {low, high, ...}} to minutes-kind rows in place
    semantics (deep copy): sets minutes_low/high and stamps provenance.
    Unknown ids or non-numeric/invalid bounds are skipped (they cannot
    happen through the apply-refit validator, but a hand-edited file must
    not crash every compute)."""
    out = copy.deepcopy(catalog)
    seen = {u["id"]: u for u in out.get("units", [])}
    for cid, ov in (overrides or {}).items():
        row = seen.get(cid)
        if row is None or row.get("kind") != "minutes":
            continue
        try:
            low = float(ov.get("low"))
            high = float(ov.get("high"))
        except (TypeError, ValueError):
            continue
        if low <= 0 or high < low:
            continue
        prior = {"low": row.get("minutes_low"), "high": row.get("minutes_high")}
        row["minutes_low"], row["minutes_high"] = low, high
        row["company_override"] = {
            "prior_minutes": prior, "low": low, "high": high,
            "mid": round((low + high) / 2.0, 6),
            "note": ov.get("note"), "updated_at": ov.get("updated_at"),
            "source": ov.get("source"),
        }
    return out


def load_estimating_config(path=None):
    """Load the estimating constants (config/estimating.json) as a fresh dict."""
    p = path or _env("ROMTOOL_ESTIMATING", DEFAULT_ESTIMATING_PATH)
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def catalog_index(catalog):
    """Rows keyed by id + zone order; raises on duplicates."""
    by_id, zones = {}, []
    for row in catalog.get("units", []):
        rid = row["id"]
        if rid in by_id:
            raise EstimateError("duplicate catalog row id '%s'" % rid)
        by_id[rid] = row
        if row["zone"] not in zones:
            zones.append(row["zone"])
    return {"by_id": by_id, "zones": zones}


def _mid(low, high):
    return (low + high) / 2.0


def _round1(v):
    return round(v, 9)


# ------------------------------------------------------------- validation

def _err_num(value, label, minimum=None, maximum=None):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise EstimateError("%s must be a number, got %r" % (label, value))
    if minimum is not None and v < minimum:
        raise EstimateError("%s must be >= %s, got %r" % (label, minimum, value))
    if maximum is not None and v > maximum:
        raise EstimateError("%s must be <= %s, got %r" % (label, maximum, value))
    return v


def _err_int(value, label, minimum=0):
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise EstimateError("%s must be an integer, got %r" % (label, value))
    if isinstance(value, float) and float(value) != v:
        raise EstimateError("%s must be an integer, got %r" % (label, value))
    if v < minimum:
        raise EstimateError("%s must be >= %d, got %d" % (label, minimum, v))
    return v


COMMERCIAL_KEYS = ("profit_risk_tier", "escalation", "tax")


def _validate_commercial_block_shape(c):
    """Definition commercial block shape (the config values behind it are
    checked against commercial.json in compute_estimate)."""
    errors = []
    for k in c:
        if k not in COMMERCIAL_KEYS:
            errors.append("commercial.%r unknown (use %s)"
                          % (k, list(COMMERCIAL_KEYS)))
    if "profit_risk_tier" in c and not isinstance(c["profit_risk_tier"], str):
        errors.append("commercial.profit_risk_tier must be a string (a key of"
                      " commercial.json profit_pct_by_risk_tier)")
    esc = c.get("escalation")
    if esc is not None:
        if not isinstance(esc, dict):
            errors.append("commercial.escalation must be an object")
        else:
            for k in esc:
                if k not in ("index", "anchor_date"):
                    errors.append("commercial.escalation.%r unknown (use "
                                  "index, anchor_date)" % k)
            if not isinstance(esc.get("index", ""), str):
                errors.append("commercial.escalation.index must be a string")
            if esc.get("anchor_date"):
                try:
                    parse_iso_date(esc["anchor_date"],
                                   "commercial.escalation.anchor_date")
                except CommercialError as exc:
                    errors.append(str(exc))
    tax = c.get("tax")
    if tax is not None:
        if not isinstance(tax, dict):
            errors.append("commercial.tax must be an object")
        else:
            for k in tax:
                if k not in ("rate_pct",):
                    errors.append("commercial.tax.%r unknown (use rate_pct)" % k)
            if "rate_pct" in tax:
                try:
                    _err_num(tax["rate_pct"], "commercial.tax.rate_pct", 0.0)
                except EstimateError as exc:
                    errors.append(str(exc))
    return errors


def _validate_definition(d):
    errors = []
    if not isinstance(d, dict):
        raise EstimateError("estimate definition must be a JSON object")
    locs = d.get("locations")
    if not isinstance(locs, list) or not locs:
        raise EstimateError("definition needs a non-empty locations[] array")
    seen_ids = set()
    for i, loc in enumerate(locs):
        if not isinstance(loc, dict):
            errors.append("locations[%d] must be an object" % i)
            continue
        lid = loc.get("id")
        if not isinstance(lid, str) or not lid.strip():
            errors.append("locations[%d].id must be a non-empty string" % i)
        elif lid in seen_ids:
            errors.append("duplicate location id %r" % lid)
        else:
            seen_ids.add(lid)
        access = loc.get("access", "ground")
        if access not in ACCESS_METHODS:
            errors.append("locations[%d] access %r not in %s"
                          % (i, access, list(ACCESS_METHODS)))
        conds = loc.get("conditions", [])
        if not isinstance(conds, list):
            errors.append("locations[%d].conditions must be a list" % i)
        else:
            for c in conds:
                if c not in CONDITION_FLAGS:
                    errors.append("locations[%d] condition %r not in %s"
                                  % (i, c, list(CONDITION_FLAGS)))
        for j, ln in enumerate(loc.get("lines") or []):
            if not isinstance(ln, dict):
                errors.append("locations[%d].lines[%d] must be an object" % (i, j))
                continue
            if not (ln.get("catalog_id") or "").strip():
                errors.append("locations[%d].lines[%d].catalog_id required" % (i, j))
            try:
                _err_num(ln.get("qty"), "locations[%d].lines[%d].qty" % (i, j), 0.000001)
            except EstimateError as exc:
                errors.append(str(exc))
        pp = loc.get("power_plant")
        if pp is not None:
            if not isinstance(pp, dict):
                errors.append("locations[%d].power_plant must be an object" % i)
            else:
                try:
                    _err_num(pp.get("runtime_hours", 12.0),
                             "locations[%d].power_plant.runtime_hours" % i, 0.0)
                    _err_num(pp.get("load_amps"),
                             "locations[%d].power_plant.load_amps" % i, 0.000001)
                except EstimateError as exc:
                    errors.append(str(exc))
                if "rectifier_modules" in pp and pp.get("rectifier_modules") is not None:
                    try:
                        _err_int(pp["rectifier_modules"],
                                 "locations[%d].power_plant.rectifier_modules" % i, 0)
                    except EstimateError as exc:
                        errors.append(str(exc))
    moves = d.get("travel_moves")
    if moves is not None and not isinstance(moves, list):
        errors.append("travel_moves must be a list")
    else:
        for i, mv in enumerate(moves or []):
            if not isinstance(mv, dict):
                errors.append("travel_moves[%d] must be an object" % i)
                continue
            for k in ("from", "to"):
                if mv.get(k) and mv[k] not in seen_ids:
                    errors.append("travel_moves[%d].%s %r is not a location id"
                                  % (i, k, mv[k]))
            try:
                _err_int(mv.get("moves", 1), "travel_moves[%d].moves" % i, 1)
            except EstimateError as exc:
                errors.append(str(exc))
    windows = d.get("downtime_windows")
    if windows is not None and not isinstance(windows, list):
        errors.append("downtime_windows must be a list")
    else:
        for i, w in enumerate(windows or []):
            if not isinstance(w, dict):
                errors.append("downtime_windows[%d] must be an object" % i)
                continue
            try:
                _err_int(w.get("days"), "downtime_windows[%d].days" % i, 1)
            except EstimateError as exc:
                errors.append(str(exc))
            if w.get("start"):
                try:
                    parse_iso_date(w["start"], "downtime_windows[%d].start" % i)
                except CommercialError as exc:
                    errors.append(str(exc))
    if d.get("start_date"):
        try:
            parse_iso_date(d["start_date"], "start_date")
        except CommercialError as exc:
            errors.append(str(exc))
    if d.get("commercial") is not None:
        if not isinstance(d["commercial"], dict):
            errors.append("commercial must be an object")
        else:
            errors.extend(_validate_commercial_block_shape(d["commercial"]))
    alts = d.get("alternates")
    if alts is not None and not isinstance(alts, list):
        errors.append("alternates must be a list")
    else:
        for i, alt in enumerate(alts or []):
            if not isinstance(alt, dict):
                errors.append("alternates[%d] must be an object" % i)
                continue
            if not (alt.get("name") or "").strip():
                errors.append("alternates[%d].name must be a non-empty string" % i)
            kind = alt.get("kind", "add")
            if kind not in ("add", "deduct"):
                errors.append("alternates[%d].kind %r not in ('add', 'deduct')"
                              % (i, kind))
            locs = alt.get("locations")
            if locs is not None:
                if not isinstance(locs, list):
                    errors.append("alternates[%d].locations must be a list" % i)
                else:
                    for lid in locs:
                        if lid not in seen_ids:
                            errors.append("alternates[%d] references unknown"
                                          " location %r" % (i, lid))
            specs = alt.get("lines")
            if specs is not None and not isinstance(specs, list):
                errors.append("alternates[%d].lines must be a list" % i)
                continue
            spec_list = specs or []
            if locs is None and not spec_list:
                errors.append("alternates[%d] needs locations and/or lines" % i)
            for j, spec in enumerate(spec_list):
                if not isinstance(spec, dict):
                    errors.append("alternates[%d].lines[%d] must be an object"
                                  % (i, j))
                    continue
                if spec.get("location_id") not in seen_ids:
                    errors.append("alternates[%d].lines[%d].location_id %r is"
                                  " not a location id"
                                  % (i, j, spec.get("location_id")))
                if not (spec.get("catalog_id") or "").strip():
                    errors.append("alternates[%d].lines[%d].catalog_id required"
                                  % (i, j))
                if spec.get("qty") is not None:
                    try:
                        _err_num(spec["qty"], "alternates[%d].lines[%d].qty"
                                 % (i, j), 0.000001)
                    except EstimateError as exc:
                        errors.append(str(exc))
    pts = d.get("antenna_points")
    if pts is not None and not isinstance(pts, dict):
        try:
            _err_num(pts, "antenna_points", 0.000001)
        except EstimateError as exc:
            errors.append(str(exc))
    elif isinstance(pts, dict):
        for lid, v in pts.items():
            if lid not in seen_ids:
                errors.append("antenna_points %r is not a location id" % lid)
            try:
                _err_num(v, "antenna_points[%r]" % lid, 0.000001)
            except EstimateError as exc:
                errors.append(str(exc))
    sched = d.get("schedule")
    if sched is not None:
        if not isinstance(sched, dict):
            errors.append("schedule must be an object")
        else:
            for k, v in sched.items():
                if k not in SCHEDULE_FLAGS:
                    errors.append("schedule.%r unknown (use %s)" % (k, list(SCHEDULE_FLAGS)))
                elif not isinstance(v, bool):
                    errors.append("schedule.%s must be a boolean" % k)
    crew = d.get("crew")
    if crew is not None and not isinstance(crew, dict):
        errors.append("crew must be an object")
    over = d.get("rate_overrides")
    if over is not None and not isinstance(over, dict):
        errors.append("rate_overrides must be an object")
    if errors:
        raise EstimateError(errors)


def _resolve_crew(d, est):
    defaults = est["crew_defaults"]
    c = d.get("crew") or {}
    out = {
        "crews": _err_int(c.get("crews", defaults["crews"]), "crew.crews", 1),
        "techs": _err_int(c.get("techs", defaults["techs"]), "crew.techs", 1),
        "foremen": _err_int(c.get("foremen", defaults["foremen"]), "crew.foremen", 0),
        "hours_per_day": _err_num(c.get("hours_per_day", defaults["hours_per_day"]),
                                  "crew.hours_per_day", 0.5),
        "days_per_week": _err_num(c.get("days_per_week", defaults["days_per_week"]),
                                  "crew.days_per_week", 1),
    }
    return out


def _apply_rate_overrides(est, overrides):
    out = copy.deepcopy(est)
    for key, val in (overrides or {}).items():
        if key == "rate_overrides":
            continue
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **val}
        else:
            out[key] = val
    return out


# ------------------------------------------------------------- factor stack

def _condition_stack(conditions, est):
    """Ordered condition factor entries for one location."""
    out = []
    for flag in CONDITION_FLAGS:
        if flag in (conditions or []):
            spec = est["factors"][flag]
            out.append({"key": flag, "row": spec["row"], "low": spec["low"],
                        "high": spec["high"],
                        "value": round(_mid(spec["low"], spec["high"]), 6)})
    return out


def _schedule_stack(sched, est):
    out = []
    if sched.get("night"):
        spec = est["schedule_factors"]["night"]
        out.append({"key": "night", "row": spec["row"], "low": spec["low"],
                    "high": spec["high"],
                    "value": round(_mid(spec["low"], spec["high"]), 6)})
    if sched.get("weekend"):
        spec = est["schedule_factors"]["weekend"]
        out.append({"key": "weekend", "row": spec["row"], "low": spec["low"],
                    "high": spec["high"],
                    "value": round(_mid(spec["low"], spec["high"]), 6)})
    return out


def _access_entry(method, est):
    spec = est["access_factors"][method]
    return {"row": spec["row"], "low": spec["low"], "high": spec["high"],
            "value": round(_mid(spec["low"], spec["high"]), 6)}


def _line_key(loc, cid):
    """Stable per-line identity: "<location_id>:<catalog_id>", or the bare
    catalog id for estimate-level lines. Used for revisions, quotes
    (linked_line_ids) and the assumption register."""
    return cid if loc is None or not loc.get("id") else "%s:%s" % (loc["id"], cid)


def _dedupe_line_ids(lines):
    """Guarantee unique line_id within one recap (e.g. two M01 downtime
    windows -> "M01", "M01#2"). In-place, deterministic order."""
    seen = {}
    for ln in lines:
        k = ln["line_id"]
        n = seen.get(k, 0) + 1
        seen[k] = n
        if n > 1:
            ln["line_id"] = "%s#%d" % (k, n)


def _is_access_adder_row(row):
    """K-Access-ADDERS minute rows are the access adders themselves."""
    return row["zone"].upper().startswith("K-")


def _factor_multiplier(cond_stack, access, sched_stack, row):
    """Stage 4 multiplier (condition x access x schedule); skips the access
    factor on K-zone adder rows. Returns (mid_value, low_value, high_value,
    access_used_or_None)."""
    cond_mid = 1.0
    cond_low = cond_high = 1.0
    for f in cond_stack:
        cond_mid *= f["value"]
        cond_low *= f["low"]
        cond_high *= f["high"]
    sched_mid = 1.0
    sched_low = sched_high = 1.0
    for f in sched_stack:
        sched_mid *= f["value"]
        sched_low *= f["low"]
        sched_high *= f["high"]
    acc_low = acc_high = acc_mid = 1.0
    access_used = None
    if access and not _is_access_adder_row(row):
        access_used = access
        acc_mid, acc_low, acc_high = access["value"], access["low"], access["high"]
    return (cond_mid * acc_mid * sched_mid,
            cond_low * acc_low * sched_low,
            cond_high * acc_high * sched_high,
            access_used)


# ------------------------------------------------------------ line math

def _labor_for(task, est):
    """Billed labor for task hours: tech @ tech_rate, foreman/super @ foreman."""
    tech = est["rate_card"]["tech_rate"]
    fore = est["rate_card"]["foreman_rate"]
    return {
        "tech": task * tech,
        "foreman": task * fore,
        "super": task * fore,
    }


def _make_line(loc, row, qty, cond_stack, access, sched_stack, crew, est,
               derived=False, derivation=None, kind="minutes", no_stack=False):
    """Compute one recap line from catalog row + factors.

    Task hours stage through schedule premium; supervision (N01/N02) adds
    foreman/super hours; labor bills tech vs foreman rates accordingly.
    `no_stack` marks estimate-level derived logistics/wrap lines that carry
    base minutes with no factor stacking (power-plant derived lines stack
    like any other location line)."""
    ml, mh = row["minutes_low"], row["minutes_high"]
    low = ml * qty
    high = mh * qty
    base = {"low": low, "high": high, "mid": _mid(low, high)}

    # stage 4: condition x access x schedule
    mult_mid, mult_low, mult_high, access_used = _factor_multiplier(
        cond_stack, access, sched_stack, row)
    task = {"low": base["low"] * mult_low / 60.0,
            "high": base["high"] * mult_high / 60.0,
            "mid": base["mid"] * mult_mid / 60.0}

    # supervision adders (estimate-level wrap/logistics lines stay raw)
    if no_stack:
        n01_ratio = 0.0
        n02_pct = 0.0
    else:
        n01_ratio = (crew["foremen"] / crew["techs"]) * est["foreman_productive_haircut"]
        spec = est["supervision_pct"]
        n02_pct = round(_mid(spec["low"], spec["high"]), 6)
    foreman = {b: task[b] * n01_ratio for b in ("low", "mid", "high")}
    superh = {b: task[b] * n02_pct for b in ("low", "mid", "high")}
    hours = {b: task[b] + foreman[b] + superh[b] for b in ("low", "mid", "high")}

    # labor dollars
    rate_map = {"tech": est["rate_card"]["tech_rate"],
                "foreman": est["rate_card"]["foreman_rate"],
                "super": est["rate_card"]["foreman_rate"]}
    labor = {}
    for comp, rate in rate_map.items():
        src = {"tech": task, "foreman": foreman, "super": superh}[comp]
        labor[comp] = {b: src[b] * rate for b in ("low", "mid", "high")}
    labor["total"] = {b: sum(labor[c][b] for c in ("tech", "foreman", "super"))
                      for b in ("low", "mid", "high")}

    # line-risk contingency (N05) by source quality; quote/cost-carry rows none
    pct = 0.0
    if kind in ("minutes",):
        pct = float(est["contingency_pct_by_quality"].get(row.get("source_quality"), 0.0))
    contingency = {b: labor["total"][b] * pct / 100.0 for b in ("low", "mid", "high")}

    line = {
        "location_id": loc["id"] if loc else None,
        "location_name": loc["name"] if loc else GLOBAL_LOCATION_ID,
        "line_id": _line_key(loc, row["id"]),
        "row_kind": row["kind"],
        "zone": row["zone"],
        "catalog_id": row["id"],
        "item": row["item"],
        "unit": row["unit"],
        "qty": qty,
        "source_quality": row["source_quality"],
        "reference": row.get("reference"),
        "kind": kind,
        "derived": derived,
        "derivation": derivation,
        "base_minutes": {b: _round1(base[b]) for b in ("low", "mid", "high")},
        "factors": {
            "condition": cond_stack,
            "access": access_used,
            "schedule": sched_stack,
        },
        "stacked_factor": {
            "low": _round1(mult_low), "mid": _round1(mult_mid),
            "high": _round1(mult_high),
        },
        "supervision": {
            "n01_row": "N01",
            "n01_foreman_ratio": n01_ratio,
            "n01_foreman_hours": {b: _round1(foreman[b]) for b in ("low", "mid", "high")},
            "n02_row": "N02",
            "n02_pct": n02_pct,
            "n02_super_hours": {b: _round1(superh[b]) for b in ("low", "mid", "high")},
        },
        "task_hours": {b: _round1(task[b]) for b in ("low", "mid", "high")},
        "hours": {b: _round1(hours[b]) for b in ("low", "mid", "high")},
        "labor_usd": {c: {b: _round1(v[b]) for b in ("low", "mid", "high")}
                      for c, v in labor.items()},
        "contingency_pct": pct,
        "contingency_usd": {b: _round1(contingency[b]) for b in ("low", "mid", "high")},
    }
    if row.get("company_override"):
        line["company_override"] = row["company_override"]
    return line


def _cost_carry_line(loc, days, est, crew, label):
    """M01 blacked-out window: idle crew-day cost, zero hours."""
    crew_size = crew["techs"] + crew["foremen"]
    tech = est["rate_card"]["tech_rate"]
    fore = est["rate_card"]["foreman_rate"]
    blended = (crew["techs"] * tech + crew["foremen"] * fore) / crew_size
    per_day = crew_size * crew["hours_per_day"] * blended
    return {
        "location_id": loc["id"],
        "location_name": loc["name"],
        "line_id": est["idle_lockout"]["row"],
        "row_kind": "cost_carry",
        "zone": "M-DOWNTIME-LOGISTICS",
        "catalog_id": est["idle_lockout"]["row"],
        "item": label,
        "unit": "DAY",
        "qty": days,
        "source_quality": "G",
        "reference": "idle crew-day carry: %d crew x %.1f h x blended $%.2f/h"
                     % (crew_size, crew["hours_per_day"], blended),
        "kind": "cost_carry",
        "derived": True,
        "derivation": "blacked-out window: %d day(s) x $%.2f idle crew-day"
                      % (days, per_day),
        "base_minutes": {"low": 0.0, "mid": 0.0, "high": 0.0},
        "factors": {"condition": [], "access": None, "schedule": []},
        "stacked_factor": {"low": 1.0, "mid": 1.0, "high": 1.0},
        "supervision": {"n01_row": "N01", "n01_foreman_ratio": 0.0,
                        "n01_foreman_hours": {"low": 0.0, "mid": 0.0, "high": 0.0},
                        "n02_row": "N02", "n02_pct": 0.0,
                        "n02_super_hours": {"low": 0.0, "mid": 0.0, "high": 0.0}},
        "task_hours": {"low": 0.0, "mid": 0.0, "high": 0.0},
        "hours": {"low": 0.0, "mid": 0.0, "high": 0.0},
        "labor_usd": {"tech": {"low": 0.0, "mid": 0.0, "high": 0.0},
                      "foreman": {"low": 0.0, "mid": 0.0, "high": 0.0},
                      "super": {"low": 0.0, "mid": 0.0, "high": 0.0},
                      "total": {"low": 0.0, "mid": 0.0, "high": 0.0}},
        "contingency_pct": 0.0,
        "contingency_usd": {"low": 0.0, "mid": 0.0, "high": 0.0},
        "cost_carry_usd": {b: round(days * per_day, 6) for b in ("low", "mid", "high")},
        "idle_crew_day_usd": round(per_day, 6),
    }


# ------------------------------------------------------- power-plant helper

def _power_plant_lines(loc, sched_stack, crew, est, cat, warnings):
    """Auto-sized O-zone lines from a location's power_plant block.

    Ah = runtime_hours x load_amps; strings = ceil(Ah / string_ah) (IFC 12-hr
    sizing per O04 notes); rectifiers N+1 = ceil(load_amps/rect_size)+1."""
    pp = loc["power_plant"]
    spec = est["power_plant"]
    rows = spec["rows"]
    runtime = float(pp.get("runtime_hours", spec["runtime_hours_default"]))
    load = float(pp["load_amps"])
    string_ah = float(pp.get("string_ah", spec["battery_string_ah"]))
    conds = _condition_stack(loc.get("conditions"), est)
    access = _access_entry(loc.get("access", "ground"), est)

    ah = runtime * load
    strings = int(math.ceil(ah / string_ah)) if ah > 0 else 0
    if "rectifier_modules" in pp and pp.get("rectifier_modules") is not None:
        rects, rnote = int(pp["rectifier_modules"]), "pinned by definition"
    else:
        n = int(math.ceil(load / float(spec["rectifier_size_amps"]))) if load > 0 else 0
        rects = n + 1
        rnote = ("N+1 from load: ceil(%.1fA / %.1fA) = %d +1 spare = %d"
                 % (load, spec["rectifier_size_amps"], n, rects))
    if runtime < float(spec["runtime_ifc_min_hours"]):
        warnings.append("power_plant at location %r: %.1f h runtime is below the IFC "
                        "510.4.2.3 %.0f-h minimum" % (loc["id"], runtime,
                                                      spec["runtime_ifc_min_hours"]))
    out = []
    bay_row = cat["by_id"].get(rows["bay"])
    rect_row = cat["by_id"].get(rows["rectifier"])
    string_row = cat["by_id"].get(rows["strings"])
    if bay_row and strings + rects > 0:
        out.append(_make_line(loc, bay_row, 1, conds, access, sched_stack, crew, est,
                              derived=True, no_stack=False,
                              derivation="48VDC plant bay for this power-plant block"))
    if rect_row and rects > 0:
        out.append(_make_line(loc, rect_row, rects, conds, access, sched_stack, crew,
                              est, derived=True, no_stack=False,
                              derivation="rectifiers %s" % rnote))
    if string_row and strings > 0:
        out.append(_make_line(loc, string_row, strings, conds, access, sched_stack,
                              crew, est, derived=True, no_stack=False,
                              derivation=("auto-sized: Ah = %.1f h x %.1f A = %.1f Ah; "
                                          "strings = ceil(%.1f / %.1f) = %d"
                                          % (runtime, load, ah, ah, string_ah, strings))))
    for flag, row_key in (("spill_containment", rows.get("spill_containment")),
                          ("battery_monitoring", rows.get("battery_monitoring"))):
        if pp.get(flag) and row_key and cat["by_id"].get(row_key) and strings > 0:
            extra = cat["by_id"][row_key]
            out.append(_make_line(loc, extra, strings, conds, access, sched_stack,
                                  crew, est, derived=True, no_stack=False,
                                  derivation="per battery string (%d), opted in by "
                                             "power_plant.%s" % (strings, flag)))
    return out


# ------------------------------------------------------------ rollups / cost

def _sum_bound(lines, field, bound):
    return sum(ln[field][bound] for ln in lines)


def _rollup(key, lines):
    r = {"key": key, "line_count": len(lines)}
    for field, sub in (("hours", None), ("labor_usd", "total"),
                       ("contingency_usd", None)):
        vals = {}
        for b in ("low", "mid", "high"):
            if sub:
                vals[b] = round(sum(ln[field][sub][b] for ln in lines), 6)
            else:
                vals[b] = round(_sum_bound(lines, field, b), 6)
        r[field] = vals
    return r


def _cost_chain(direct_lines, cost_carry_lines, est, commercial, defn,
                esc_meta, profit_pct, profit_tier=None, tax_rate=None):
    """$ rollup in the documented v0.6.0 wrap order (module docstring,
    config/commercial.json comment, /how-it-works section 11):

    labor -> downtime -> small tools -> escalation -> taxes ->
    insurance/bond -> PM -> OH -> profit -> line-risk contingency.

    Each percentage step applies to the running subtotal produced by the
    steps before it; escalation is the exception that always applies to
    the (labor + downtime) base only, and taxes apply to the taxable
    wrapped labor per the line-type rules. Contingency is summed per line
    from the marked-up-free labor carries."""
    bounds = ("low", "mid", "high")
    tech = est["rate_card"]["tech_rate"]
    fore = est["rate_card"]["foreman_rate"]
    labor = {b: round(sum(ln["labor_usd"]["total"][b] for ln in direct_lines), 6)
             for b in bounds}
    downtime = round(sum(ln.get("cost_carry_usd", {"mid": 0.0})["mid"]
                         for ln in cost_carry_lines), 6)

    steps = wrap_steps(labor, {b: downtime for b in bounds},
                       list(direct_lines) + list(cost_carry_lines),
                       commercial, defn or {}, esc_meta=esc_meta,
                       tax_rate=tax_rate)
    st, esc = steps["small_tools"], steps["escalation"]
    tax, ins = steps["taxes"], steps["insurance_bond"]

    run = {b: labor[b] + downtime for b in bounds}
    st_usd, esc_usd, tax_usd, ins_usd = st["usd"], esc["usd"], tax["usd"], ins["usd"]
    run = {b: run[b] + st_usd[b] for b in bounds}          # small tools
    run = {b: run[b] + esc_usd[b] for b in bounds}         # escalation
    run = {b: run[b] + tax_usd[b] for b in bounds}         # taxes
    run = {b: run[b] + ins_usd[b] for b in bounds}         # insurance/bond

    pm_pct = float(est["pm_pct"])
    oh_pct = float(est["overhead_pct"])
    profit_pct = float(profit_pct)
    pm_base = run
    pm = {b: round(pm_base[b] * pm_pct / 100.0, 6) for b in bounds}
    oh_base = {b: run[b] + pm[b] for b in bounds}
    oh = {b: round(oh_base[b] * oh_pct / 100.0, 6) for b in bounds}
    p_base = {b: oh_base[b] + oh[b] for b in bounds}
    prof = {b: round(p_base[b] * profit_pct / 100.0, 6) for b in bounds}
    subtotal = {b: round(run[b] + pm[b] + oh[b] + prof[b], 6) for b in bounds}
    cont = {b: round(sum(ln["contingency_usd"][b] for ln in direct_lines), 6)
            for b in bounds}
    total = {b: round(subtotal[b] + cont[b], 6) for b in bounds}
    dt_all = {b: downtime for b in bounds}

    out = {
        "direct_labor_usd": labor, "downtime_usd": downtime,
        "small_tools_pct": st["pct"], "small_tools_usd": st_usd,
        "escalation_usd": esc_usd, "escalation_meta": esc,
        "tax_rate_pct": tax["rate_pct"], "tax_usd": tax_usd,
        "tax_taxable_labor_usd": tax["tax_view"]["taxable_labor_usd"],
        "tax_exempt_labor_usd": tax["tax_view"]["exempt_labor_usd"],
        "insurance_bond_pct": ins["pct"], "insurance_bond_usd": ins_usd,
        "profit_risk_tier": profit_tier,
        "pm_pct": pm_pct, "pm_usd": pm,
        "pm_basis": "pct x (labor + downtime + small tools + escalation +"
                    " taxes + insurance/bond)",
        "overhead_pct": oh_pct, "overhead_usd": oh,
        "profit_pct": profit_pct, "profit_usd": prof,
        "contingency_usd": cont, "contingency_note": est.get("contingency_note"),
        "subtotal_usd": subtotal, "grand_total_usd": total,
        "wrap_order": WRAP_ORDER,
        "rate_card": {"tech_rate": tech, "foreman_rate": fore,
                      "currency": est["rate_card"]["currency"]},
    }
    money = [
        {"code": "DIRECT_LABOR", "label": "Direct labor (all wrapped lines)",
         "basis": "tech @ $%s/h; foreman+super @ $%s/h" % (tech, fore),
         "amount": labor},
        {"code": "DOWNTIME_M01", "label": "Downtime / lockout idle crew-day carry",
         "basis": est["idle_lockout"]["row"] + " windows (schedule risk, not hours)",
         "amount": dt_all},
        {"code": "SMALL_TOOLS", "label": "Small tools / consumables allowance",
         "basis": "%.1f%% x (labor + downtime)" % st["pct"], "amount": st_usd},
        {"code": "ESCALATION", "label": "Escalation (%s index)" % esc["index"],
         "basis": "(factor %s - 1) x (labor + downtime); %s -> %s = %s days "
                  "%s" % (esc["factor"], esc["base_date"],
                          esc.get("anchor_date") or "n/a",
                          ("%s -> " % esc["days"]) if esc["applied"] else "",
                          ("[%s]" % esc["note"]) if not esc["applied"] else ""),
         "amount": esc_usd},
        {"code": "TAXES", "label": "Sales/use tax",
         "basis": "%.2f%% x taxable wrapped labor $%s (exempt $%s)"
                  % (tax["rate_pct"], tax["tax_view"]["taxable_labor_usd"]["mid"],
                     tax["tax_view"]["exempt_labor_usd"]["mid"]),
         "amount": tax_usd},
        {"code": "INSURANCE_BOND", "label": "Insurance & bond carry",
         "basis": "%.1f%% x (labor + downtime + small tools + escalation +"
                  " taxes)" % ins["pct"], "amount": ins_usd},
        {"code": "PM_I05", "label": "Project management", "basis": out["pm_basis"],
         "amount": pm},
        {"code": "OVERHEAD", "label": "Overhead",
         "basis": "%.1f%% x running subtotal through PM" % oh_pct,
         "amount": oh},
        {"code": "PROFIT", "label": "Profit",
         "basis": "%.1f%% x running subtotal through OH" % profit_pct,
         "amount": prof},
        {"code": "SUBTOTAL", "label": "Subtotal before contingency",
         "basis": "labor + downtime + small tools + escalation + taxes +"
                  " insurance/bond + PM + OH + profit", "amount": subtotal},
        {"code": "CONTINGENCY_N05", "label": "Line-risk contingency (Class 1)",
         "basis": "per line by source_quality: " + ", ".join(
             "%s %s%%" % (q, est["contingency_pct_by_quality"][q]) for q in QUALITIES),
         "amount": cont},
        {"code": "GRAND_TOTAL", "label": "Grand total",
         "basis": "subtotal + contingency", "amount": total},
    ]
    return out, money


# ----------------------------------------------------------------- compute

def compute_estimate(definition, catalog=None, estimating=None,
                     commercial=None, quote_swap=None):
    """Validate + compute a full estimate recap. Pure function (no IO).

    `quote_swap` (optional callable) receives the built line list right
    before the $ chain and mutates line labor/contingency in place -- the
    scenario engine uses it so quote swaps flow through the full wrap
    (small tools, escalation, taxes, insurance/bond, PM/OH/profit)."""
    d = definition
    catalog = catalog or load_labor_catalog()
    estimating = estimating or load_estimating_config()
    if not isinstance(d, dict):
        raise EstimateError("estimate definition must be a JSON object")
    if d.get("rate_overrides"):
        estimating = _apply_rate_overrides(estimating, d["rate_overrides"])
    _validate_definition(d)
    crew = _resolve_crew(d, estimating)
    schedule = {"night": bool((d.get("schedule") or {}).get("night")),
                "weekend": bool((d.get("schedule") or {}).get("weekend"))}
    warnings = []

    idx = catalog_index(catalog)
    sup_spec = estimating["supervision_pct"]

    lines = []
    for loc in d["locations"]:
        conds = _condition_stack(loc.get("conditions"), estimating)
        sched_stack = _schedule_stack(schedule, estimating)
        access = _access_entry(loc.get("access", "ground"), estimating)
        seen = set()
        for ln in loc.get("lines") or []:
            rid = str(ln["catalog_id"]).strip()
            row = idx["by_id"].get(rid)
            if row is None:
                raise EstimateError("unknown catalog_id %r (line in location %r)"
                                    % (rid, loc.get("id")))
            if row["kind"] == "factor":
                raise EstimateError(
                    "catalog row %s is a FACTOR row (multiplier), not a quantity line "
                    "- it is applied via access/condition flags, not lines[]" % rid)
            if rid in seen:
                raise EstimateError("duplicate line %s in location %r - merge into one "
                                    "line with the summed qty" % (rid, loc.get("id")))
            seen.add(rid)
            if row["kind"] == "quote":
                warnings.append("catalog row %s is a 0-minute quote/cost-carry row "
                                "(%s); it carries no hours - price it in the quote, "
                                "not the labor recap" % (rid, row["unit"]))
            qty = float(ln["qty"])
            lines.append(_make_line(loc, row, qty, conds, access, sched_stack,
                                    crew, estimating))
        # power-plant helper (O-zone auto-sizing)
        if loc.get("power_plant"):
            pl = _power_plant_lines(loc, sched_stack, crew, estimating, idx, warnings)
            derived_ids = {l["catalog_id"] for l in pl}
            clash = sorted(derived_ids & seen)
            if clash:
                raise EstimateError(
                    "location %r: rows %s are auto-derived from power_plant; remove "
                    "them from lines[] to avoid double-counting"
                    % (loc.get("id"), clash))
            lines.extend(pl)

    n_locations = len(d["locations"])
    direct_lines = [l for l in lines if l["kind"] == "minutes"]
    direct_hours_mid = _sum_bound(direct_lines, "hours", "mid")
    crew_size = crew["techs"] + crew["foremen"]
    capacity_day = crew["crews"] * crew_size * crew["hours_per_day"]
    import math
    crew_days = int(math.ceil(direct_hours_mid / capacity_day)) if capacity_day > 0 else 0

    # derived estimate-level logistics / wrap lines (base minutes, no stacking)
    def _derived_line(rid, qty, why):
        row = idx["by_id"][rid]
        empty = []
        return _make_line(None, row, float(qty), [], None, empty, crew, estimating,
                          derived=True, no_stack=True, derivation=why,
                          kind="minutes")

    total_moves = 0
    for mv in d.get("travel_moves") or []:
        total_moves += int(mv.get("moves", 1))
    derived_lines = []
    if total_moves:
        derived_lines.append(_derived_line(estimating["wrap_rows"]["travel"],
                                           total_moves,
                                           "%d in-venue travel move(s) between locations"
                                           % total_moves))
    orientation_qty = crew["crews"] * n_locations
    derived_lines.append(_derived_line(estimating["wrap_rows"]["orientation"],
                                       orientation_qty,
                                       "%d crew x %d location(s) (phase = location)"
                                       % (crew["crews"], n_locations)))
    derived_lines.append(_derived_line(estimating["wrap_rows"]["cleanup"], crew_days,
                                       "ceil(%0.4f wrapped direct h / %d h crew-day "
                                       "capacity) = %d crew-day(s)"
                                       % (direct_hours_mid, capacity_day, crew_days)))
    lines.extend(derived_lines)

    cost_carry = []
    for w in d.get("downtime_windows") or []:
        cost_carry.append(_cost_carry_line(
            {"id": None, "name": "(estimate-wide)"}, int(w["days"]), estimating, crew,
            w.get("label") or "Owner lockout / blacked-out window"))
    lines.extend(cost_carry)  # visible in the recap line table (zero hours)

    # scenario hook: swap labor dollars before the chain sees the lines
    if quote_swap is not None:
        quote_swap(lines)

    # ---- commercial wrap (phase E3): profit tier, escalation anchor, $ chain
    if not isinstance(commercial, dict):
        commercial = load_commercial_config()
    defn_commercial = d.get("commercial") or {}
    tier = defn_commercial.get("profit_risk_tier") or \
        commercial.get("default_profit_risk_tier", "standard")
    tiers = commercial.get("profit_pct_by_risk_tier") or {}
    if tier not in tiers:
        raise EstimateError(
            "commercial.profit_risk_tier %r is not a key of commercial.json"
            " profit_pct_by_risk_tier (keys: %s)" % (tier, sorted(tiers)))
    # an explicit definition profit_pct override (rate_overrides) beats the
    # risk tier; otherwise the tier carries the profit pct
    prof_override = (d.get("rate_overrides") or {}).get("profit_pct")
    profit_pct = float(prof_override) if prof_override is not None \
        else float(tiers[tier])
    tax_rate = (defn_commercial.get("tax") or {}).get("rate_pct")

    loc_hours = {}
    for ln in lines:
        if ln.get("location_id"):
            loc_hours[ln["location_id"]] = \
                loc_hours.get(ln["location_id"], 0.0) + float(ln["hours"]["mid"])
    esc_anchor = None
    if d.get("start_date"):
        esc_anchor = schedule_midpoint_date(
            d,
            [(l["id"], loc_hours.get(l["id"], 0.0)) for l in d["locations"]],
            crew["crews"], crew_size, crew["hours_per_day"],
            crew["days_per_week"])
    esc_meta = escalation_meta(commercial, d, anchor_date=esc_anchor)
    if not esc_meta["applied"]:
        warnings.append("escalation: %s" % esc_meta["note"])

    hours = {b: round(_sum_bound(lines, "hours", b), 6) for b in ("low", "mid", "high")}
    capacity_week = capacity_day * crew["days_per_week"]
    crew_weeks = int(math.ceil(hours["mid"] / capacity_week)) if capacity_week > 0 else 0
    cost, money_lines = _cost_chain(
        direct_lines + derived_lines, cost_carry, estimating, commercial, d,
        esc_meta, profit_pct, profit_tier=tier, tax_rate=tax_rate)

    idx_rows = idx["by_id"]
    zone_order = idx["zones"]
    by_zone = {}
    for ln in lines:
        by_zone.setdefault(ln["zone"], []).append(ln)
    zone_rollups = []
    for zone in zone_order:
        zl = by_zone.get(zone)
        if not zl:
            continue
        r = _rollup(zone, zl)
        r["cost_carry_usd"] = round(
            sum((l.get("cost_carry_usd") or {"mid": 0.0})["mid"] for l in zl), 6)
        zone_rollups.append(r)
    by_loc = {}
    for ln in lines:
        key = ln["location_id"] or GLOBAL_LOCATION_ID
        by_loc.setdefault(key, []).append(ln)
    loc_by_id = {l.get("id"): l for l in d["locations"]}
    location_rollups = []
    for key, ll in by_loc.items():
        nm = loc_by_id[key]["name"] if key in loc_by_id else GLOBAL_LOCATION_ID
        r = _rollup(key, ll)
        r["name"] = nm
        r["cost_carry_usd"] = round(sum(
            (l.get("cost_carry_usd") or {"mid": 0.0})["mid"] for l in ll), 6)
        location_rollups.append(r)

    _dedupe_line_ids(lines)

    recap = {
        "engine": "romtool-estimate/%s" % _engine_version(),
        "name": d.get("name") or "Untitled Estimate",
        "definition": d,
        "schedule": schedule,
        "crew": {
            "crews": crew["crews"], "techs": crew["techs"], "foremen": crew["foremen"],
            "crew_size_per_crew": crew_size,
            "total_crew_size": crew["crews"] * crew_size,
            "hours_per_day": crew["hours_per_day"],
            "days_per_week": crew["days_per_week"],
            "capacity_per_crew_day_h": capacity_day,
            "capacity_per_crew_week_h": round(capacity_week, 6),
            "n01_foreman_ratio": round(
                (crew["foremen"] / crew["techs"]) * estimating["foreman_productive_haircut"], 6),
            "n02_supervision_pct": round(_mid(sup_spec["low"], sup_spec["high"]), 6),
            "foreman_productive_haircut": estimating["foreman_productive_haircut"],
        },
        "lines": lines,
        "warnings": warnings,
        "zone_rollups": zone_rollups,
        "location_rollups": location_rollups,
        "totals": {
            "hours": hours,
            "total_crew_hours_mid": hours["mid"],
            "crew_days": crew_days,
            "crew_weeks": crew_weeks,
            "hours_per_crew_week": round(hours["mid"] / crew_weeks, 6) if crew_weeks else 0.0,
            "travel_moves": total_moves,
        },
        "cost": cost,
        "money_lines": money_lines,
        "commercial": {
            "wrap_order": WRAP_ORDER,
            "wrap_applied": True,
            "small_tools_pct": cost["small_tools_pct"],
            "escalation": {k: esc_meta[k] for k in
                           ("applied", "index", "scope", "base_date",
                            "annual_rate_pct", "anchor_date", "anchor_source",
                            "days", "factor", "note")},
            "taxes": {"rate_pct": cost["tax_rate_pct"],
                      "taxable_line_types":
                          list(commercial["sales_use_tax"].get(
                              "taxable_line_types") or []),
                      "exempt_line_types":
                          list(commercial["sales_use_tax"].get(
                              "exempt_line_types") or []),
                      "taxable_labor_usd": cost["tax_taxable_labor_usd"],
                      "exempt_labor_usd": cost["tax_exempt_labor_usd"]},
            "insurance_bond_pct": cost["insurance_bond_pct"],
            "profit_risk_tier": tier,
            "profit_pct": profit_pct,
            "pm_pct": cost["pm_pct"],
            "overhead_pct": cost["overhead_pct"],
        },
        "factor_legend": {
            "stack_order": ["base_minutes", "condition", "access", "schedule",
                            "supervision_N01_N02", "wrap_and_contingency"],
            "condition_map": {f["key"]: f["row"]
                              for f in _condition_stack(CONDITION_FLAGS, estimating)},
            "access_map": {m: _access_entry(m, estimating)["row"]
                           for m in ACCESS_METHODS},
            "schedule_map": {f["key"]: f["row"]
                             for f in _schedule_stack({"night": True, "weekend": True},
                                                      estimating)},
        },
        "catalog": {"source_csv": catalog.get("source_csv"),
                    "row_count": catalog.get("row_count")},
        "company_override_catalog_ids": sorted({ln["catalog_id"] for ln in lines
                                                if ln.get("company_override")}),
    }
    return recap


def compute_alternates(definition, catalog=None, estimating=None,
                       commercial=None):
    """Price definition.alternates[] with the same engine (factor stacks,
    supervision wrap, full commercial $ chain) so bid-form totals and base
    totals are built by identical math.

    Semantics (mirrored in /help section id=bidform):
      - locations-only alternate  -> the whole current scope of those
        locations, incl. auto-derived power-plant lines;
      - lines specs               -> that subset (qty may be replaced);
        a subset alternate drops the location's power_plant block so the
        auto-derived O-zone lines are not double-carried on a partial
        scope; kind 'add' may introduce NEW catalog lines with an
        explicit qty;
      - kind 'add' (default) prices the scope as an additive alternate;
        kind 'deduct' prices the scope and the bid form signs the amount
        negative (credit when accepted);
      - alternates are standalone mini-estimates: no travel moves, no
        downtime windows, no estimate-wide derived lines beyond the
        engine's own M05/N03/N04 recomputed at alternate scope; schedule
        flags, crew plan and the commercial block are reused from the
        base definition.
    """
    d = definition
    alts = d.get("alternates") or []
    catalog = catalog or load_labor_catalog()
    estimating = estimating or load_estimating_config()
    if not isinstance(commercial, dict):
        commercial = load_commercial_config()
    base_locs = {l["id"]: l for l in d["locations"]}
    out = []
    for i, alt in enumerate(alts):
        name = str(alt["name"]).strip()
        kind = alt.get("kind", "add")
        loc_filter = [l for l in (alt.get("locations") or []) if l in base_locs]
        specs = {}
        for s in alt.get("lines") or []:
            specs[(s["location_id"], str(s["catalog_id"]))] = s
        spec_locs = sorted({lid for lid, _ in specs})
        involved = list(dict.fromkeys(loc_filter + spec_locs))
        errors = ["alternates[%d] references unknown location %r" % (i, lid)
                  for lid in involved if lid not in base_locs]
        if errors:
            raise EstimateError(errors)
        if not involved:
            raise EstimateError("alternates[%d] needs locations and/or lines"
                                % i)
        sub_locations = []
        for lid in involved:
            loc = copy.deepcopy(base_locs[lid])
            has_specs = any(k[0] == lid for k in specs)
            if loc["id"] in loc_filter and not has_specs:
                sub_locations.append(loc)        # whole scope incl power plant
                continue
            keep = []
            for ln in loc.get("lines") or []:
                key = (lid, str(ln["catalog_id"]))
                if key in specs:
                    spec = specs[key]
                    if spec.get("qty") is not None:
                        ln = dict(ln)
                        ln["qty"] = float(spec["qty"])
                    keep.append(ln)
            for key, spec in specs.items():
                if key[0] == lid and not any(
                        ln_key == key[1] for ln_key in
                        [str(l.get("catalog_id")) for l in keep]):
                    if kind == "deduct":
                        raise EstimateError(
                            "alternates[%d]: deduct line %r is not in "
                            "location %r's definition lines" % (i, key[1], lid))
                    if spec.get("qty") is None:
                        raise EstimateError(
                            "alternates[%d]: add line %r in location %r "
                            "needs an explicit qty" % (i, key[1], lid))
                    keep.append({"catalog_id": key[1],
                                 "qty": float(spec["qty"])})
            loc["lines"] = keep
            loc.pop("power_plant", None)
            if not keep:
                raise EstimateError("alternates[%d]: location %r would end "
                                    "with no lines" % (i, lid))
            sub_locations.append(loc)
        sub = {"name": name, "locations": sub_locations,
               "schedule": d.get("schedule"), "crew": d.get("crew")}
        if d.get("commercial"):
            sub["commercial"] = copy.deepcopy(d["commercial"])
        if d.get("rate_overrides"):
            # keep alternate pricing in the base estimate's rate context
            sub["rate_overrides"] = copy.deepcopy(d["rate_overrides"])
        recap2 = compute_estimate(sub, catalog, estimating, commercial)
        amount = float(recap2["cost"]["grand_total_usd"]["mid"])
        entry = {
            "name": name, "kind": kind,
            "locations": sorted(involved),
            "amount_usd_mid": round(-amount if kind == "deduct" else amount, 6),
            "absolute_usd_mid": round(amount, 6),
            "crew_hours_mid": recap2["totals"]["total_crew_hours_mid"],
            "crew_days": recap2["totals"]["crew_days"],
            "grand_total_low": recap2["cost"]["grand_total_usd"]["low"],
            "grand_total_high": recap2["cost"]["grand_total_usd"]["high"],
        }
        pts = alt.get("antenna_points")
        if pts is not None:
            entry["unit_price_per_antenna_point"] = round(
                amount / float(pts), 6)
            entry["antenna_points"] = float(pts)
        out.append(entry)
    return out


def _engine_version():
    from app.routes import __version__

    return __version__


# ---------------------------------------------------------------- exporters

def recap_csv(recap, governance=None, sanity=None):
    """Render the recap as CSV text: line table, then the $ rollup block,
    then an optional governance section (state/revision/assumption &
    quote registers) and an optional sanity section (hard findings /
    warnings) when the caller passes those views."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["location", "zone", "catalog_id", "item", "unit", "qty",
                "quality", "kind", "derived", "base_min_mid",
                "condition", "access", "schedule", "stacked_factor_mid",
                "n01_foreman_ratio", "n02_supervision_pct",
                "task_hours_mid", "foreman_hours_mid", "super_hours_mid",
                "hours_low", "hours_mid", "hours_high",
                "labor_usd_mid", "contingency_pct", "contingency_usd_mid",
                "cost_carry_usd", "derivation"])
    for ln in recap["lines"]:
        f = ln["factors"]
        w.writerow([
            ln["location_name"], ln["zone"], ln["catalog_id"], ln["item"],
            ln["unit"], ln["qty"], ln["source_quality"], ln["kind"],
            "yes" if ln["derived"] else "", ln["base_minutes"]["mid"],
            "+".join("%s=%s" % (c["key"], c["value"]) for c in f["condition"]) or "",
            ("%s=%s" % (f["access"]["row"], f["access"]["value"])
             if f["access"] else "none"),
            "+".join("%s=%s" % (c["key"], c["value"]) for c in f["schedule"]) or "",
            ln["stacked_factor"]["mid"],
            ln["supervision"]["n01_foreman_ratio"],
            ln["supervision"]["n02_pct"],
            ln["task_hours"]["mid"], ln["supervision"]["n01_foreman_hours"]["mid"],
            ln["supervision"]["n02_super_hours"]["mid"],
            ln["hours"]["low"], ln["hours"]["mid"], ln["hours"]["high"],
            ln["labor_usd"]["total"]["mid"], ln["contingency_pct"],
            ln["contingency_usd"]["mid"],
            (ln.get("cost_carry_usd") or {"mid": ""})["mid"],
            ln.get("derivation") or "",
        ])
    t = recap["totals"]
    c = recap["cost"]
    w.writerow([])
    w.writerow(["section", "code", "label", "basis",
                "amount_low", "amount_mid", "amount_high"])
    w.writerow(["hours", "CREW_HOURS", "Total crew-hours (wrapped lines)",
                "task + foreman (N01) + super (N02)",
                t["hours"]["low"], t["hours"]["mid"], t["hours"]["high"]])
    w.writerow(["hours", "CREW_DAYS/CREW_WEEKS", "Crew-days; crew-weeks",
                "ceil(direct h / crew-day capacity); ceil(h / crew-week cap)",
                t["crew_days"], t["crew_weeks"],
                t["hours_per_crew_week"]])
    w.writerow(["legend", "wrap_order", "", "",
                "labor -> downtime -> small tools -> escalation -> taxes ->"
                " insurance/bond -> PM -> OH -> profit -> contingency"])
    for m in recap["money_lines"]:
        w.writerow(["money", m["code"], m["label"], m["basis"],
                    m["amount"]["low"], m["amount"]["mid"], m["amount"]["high"]])
    if governance:
        w.writerow([])
        w.writerow(["governance", "field", "value"])
        w.writerow(["governance", "estimate_state", governance["state"]])
        w.writerow(["governance", "revision", governance["revision"]])
        a = governance["assumptions"]
        w.writerow(["governance", "assumption_register",
                    "open=%d verified=%d closed=%d total=%d"
                    % (a["open"], a["verified"], a["closed"], a["total"])])
        q = governance["quotes"]
        w.writerow(["governance", "quote_register",
                    "draft=%d received=%d bound=%d expired=%d total=%d"
                    % (q["draft"], q["received"], q["bound"], q["expired"],
                       q["total"])])
        for warn in governance["quote_warnings"]:
            w.writerow(["governance", warn["code"], warn["message"]])
    if sanity:
        from romtool.sanity import sanity_export_rows
        for row in sanity_export_rows(sanity):
            w.writerow(row)
    return buf.getvalue()
