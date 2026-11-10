"""Actuals loop (phase E4, v0.7.0).

Import actual hours/cost per catalog card against an estimate, measure norm
drift, propose re-fit cards -- and NEVER auto-apply anything.

Data flow:

1. POST /estimates/{id}/actuals stores rows
   {catalog_id, location_id?, qty, hours, cost?, date, note?} per estimate
   (estimate_actuals table + one audit event).
2. GET /estimates/{id}/actuals/drift compares, per catalog_id with actuals,
   the unit actual hours against the EFFECTIVE norm card (config/
   labor_units.json as adjusted by the company override layer):
       row ratio  = (actual hours / qty) / (card mid minutes / 60)
   The report-level ratio is qty-weighted (sum actual hours / sum norm mid
   hours at the row qtys) and the per-row distribution median is the
   qty-weighted median. drift_pct = (ratio - 1) x 100 with quality flags
   from config/actuals.json (tight <= 10%, watch <= 25%, else divergent).
3. POST /estimates/{id}/actuals/propose-refit -> per card a proposed new
   low/high: norm low/high scaled by the weighted median ratio, with the
   median ratio clamped into [guard_ratio_low, guard_ratio_high] of the
   current norm (guarded bounds). Proposals are stored for review only.
4. POST /estimates/{id}/actuals/apply-refit {catalog_id, low, high, note?}
   takes OWNER-APPROVED numbers (never proposed silently) and writes the
   company override layer in config/company_labor_overrides.json; the
   engine (romtool/estimate.load_labor_catalog) prefers the override card
   on every later compute and stamps provenance on affected recap lines.

Constants live in config/actuals.json; the override file path comes from
ROMTOOL_COMPANY_OVERRIDES (default config/company_labor_overrides.json).
"""

import json
import os
import tempfile

from .estimate import catalog_index, load_labor_catalog

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_MODULE_DIR)
DEFAULT_ACTUALS_CFG = os.path.join(_REPO_ROOT, "config", "actuals.json")
DEFAULT_OVERRIDES_PATH = os.path.join(_REPO_ROOT, "config",
                                      "company_labor_overrides.json")


class ActualsError(ValueError):
    """Payload/validation rejection; .errors is a list (API maps to 422)."""

    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# ------------------------------------------------------------------ config

