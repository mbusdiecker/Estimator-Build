"""Bid form export (phase E3, v0.6.0).

Assembles a bid-form payload from a computed estimate + its definition and
renders it through the zero-dep pdf.py / xlsx.py writers:

- BASE BID: the wrapped grand total (mid) from the recap.
- ALTERNATES: definition.alternates[] priced by romtool.estimate.
  compute_alternates() -- identical engine, factor stacks and wrap chain;
  'deduct' alternates are signed negative (credit when accepted).
- UNIT PRICE: per antenna point = wrapped grand total mid / antenna-point
  count from the definition (scalar or per-location map summed here).
- SCHEDULE OF VALUES (condensed): the money lines in wrap order plus the
  per-zone wrapped-labor breakdown.
- EXCLUSIONS SUMMARY: the commercial-draft sheet's exclusions (templates +
  definition-derived + vendor quote terms) via governance.
  build_commercial_sheet.
- SIGNATURE BLOCK per config/commercial.json bid_form fields.

All bid dollars are MID band; the low/high bounds stay in the recap and
alternate entries (bid forms do not publish ranges).
"""

import datetime

from romtool.governance import build_commercial_sheet
from romtool.pdf import encode_pdf, wrap_text
from romtool.xlsx import encode_xlsx


def antenna_point_count(definition):
    """Scalar antenna_points or the sum of a per-location map; None when
    the definition carries none (unit price reads n-a)."""
    pts = (definition or {}).get("antenna_points")
    if pts is None:
        return None
    if isinstance(pts, dict):
        return round(sum(float(v) for v in pts.values()), 6)
    return round(float(pts), 6)


def build_bid_form(definition, recap, quotes, estimating, commercial,
                   draft, today=None):
    """Assemble the bid-form payload (pure data; writers consume this)."""
    today = today or datetime.date.today()
    cost = recap["cost"]
    base = float(cost["grand_total_usd"]["mid"])
    pts = antenna_point_count(definition)

    # pricing helpers are injected to avoid a circular import with estimate
    from romtool.estimate import compute_alternates

    alternates = compute_alternates(definition,
                                    estimating=estimating,
                                    commercial=commercial)
    all_in = base + sum(a["amount_usd_mid"] for a in alternates)

    sheet = build_commercial_sheet(recap, quotes, estimating, draft, today)
    excl = sheet["exclusions"]

    bf_cfg = commercial["bid_form"] or {}
    signature = {
        "fields": list(bf_cfg.get("signature_fields") or
                       ["Bidder (company)", "Signature", "Printed name",
                        "Title", "Date"]),
        "note": bf_cfg.get("signature_note", ""),
    }
    return {
        "title": bf_cfg.get("title_default", "Bid Form"),
        "estimate_name": recap.get("name"),
        "as_of": today.isoformat(),
        "base_bid": {"label": "Base bid (locations/scope as listed, wrap"
                               " chain applied)",
                     "amount_usd_mid": round(base, 6),
                     "amount_usd_low": cost["grand_total_usd"]["low"],
                     "amount_usd_high": cost["grand_total_usd"]["high"]},
        "unit_prices": {
            "antenna_points": pts,
            "per_antenna_point_usd": (round(base / pts, 6) if pts else None),
            "basis": bf_cfg.get("unit_price_note",
                                "wrapped grand total mid / antenna-point"
                                " count from the definition"),
        },
        "alternates": alternates,
        "all_in_total_usd_mid": round(all_in, 6),
        "all_in_note": "base bid + every named alternate (deduct alternates"
                       " signed negative)",
        "sov": [{"code": m["code"], "label": m["label"], "basis": m["basis"],
                 "amount_usd_mid": m["amount"]["mid"]}
                for m in recap["money_lines"]],
        "zone_sov": [{"zone": z["key"], "hours_mid": z["hours"]["mid"],
                      "labor_usd_mid": z["labor_usd"]["mid"],
                      "contingency_usd_mid": z["contingency_usd"]["mid"]}
                     for z in recap.get("zone_rollups", [])],
        "exclusions": excl,
        "signature": signature,
        "crew_hours_mid": recap["totals"]["total_crew_hours_mid"],
        "crew_days": recap["totals"]["crew_days"],
    }


