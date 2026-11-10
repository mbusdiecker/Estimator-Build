"""Estimate governance + procurement layer (phase E2).

Pure functions over the estimate definition/recap and the governance config
(config/governance.json) + commercial templates (config/commercial_draft.json) --
no SQL here; app/db.py owns storage and app/routes.py owns the endpoint wiring.

Blocks:

1. State machine  draft->review->approved->sent->superseded, transitions from
   config, illegal ones rejected (API maps to 409). Revisions are allowed only
   in states_allowing_revision; register edits only in
   states_allowing_editor_changes.

2. Revision diffs. compute_diff() compares two definition/recap snapshots by
   stable line_id ("<location>:<catalog_id>", estimate-level lines bare,
   duplicates suffixed #2/#3) and by definition meta paths; emits the audit's
   changed_paths plus a one-line summary.

3. Assumption register. auto_assumption_targets() picks every recap line whose
   row kind is a labor norm (catalog 'minutes') and whose unit card is
   E-flagged; the text is "<item> - <reference>". 0-minute quote/cost-carry
   rows are procurement items, not norms -> quote register, not here.

4. Quote register date math. binding_until is INCLUSIVE: a received/bound
   quote stays binding through that date and reads "expired" strictly after.
   Drafts never expire (they are not vendor commitments) but a lapsed draft is
   flagged. Every expired quote yields exactly ONE warning (chain of elifs).
   Bid tab groups quotes by scope (linked line_ids or whole estimate), keeps
   only bid_tab_quote_statuses for low/median/high stats, and compares against
   the estimate carry (wrapped labor + line-risk contingency + idle-cost
   carry, mid band; whole-estimate quotes compare against the grand total).

5. Inclusions/exclusions sheet. Templates from commercial_draft.json + lines
   derived from the definition (schedule/travel/downtime/supervision/
   power-plant/quality markers) + verbatim vendor-quote terms (only active
   received/bound quotes; expired quotes get an exclusion notice instead).
   Renders via the zero-dep xlsx.py and pdf.py writers.
"""

import csv
import datetime
import io
import json
import os
import statistics

from romtool.pdf import encode_pdf, wrap_text
from romtool.xlsx import encode_xlsx

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_MODULE_DIR)
DEFAULT_GOVERNANCE_PATH = os.path.join(_REPO_ROOT, "config", "governance.json")
DEFAULT_COMMERCIAL_PATH = os.path.join(_REPO_ROOT, "config", "commercial_draft.json")

QUOTE_EXPIRED = "QUOTE_EXPIRED"
QUOTE_BINDING_SOON = "QUOTE_BINDING_SOON"
QUOTE_DRAFT_LAPSED = "QUOTE_DRAFT_LAPSED"


class GovernanceError(ValueError):
    """Payload/validation rejection; .errors is a list (API maps to 422)."""

    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class StateRuleError(GovernanceError):
    """Illegal state/status transition (API maps to 409)."""


# ------------------------------------------------------------------ config

def load_governance_config(path=None):
    p = path or os.environ.get("ROMTOOL_GOVERNANCE") or DEFAULT_GOVERNANCE_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_commercial_draft(path=None):
    p = path or os.environ.get("ROMTOOL_COMMERCIAL_DRAFT") or DEFAULT_COMMERCIAL_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ----------------------------------------------------------- state machine

def next_states(state, gov):
    return list(gov["estimate_state_transitions"].get(state, []))


def assert_estimate_transition(cur_state, to_state, gov):
    """Raise StateRuleError on an illegal estimate-state transition."""
    if to_state not in gov["estimate_states"]:
        raise GovernanceError("unknown estimate state %r (known: %s)"
                              % (to_state, gov["estimate_states"]))
    allowed = next_states(cur_state, gov)
    if to_state not in allowed:
        raise StateRuleError("illegal state transition %s -> %s (allowed from"
                             " '%s': %s)" % (cur_state, to_state, cur_state,
                                             allowed or "nothing (terminal)"))


