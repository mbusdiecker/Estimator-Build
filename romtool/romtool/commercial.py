"""Commercial wrap depth (phase E3, v0.6.0).

Pure functions over the estimate definition / recap and the commercial
config (config/commercial.json) -- no SQL; app/db.py owns storage and
app/routes.py owns the endpoint wiring. romtool/estimate.py imports the
wrap helpers (escalation/tax/...) to build the $ chain; the schedule /
cash-flow / scenario helpers here take a computed recap dict as DATA so
this module stays import-light (it never imports romtool.estimate).

Blocks:

1. Wrap chain steps. The recap $ chain applies, in documented order:
   labor+supervision -> downtime idle carry -> small-tools allowance ->
   escalation -> sales/use tax -> insurance/bond -> PM -> OH -> profit ->
   line-risk contingency. wrap_steps() computes the four new commercial
   steps (small tools, escalation, taxes, insurance/bond) from the labor
   base; estimate.py stitches them into PM/OH/profit. Bands: percentages
   apply uniformly, each step on its documented basis.

2. Escalation. One factor per apply rule:
       factor = 1 + days(base_date -> anchor_date)/365 x annual_rate_pct/100
   Anchor resolution: definition.commercial.escalation.anchor_date, else
   the midpoint working day of the with-crews schedule (definition
   start_date present), else escalation is NOT applied (factor 1) with a
   warning. Anchor before base_date never de-escalates (factor 1).

3. Sales/use tax. Each recap line classifies to a line type by catalog
   zone prefix via config zone_line_types (default_line_type fallback);
   taxable_line_types are taxed at rate_pct on their wrapped labor $.
   Shipped default: every zone is labor and labor is exempt -> $0.00 tax
   reported explicitly (no fabricated tax base).

4. Crew schedule. Per-location crew-days = ceil(location wrapped mid
   hours / single-crew daily capacity). A working-day calendar (first
   `days_per_week` weekdays starting Monday) excludes dated blackout
   windows (downtime_windows entries with a start date). Phases are
   claimed whole by a free crew; the same greedy simulation defines the
   early bound (estimate crew count) and the late bound (one crew,
   serial). Overtime is a schedule-level flag against the weekly DT
   threshold (cost still moves through the M02 hour differentials).

5. Cash flow. Milestone % splits (must sum to 100) of the wrapped grand
   total mid band -> a monthly curve. INSTALL spreads across the months
   the installation schedule spans (or ceil(workdays/21) without dates).

6. What-if scenarios. Overlays: access_overrides (per location access
   method), factor_overrides (deep-merged onto the estimating config,
   same mechanism as definition rate_overrides), qty_overrides (set qty
   on an existing definition line), quote_swap (a vendor quote's amount
   replaces the wrapped labor dollars of the named recap lines,
   allocated proportionally by each line's share of the group carry;
   hours/schedule unchanged). scenario_diff() returns only changed lines
   (qty / hours mid / labor mid) plus the totals delta.
"""

import copy
import csv
import datetime
import io
import json
import math
import os

from romtool.pdf import encode_pdf, wrap_text
from romtool.xlsx import encode_xlsx

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_MODULE_DIR)
DEFAULT_COMMERCIAL_PATH = os.path.join(_REPO_ROOT, "config", "commercial.json")

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
ACCESS_METHODS = ("ground", "ladder", "lift", "scissor", "rope")

# wrap chain step codes, in documented order
WRAP_ORDER = ["direct_labor_incl_supervision", "downtime_m01_idle_carry",
              "small_tools_allowance", "escalation", "sales_use_tax",
              "insurance_bond", "pm", "overhead", "profit",
              "line_risk_contingency_n05", "grand_total"]


class CommercialError(ValueError):
    """Commercial config/payload rejection; .errors is a list."""

    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# ------------------------------------------------------------------ config