def bid_form_xlsx(data):
    """-> bid-form workbook bytes (sheets: Bid_Form, SOV, Exclusions)."""
    rows = [
        [data["title"]],
        ["estimate", data["estimate_name"], "as_of", data["as_of"]],
        [],
        ["BASE BID", "", "amount_usd_mid", data["base_bid"]["amount_usd_mid"]],
        ["", "", "amount_usd_low", data["base_bid"]["amount_usd_low"]],
        ["", "", "amount_usd_high", data["base_bid"]["amount_usd_high"]],
        ["unit_prices", "antenna_points", data["unit_prices"]["antenna_points"],
         data["unit_prices"]["per_antenna_point_usd"]],
        ["crew", "hours_mid", data["crew_hours_mid"], "crew_days",
         data["crew_days"]],
        [],
        ["ALTERNATES", "name", "kind", "locations", "amount_usd_mid",
         "antenna_points", "unit_price"],
    ]
    for a in data["alternates"]:
        rows.append([None, a["name"], a["kind"], ", ".join(a["locations"]),
                     a["amount_usd_mid"], a.get("antenna_points"),
                     a.get("unit_price_per_antenna_point")])
    rows += [[], ["ALL IN (base + every alternate)",
                  data["all_in_total_usd_mid"]],
             ["note", data["all_in_note"]], [],
             ["SIGNATURE BLOCK"]]
    for f in data["signature"]["fields"]:
        rows.append(["", f, "______________________"])
    rows.append(["note", data["signature"]["note"]])

    sov_rows = [["Schedule of values (condensed, mid band, wrap order)"],
                ["code", "label", "basis", "amount_usd_mid"]]
    sov_rows += [[s["code"], s["label"], s["basis"], s["amount_usd_mid"]]
                 for s in data["sov"]]
    sov_rows += [[], ["zone breakdown", "hours_mid", "labor_usd_mid",
                      "contingency_usd_mid"]]
    sov_rows += [[z["zone"], z["hours_mid"], z["labor_usd_mid"],
                  z["contingency_usd_mid"]] for z in data["zone_sov"]]

    excl_rows = [["Exclusions summary"], ["#", "text", "source", "ref"]]
    for i, e in enumerate(data["exclusions"], 1):
        excl_rows.append([i, e["text"], e["source"], e.get("ref", "")])

    return encode_xlsx([
        {"name": "Bid_Form", "rows": rows},
        {"name": "SOV", "rows": sov_rows},
        {"name": "Exclusions", "rows": excl_rows},
    ])


def bid_form_pdf(data):
    """-> bid-form PDF bytes; mid-band dollars, flowing pages."""
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
        nonlocal y
        ops.append(("text", x, y, size, text, bold))
        y -= size + 3

    def _wrap_line(text, size, indent=0.0):
        nonlocal y
        for line in wrap_text(text, size, width - indent):
            _need(size + 4)
            _text(margin + indent, size, line)
            y -= 1

    def _section(label):
        nonlocal y
        _need(34)
        _text(margin, 12, label, True)
        ops.append(("rule", margin, y, width))
        y -= 13

    def _usd(v):
        return "$%s" % format(float(v), ",.2f")

    _text(margin, 16, data["title"], True)
    _text(margin, 10, "Estimate: %s   |   bid date: %s   |   crew-hours (mid)"
                      " %.2f   |   crew-days %s"
          % (data["estimate_name"], data["as_of"], data["crew_hours_mid"],
             data["crew_days"]))
    _section("BASE BID")
    _wrap_line("%s  %s" % (data["base_bid"]["label"],
                           _usd(data["base_bid"]["amount_usd_mid"])), 11)
    up = data["unit_prices"]
    if up["per_antenna_point_usd"] is not None:
        pts_txt = (format(up["antenna_points"], ",")
                   if float(up["antenna_points"]).is_integer()
                   else str(up["antenna_points"]))
        _wrap_line("Unit price: %s per antenna point (%s antenna points; %s)"
                   % (_usd(up["per_antenna_point_usd"]), pts_txt,
                      up["basis"]), 9)
    else:
        _wrap_line("Unit price: n/a - the definition carries no"
                   " antenna_points count (%s)" % up["basis"], 9)
    y -= 4
    if data["alternates"]:
        _section("NAMED ALTERNATES (same wrap engine)")
        for i, a in enumerate(data["alternates"], 1):
            up_a = ("  |  %s/antenna point" % _usd(
                a["unit_price_per_antenna_point"])
                if a.get("unit_price_per_antenna_point") is not None else "")
            _need(28)
            _text(margin, 10, "Alt %d.  %s  (%s)  %s%s" % (
                i, a["name"], a["kind"],
                _usd(a["amount_usd_mid"]), up_a), True)
            y -= 12
            _wrap_line("locations: %s; crew-hours (mid) %.2f"
                       % (", ".join(a["locations"]), a["crew_hours_mid"]),
                       9, indent=14)
            y -= 3
        _need(16)
        _text(margin, 10, "All-in total (base + every alternate): %s   "
                          "(%s)" % (_usd(data["all_in_total_usd_mid"]),
                                    data["all_in_note"]), True)
        y -= 12
    _section("SCHEDULE OF VALUES (condensed, mid band, wrap order)")
    for s in data["sov"]:
        _need(14)
        _text(margin, 10, "%-22s %26s   %s"
              % (s["code"], _usd(s["amount_usd_mid"]), s["label"]))
        y -= 12
    y -= 2
    _section("EXCLUSIONS SUMMARY")
    for i, e in enumerate(data["exclusions"], 1):
        _wrap_line("%s%d. %s" % ("", i, e["text"]), 9)
        y -= 2
    _section("SIGNATURE BLOCK")
    for f in data["signature"]["fields"]:
        _need(14)
        _text(margin, 10, "%s: ______________________________" % f)
        y -= 13
    _wrap_line(data["signature"]["note"], 9)
    if ops:
        pages.append(ops)
    return encode_pdf(pages or [[("text", margin, 720, 12,
                                  "(empty bid form)", False)]])