def assert_transition_rules(status, to_status, transitions, known):
    """Same rule engine for the register status machines (assumptions,
    quotes): unknown target -> GovernanceError; illegal -> StateRuleError."""
    if to_status not in known:
        raise GovernanceError("unknown status %r (known: %s)" % (to_status, known))
    allowed = list(transitions.get(status, []))
    if to_status not in allowed:
        raise StateRuleError("illegal status transition %s -> %s (allowed from"
                             " '%s': %s)" % (status, to_status, status,
                                             allowed or "nothing (terminal)"))


# ---------------------------------------------------------------- line ids

def line_key_of(ln):
    """line_id of a recap line, tolerant of v0.4 blobs without one."""
    if ln.get("line_id"):
        return ln["line_id"]
    loc = ln.get("location_id")
    cid = ln.get("catalog_id")
    return cid if not loc else "%s:%s" % (loc, cid)


def line_carry_usd(ln):
    """What the estimate carries for a line, mid band: wrapped labor +
    line-risk contingency + idle-cost carry (cost-carry rows only)."""
    carry = ln["labor_usd"]["total"]["mid"] + ln["contingency_usd"]["mid"]
    carry += ln.get("cost_carry_usd", {"mid": 0.0})["mid"]
    return round(carry, 6)


# ------------------------------------------------------------------- diffs

def _qty_changed(a, b):
    try:
        return abs(float(a) - float(b)) > 1e-9
    except (TypeError, ValueError):
        return a != b


def _walk_meta(old, new):
    """(path, old, new) tuples for definition-level setting changes."""
    out = []
    for k in ("name",):
        if (old.get(k) or None) != (new.get(k) or None):
            out.append((k, old.get(k), new.get(k)))
    so, sn = old.get("schedule") or {}, new.get("schedule") or {}
    for k in ("night", "weekend"):
        if bool(so.get(k)) != bool(sn.get(k)):
            out.append(("schedule.%s" % k, bool(so.get(k)), bool(sn.get(k))))
    co, cn = old.get("crew") or {}, new.get("crew") or {}
    for k in ("crews", "techs", "foremen", "hours_per_day", "days_per_week"):
        if co.get(k) != cn.get(k):
            out.append(("crew.%s" % k, co.get(k), cn.get(k)))
    for k in ("travel_moves", "downtime_windows", "rate_overrides"):
        if k in old or k in new:
            ov, nv = old.get(k), new.get(k)
            if (ov or None) != (nv or None):
                out.append((k, ov, nv))
    lo = {l.get("id"): l for l in old.get("locations", []) if isinstance(l, dict)}
    ln = {l.get("id"): l for l in new.get("locations", []) if isinstance(l, dict)}
    lo = {k: v for k, v in lo.items() if k is not None}
    ln = {k: v for k, v in ln.items() if k is not None}
    for lid in sorted(set(lo) - set(ln)):
        out.append(("locations.%s" % lid, "<location removed>", None))
    for lid in sorted(set(ln) - set(lo)):
        out.append(("locations.%s" % lid, None, "<location added>"))
    for lid in sorted(set(lo) & set(ln)):
        if lid is None:
            continue
        if lo[lid].get("access") != ln[lid].get("access"):
            out.append(("locations.%s.access" % lid,
                        lo[lid].get("access"), ln[lid].get("access")))
        if (lo[lid].get("conditions") or []) != (ln[lid].get("conditions") or []):
            out.append(("locations.%s.conditions" % lid,
                        lo[lid].get("conditions"), ln[lid].get("conditions")))
        if (lo[lid].get("power_plant") or None) != (ln[lid].get("power_plant") or None):
            out.append(("locations.%s.power_plant" % lid,
                        lo[lid].get("power_plant"), ln[lid].get("power_plant")))
    return out