def load_actuals_config(path=None):
    p = path or os.environ.get("ROMTOOL_ACTUALS") or DEFAULT_ACTUALS_CFG
    with open(p, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    validate_actuals_config(cfg)
    return cfg


def validate_actuals_config(cfg):
    errors = []
    if int(cfg.get("min_rows_for_refit_proposal", 0)) < 1:
        errors.append("min_rows_for_refit_proposal must be >= 1")
    lo, hi = float(cfg.get("guard_ratio_low", 0)), float(cfg.get("guard_ratio_high", 0))
    if not (0 < lo <= hi):
        errors.append("guard_ratio_low must be > 0 and <= guard_ratio_high")
    qf = cfg.get("quality_flags") or {}
    t, w = float(qf.get("tight_pct", -1)), float(qf.get("watch_pct", -1))
    if not (0 <= t <= w):
        errors.append("quality_flags need 0 <= tight_pct <= watch_pct")
    if errors:
        raise ActualsError(errors)


# ------------------------------------------------------- row validation

def _num(value, label, errors, minimum=0.0, exclusive=True):
    try:
        v = float(value)
    except (TypeError, ValueError):
        errors.append("%s must be a number, got %r" % (label, value))
        return None
    if exclusive and v <= minimum:
        errors.append("%s must be > %s, got %r" % (label, minimum, value))
        return None
    if not exclusive and v < minimum:
        errors.append("%s must be >= %s, got %r" % (label, minimum, value))
        return None
    return v


def validate_actual_rows(rows, catalog=None):
    """Validate a list of actual rows; returns normalized rows. ActualsError
    with collected messages when anything is off."""
    if not isinstance(rows, list) or not rows:
        raise ActualsError("actuals payload needs a non-empty rows[] array")
    idx = catalog_index(_load_catalog(catalog))["by_id"]
    errors, out = [], []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            errors.append("rows[%d] must be an object" % i)
            continue
        cid = str(r.get("catalog_id") or "").strip()
        if not cid:
            errors.append("rows[%d].catalog_id required" % i)
        elif cid not in idx:
            errors.append("rows[%d].catalog_id %r is not in the labor catalog"
                          % (i, cid))
        elif idx[cid].get("kind") != "minutes":
            errors.append("rows[%d].catalog_id %r is a %s row, not a labor"
                          " norm - actual hours map to minutes-kind cards only"
                          % (i, cid, idx[cid].get("kind")))
        qty = _num(r.get("qty"), "rows[%d].qty" % i, errors, 0.0)
        hours = _num(r.get("hours"), "rows[%d].hours" % i, errors, 0.0)
        cost = _num(r.get("cost"), "rows[%d].cost" % i, errors, 0.0,
                    exclusive=False) if r.get("cost") is not None else None
        date = str(r.get("date") or "").strip()
        try:
            from datetime import date as _d
            date = _d.fromisoformat(date[:10]).isoformat()
        except ValueError:
            errors.append("rows[%d].date must be an ISO YYYY-MM-DD date, got"
                          " %r" % (i, r.get("date")))
            date = None
        loc = r.get("location_id")
        if loc is not None and (not isinstance(loc, str) or not loc.strip()):
            errors.append("rows[%d].location_id must be a non-empty string or"
                          " null" % i)
            loc = None
        note = r.get("note")
        if note is not None and not isinstance(note, str):
            errors.append("rows[%d].note must be a string" % i)
        if errors:
            continue
        out.append({"catalog_id": cid, "location_id": (loc or "").strip() or None,
                    "qty": qty, "hours": hours, "cost": cost, "date": date,
                    "note": (note or "").strip() or None})
    if errors:
        raise ActualsError(errors)
    return out


# -------------------------------------------------------------- drift math

def _load_catalog(catalog):
    if catalog is None:
        return load_labor_catalog()
    return catalog


def card_mid_minutes(row):
    return (float(row["minutes_low"]) + float(row["minutes_high"])) / 2.0


def weighted_median(pairs):
    """Qty-weighted median of [(value, weight)]: smallest v whose cumulative
    weight >= half the total weight. Single-row -> that row's value."""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    half = total / 2.0
    cum = 0.0
    for v, w in pairs:
        cum += w
        if cum >= half - 1e-9:
            return v
    return pairs[-1][0]  # pragma: no cover - guarded by the loop


def drift_report(actual_rows, catalog=None, cfg=None, today=None):
    """Per catalog card: effective norm vs actuals. `actual_rows` are stored
    row dicts (catalog_id, qty, hours, cost?, date...). Unknown catalog ids
    (card removed/re-keyed since import) surface in `unresolved_ids`."""
    cfg = cfg or load_actuals_config()
    catalog = _load_catalog(catalog)
    idx = catalog_index(catalog)["by_id"]
    qf = cfg["quality_flags"]
    groups = {}
    unresolved = set()
    usable = 0
    for r in actual_rows:
        cid = r.get("catalog_id")
        row = idx.get(cid)
        if row is None or row.get("kind") != "minutes" or not row.get("minutes_high"):
            if cid:
                unresolved.add(cid)
            continue
        if float(r.get("qty") or 0) <= 0 or float(r.get("hours") or 0) <= 0:
            continue
        usable += 1
        groups.setdefault(cid, []).append(r)

    cards = []
    for cid in sorted(groups):
        rows = groups[cid]
        row = idx[cid]
        low, high = float(row["minutes_low"]), float(row["minutes_high"])
        mid = card_mid_minutes(row)
        norm_h = mid / 60.0
        entries = []
        for r in rows:
            qty, hours = float(r["qty"]), float(r["hours"])
            ratio = (hours / qty) / norm_h
            entries.append({
                "row_id": r.get("id"), "location_id": r.get("location_id"),
                "date": r.get("date"), "qty": qty, "hours": hours,
                "cost": r.get("cost"), "unit_norm_hours": round(norm_h, 9),
                "unit_actual_hours": round(hours / qty, 9),
                "ratio": round(ratio, 6)})
        total_qty = sum(float(r["qty"]) for r in rows)
        total_hours = sum(float(r["hours"]) for r in rows)
        weighted_ratio = (total_hours / total_qty) / norm_h if total_qty else 0.0
        med = weighted_median([(e["ratio"], e["qty"]) for e in entries])
        drift_pct = (weighted_ratio - 1.0) * 100.0
        adrift = abs(drift_pct)
        flag = ("tight" if adrift <= float(qf["tight_pct"]) + 1e-9 else
                "watch" if adrift <= float(qf["watch_pct"]) + 1e-9 else
                "divergent")
        cards.append({
            "catalog_id": cid, "item": row.get("item"), "unit": row.get("unit"),
            "n_rows": len(rows),
            "norm_minutes": {"low": low, "mid": mid, "high": high},
            "company_override": bool(row.get("company_override")),
            "total_qty": round(total_qty, 6),
            "total_actual_hours": round(total_hours, 6),
            "total_cost": None if all(r.get("cost") is None for r in rows)
            else round(sum(float(r["cost"] or 0.0) for r in rows), 6),
            "ratio_row_min": round(min(e["ratio"] for e in entries), 6),
            "ratio_row_max": round(max(e["ratio"] for e in entries), 6),
            "ratio_weighted_mean": round(weighted_ratio, 6),
            "ratio_weighted_median": round(med, 6),
            "drift_pct": round(drift_pct, 4),
            "quality_flag": flag,
            "rows": entries})
    return {
        "usable_rows": usable,
        "cards_with_actuals": len(cards),
        "unresolved_ids": sorted(unresolved),
        "quality_flags": {"tight_pct": float(qf["tight_pct"]),
                          "watch_pct": float(qf["watch_pct"])},
        "basis": "row ratio = (actual hours / qty) / (effective card mid"
                 " minutes / 60); report ratio = qty-weighted mean; median ="
                 " qty-weighted median of row ratios",
        "cards": cards,
    }


# ------------------------------------------------------------ refit proposal

def propose_refit(drift, catalog=None, cfg=None):
    """Proposed cards from a drift report: norm low/high scaled by the
    weighted median ratio, clamped into the guard window. NEVER applied."""
    cfg = cfg or load_actuals_config()
    catalog = _load_catalog(catalog)
    idx = catalog_index(catalog)["by_id"]
    lo_g, hi_g = float(cfg["guard_ratio_low"]), float(cfg["guard_ratio_high"])
    min_rows = int(cfg["min_rows_for_refit_proposal"])
    proposals, reasons = [], []
    for card in drift.get("cards", []):
        cid = card["catalog_id"]
        row = idx.get(cid)
        if row is None or card["n_rows"] < min_rows:
            reasons.append({"catalog_id": cid,
                            "reason": "needs >= %d usable rows (has %d)"
                                      % (min_rows, card["n_rows"])})
            continue
        med = card["ratio_weighted_median"]
        clamped = med < lo_g - 1e-9 or med > hi_g + 1e-9
        ratio_eff = min(max(med, lo_g), hi_g)
        low = round(float(row["minutes_low"]) * ratio_eff, 4)
        high = round(float(row["minutes_high"]) * ratio_eff, 4)
        cur = {"low": float(row["minutes_low"]),
               "mid": card_mid_minutes(row),
               "high": float(row["minutes_high"])}
        proposals.append({
            "catalog_id": cid, "item": row.get("item"), "unit": row.get("unit"),
            "n_rows": card["n_rows"], "median_ratio": med,
            "current_card": cur,
            "proposed_low": low, "proposed_high": high,
            "proposed_mid": round((low + high) / 2.0, 6),
            "guard_applied": {"low": lo_g, "high": hi_g},
            "clamped": clamped,
            "note": ("median ratio %0.4f clamped into the guard window"
                     % med) if clamped else
            ("card scaled by median ratio %0.4f of actuals" % med)})
    return {
        "min_rows": min_rows,
        "guard_ratio": {"low": lo_g, "high": hi_g},
        "never_auto_applies": True,
        "note": "proposals are review-only; an owner-approved POST"
                " .../apply-refit {catalog_id, low, high} writes the company"
                " override layer",
        "proposals": proposals,
        "skipped": reasons,
    }


# ------------------------------------------------------------ override layer

def read_overrides(path=None):
    """Read the company override layer; missing file -> empty layer."""
    p = path or os.environ.get("ROMTOOL_COMPANY_OVERRIDES") or DEFAULT_OVERRIDES_PATH
    if not os.path.exists(p):
        return {"version": None, "overrides": {}, "history": [], "path": p}
    with open(p, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return {"version": data.get("version"),
            "overrides": data.get("overrides") or {},
            "history": data.get("history") or [],
            "path": p}


def _validate_refit_payload(payload, catalog):
    cid = str(payload.get("catalog_id") or "").strip()
    errors = []
    catalog = _load_catalog(catalog)
    idx = catalog_index(catalog)["by_id"]
    row = idx.get(cid)
    if not cid:
        errors.append("catalog_id required")
    elif row is None:
        errors.append("catalog_id %r is not in the labor catalog" % cid)
    elif row.get("kind") != "minutes":
        errors.append("catalog_id %r is a %s row; refits apply to minutes-kind"
                      " labor cards" % (cid, row.get("kind")))

    def _m(k, minv=0.0):
        try:
            v = float(payload.get(k))
        except (TypeError, ValueError):
            errors.append("%s must be a number, got %r" % (k, payload.get(k)))
            return None
        if v <= minv:
            errors.append("%s must be > %s" % (k, minv))
            return None
        return v

    low, high = _m("low"), _m("high")
    if low is not None and high is not None and high <= low:
        errors.append("high must be > low (got low=%r high=%r)"
                      % (payload.get("low"), payload.get("high")))
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        errors.append("note must be a string")
    if errors:
        raise ActualsError(errors)
    return {"catalog_id": cid, "low": round(low, 4), "high": round(high, 4),
            "note": (note or "").strip() or None}


def apply_refit_to_file(payload, refit_meta, path=None, now_iso=None):
    """Write an owner-approved override into the company layer (atomic
    replace + history entry). Returns the updated override layer view.
    `refit_meta`: {estimate_id, n_rows, median_ratio, actor, revision,
    catalog (the loaded catalog dict)}."""
    clean = _validate_refit_payload(payload, refit_meta.get("catalog"))
    meta = {k: v for k, v in refit_meta.items() if k != "catalog"}
    p = path or os.environ.get("ROMTOOL_COMPANY_OVERRIDES") or DEFAULT_OVERRIDES_PATH
    now = now_iso
    if not now:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    cid = clean["catalog_id"]
    mid = (clean["low"] + clean["high"]) / 2.0
    data = {"version": "0.7.0",
            "comment": _LAYER_COMMENT,
            "overrides": {}, "history": []}
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    overrides = data.get("overrides") or {}
    prior = overrides.get(cid)
    entry = {
        "low": clean["low"], "high": clean["high"], "mid": round(mid, 6),
        "note": clean["note"], "updated_at": now,
        "source": {"estimate_id": refit_meta.get("estimate_id"),
                   "n_rows": refit_meta.get("n_rows"),
                   "median_ratio": refit_meta.get("median_ratio"),
                   "actor": refit_meta.get("actor"),
                   "revision": refit_meta.get("revision")},
    }
    if prior:
        entry["prior"] = {"low": prior.get("low"), "high": prior.get("high"),
                          "updated_at": prior.get("updated_at")}
    overrides[cid] = entry
    data["overrides"] = overrides
    hist = data.get("history") or []
    hist.append({"ts": now, "estimate_id": refit_meta.get("estimate_id"),
                 "catalog_id": cid, "low": clean["low"], "high": clean["high"],
                 "refit_ratio": round(mid / ((prior or {}).get("mid") or mid), 6)
                 if prior and prior.get("mid") else refit_meta.get("median_ratio"),
                 "n_rows": refit_meta.get("n_rows"),
                 "median_ratio": refit_meta.get("median_ratio"),
                 "actor": refit_meta.get("actor")})
    data["history"] = hist
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
    layer = read_overrides(p)
    layer["applied"] = {"catalog_id": cid, **entry}
    return layer


_LAYER_COMMENT = (
    "Company labor-norm override layer (phase E4 actuals loop). DATA-DRIVEN:"
    " romtool/estimate.py applies these low/high unit minutes on top of"
    " config/labor_units.json for every compute - the engine prefers the"
    " override card. Rows are written ONLY by POST"
    " /estimates/{id}/actuals/apply-refit with owner-approved numbers;"
    " propose-refit NEVER writes here (proposals are review-only).")