def load_commercial_config(path=None):
    """Load the commercial constants (config/commercial.json) as a dict."""
    p = path or os.environ.get("ROMTOOL_COMMERCIAL") or DEFAULT_COMMERCIAL_PATH
    with open(p, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    validate_commercial_config(cfg)
    return cfg


def validate_commercial_config(cfg):
    """Fail fast on malformed commercial config (API maps to 500/422)."""
    errors = []
    milestones = (cfg.get("cash_flow") or {}).get("milestones")
    if not milestones:
        errors.append("commercial.cash_flow.milestones missing")
    else:
        total = sum(float(m.get("pct", 0)) for m in milestones)
        if abs(total - 100.0) > 1e-6:
            errors.append("commercial.cash_flow milestone pcts sum to %s, "
                          "not 100" % total)
    if not (cfg.get("profit_pct_by_risk_tier") or {}):
        errors.append("commercial.profit_pct_by_risk_tier missing")
    if not (cfg.get("escalation") or {}).get("indices"):
        errors.append("commercial.escalation.indices missing")
    if errors:
        raise CommercialError(errors)


def parse_iso_date(value, label):
    try:
        return datetime.date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise CommercialError("%s must be an ISO date (YYYY-MM-DD), got %r"
                              % (label, value))


# -------------------------------------------------------------- escalation

def escalation_meta(commercial, definition, anchor_date=None):
    """Resolve the escalation index + anchor for one estimate.

    `anchor_date` (passed by estimate.compute_estimate when a start_date
    made a schedule midpoint possible) wins over nothing explicit: the
    definition's commercial.escalation.anchor_date always wins."""
    esc_cfg = commercial["escalation"]
    esc_defn = ((definition or {}).get("commercial") or {}).get("escalation") or {}
    idx_key = esc_defn.get("index") or esc_cfg["default_index"]
    idx = esc_cfg["indices"].get(idx_key)
    if idx is None:
        raise CommercialError("escalation index %r not in %s"
                              % (idx_key, sorted(esc_cfg["indices"])))
    base = parse_iso_date(idx["base_date"],
                          "escalation.indices.%s.base_date" % idx_key)
    rate = float(idx["annual_rate_pct"])

    anchor, source = None, None
    if esc_defn.get("anchor_date"):
        anchor = parse_iso_date(esc_defn["anchor_date"],
                                "commercial.escalation.anchor_date")
        source = "definition.commercial.escalation.anchor_date"
    elif anchor_date is not None:
        anchor = anchor_date
        source = "schedule midpoint (start_date + work days/2)"

    if anchor is None:
        return {"applied": False, "index": idx_key, "scope": idx["scope"],
                "base_date": base.isoformat(), "annual_rate_pct": rate,
                "anchor_date": None, "anchor_source": None, "days": 0,
                "factor": 1.0,
                "note": "escalation not applied: no anchor date (give the "
                        "definition commercial.escalation.anchor_date or "
                        "start_date)"}

    days = (anchor - base).days
    if days < 0:
        return {"applied": False, "index": idx_key, "scope": idx["scope"],
                "base_date": base.isoformat(), "annual_rate_pct": rate,
                "anchor_date": anchor.isoformat(), "anchor_source": source,
                "days": days, "factor": 1.0,
                "note": "anchor date %s is before base date %s - factor 1 "
                        "(escalation never de-escalates)" % (anchor, base)}
    factor = 1.0 + days / 365.0 * rate / 100.0
    return {"applied": True, "index": idx_key, "scope": idx["scope"],
            "base_date": base.isoformat(), "annual_rate_pct": rate,
            "anchor_date": anchor.isoformat(), "anchor_source": source,
            "days": days, "factor": round(factor, 10),
            "note": "1 + %d/365 x %.2f%% = %.6f" % (days, rate, factor)}


# --------------------------------------------------------------------- tax

def tax_view(recap_lines, commercial):
    """Classify recap lines by catalog zone prefix -> per-band taxable and
    exempt wrapped-labor bases (cost-carry M01 rows are labor dollars, so
    they always land on the exempt side)."""
    rules = commercial["sales_use_tax"]
    taxable_types = set(rules.get("taxable_line_types") or [])
    exempt_types = set(rules.get("exempt_line_types") or [])
    taxable = {b: 0.0 for b in ("low", "mid", "high")}
    exempt = {b: 0.0 for b in ("low", "mid", "high")}
    taxable_ids, exempt_ids = [], []

    def _type_of(zone):
        prefix = (zone or "").split("-", 1)[0]
        return (rules.get("zone_line_types") or {}).get(
            prefix, rules.get("default_line_type", "labor"))

    for ln in recap_lines:
        ltype = _type_of(ln.get("zone"))
        if ltype in taxable_types:
            ids, base = taxable_ids, taxable
        elif ltype in exempt_types:
            ids, base = exempt_ids, exempt
        else:
            continue
        if ln.get("kind") == "minutes":
            for b in ("low", "mid", "high"):
                base[b] += float(ln["labor_usd"]["total"][b])
        elif ln.get("kind") == "cost_carry":
            for b in ("low", "mid", "high"):
                base[b] += float((ln.get("cost_carry_usd") or {}).get(b, 0.0))
        ids.append(ln["line_id"])
    return {"taxable_labor_usd": {b: round(taxable[b], 6)
                                  for b in ("low", "mid", "high")},
            "exempt_labor_usd": {b: round(exempt[b], 6)
                                 for b in ("low", "mid", "high")},
            "taxable_line_ids": sorted(taxable_ids),
            "exempt_line_ids": sorted(exempt_ids)}


# ------------------------------------------------------------- wrap steps

def wrap_steps(labor_bounds, downtime_bounds, recap_lines, commercial,
               definition, esc_meta=None, tax_rate=None):
    """The four commercial wrap steps (v0.6 additions to the $ chain).

    labor_bounds / downtime_bounds: {"low","mid","high"} wrapped labor and
    M01 idle-carry dollars. recap_lines: recap line dicts (for tax
    classification). Bases, exactly as documented in the
    config/commercial.json comment and /how-it-works section 11:
      small_tools = pct x (labor + downtime)
      escalation  = (factor - 1) x (labor + downtime)     [labor base only]
      taxes       = rate x taxable wrapped labor (line type rules)
      insurance   = pct x running subtotal through taxes
    """
    run = {b: labor_bounds[b] + downtime_bounds[b] for b in ("low", "mid", "high")}
    st_pct = float(commercial["small_tools"]["pct"])
    small_tools = {b: round(run[b] * st_pct / 100.0, 6)
                   for b in ("low", "mid", "high")}

    meta = esc_meta or escalation_meta(commercial, definition)
    esc_factor = float(meta["factor"])
    escalation = {b: round(run[b] * (esc_factor - 1.0), 6)
                  for b in ("low", "mid", "high")}

    if tax_rate is None:
        tax_rate = float(commercial["sales_use_tax"]["rate_pct"])
    tv = tax_view(recap_lines, commercial)
    tax_usd = {b: round(tv["taxable_labor_usd"][b] * tax_rate / 100.0, 6)
               for b in ("low", "mid", "high")}

    run_after_tax = {b: run[b] + small_tools[b] + escalation[b] + tax_usd[b]
                     for b in ("low", "mid", "high")}
    ins_pct = float(commercial["insurance_bond"]["pct"])
    insurance = {b: round(run_after_tax[b] * ins_pct / 100.0, 6)
                 for b in ("low", "mid", "high")}

    return {
        "small_tools": {"pct": st_pct, "usd": small_tools,
                        "basis": commercial["small_tools"]["basis"]},
        "escalation": {**meta, "usd": escalation,
                       "basis": "(factor - 1) x (direct labor + M01 idle carry)"},
        "taxes": {"rate_pct": tax_rate, "usd": tax_usd, "tax_view": tv,
                  "basis": "rate x taxable wrapped labor (line type rules)"},
        "insurance_bond": {"pct": ins_pct, "usd": insurance,
                           "basis": commercial["insurance_bond"]["basis"]},
    }


# ------------------------------------------------------- working calendar

def working_weekdays(days_per_week):
    """First `days_per_week` weekdays starting Monday -> weekday ints."""
    n = max(1, min(7, int(days_per_week)))
    return list(range(n))


def blackout_dates(definition, start_date):
    """Dated downtime windows -> set of blackout dates. Windows without a
    start date (or estimates without start_date) block nothing: they still
    price the M01 idle carry but stay off the calendar."""
    out = set()
    for w in (definition or {}).get("downtime_windows") or []:
        if not w.get("start") or start_date is None:
            continue
        s = parse_iso_date(w["start"], "downtime_windows[].start")
        for i in range(max(int(w.get("days", 0)), 0)):
            out.add(s + datetime.timedelta(days=i))
    return out


def _simulate(phases, crews, stream_iter, limit=20000):
    """Greedy crew loading: phases [(location_id, name, crew_days)] in
    definition order are claimed whole by a free crew; a crew burns one
    calendar work day per crew-day. A crew that finishes a phase on day D
    starts its next phase on the NEXT work day (a work day holds one
    crew-day per crew). stream_iter yields work-day values (dates when
    start_date, else running day indices)."""
    spans = {}
    busy = {}            # crew -> {"loc": id, "days_left": n}  (busy today)
    pending = list(phases)
    work_days = []
    gen = iter(stream_iter)
    spent = 0
    while True:
        if not pending and not busy:
            break
        if spent >= limit:
            raise CommercialError("schedule simulation hit the %d work-day "
                                  "limit (check crew plan / blackout windows)"
                                  % limit)
        try:
            day_val = next(gen)
        except StopIteration:
            raise CommercialError("work-day stream exhausted before schedule "
                                  "converged") from None
        spent += 1
        work_days.append(day_val)
        idle_at_start = [c for c in range(1, crews + 1) if c not in busy]
        for c in list(busy):
            busy[c]["days_left"] -= 1
            if not busy[c]["days_left"]:
                s = spans[busy[c]["loc"]]
                s["finish_day"] = len(work_days) - 1
                s["finish"] = day_val
                busy.pop(c)
        for c in idle_at_start:
            if not pending:
                break
            lid, name, cdays = pending.pop(0)
            if int(cdays) <= 0:
                spans[lid] = {"location_id": lid, "name": name,
                              "crew_days": 0, "crew": None,
                              "start_day": None, "start": None,
                              "finish_day": None, "finish": None}
                continue
            spans[lid] = {"location_id": lid, "name": name,
                          "crew_days": int(cdays), "crew": c,
                          "start_day": len(work_days) - 1,
                          "start": day_val}
            if int(cdays) == 1:
                spans[lid]["finish_day"] = len(work_days) - 1
                spans[lid]["finish"] = day_val
            else:
                busy[c] = {"loc": lid, "days_left": int(cdays) - 1}
    return spans, work_days


def _workday_stream(start, weekdays, blackout):
    """Yield work days: dates from `start` (skipping non-working weekdays and
    blackout dates), or bare day indices when start is None."""
    if start is None:
        n = len(weekdays)
        i = 0
        while True:
            if (i % 7) in weekdays:
                yield i
            i += 1
    else:
        d = start
        guard = 0
        while True:
            if d.weekday() in weekdays and d not in blackout:
                yield d
            d += datetime.timedelta(days=1)
            guard += 1
            if guard > 200000:
                raise CommercialError("work-day stream exceeded 200000 days")


def _fmt_day(v):
    return v.isoformat() if isinstance(v, datetime.date) else v


def schedule_phases(recap, per_crew_cap=None):
    """[(location_id, name, crew_days)] from a recap's location rollups
    (estimate-wide derived rows excluded). crew_days ceil against the
    per-crew daily capacity unless given."""
    crew = recap["crew"]
    cap = per_crew_cap if per_crew_cap is not None else \
        int(crew["crew_size_per_crew"]) * float(crew["hours_per_day"])
    out = []
    for roll in recap["location_rollups"]:
        if roll["key"] == "(estimate-wide)":
            continue
        hours = float(roll["hours"]["mid"])
        cdays = int(math.ceil(hours / cap)) if cap > 0 and hours > 0 else 0
        out.append((roll["key"], roll.get("name") or roll["key"], cdays))
    return out


def crew_schedule(recap, definition, commercial, today=None):
    """Full schedule payload for GET /estimates/{id}/schedule."""
    crew = recap["crew"]
    crews = int(crew["crews"])
    crew_size = int(crew["crew_size_per_crew"])
    hpd = float(crew["hours_per_day"])
    dpw = int(crew["days_per_week"])
    per_crew_cap = crew_size * hpd
    weekdays = working_weekdays(dpw)

    start_raw = definition.get("start_date")
    start = parse_iso_date(start_raw, "start_date") if start_raw else None
    blackout = blackout_dates(definition, start)
    phases = schedule_phases(recap)
    has_work = [p for p in phases if p[2] > 0]

    notes = []
    undated = [w for w in (definition.get("downtime_windows") or [])
               if not w.get("start")]
    if undated:
        notes.append("%d downtime window(s) carry no start date: they price "
                     "the M01 idle carry but cannot block the working calendar"
                     % len(undated))
    if start is None:
        notes.append("no definition start_date: the schedule runs on work-day "
                     "indices (dates null); dated windows cannot block")
    if definition.get("start_date"):
        notes.append("per-location crew-days = ceil(location wrapped mid "
                     "hours / %.1f h per-crew daily capacity); this sums "
                     "per-location ceils and can differ by rounding from the "
                     "whole-estimate totals.crew_days" % per_crew_cap)

    early_spans, early_days = _simulate(has_work, crews,
                                        _workday_stream(start, weekdays, blackout))
    late_spans, late_days = _simulate(has_work, 1,
                                      _workday_stream(start, weekdays, blackout))

    locs_out = []
    for roll in recap["location_rollups"]:
        lid = roll["key"]
        if lid == "(estimate-wide)":
            continue
        sp = early_spans.get(lid) or {}
        locs_out.append({
            "location_id": lid, "name": roll.get("name"),
            "hours_mid": round(float(roll["hours"]["mid"]), 6),
            "crew_days": sp.get("crew_days", 0),
            "crew": sp.get("crew"),
            "start_day_index": sp.get("start_day"),
            "finish_day_index": sp.get("finish_day"),
            "start": _fmt_day(sp.get("start")),
            "finish": _fmt_day(sp.get("finish")),
        })

    def _bound(days):
        return {"work_days": len(days),
                "total_location_crew_days": sum(p[2] for p in phases),
                "first_work_day": _fmt_day(days[0]) if days else None,
                "last_work_day": _fmt_day(days[-1]) if days else None,
                "calendar_days": ((days[-1] - days[0]).days + 1)
                if days and start is not None else None}

    ot_cfg = commercial["overtime"]
    threshold = float(ot_cfg["weekly_dt_threshold_hours"])
    dt_mult = float(ot_cfg["dt_multiplier"])
    flagged = []
    if start is not None and early_days:
        week_hours = {}
        for d in early_days:
            wk = d - datetime.timedelta(days=d.weekday())
            week_hours[wk] = week_hours.get(wk, 0.0) + hpd
        for wk in sorted(week_hours):
            if week_hours[wk] > threshold:
                flagged.append({
                    "week_start": wk.isoformat(),
                    "hours_per_crew": round(week_hours[wk], 6),
                    "dt_applies": True,
                    "note": "%.2f scheduled h/crew/week above the %.2f "
                            "threshold; the hours beyond it bill straight -> "
                            "double time (x%s). No extra cost moves through "
                            "the wrap: the M02 differentials remain the "
                            "labor-costing mechanism."
                            % (week_hours[wk], threshold, dt_mult)})
    return {
        "start_date": start.isoformat() if start else None,
        "crews": crews, "crew_size": crew_size,
        "hours_per_day": hpd, "days_per_week": dpw,
        "per_crew_daily_capacity_h": round(per_crew_cap, 6),
        "working_weekdays": [WEEKDAY_NAMES[wd] for wd in weekdays],
        "blackout_dates": sorted(b.isoformat() for b in blackout),
        "locations": locs_out,
        "total_location_crew_days": sum(p[2] for p in phases),
        "duration": {
            "early": _bound(early_days),
            "early_note": "all %d configured crew(s) start at NTP" % crews,
            "late": _bound(late_days),
            "late_note": "single-crew serial schedule (late bound)" if crews > 1
            else "crews=1 equals the configured crew count; early == late",
        },
        "overtime": {"weekly_dt_threshold_hours": threshold,
                     "dt_multiplier": dt_mult,
                     "flagged_weeks": flagged,
                     "note": ot_cfg["note"]},
        "notes": notes,
    }


def schedule_midpoint_date(definition, per_location_hours, crews,
                           crew_size, hours_per_day, days_per_week):
    """Escalation anchor fallback: the ceil(W/2)-th working day of the
    with-crews schedule, W approximated as ceil(total crew-days / crews).
    per_location_hours: [(location_id, wrapped_hours_mid)]."""
    start_raw = definition.get("start_date")
    if not start_raw:
        return None
    start = parse_iso_date(start_raw, "start_date")
    blackout = blackout_dates(definition, start)
    weekdays = set(working_weekdays(days_per_week))
    cap = crew_size * float(hours_per_day)
    total = sum(int(math.ceil(h / cap)) for _, h in per_location_hours
                if h > 0)
    if total <= 0:
        return start
    target = max(int(math.ceil(total / max(crews, 1))), 1)
    half = max(int(math.ceil(target / 2.0)), 1)

    used, last, d, guard = 0, start, start, 0
    while used < half and guard < 200000:
        if d.weekday() in weekdays and d not in blackout:
            used += 1
            last = d
        d += datetime.timedelta(days=1)
        guard += 1
    if used < half:
        raise CommercialError("escalation midpoint walk exceeded the work-day "
                              "limit")
    return last


# --------------------------------------------------------------- cash flow

def cash_curve(recap, definition, commercial, today=None):
    """Monthly cash curve: milestone % splits (config, sum = 100) of the
    wrapped grand total (mid band). INSTALL spreads across the calendar
    months the early schedule spans (dates) or ceil(work days/21) months."""
    milestones = commercial["cash_flow"]["milestones"]
    grand = float(recap["cost"]["grand_total_usd"]["mid"])

    sched = crew_schedule(recap, definition, commercial)
    schedule_days = max(sched["duration"]["early"]["work_days"], 0)

    start = None
    if definition.get("start_date"):
        start = parse_iso_date(definition["start_date"], "start_date")

    def month_label(idx):
        if start is None:
            return "M%d" % idx
        m = start.month - 1 + idx
        return "%04d-%02d" % (start.year + m // 12, m % 12 + 1)

    if start is not None and sched["duration"]["early"]["first_work_day"]:
        first = datetime.date.fromisoformat(
            sched["duration"]["early"]["first_work_day"])
        last = datetime.date.fromisoformat(
            sched["duration"]["early"]["last_work_day"])
        install_start = (first.year - start.year) * 12 + (first.month - start.month)
        install_months = max((last.year - first.year) * 12 +
                             (last.month - first.month) + 1, 1)
    else:
        install_start = 1
        wpm = float(commercial["cash_flow"].get("workdays_per_month_default", 21))
        install_months = max(int(math.ceil(schedule_days / wpm)), 1)

    flows = {}

    def add(idx, code, pct):
        flows.setdefault(idx, {})[code] = round(grand * pct / 100.0, 6)

    last_used = 0
    for ms in milestones:
        pct = float(ms["pct"])
        pat = ms["pattern"]
        if pat == "upfront":
            add(0, ms["code"], pct)
            last_used = max(last_used, 0)
        elif pat == "months":
            if "offset_from_install_end" in ms:
                off = install_start + install_months - 1 + \
                    int(ms["offset_from_install_end"])
            else:
                off = int(ms.get("offset_months", 0))
            n = max(int(ms.get("months", 1)), 1)
            for k in range(n):
                add(off + k, ms["code"], pct / n)
            last_used = max(last_used, off + n - 1)
        elif pat == "schedule":
            n = max(install_months, 1)
            for k in range(n):
                add(install_start + k, ms["code"], pct / n)
            last_used = max(last_used, install_start + n - 1)
        elif pat == "final":
            flows.setdefault("__final__", {})[ms["code"]] = pct
        else:
            raise CommercialError("unknown milestone pattern %r for %s"
                                  % (pat, ms["code"]))

    overall_last = max([i for i in flows if i != "__final__"], default=0)
    close_month = overall_last + 1

    months_out = []
    for idx in range(0, close_month + 1):
        codes = dict(flows.get(idx, {}))
        if idx == close_month and "__final__" in flows:
            for code, pct in flows["__final__"].items():
                codes[code] = round(grand * pct / 100.0, 6)
        amount = round(sum(codes.values()), 6)
        months_out.append({
            "month": month_label(idx),
            "month_index": idx,
            "amount_usd": amount,
            "pct": round(amount / grand * 100.0, 6) if grand else 0.0,
            "milestones": [{"code": c, "amount_usd": a}
                           for c, a in sorted(codes.items())],
        })
    return {
        "grand_total_usd_mid": round(grand, 6),
        "splits_sum_pct": round(sum(float(m["pct"]) for m in milestones), 6),
        "install_months": install_months,
        "months": months_out,
        "notes": ["curve is mid-band only",
                  "splits: %s" % ", ".join("%s %.1f%%" % (m["code"], m["pct"])
                                           for m in milestones)],
    }


def cash_curve_csv(curve):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["month", "month_index", "amount_usd", "pct",
                "milestone_code", "milestone_amount_usd",
                "grand_total_usd_mid"])
    for m in curve["months"]:
        if not m["milestones"]:
            w.writerow([m["month"], m["month_index"], m["amount_usd"],
                        m["pct"], "", "", curve["grand_total_usd_mid"]])
        for ms in m["milestones"]:
            w.writerow([m["month"], m["month_index"], m["amount_usd"],
                        m["pct"], ms["code"], ms["amount_usd"],
                        curve["grand_total_usd_mid"]])
    return buf.getvalue()


def cash_curve_xlsx(curve):
    rows = [["ROMtool v0.6.0 monthly cash curve (mid band)"],
            ["grand_total_usd_mid", curve["grand_total_usd_mid"]],
            ["splits_sum_pct", curve["splits_sum_pct"]],
            ["install_months", curve["install_months"]],
            [],
            ["month", "month_index", "pct", "amount_usd",
             "milestones (code: amount)"]]
    for m in curve["months"]:
        ms = "; ".join("%s: %s" % (x["code"], x["amount_usd"])
                       for x in m["milestones"])
        rows.append([m["month"], m["month_index"], m["pct"],
                     m["amount_usd"], ms])
    return encode_xlsx([{"name": "Cash_Flow", "rows": rows}])


# ----------------------------------------------------------------- scenarios

def validate_scenario_overlay(overlay, definition, recap_lines, quotes):
    """Structural + referential validation of a scenario overlay."""
    errors = []
    if not isinstance(overlay, dict) or not overlay:
        raise CommercialError("overlay must be a non-empty JSON object")
    loc_ids = {l["id"] for l in (definition.get("locations") or [])
               if isinstance(l, dict)}
    defn_line_ids = {(l["id"], str(ln["catalog_id"]))
                     for l in definition.get("locations") or []
                     if isinstance(l, dict) for ln in (l.get("lines") or [])}
    recap_by_id = {l["line_id"]: l for l in recap_lines}
    known_quotes = {q["id"] for q in quotes}

    access = overlay.get("access_overrides") or {}
    if not isinstance(access, dict):
        errors.append("overlay.access_overrides must be an object")
    else:
        for lid, method in access.items():
            if lid not in loc_ids:
                errors.append("access_overrides: unknown location %r" % lid)
            if method not in ACCESS_METHODS:
                errors.append("access_overrides[%r]: unknown access method %r"
                              % (lid, method))

    fac = overlay.get("factor_overrides") or {}
    if not isinstance(fac, dict):
        errors.append("overlay.factor_overrides must be an object")

    qtys = overlay.get("qty_overrides") or []
    if not isinstance(qtys, list):
        errors.append("overlay.qty_overrides must be a list")
    else:
        for i, q in enumerate(qtys):
            if not isinstance(q, dict):
                errors.append("qty_overrides[%d] must be an object" % i)
                continue
            key = (q.get("location_id"), str(q.get("catalog_id")))
            if key not in defn_line_ids:
                errors.append("qty_overrides[%d]: no definition line %r in "
                              "location %r" % (i, q.get("catalog_id"),
                                               q.get("location_id")))
            try:
                v = float(q.get("qty"))
                if v <= 0:
                    errors.append("qty_overrides[%d].qty must be > 0" % i)
            except (TypeError, ValueError):
                errors.append("qty_overrides[%d].qty must be a number" % i)

    swap = overlay.get("quote_swap")
    if swap is not None:
        if not isinstance(swap, dict):
            errors.append("overlay.quote_swap must be an object")
        else:
            if swap.get("quote_id") not in known_quotes:
                errors.append("quote_swap: quote %r not found on this "
                              "estimate" % (swap.get("quote_id"),))
            ids = swap.get("line_ids")
            if not isinstance(ids, list) or not ids:
                errors.append("quote_swap.line_ids must be a non-empty list "
                              "of recap line ids")
            else:
                for lid in ids:
                    ln = recap_by_id.get(lid)
                    if ln is None:
                        errors.append("quote_swap: unknown recap line_id %r"
                                      % lid)
                    elif ln.get("kind") == "cost_carry":
                        errors.append("quote_swap: line %r is a cost-carry "
                                      "row and cannot be quote-swapped" % lid)
                if not errors:
                    group = sum(float(recap_by_id[l]["labor_usd"]["total"]["mid"])
                                for l in ids if l in recap_by_id)
                    if group <= 0:
                        errors.append("quote_swap: group labor carry is zero; "
                                      "nothing to swap")
    if errors:
        raise CommercialError(errors)


def scenario_definition(definition, overlay):
    """Deep-copied definition with access/qty overlays applied (factor
    overrides merge at compute time)."""
    d = copy.deepcopy(definition)
    for lid, method in (overlay.get("access_overrides") or {}).items():
        for loc in d["locations"]:
            if loc["id"] == lid:
                loc["access"] = method
    for q in overlay.get("qty_overrides") or []:
        for loc in d["locations"]:
            if loc["id"] != q["location_id"]:
                continue
            for ln in loc.get("lines") or []:
                if str(ln["catalog_id"]) == str(q["catalog_id"]):
                    ln["qty"] = float(q["qty"])
    return d


def merge_factor_overrides(estimating, factor_overrides):
    """Deep-merge scenario factor_overrides onto a fresh estimating config
    (dicts merge recursively, everything else replaces)."""
    out = copy.deepcopy(estimating or {})

    def _merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                _merge(dst[k], v)
            else:
                dst[k] = copy.deepcopy(v)

    _merge(out, factor_overrides or {})
    return out


def apply_quote_swap(recap, swap, quotes):
    """Replace the wrapped labor dollars of the named recap lines with the
    vendor quote amount, allocated proportionally by each line's share of
    the group carry, per bound (each band's group labor total becomes the
    quote exactly). Hours/schedule unchanged; line-risk contingency rides
    the swapped carry at the line's pct. Mutates and returns the recap."""
    quote = next((q for q in quotes if q["id"] == swap["quote_id"]), None)
    if quote is None:
        raise CommercialError("quote %r not found" % swap["quote_id"])
    by_id = {l["line_id"]: l for l in recap["lines"]}
    ids = list(swap.get("line_ids") or [])
    amount = float(quote["amount"])
    for b in ("low", "mid", "high"):
        group = sum(float(by_id[l]["labor_usd"]["total"][b]) for l in ids)
        for lid in ids:
            ln = by_id[lid]
            share = (float(ln["labor_usd"]["total"][b]) / group) if group else 0.0
            new_total = amount * share
            old = float(ln["labor_usd"]["total"][b])
            ratio = (new_total / old) if old > 0 else 0.0
            for comp in ("tech", "foreman", "super"):
                ln["labor_usd"][comp][b] = round(
                    ln["labor_usd"][comp][b] * ratio, 6)
            ln["labor_usd"]["total"][b] = round(new_total, 6)
            pct = float(ln.get("contingency_pct") or 0.0)
            ln["contingency_usd"][b] = round(new_total * pct / 100.0, 6)
    note = ("quote swap: vendor quote #%s (%s) $%s replaces the wrapped "
            "labor of %s" % (quote["id"], quote["vendor"], amount,
                             ", ".join(ids)))
    for lid in ids:
        by_id[lid]["quote_swap"] = {"quote_id": quote["id"],
                                    "vendor": quote["vendor"],
                                    "amount": amount}
        by_id[lid]["derivation"] = note
    recap["warnings"] = list(recap.get("warnings") or []) + [note]
    return recap


def scenario_diff(base_recap, scen_recap):
    """Line-level diff between the base recap and a scenario recap: only
    changed lines (qty / hours mid / labor mid moved) plus totals delta."""
    def _index(recap):
        return {l["line_id"]: l for l in recap["lines"]}

    base_l, scen_l = _index(base_recap), _index(scen_recap)
    changed = []
    for lid in sorted(scen_l):
        sline, bline = scen_l[lid], base_l.get(lid)
        if bline is None:
            changed.append({
                "line_id": lid, "status": "added",
                "location_id": sline["location_id"],
                "catalog_id": sline["catalog_id"],
                "qty_base": None, "qty_scenario": sline["qty"],
                "hours_mid_base": None,
                "hours_mid_scenario": sline["hours"]["mid"],
                "labor_mid_base": None,
                "labor_mid_scenario": sline["labor_usd"]["total"]["mid"],
                "delta_labor_mid": round(
                    float(sline["labor_usd"]["total"]["mid"]), 6)})
            continue
        q0, q1 = float(bline["qty"]), float(sline["qty"])
        h0, h1 = float(bline["hours"]["mid"]), float(sline["hours"]["mid"])
        l0, l1 = (float(bline["labor_usd"]["total"]["mid"]),
                  float(sline["labor_usd"]["total"]["mid"]))
        if abs(q0 - q1) < 1e-12 and abs(h0 - h1) < 1e-9 and abs(l0 - l1) < 1e-9:
            continue
        changed.append({
            "line_id": lid, "status": "changed",
            "location_id": sline["location_id"],
            "catalog_id": sline["catalog_id"],
            "qty_base": q0, "qty_scenario": q1,
            "hours_mid_base": h0, "hours_mid_scenario": h1,
            "labor_mid_base": l0, "labor_mid_scenario": l1,
            "delta_labor_mid": round(l1 - l0, 6)})
    for lid in sorted(base_l):
        if lid in scen_l:
            continue
        bline = base_l[lid]
        changed.append({
            "line_id": lid, "status": "removed",
            "location_id": bline["location_id"],
            "catalog_id": bline["catalog_id"],
            "qty_base": bline["qty"], "qty_scenario": None,
            "hours_mid_base": bline["hours"]["mid"],
            "hours_mid_scenario": None,
            "labor_mid_base": bline["labor_usd"]["total"]["mid"],
            "labor_mid_scenario": None,
            "delta_labor_mid": -float(bline["labor_usd"]["total"]["mid"])})
    bc, sc = base_recap["cost"], scen_recap["cost"]
    bt, st = base_recap["totals"], scen_recap["totals"]
    return {
        "lines_changed_count": len(changed),
        "lines_changed": sorted(changed,
                                key=lambda c: (c["location_id"] or "",
                                               c["line_id"])),
        "totals": {
            "grand_total_usd_mid_base": bc["grand_total_usd"]["mid"],
            "grand_total_usd_mid_scenario": sc["grand_total_usd"]["mid"],
            "delta_grand_total_usd_mid": round(
                sc["grand_total_usd"]["mid"] - bc["grand_total_usd"]["mid"], 6),
            "direct_labor_usd_mid_base": bc["direct_labor_usd"]["mid"],
            "direct_labor_usd_mid_scenario": sc["direct_labor_usd"]["mid"],
            "delta_direct_labor_usd_mid": round(
                sc["direct_labor_usd"]["mid"] - bc["direct_labor_usd"]["mid"], 6),
            "hours_mid_base": bt["hours"]["mid"],
            "hours_mid_scenario": st["hours"]["mid"],
            "delta_hours_mid": round(st["hours"]["mid"] - bt["hours"]["mid"], 6),
        },
        "chain_note": "totals deltas include the wrap: the scenario recap is "
                      "recomputed through the full commercial chain (small "
                      "tools, escalation, taxes, insurance/bond, PM, OH, "
                      "profit, contingency), so the grand-total delta is not "
                      "the raw labor delta",
    }