def compute_diff(old_def, old_recap, new_def, new_recap):
    """Revision diff summary: changed lines by stable line_id, definition
    meta changes, totals drift, the audit changed_paths list, one summary
    sentence. Pure; used by PATCH /estimates/{id} -> revision N+1."""
    old_by_key = {line_key_of(l): l for l in old_recap.get("lines", [])}
    new_by_key = {line_key_of(l): l for l in new_recap.get("lines", [])}
    added = sorted(set(new_by_key) - set(old_by_key))
    removed = sorted(set(old_by_key) - set(new_by_key))
    lines_changed = []
    for k in sorted(set(old_by_key) & set(new_by_key)):
        o, n = old_by_key[k], new_by_key[k]
        ch = {}
        if _qty_changed(o.get("qty"), n.get("qty")):
            ch["qty"] = {"old": o.get("qty"), "new": n.get("qty")}
        if abs(o["hours"]["mid"] - n["hours"]["mid"]) > 1e-6:
            ch["hours_mid"] = {"old": round(o["hours"]["mid"], 4),
                               "new": round(n["hours"]["mid"], 4)}
        if ch:
            lines_changed.append({"line_key": k, "catalog_id": o.get("catalog_id"),
                                  "location_id": o.get("location_id"),
                                  "changes": ch})
    meta_changed = [{"path": p, "old": ov, "new": nv}
                    for p, ov, nv in _walk_meta(old_def or {}, new_def or {})]
    lo = {l.get("id") for l in (old_def or {}).get("locations", []) if isinstance(l, dict) and l.get("id")}
    lnn = {l.get("id") for l in (new_def or {}).get("locations", []) if isinstance(l, dict) and l.get("id")}

    o_tot, n_tot = old_recap.get("totals", {}).get("hours", {}).get("mid"), \
        new_recap["totals"]["hours"]["mid"]
    o_gt, n_gt = old_recap.get("cost", {}).get("grand_total_usd", {}).get("mid"), \
        new_recap["cost"]["grand_total_usd"]["mid"]
    totals = {"hours_mid": {"old": o_tot, "new": n_tot},
              "grand_total_usd_mid": {"old": o_gt, "new": n_gt}}

    changed_paths = []
    for k in added:
        changed_paths.append("lines.%s" % k)
    for k in removed:
        changed_paths.append("lines.%s" % k)
    for c in lines_changed:
        changed_paths.append("lines.%s.%s"
                             % (c["line_key"], "+".join(sorted(c["changes"]))))
    changed_paths.extend(m["path"] for m in meta_changed)
    if o_tot is not None and abs(o_tot - n_tot) > 1e-9:
        changed_paths.append("totals.hours_mid")
        changed_paths.append("totals.grand_total_usd_mid")

    if not (added or removed or lines_changed or meta_changed):
        summary = ("No definition changes: revision records the re-compute "
                   "(%d lines unchanged)." % len(new_by_key))
    else:
        summary = ("Revision: +%d / -%d lines, %d line(s) re-priced, "
                   "%d setting change(s); crew-hours mid %s -> %s; grand total"
                   " mid $%s -> $%s."
                   % (len(added), len(removed), len(lines_changed),
                      len(meta_changed),
                      ("%0.2f" % o_tot if o_tot is not None else "n/a"),
                      "%0.2f" % n_tot,
                      ("%0.2f" % o_gt if o_gt is not None else "n/a"),
                      "%0.2f" % n_gt))
    return {
        "lines_added": added,
        "lines_removed": removed,
        "lines_changed": lines_changed,
        "meta_changed": meta_changed,
        "locations_added": sorted(lnn - lo),
        "locations_removed": sorted(lo - lnn),
        "totals": totals,
        "changed_paths": changed_paths,
        "summary": summary,
    }


# ------------------------------------------------------- assumption register

def assumption_text(ln):
    text = ln.get("item") or ""
    ref = ln.get("reference") or ""
    return "%s - %s" % (text, ref) if ref else text


def auto_assumption_targets(recap_lines, gov=None):
    """One register row per labor-norm line whose unit card is E-flagged."""
    gov = gov or load_governance_config()
    quality = gov.get("auto_assumption_source_quality", "E")
    out = []
    for ln in recap_lines:
        if ln.get("row_kind") == "minutes" and ln.get("source_quality") == quality:
            out.append({"line_key": line_key_of(ln), "catalog_id": ln["catalog_id"],
                        "location_id": ln.get("location_id"),
                        "text": assumption_text(ln)})
    return out


# ---------------------------------------------------------- quote date math

def _parse_date(value):
    if isinstance(value, datetime.date):
        return value
    try:
        s = str(value or "").strip()
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        raise GovernanceError(
            "binding_until must be an ISO date YYYY-MM-DD, got %r" % (value,))


def quote_days_to_binding(quote, today):
    return (_parse_date(quote["binding_until"]) - today).days


def effective_quote_status(quote, today=None):
    """binding_until inclusive: received/bound stay binding THROUGH the date
    and flip to 'expired' the day after. Drafts never auto-expire."""
    today = today or datetime.date.today()
    status = quote["status"]
    if status in ("received", "bound") and \
            _parse_date(quote["binding_until"]) < today:
        return "expired"
    return status


def quote_view(quote, today=None):
    today = today or datetime.date.today()
    q = dict(quote)
    q["effective_status"] = effective_quote_status(q, today)
    q["days_to_binding"] = quote_days_to_binding(q, today)
    return q


def quote_warnings(quotes, today=None, gov=None):
    """Exactly one warning per quote per condition kind (expired warns once)."""
    gov = gov or load_governance_config()
    today = today or datetime.date.today()
    warn_days = int(gov["quote_binding_warn_days"])
    out = []
    for q in quotes:
        qid, vendor = q.get("id"), q.get("vendor") or "?"
        scope = (q.get("scope") or "")[:70]
        status = q["status"]
        bu = _parse_date(q["binding_until"])
        if status in ("received", "bound") and bu < today:
            out.append({
                "code": QUOTE_EXPIRED, "quote_id": qid, "vendor": vendor,
                "message": "Quote #%s from %s (%s) EXPIRED: binding_until %s has"
                           " passed; its price is no longer binding and it is"
                           " excluded from bid-tab stats."
                           % (qid, vendor, scope, bu.isoformat())})
        elif status in ("received", "bound"):
            days = (bu - today).days
            if days <= warn_days:
                out.append({
                    "code": QUOTE_BINDING_SOON, "quote_id": qid, "vendor": vendor,
                    "message": "Quote #%s from %s (%s) binding window closes %s"
                               " (%d day(s) left; warn threshold %d)."
                               % (qid, vendor, scope, bu.isoformat(), days,
                                  warn_days)})
        elif status == "draft" and bu < today:
            out.append({
                "code": QUOTE_DRAFT_LAPSED, "quote_id": qid, "vendor": vendor,
                "message": "Draft quote #%s from %s (%s) was never issued and its"
                           " binding_until %s already passed."
                           % (qid, vendor, scope, bu.isoformat())})
    return out


# --------------------------------------------------------- quote validation

def validate_quote_payload(payload, known_line_ids, gov=None, creating=False):
    """Normalize + validate a quote create/update payload; raises
    GovernanceError (-> 422) with collected messages."""
    gov = gov or load_governance_config()
    errors = []

    def _str_list(key, val):
        if val is None:
            return []
        if not isinstance(val, list) or any(not isinstance(x, str) for x in val):
            errors.append("%s must be a list of strings" % key)
            return None
        return val

    out = {}
    if creating or "vendor" in payload:
        v = payload.get("vendor")
        if not isinstance(v, str) or not v.strip():
            errors.append("vendor must be a non-empty string")
        else:
            out["vendor"] = v.strip()
    if creating or "scope" in payload:
        s = payload.get("scope")
        if not isinstance(s, str) or not s.strip():
            errors.append("scope must be a non-empty string")
        else:
            out["scope"] = s.strip()
    if creating or "amount" in payload:
        try:
            a = float(payload.get("amount"))
            if a < 0:
                raise ValueError()
        except (TypeError, ValueError):
            errors.append("amount must be a number >= 0, got %r"
                          % payload.get("amount"))
        else:
            out["amount"] = a
    if creating or "currency" in payload:
        c = payload.get("currency") or gov["default_quote_currency"]
        if not isinstance(c, str) or not c.strip():
            errors.append("currency must be a non-empty string")
        else:
            out["currency"] = c.strip()
    if creating or "binding_until" in payload:
        if "binding_until" not in payload or not payload.get("binding_until"):
            if creating:
                errors.append("binding_until date is required (YYYY-MM-DD)")
        else:
            try:
                out["binding_until"] = _parse_date(payload["binding_until"])\
                    .isoformat()
            except GovernanceError as exc:
                errors.append(str(exc))
    for key in ("inclusions", "exclusions", "linked_line_ids"):
        if creating or key in payload:
            vals = _str_list(key, payload.get(key))
            if vals is not None:
                out[key] = vals
    links = out.get("linked_line_ids")
    if links and creating or (links and "linked_line_ids" in payload):
        unknown = sorted(set(links) - set(known_line_ids))
        if unknown:
            errors.append("linked_line_ids not in this estimate: %s (known ids"
                          " look like '<location>:<catalog_id>')"
                          % ", ".join(unknown))
            out.pop("linked_line_ids", None)
    if creating or "status" in payload:
        st = payload.get("status") or ("draft" if creating else None)
        if creating:
            if st not in gov["quote_statuses"]:
                errors.append("status %r not in %s" % (st, gov["quote_statuses"]))
            else:
                out["status"] = st
        elif "status" in payload:
            errors.append("quote status changes go through "
                          "POST /estimates/{id}/quotes/{qid}/status")
    if "notes" in payload and not (payload.get("notes") is None
                                   or isinstance(payload.get("notes"), str)):
        errors.append("notes must be a string")
    if errors:
        raise GovernanceError(errors)
    if "notes" in payload:
        out["notes"] = payload.get("notes")
    return out


# ------------------------------------------------------------------ bid tab

def bid_tab(quotes, recap, today=None, gov=None):
    """Bid-tab comparison: quotes grouped by linked line ids (or whole
    estimate), low/median/high over the config's bid-able statuses, gap
    analysis vs the estimate carry, plus the standard expiry warnings."""
    gov = gov or load_governance_config()
    today = today or datetime.date.today()
    lines_by_key = {line_key_of(l): l for l in recap["lines"]}

    def _carry_for(links):
        unknown = [k for k in links if k not in lines_by_key]
        known = [k for k in links if k in lines_by_key]
        if links:
            carry = round(sum(line_carry_usd(lines_by_key[k]) for k in known), 6)
        else:
            carry = round(recap["cost"]["grand_total_usd"]["mid"], 6)
        return carry, unknown

    groups = {}
    for q in quotes:
        links = sorted(q.get("linked_line_ids") or [])
        key = ",".join(links) if links else "(whole estimate)"
        groups.setdefault(key, []).append(quote_view(q, today))

    out_groups = []
    warnings = quote_warnings(quotes, today, gov)
    unknown_all = set()
    for key in sorted(groups):
        q_views = sorted(groups[key], key=lambda q: (q["amount"], q["vendor"] or ""))
        links = [] if key == "(whole estimate)" else key.split(",")
        carry, unknown = _carry_for(links)
        unknown_all.update("%s (quote #%s)" % (k, q["id"])
                           for q in q_views for k in unknown)
        active = [q for q in q_views
                  if q["effective_status"] in gov["bid_tab_quote_statuses"]]
        amounts = [q["amount"] for q in active]
        bid = None
        if amounts:
            low, high = amounts[0], amounts[-1]
            mid = round(float(statistics.median(amounts)), 6)
            gaps = {"low": round(carry - low, 6), "mid": round(carry - mid, 6),
                    "high": round(carry - high, 6)}
            pct = {side: (round(gaps[side] / amt * 100.0, 4) if amt else None)
                   for side, amt in (("low", low), ("mid", mid), ("high", high))}
            bid = {"low": low, "mid": mid, "high": high,
                   "n_quotes": len(amounts), "gaps_vs_carry_usd": gaps,
                   "gaps_vs_carry_pct": pct}
        notes = []
        if links and unknown:
            notes.append("linked line ids not in this estimate revision: %s"
                         % ", ".join(unknown))
        if amounts:
            low, high = amounts[0], amounts[-1]
            if carry < low - 1e-9:
                notes.append("estimate carry $%0.2f is BELOW the lowest active"
                             " quote by $%0.2f - check quote exclusions before"
                             " taking the low number"
                             % (carry, low - carry))
            elif carry > high + 1e-9:
                notes.append("estimate carry $%0.2f is ABOVE the highest active"
                             " quote by $%0.2f - taking the low bid saves"
                             " $%0.2f" % (carry, carry - high, carry - low))
            elif low and (carry - low) / low > 0.10:
                notes.append("estimate carry sits %0.1f%% above the lowest"
                             " active quote"
                             % ((carry - low) / low * 100.0))
        out_groups.append({
            "scope": key, "line_ids": links, "n_quotes": len(q_views),
            "quotes": q_views, "bid": bid, "estimate_carry_usd": carry,
            "notes": notes})
    if unknown_all:
        warnings.append({"code": "QUOTE_LINK_UNKNOWN", "quote_id": None,
                         "vendor": None,
                         "message": "linked_line_ids not in this estimate"
                                    " revision: %s" % "; ".join(sorted(unknown_all))})
    return {"estimate": recap.get("name"),
            "as_of": today.isoformat(),
            "carry_basis": "wrapped labor + line-risk contingency + idle-cost"
                           " carry (mid); whole-estimate quotes compare against"
                           " the grand total",
            "groups": out_groups, "warnings": warnings}


# --------------------------------------------- inclusions/exclusions sheet

def build_commercial_sheet(recap, quotes, estimating, draft, today=None):
    """Merge default templates (config/commercial_draft.json) with lines
    derived from the definition and active vendor-quote terms."""
    today = today or datetime.date.today()
    inclusions = [{"text": t, "source": "template"}
                  for t in draft["inclusions_default"]]
    exclusions = [{"text": t, "source": "template"}
                  for t in draft["exclusions_default"]]
    derived_inc, derived_exc = [], []

    sched = recap.get("schedule") or {}
    sf = estimating["schedule_factors"]
    if sched.get("night"):
        derived_inc.append({"text": "Night-shift differential applied to"
                            " night-scheduled work (M02 %.2f-%.2fx of base"
                            " hours)" % (sf["night"]["low"], sf["night"]["high"]),
                            "source": "derived", "ref": "M02"})
    if sched.get("weekend"):
        derived_inc.append({"text": "Weekend differential applied to"
                            " weekend-scheduled work (M02 %.2f-%.2fx)"
                            % (sf["weekend"]["low"], sf["weekend"]["high"]),
                            "source": "derived", "ref": "M02"})
    moves = recap["totals"].get("travel_moves", 0)
    if moves:
        derived_inc.append({"text": "%d in-venue travel move(s) between listed"
                            " locations priced under the M05 crew-travel adder"
                            % moves, "source": "derived", "ref": "M05"})
    carry_lines = [l for l in recap["lines"] if l.get("kind") == "cost_carry"]
    if carry_lines:
        derived_inc.append({"text": "%d blacked-out downtime window(s)"
                            " (%d idle day(s)) priced as idle crew-day carry"
                            " (M01): $%s total - schedule risk carried, not"
                            " hours"
                            % (len(carry_lines),
                               sum(l["qty"] for l in carry_lines),
                               format(recap["cost"]["downtime_usd"], ",.2f")),
                            "source": "derived", "ref": "M01"})
    crew = recap.get("crew") or {}
    derived_inc.append({"text": "Supervision wrap included on every wrapped"
                        " line: N01 working-foreman %.3f task-h per task-hour,"
                        " N02 non-working supervision %.1f%% of direct hours"
                        % (crew.get("n01_foreman_ratio", 0.0),
                           crew.get("n02_supervision_pct", 0.0) * 100.0),
                        "source": "derived", "ref": "N01/N02"})
    if any((l.get("zone") or "").startswith("O-") for l in recap["lines"]):
        derived_inc.append({"text": "48VDC DC power plant lines auto-sized per"
                            " estimating config (IFC 12-hr runtime battery"
                            " strings, N+1 rectifiers)", "source": "derived",
                            "ref": "O-zone"})
    derived_inc.append({"text": "Labor norms carry provenance quality flags"
                        " (P published / E estimated / G practice); every"
                        " E-grade norm is tracked as an open row in this"
                        " estimate's assumption register",
                        "source": "derived", "ref": "assumptions"})
    derived_exc.append({"text": "Work outside the %d listed location(s);"
                        " other areas are excluded from this bid"
                        % len((recap.get("definition") or {}).get("locations", [])),
                        "source": "derived", "ref": "definition"})
    for q in quotes:
        eff = effective_quote_status(q, today)
        if eff in ("received", "bound"):
            tag = "vendor quote #%s from %s (%s)" % (q["id"], q["vendor"], eff)
            for t in q.get("inclusions") or []:
                derived_inc.append({"text": "%s [%s]" % (t, tag),
                                    "source": "quote", "ref": "quote#%s" % q["id"]})
            for t in q.get("exclusions") or []:
                derived_exc.append({"text": "%s [%s]" % (t, tag),
                                    "source": "quote", "ref": "quote#%s" % q["id"]})
        elif eff == "expired":
            derived_exc.append({"text": "Vendor quote #%s from %s expired"
                                " (binding_until %s); its pricing and terms are"
                                " not honored in this draft"
                                % (q["id"], q["vendor"], q["binding_until"]),
                                "source": "quote_expired",
                                "ref": "quote#%s" % q["id"]})
    return {
        "title": draft.get("sheet_title", "Inclusions & Exclusions"),
        "as_of": today.isoformat(),
        "estimate_name": recap.get("name"),
        "inclusions": inclusions + derived_inc,
        "exclusions": exclusions + derived_exc,
        "quote_summary": [quote_view(q, today) for q in quotes],
        "notes": [draft.get("inclusions_note"), draft.get("exclusions_note")],
    }


def commercial_xlsx(sheet, gov_view):
    """-> inclusions/exclusions workbook bytes (zero-dep xlsx writer)."""
    ie_rows = [
        [sheet["title"]],
        ["estimate", sheet["estimate_name"], "as_of", sheet["as_of"]],
        ["state", gov_view["state"], "revision", gov_view["revision"]],
        [],
        ["INCLUSIONS", len(sheet["inclusions"]), "item(s)"],
        ["#", "text", "source", "ref"],
    ]
    for i, it in enumerate(sheet["inclusions"], 1):
        ie_rows.append([i, it["text"], it["source"], it.get("ref", "")])
    ie_rows += [[], ["EXCLUSIONS", len(sheet["exclusions"]), "item(s)"],
                ["#", "text", "source", "ref"]]
    for i, it in enumerate(sheet["exclusions"], 1):
        ie_rows.append([i, it["text"], it["source"], it.get("ref", "")])
    ie_rows += [[], ["VENDOR QUOTES (register summary)"],
                ["id", "vendor", "scope", "amount", "currency", "binding_until",
                 "status", "effective_status", "days_to_binding", "warnings"]]
    for q in sheet["quote_summary"]:
        warns = [w["message"] for w in (gov_view.get("quote_warnings") or [])
                 if w.get("quote_id") == q["id"]]
        ie_rows.append([q["id"], q["vendor"], q["scope"], q["amount"],
                        q["currency"], q["binding_until"], q["status"],
                        q["effective_status"], q["days_to_binding"],
                        " | ".join(warns)])
    quote_rows = [["id", "vendor", "scope", "amount", "currency",
                   "binding_until", "status", "effective_status",
                   "days_to_binding", "linked_line_ids", "inclusions",
                   "exclusions", "notes"]]
    for q in sheet["quote_summary"]:
        quote_rows.append([q["id"], q["vendor"], q["scope"], q["amount"],
                           q["currency"], q["binding_until"], q["status"],
                           q["effective_status"], q["days_to_binding"],
                           "; ".join(q.get("linked_line_ids") or []),
                           "; ".join(q.get("inclusions") or []),
                           "; ".join(q.get("exclusions") or []),
                           q.get("notes") or ""])
    return encode_xlsx([
        {"name": "Inclusions_Exclusions", "rows": ie_rows},
        {"name": "Quotes", "rows": quote_rows},
    ])


def commercial_pdf(sheet, gov_view):
    """-> inclusions/exclusions PDF bytes (zero-dep pdf writer), flowing
    across pages with 10 pt text, ~0.55 em/char wrap."""
    margin, top, bottom = 50.0, 742.0, 56.0
    width = 612.0 - 2 * margin
    pages, ops, y = [], [], top

    def _new_page():
        nonlocal ops, y
        if ops:
            pages.append(ops)
        ops, y = [], top

    def _need(h):
        nonlocal y
        if y - h < bottom:
            _new_page()

    def _text(x, size, text, bold=False):
        ops.append(("text", x, y, size, text, bold))
        return wrap_text(text, size, width - (x - margin))

    _text(margin, 15, sheet["title"], True)
    y -= 16
    _text(margin, 10, "Estimate: %s   |   state: %s   |   revision: %s   |   as-of: %s"
          % (sheet["estimate_name"], gov_view["state"], gov_view["revision"],
             sheet["as_of"]))
    y -= 18

    def _section(label, items, bullet):
        nonlocal y
        _need(30)
        _text(margin, 12, label, True)
        y -= 6
        ops.append(("rule", margin, y, width))
        y -= 12
        for i, it in enumerate(items, 1):
            head = "%s%d.  " % (bullet, i)
            first = True
            for line in wrap_text(it["text"], 10, width - 24):
                _need(13)
                if first:
                    _text(margin, 10, head + line)
                    first = False
                else:
                    _text(margin + 24, 10, line)
                y -= 12
            y -= 2
        y -= 6

    _section("INCLUSIONS", sheet["inclusions"], "")
    _section("EXCLUSIONS", sheet["exclusions"], "")
    if sheet["quote_summary"]:
        _need(40)
        _text(margin, 12, "VENDOR QUOTES (register summary)", True)
        y -= 6
        ops.append(("rule", margin, y, width))
        y -= 12
        for q in sheet["quote_summary"]:
            warn = "  [EXPIRED - excluded from bid tab]" \
                if q["effective_status"] == "expired" else ""
            _need(40)
            _text(margin, 10, "%s - %s %s  (%s, binding until %s%s)"
                  % (q["vendor"], format(q["amount"], ",.2f"), q["currency"],
                     q["effective_status"], q["binding_until"], warn), True)
            y -= 12
            for line in wrap_text("scope: %s" % q["scope"], 9, width):
                _need(11)
                _text(margin + 10, 9, line)
                y -= 11
            if q.get("linked_line_ids"):
                _need(11)
                _text(margin + 10, 9, "lines: %s"
                      % ", ".join(q["linked_line_ids"]))
                y -= 11
            y -= 4
    if ops:
        pages.append(ops)
    return encode_pdf(pages or [[("text", margin, 720, 12,
                                  "(empty commercial draft)", False)]])


# ------------------------------------------------------------ assumptions csv

ASSUMPTION_CSV_HEADER = ["id", "revision", "catalog_id", "location_id",
                         "line_key", "assumption", "owner", "status",
                         "review_date", "created_at", "updated_at", "origin"]


def assumptions_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(ASSUMPTION_CSV_HEADER)
    for r in rows:
        w.writerow([r["id"], r["revision"], r["catalog_id"],
                    r["location_id"], r["line_key"], r["text"], r["owner"],
                    r["status"], r["review_date"], r["created_at"],
                    r["updated_at"],
                    "auto" if r.get("line_key") else "manual"])
    return buf.getvalue()
