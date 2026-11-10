"""Phase E2 (v0.5.0): estimate governance + procurement layer.

Verified with independent hand anchors:
- assumption auto-population: 24 E-quality labor lines on the stadium fixture
  (18 definition lines + M05/N03/N04 wrap + O01/O02/O04 power-plant), with
  G03/H07 (G flags) and M01 (cost-carry) excluded;
- state machine: legal walk draft->review->approved->sent->superseded from
  config, 409 on illegal jumps;
- quote date math: binding_until is inclusive (valid through the date),
  expiry strictly after, one warning per expired quote, warn threshold from
  config;
- bid tab: low/median/high over active quotes, gap math vs the estimate
  carry (E06 anchor: 40 x 0.75 h x M03/K03/M02 x $110.20 wrapped rate x 1.07
  E-contingency = $8,861.51), whole-estimate group against grand total;
- revision diffs: a qty-only change yields exactly
  ["lines.bowl-lower:E06.hours_mid+qty", "totals.hours_mid",
   "totals.grand_total_usd_mid"] (qty 40->44 keeps crew-days at 15, so no
  other line moves);
- inclusions/exclusions sheet: config templates + definition-derived lines +
  vendor-quote terms, zero-dep xlsx + pdf renderers.
"""

import csv
import datetime
import io
import json
import math
import os
import zipfile
import xml.etree.ElementTree as ET

import pytest

from romtool.estimate import (compute_estimate, load_estimating_config,
                              load_labor_catalog)
from romtool.governance import (GovernanceError, StateRuleError,
                                assert_estimate_transition, assumption_text,
                                auto_assumption_targets, bid_tab,
                                build_commercial_sheet, compute_diff,
                                effective_quote_status, line_carry_usd,
                                line_key_of, load_commercial_draft,
                                load_governance_config, quote_warnings,
                                validate_quote_payload)
from romtool.pdf import extract_pdf_text

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixtures", "stadium_estimate.json")

GOV = load_governance_config()
DRAFT = load_commercial_draft()
ESTIMATING = load_estimating_config()

F_OCC, F_LIFT, F_NIGHT = 1.375, 1.375, 1.325   # config mids (M03/K03/M02)
N01_RATIO = (1 / 4) * 0.5                       # foremen 1 / techs 4 x haircut 0.5
N02_PCT = 0.115                                 # mid of 8-15%
WRAPPED_RATE = 85.0 + N01_RATIO * 105.0 + N02_PCT * 105.0   # $110.20/task-h
E_CONT = 0.07                                   # E-quality line-risk band


@pytest.fixture(scope="module")
def stadium():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def recap(stadium):
    return compute_estimate(stadium)


def _load_fixture():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _fmt_date(d):
    return d.isoformat()


# ------------------------------------------------- pure: state machine

def test_state_machine_walk_and_409_rules():
    walk = [("draft", "review"), ("review", "approved"), ("approved", "sent"),
            ("sent", "superseded")]
    for frm, to in walk:
        assert to in GOV["estimate_state_transitions"][frm], (frm, to)
        assert_estimate_transition(frm, to, GOV)     # legal: no raise
    for frm, to in (("draft", "approved"), ("draft", "sent"),
                    ("review", "sent"), ("sent", "review"),
                    ("approved", "review"), ("superseded", "draft"),
                    ("superseded", "review")):
        with pytest.raises(StateRuleError):
            assert_estimate_transition(frm, to, GOV)
    # approved->superseded (withdraw) and review->draft (reject) are legal
    assert_estimate_transition("approved", "superseded", GOV)
    assert_estimate_transition("review", "draft", GOV)
    # unknown target state is a payload error, not a 409 rule error
    with pytest.raises(GovernanceError) as exc:
        assert_estimate_transition("draft", "shipping", GOV)
    assert not isinstance(exc.value, StateRuleError)


# ------------------------------------- pure: assumption auto-population

def test_auto_assumption_targets_hand_count(recap, stadium):
    targets = auto_assumption_targets(recap["lines"])
    keys = [t["line_key"] for t in targets]

    # independent recount straight from the catalog + fixture definition
    by_id = {u["id"]: u for u in load_labor_catalog()["units"]}
    expected = 0
    for loc in stadium["locations"]:
        for ln in loc["lines"]:
            row = by_id[ln["catalog_id"]]
            if row["kind"] == "minutes" and row["source_quality"] == "E":
                expected += 1
    for rid in ("M05", "N03", "N04"):        # derived estimate-level wrap
        if by_id[rid]["source_quality"] == "E":
            expected += 1
    for loc in stadium["locations"]:         # power-plant derived O rows
        if loc.get("power_plant"):
            pp = loc["power_plant"]
            ah = float(pp.get("runtime_hours", 12.0)) * float(pp["load_amps"])
            strings = math.ceil(ah / ESTIMATING["power_plant"]["battery_string_ah"])
            rects = math.ceil(float(pp["load_amps"]) /
                              ESTIMATING["power_plant"]["rectifier_size_amps"]) + 1
            if strings + rects > 0:
                expected += 1                     # O01 bay
            if rects > 0:
                expected += 1                     # O02 rectifiers
            if strings > 0:
                expected += 1                     # O04 strings
    assert len(targets) == expected == 24

    assert "roof:A01" in keys and "headend:O04" in keys
    assert "M05" in keys and "N03" in keys and "N04" in keys
    for excluded in ("headend:G03", "headend:H07", "M01", "M01#2"):
        assert excluded not in keys, excluded      # G flags + cost-carry rows
    a01 = next(t for t in targets if t["line_key"] == "roof:A01")
    row = by_id["A01"]
    assert a01["text"] == assumption_text(row) == \
        "%s - %s" % (row["item"], row["reference"])


def test_line_ids_unique_and_m01_dedupe(recap):
    ids = [line_key_of(l) for l in recap["lines"]]
    assert len(ids) == len(set(ids))
    assert ids[0] == "roof:A01"
    two = compute_estimate({**_load_fixture(),
                            "downtime_windows": [{"days": 1}, {"days": 2}]})
    m01 = [l["line_id"] for l in two["lines"] if l["catalog_id"] == "M01"]
    assert m01 == ["M01", "M01#2"]
    assert len({l["line_id"] for l in two["lines"]}) == len(two["lines"])


def test_line_carry_usd_anchor(recap):
    e06 = next(l for l in recap["lines"]
               if l["catalog_id"] == "E06" and l["location_id"] == "bowl-lower")
    task = 40 * 0.75 * F_OCC * F_LIFT * F_NIGHT                  # task hours
    carry = task * WRAPPED_RATE * (1 + E_CONT)
    assert line_carry_usd(e06) == pytest.approx(carry, abs=1e-4)
    assert carry == pytest.approx(8861.51, abs=0.01)


# ---------------------------------------------- pure: quote date math

def _quote(idx, amount, status, binding_until, linked=None, vendor="Vendor"):
    return {"id": idx, "estimate_id": 1, "vendor": vendor, "scope": "scope %s" % idx,
            "amount": float(amount), "currency": "USD",
            "binding_until": _fmt_date(binding_until),
            "inclusions": [], "exclusions": [],
            "linked_line_ids": linked or [], "status": status, "notes": None}


TODAY = datetime.date.today()


def test_quote_expiry_and_binding_hand_anchors():
    past = TODAY - datetime.timedelta(days=1)
    assert effective_quote_status(_quote(1, 100, "bound", past), TODAY) == "expired"
    assert effective_quote_status(_quote(1, 100, "received", past), TODAY) == "expired"
    assert effective_quote_status(_quote(1, 100, "bound", TODAY), TODAY) == "bound"  # inclusive
    assert effective_quote_status(_quote(1, 100, "draft", past), TODAY) == "draft"

    expired = [_quote(1, 100, "bound", past)]
    w1 = quote_warnings(expired, TODAY, GOV)
    assert len(w1) == 1 and w1[0]["code"] == "QUOTE_EXPIRED"
    assert w1[0]["quote_id"] == 1
    assert quote_warnings(expired, TODAY, GOV) == w1          # warn once
    msgs = [w["message"] for w in quote_warnings([_quote(2, 100, "received", past)],
                                                 TODAY, GOV)]
    assert len(msgs) == 1 and "EXPIRED" in msgs[0]

    warn_days = GOV["quote_binding_warn_days"]
    soon = quote_warnings([_quote(3, 100, "bound", TODAY +
                                  datetime.timedelta(days=warn_days - 1))],
                          TODAY, GOV)
    assert len(soon) == 1 and soon[0]["code"] == "QUOTE_BINDING_SOON"
    assert "%d day(s) left" % (warn_days - 1) in soon[0]["message"]
    far = quote_warnings([_quote(4, 100, "bound", TODAY +
                                 datetime.timedelta(days=warn_days + 1))],
                         TODAY, GOV)
    assert far == []
    edge = quote_warnings([_quote(5, 100, "bound", TODAY +
                                  datetime.timedelta(days=warn_days))],
                          TODAY, GOV)                       # boundary counts
    assert len(edge) == 1 and edge[0]["code"] == "QUOTE_BINDING_SOON"

    lapsed = quote_warnings([_quote(6, 100, "draft", past)], TODAY, GOV)
    assert len(lapsed) == 1 and lapsed[0]["code"] == "QUOTE_DRAFT_LAPSED"
    # a fresh draft inside its binding window is silent
    assert quote_warnings([_quote(6, 100, "draft",
                                  TODAY + datetime.timedelta(days=10))],
                          TODAY, GOV) == []


# --------------------------------------------- pure: bid tab math

def test_bid_tab_low_mid_high_ordering_and_gaps(recap):
    e06_carry = 40 * 0.75 * F_OCC * F_LIFT * F_NIGHT * WRAPPED_RATE * (1 + E_CONT)
    future = TODAY + datetime.timedelta(days=60)
    quotes = [
        _quote(1, 135000, "bound", future, linked=["bowl-lower:E06"], vendor="C"),
        _quote(2, 118000, "received", future, linked=["bowl-lower:E06"], vendor="A"),
        _quote(3, 120000, "received", future, linked=["bowl-lower:E06"], vendor="B"),
        _quote(4, 999999, "received", TODAY - datetime.timedelta(days=2),
               linked=["bowl-lower:E06"], vendor="STALE"),
        _quote(5, 1200000, "bound", future, linked=[], vendor="WHOLE"),
    ]
    tab = bid_tab(quotes, recap, TODAY, GOV)
    assert tab["as_of"] == _fmt_date(TODAY)
    group = next(g for g in tab["groups"] if g["scope"] == "bowl-lower:E06")
    assert [q["vendor"] for q in group["quotes"]] == ["A", "B", "C", "STALE"]
    assert group["bid"]["low"] == 118000.0
    assert group["bid"]["mid"] == 120000.0      # median of 3
    assert group["bid"]["high"] == 135000.0     # expired STALE 999999 excluded
    assert group["bid"]["n_quotes"] == 3
    assert group["estimate_carry_usd"] == pytest.approx(e06_carry, abs=1e-4)
    gaps = group["bid"]["gaps_vs_carry_usd"]
    assert gaps["low"] == pytest.approx(e06_carry - 118000, abs=1e-4)
    assert gaps["high"] == pytest.approx(e06_carry - 135000, abs=1e-4)
    assert any("BELOW the lowest active quote" in n for n in group["notes"])
    stale = next(q for q in group["quotes"] if q["vendor"] == "STALE")
    assert stale["effective_status"] == "expired"
    assert len([w for w in tab["warnings"]
                if w["code"] == "QUOTE_EXPIRED"]) == 1

    whole = next(g for g in tab["groups"] if g["scope"] == "(whole estimate)")
    assert whole["estimate_carry_usd"] == pytest.approx(
        recap["cost"]["grand_total_usd"]["mid"], abs=1e-6)
    assert whole["bid"]["mid"] == 1200000.0


def test_bid_tab_median_even_count(recap):
    future = TODAY + datetime.timedelta(days=30)
    links = ["bowl-lower:E06"]
    amounts = (100000, 120000, 130000, 150000)
    quotes = [_quote(i + 1, a, "received", future, linked=links)
              for i, a in enumerate(amounts)]
    group = bid_tab(quotes, recap, TODAY, GOV)["groups"][0]
    assert group["bid"]["low"] == 100000
    assert group["bid"]["mid"] == 125000        # mean of the two middle
    assert group["bid"]["high"] == 150000
    ranked = [q["amount"] for q in group["quotes"]]
    assert ranked == sorted(amounts)


# ---------------------------------------------- pure: revision diffs

def test_compute_diff_qty_only_exact_changed_paths(recap):
    new_def = _load_fixture()
    for loc in new_def["locations"]:
        if loc["id"] == "bowl-lower":
            for ln in loc["lines"]:
                if ln["catalog_id"] == "E06":
                    ln["qty"] = 44          # keeps crew-days at 15
    new_recap = compute_estimate(new_def)
    diff = compute_diff(_load_fixture(), recap, new_def, new_recap)
    assert diff["changed_paths"] == ["lines.bowl-lower:E06.hours_mid+qty",
                                     "totals.hours_mid",
                                     "totals.grand_total_usd_mid"]
    chg = diff["lines_changed"][0]
    assert chg["line_key"] == "bowl-lower:E06"
    assert chg["changes"]["qty"] == {"old": 40, "new": 44}
    assert diff["lines_added"] == [] and diff["lines_removed"] == []
    assert diff["totals"]["hours_mid"]["old"] == pytest.approx(
        recap["totals"]["hours"]["mid"], abs=1e-6)
    assert diff["totals"]["hours_mid"]["new"] == pytest.approx(
        recap["totals"]["hours"]["mid"] + (44 - 40) / 40 * 93.18890625, abs=1e-3)
    assert diff["totals"]["hours_mid"]["new"] > diff["totals"]["hours_mid"]["old"]


def test_compute_diff_add_remove_meta_and_noop(recap):
    old_def = _load_fixture()
    new_def = _load_fixture()
    for loc in new_def["locations"]:
        if loc["id"] == "roof":
            loc["lines"] = [l for l in loc["lines"]
                            if l["catalog_id"] not in ("K10",)]
            loc["lines"].append({"catalog_id": "A02", "qty": 3})
        if loc["id"] == "closet-500":
            loc["access"] = "scissor"
    new_def["schedule"]["weekend"] = True
    new_recap = compute_estimate(new_def)
    diff = compute_diff(old_def, recap, new_def, new_recap)
    assert diff["lines_removed"] == ["roof:K10"]
    assert diff["lines_added"] == ["roof:A02"]
    paths = diff["changed_paths"]
    assert "lines.roof:K10" in paths and "lines.roof:A02" in paths
    assert "schedule.weekend" in paths and "locations.closet-500.access" in paths
    assert diff["meta_changed"][0]["path"] == "schedule.weekend"
    assert any("grand total mid" in diff["summary"] for _ in [0])

    noop = compute_diff(old_def, recap, old_def, compute_estimate(old_def))
    assert noop["changed_paths"] == []
    assert noop["lines_changed"] == []
    assert "No definition changes" in noop["summary"]


# --------------------------------------- pure: validation + commercial

def test_quote_payload_validation(recap):
    known = {line_key_of(l) for l in recap["lines"]}
    ok = validate_quote_payload(
        {"vendor": "  V  ", "scope": "labor bundle", "amount": "185000",
         "binding_until": "2027-01-15", "inclusions": ["rack"], "notes": None},
        known, GOV, creating=True)
    assert ok["vendor"] == "V" and ok["amount"] == 185000.0
    assert ok["currency"] == GOV["default_quote_currency"] == "USD"
    assert ok["status"] == "draft"

    with pytest.raises(GovernanceError) as exc:
        validate_quote_payload({"vendor": "", "amount": -5,
                                "binding_until": "15/01/2027",
                                "linked_line_ids": ["nope:ZZZ", "roof:A01"]},
                               known, GOV, creating=True)
    msgs = exc.value.errors
    assert any("vendor" in m for m in msgs)
    assert any("amount" in m for m in msgs)
    assert any("binding_until" in m and "YYYY-MM-DD" in m for m in msgs)
    assert any("nope:ZZZ" in m for m in msgs)

    upd = validate_quote_payload({"amount": 10}, known, GOV, creating=False)
    assert upd == {"amount": 10.0}
    with pytest.raises(GovernanceError):
        validate_quote_payload({"status": "bound"}, known, GOV, creating=False)


def test_commercial_sheet_content(recap):
    future = TODAY + datetime.timedelta(days=45)
    quotes = [
        {"id": 7, "vendor": "DAS Vendor A", "scope": "headend bundle",
         "amount": 185000.0, "currency": "USD",
         "binding_until": _fmt_date(future),
         "inclusions": ["All headend rack hardware installed"],
         "exclusions": ["Permit expediting fees"],
         "linked_line_ids": [], "status": "bound", "notes": None},
        {"id": 8, "vendor": "STALE Co", "scope": "roof labor",
         "amount": 90000.0, "currency": "USD",
         "binding_until": _fmt_date(TODAY - datetime.timedelta(days=3)),
         "inclusions": ["Old pricing"], "exclusions": ["Old exclusion"],
         "linked_line_ids": [], "status": "received", "notes": None},
    ]
    sheet = build_commercial_sheet(recap, quotes, ESTIMATING, DRAFT, TODAY)
    inc_text = [t["text"] for t in sheet["inclusions"]]
    exc_text = [t["text"] for t in sheet["exclusions"]]
    # template anchors from config/commercial_draft.json
    assert DRAFT["inclusions_default"][0] in inc_text
    assert any("Sales/use tax" in t for t in exc_text)
    # derived from the definition
    assert any("Night-shift" in t and "M02 1.25-1.40x" in t for t in inc_text)
    assert any("idle crew-day carry" in t and "$3,560" in t for t in inc_text)
    assert any("N01" in t and "N02" in t for t in inc_text)
    assert any("48VDC" in t for t in inc_text)
    assert any("assumption register" in t for t in inc_text)
    assert any("blacked-out downtime window(s)" in t for t in inc_text)
    assert any("other areas are excluded" in t for t in exc_text)
    # quote terms: active quote verbatim, expired quote disclaimed
    assert any("All headend rack hardware installed" in t and
               "DAS Vendor A" in t for t in inc_text)
    assert any("Permit expediting fees" in t and "DAS Vendor A" in t
               for t in exc_text)
    assert not any("Old pricing" in t for t in inc_text)
    assert any("STALE Co" in t and "not honored" in t for t in exc_text)
    # summary view math
    assert sheet["quote_summary"][0]["effective_status"] == "bound"
    assert sheet["quote_summary"][1]["effective_status"] == "expired"


def test_pdf_writer_smoke():
    from romtool.pdf import encode_pdf, wrap_text
    wrapped = wrap_text("word " * 60, 10, 100)
    assert all(len(l) * 10 * 0.55 <= 100.5 for l in wrapped)
    blob = encode_pdf([[("text", 50, 700, 12, "Governance (test) \\ page", True),
                        ("rule", 50, 690, 512)],
                       [("text", 50, 670, 10, wrapped[0], False)]])
    assert blob.startswith(b"%PDF-1.4") and blob.rstrip().endswith(b"%%EOF")
    texts = extract_pdf_text(blob)
    assert "Governance (test) \\ page" in texts and wrapped[0] in texts


# ------------------------------------------------------- API: governance

def _post(client, url, actor=None, **kw):
    headers = kw.pop("headers", {})
    if actor:
        headers["X-Romtool-Actor"] = actor
    with open(FIXTURE, "rb") as fh:
        return client.post(url, content=fh.read(),
                           headers={**headers, "Content-Type": "application/json"})


def test_estimate_governance_full_cycle(client):
    r = _post(client, "/estimates", actor="rowan")
    assert r.status_code == 200, r.text
    est = r.json()["estimate"]
    eid = est["id"]
    assert est["state"] == "draft" and est["revision"] == 1
    gov = est["governance"]
    assert gov["assumptions"]["open"] == 24 and gov["assumptions"]["total"] == 24
    assert gov["assumptions"]["auto_populated"] == 24
    assert gov["revision_count"] == 1 and gov["quote_warnings"] == []

    # illegal jumps are 409 with the allowed list
    bad = client.post("/estimates/%d/state" % eid, json={"to": "approved"})
    assert bad.status_code == 409
    assert bad.json()["detail"]["allowed"] == ["review"]
    unknown = client.post("/estimates/%d/state" % eid, json={"to": "shipping"})
    assert unknown.status_code == 422
    mismatch = client.post("/estimates/%d/state" % eid,
                           json={"to": "review", "from": "approved"})
    assert mismatch.status_code == 409

    walk = [("review", "send for check"), ("approved", "internal sign-off"),
            ("sent", "bid issued"), ("superseded", "replaced")]
    prev = "draft"
    for to, note in walk:
        res = client.post("/estimates/%d/state" % eid,
                          json={"to": to, "note": note},
                          headers={"X-Romtool-Actor": "rowan"})
        assert res.status_code == 200, res.text
        assert res.json()["transition"] == {"from": prev, "to": to,
                                            "note": note, "actor": "rowan"}
        prev = to
    body = client.get("/estimates/%d" % eid).json()
    assert body["estimate"]["state"] == "superseded"
    assert body["estimate"]["governance"]["revision_count"] == 1

    # frozen state: PATCH and register edits are blocked
    patch = client.patch("/estimates/%d" % eid, json={"definition": _load_fixture()})
    assert patch.status_code == 409
    qedit = client.post("/estimates/%d/quotes" % eid, json={"vendor": "V",
                                                            "scope": "s",
                                                            "amount": 1,
                                                            "binding_until": "2027-01-01"})
    assert qedit.status_code == 409
    aedit = client.post("/estimates/%d/assumptions" % eid, json={"text": "x"})
    assert aedit.status_code == 409

    audit = client.get("/estimates/%d/audit" % eid).json()["audit"]
    actions = [(e["action"], e["actor"]) for e in audit]
    assert ("estimate.created", "rowan") in actions
    assert ("assumptions.synced", "rowan") in actions
    states = [e for e in audit if e["action"] == "estimate.state_changed"]
    assert [e["changed_paths"] for e in states] == [["state"]] * len(states)
    assert "draft -> review: send for check" in states[0]["summary"]
    for e in audit:
        assert e["ts"].endswith("Z") and e["actor"] and e["action"]

    # superseded allows delete; the whole governance history goes with it
    del_r = client.delete("/estimates/%d" % eid)
    assert del_r.status_code == 200
    assert client.get("/estimates/%d" % eid).status_code == 404
    assert client.get("/estimates/%d/audit" % eid).status_code == 404


def test_estimate_delete_blocked_when_sent(client):
    est = _post(client, "/estimates", actor="mb").json()["estimate"]
    eid = est["id"]
    for to in ("review", "approved", "sent"):
        assert client.post("/estimates/%d/state" % eid,
                           json={"to": to}).status_code == 200
    blocked = client.delete("/estimates/%d" % eid)
    assert blocked.status_code == 409
    assert client.get("/estimates/%d" % eid).status_code == 200
    assert client.post("/estimates/%d/state" % eid,
                       json={"to": "superseded"}).status_code == 200
    assert client.delete("/estimates/%d" % eid).status_code == 200


def test_revision_flow_diff_and_snapshots(client):
    est = _post(client, "/estimates", actor="rowan").json()["estimate"]
    eid = est["id"]

    noop = client.patch("/estimates/%d" % eid,
                        json={"definition": _load_fixture(),
                              "note": "recompute"},
                        headers={"X-Romtool-Actor": "rowan"})
    assert noop.status_code == 200, noop.text
    body = noop.json()
    assert body["estimate"]["revision"] == 2
    assert body["revision"]["diff"]["changed_paths"] == []
    assert "No definition changes" in body["revision"]["diff"]["summary"]
    assert body["revision"]["note"] == "recompute"

    new_def = _load_fixture()
    for loc in new_def["locations"]:
        if loc["id"] == "bowl-lower":
            for ln in loc["lines"]:
                if ln["catalog_id"] == "E06":
                    ln["qty"] = 44
    rev = client.patch("/estimates/%d" % eid, json={"definition": new_def})
    assert rev.status_code == 200, rev.text
    body = rev.json()
    assert body["estimate"]["revision"] == 3
    assert body["revision"]["diff"]["changed_paths"] == [
        "lines.bowl-lower:E06.hours_mid+qty", "totals.hours_mid",
        "totals.grand_total_usd_mid"]
    assert body["revision"]["diff"]["lines_changed"][0]["changes"]["qty"] == \
        {"old": 40, "new": 44}
    new_hours = body["estimate"]["recap"]["totals"]["total_crew_hours_mid"]
    assert new_hours == pytest.approx(est["recap"]["totals"]["total_crew_hours_mid"]
                                      + (44 - 40) / 40 * 93.18890625, abs=1e-4)

    listing = client.get("/estimates/%d/revisions" % eid).json()
    assert [r["revision"] for r in listing["revisions"]] == [1, 2, 3]
    assert listing["current_revision"] == 3
    assert listing["revisions"][0]["diff"] is None
    assert listing["revisions"][1]["actor"] == "rowan"

    snap = client.get("/estimates/%d/revisions/3" % eid).json()["revision"]
    assert snap["definition"] == new_def
    assert snap["recap"]["totals"] == body["estimate"]["recap"]["totals"]
    assert client.get("/estimates/%d/revisions/9" % eid).status_code == 404

    audit = client.get("/estimates/%d/audit" % eid).json()["audit"]
    revised = [e for e in audit if e["action"] == "estimate.revised"]
    assert len(revised) == 2
    assert revised[1]["changed_paths"] == body["revision"]["diff"]["changed_paths"]
    assert revised[1]["revision"] == 3
    dry = client.post("/estimates/compute", json=new_def)
    assert dry.json()["recap"]["totals"] == body["estimate"]["recap"]["totals"]
    client.delete("/estimates/%d" % eid)


def test_revision_syncs_assumption_register(client):
    """Verified close-out rows survive line removal; open rows are dropped."""
    est = _post(client, "/estimates").json()["estimate"]
    eid = est["id"]
    rows = client.get("/estimates/%d/assumptions" % eid).json()["assumptions"]
    c07 = next(r for r in rows if r["line_key"] == "closet-500:C07")
    ok = client.post("/estimates/%d/assumptions/%d/status"
                     % (eid, c07["id"]), json={"to": "verified"})
    assert ok.status_code == 200

    new_def = _load_fixture()
    for loc in new_def["locations"]:
        if loc["id"] == "closet-500":
            loc["lines"] = [l for l in loc["lines"]
                            if l["catalog_id"] != "C07"]
    rev = client.patch("/estimates/%d" % eid, json={"definition": new_def})
    assert rev.status_code == 200
    assert rev.json()["revision"]["assumptions_sync"] == \
        {"added": 0, "removed": 0, "kept": 24}
    summary = client.get("/estimates/%d/assumptions" % eid).json()["summary"]
    assert summary == {"open": 23, "verified": 1, "closed": 0, "total": 24}

    # fresh estimate: dropping an unverified E line removes its open row
    est2 = _post(client, "/estimates").json()["estimate"]
    eid2 = est2["id"]
    new_def2 = _load_fixture()
    for loc in new_def2["locations"]:
        if loc["id"] == "bowl-lower":
            loc["lines"] = [l for l in loc["lines"]
                            if l["catalog_id"] != "E06"]
    rev2 = client.patch("/estimates/%d" % eid2, json={"definition": new_def2})
    assert rev2.json()["revision"]["assumptions_sync"]["removed"] == 1
    summary2 = client.get("/estimates/%d/assumptions" % eid2).json()["summary"]
    assert summary2["total"] == 23 and summary2["open"] == 23
    client.delete("/estimates/%d" % eid)
    client.delete("/estimates/%d" % eid2)


# -------------------------------------------------- API: assumptions

def test_assumption_register_crud_and_closeout(client):
    est = _post(client, "/estimates", actor="rowan").json()["estimate"]
    eid = est["id"]
    listing = client.get("/estimates/%d/assumptions" % eid).json()
    rows = listing["assumptions"]
    assert listing["summary"]["open"] == 24
    a01 = next(r for r in rows if r["line_key"] == "roof:A01")
    assert a01["owner"] == GOV["default_assumption_owner"] == \
        "Senior Project Estimator"
    assert a01["status"] == "open" and a01["revision"] == 1
    assert a01["text"].startswith("Donor antenna, mount/aim/seal")

    add = client.post("/estimates/%d/assumptions" % eid, json={
        "text": "Owner requires shroud color RAL 7016 on bowl antennas",
        "owner": "M. Busdiecker", "review_date": "2026-12-01"})
    assert add.status_code == 200, add.text
    manual = add.json()["assumption"]
    assert manual["line_key"] is None and manual["catalog_id"] is None
    assert manual["owner"] == "M. Busdiecker"
    assert client.post("/estimates/%d/assumptions" % eid,
                       json={"text": "  "}).status_code == 422
    assert client.post("/estimates/%d/assumptions" % eid,
                       json={"text": "x", "review_date": "12/01/2026"}
                       ).status_code == 422

    patched = client.patch("/estimates/%d/assumptions/%d"
                           % (eid, manual["id"]),
                           json={"text": "Shroud color pending owner CAD",
                                 "review_date": "2026-12-15"})
    assert patched.status_code == 200
    assert patched.json()["assumption"]["text"] == "Shroud color pending owner CAD"

    aid = manual["id"]
    # skipping the close-out ladder is illegal: open -> closed is 409
    skip = client.post("/estimates/%d/assumptions/%d/status" % (eid, aid),
                       json={"to": "closed"})
    assert skip.status_code == 409
    assert skip.json()["detail"]["allowed"] == ["verified"]
    ok = client.post("/estimates/%d/assumptions/%d/status" % (eid, aid),
                     json={"to": "verified"})
    assert ok.status_code == 200 and ok.json()["transition"] == \
        {"from": "open", "to": "verified"}
    reopen = client.post("/estimates/%d/assumptions/%d/status" % (eid, aid),
                         json={"to": "open"})                # verification rejected
    assert reopen.status_code == 200
    ok = client.post("/estimates/%d/assumptions/%d/status" % (eid, aid),
                     json={"to": "verified"})
    assert ok.status_code == 200
    closed = client.post("/estimates/%d/assumptions/%d/status" % (eid, aid),
                         json={"status": "closed"})          # alias accepted
    assert closed.status_code == 200
    back = client.post("/estimates/%d/assumptions/%d/status" % (eid, aid),
                       json={"to": "verified"})
    assert back.status_code == 409                     # closed is terminal
    assert back.json()["detail"]["allowed"] == []
    assert client.post("/estimates/%d/assumptions/%d/status" % (eid, aid),
                       json={"to": "dead"}).status_code == 422
    assert client.post("/estimates/%d/assumptions/999/status" % eid,
                       json={"to": "verified"}).status_code == 404

    # close-out progress visible in CSV export with origin column
    csv_r = client.get("/exports/estimates/%d/assumptions.csv" % eid)
    assert csv_r.status_code == 200 and "text/csv" in csv_r.headers["content-type"]
    raw = list(csv.reader(io.StringIO(csv_r.text)))
    header = raw[0]
    assert header[:6] == ["id", "revision", "catalog_id", "location_id",
                          "line_key", "assumption"]
    body_rows = raw[1:]
    autos = [r for r in body_rows if r[11] == "auto"]
    manuals = [r for r in body_rows if r[11] == "manual"]
    assert len(autos) == 24 and len(manuals) == 1
    assert any("Donor antenna" in r[5] for r in autos)
    assert any(r[6] == "Senior Project Estimator" and r[7] == "open"
               for r in autos)
    assert manuals[0][8] == "2026-12-15"        # review_date column

    del_r = client.delete("/estimates/%d/assumptions/%d" % (eid, aid))
    assert del_r.status_code == 200
    assert del_r.json()["summary"]["total"] == 24
    assert client.delete("/estimates/%d/assumptions/%d" % (eid, aid)).status_code == 404

    audit = client.get("/estimates/%d/audit" % eid).json()["audit"]
    kinds = [e["action"] for e in audit]
    for action in ("assumption.added", "assumption.updated",
                   "assumption.status_changed", "assumption.deleted"):
        assert action in kinds, action
    paths = next(e["changed_paths"] for e in audit
                 if e["action"] == "assumption.status_changed")
    assert paths[0].startswith("assumptions#") and paths[0].endswith(".status")
    client.delete("/estimates/%d" % eid)


# ------------------------------------------------------- API: quotes

def test_quote_register_crud_status_and_links(client):
    est = _post(client, "/estimates").json()["estimate"]
    eid = est["id"]
    r = client.post("/estimates/%d/quotes" % eid, headers={"X-Romtool-Actor": "MB"}, json={
        "vendor": "DAS Vendor A", "scope": "ERCES headend + cabling bundle",
        "amount": 185000, "binding_until": _fmt_date(TODAY + datetime.timedelta(days=45)),
        "inclusions": ["All headend rack hardware"],
        "exclusions": ["Sales/use tax", "Permit expediting"],
        "linked_line_ids": ["headend:B03", "headend:B11"], "notes": "bid valid Q1"})
    assert r.status_code == 200, r.text
    q = r.json()["quote"]
    qid = q["id"]
    assert q["status"] == "draft" and q["effective_status"] == "draft"
    assert q["currency"] == "USD" and q["days_to_binding"] >= 40
    assert q["linked_line_ids"] == ["headend:B03", "headend:B11"]

    assert client.post("/estimates/%d/quotes" % eid, json={
        "vendor": "V", "scope": "s", "amount": 1,
        "binding_until": _fmt_date(TODAY), "linked_line_ids": ["bogus:ZZZ"],
    }).status_code == 422
    assert client.post("/estimates/%d/quotes" % eid, json={
        "vendor": "V", "scope": "s", "amount": "free",
        "binding_until": _fmt_date(TODAY)}).status_code == 422

    bad_jump = client.post("/estimates/%d/quotes/%d/status" % (eid, qid),
                           json={"to": "bound"})
    assert bad_jump.status_code == 409
    assert bad_jump.json()["detail"]["allowed"] == ["received", "expired"]

    rec = client.post("/estimates/%d/quotes/%d/status" % (eid, qid),
                      json={"to": "received"}, headers={"X-Romtool-Actor": "MB"})
    assert rec.status_code == 200 and rec.json()["quote"]["status"] == "received"
    bound = client.post("/estimates/%d/quotes/%d/status" % (eid, qid),
                        json={"to": "bound"})
    assert bound.status_code == 200
    assert client.post("/estimates/%d/quotes/%d/status" % (eid, qid),
                       json={"to": "received"}).status_code == 409

    # PATCH terms; status via PATCH is rejected
    patched = client.patch("/estimates/%d/quotes/%d" % (eid, qid),
                           json={"amount": 190000,
                                 "binding_until": _fmt_date(TODAY - datetime.timedelta(days=1))})
    assert patched.status_code == 200
    q = patched.json()["quote"]
    assert q["amount"] == 190000 and q["effective_status"] == "expired"
    assert q["status"] == "bound"                    # stored state untouched
    assert client.patch("/estimates/%d/quotes/%d" % (eid, qid),
                        json={"status": "received"}).status_code == 422

    # lapsed bound quote -> expired workflow, then re-issue (expired->received)
    expired = client.post("/estimates/%d/quotes/%d/status" % (eid, qid),
                          json={"to": "expired"})
    assert expired.status_code == 200
    assert expired.json()["quote"]["effective_status"] == "expired"
    reissue = client.post("/estimates/%d/quotes/%d/status" % (eid, qid),
                          json={"to": "received"})
    assert reissue.status_code == 200
    future = client.patch("/estimates/%d/quotes/%d" % (eid, qid),
                          json={"binding_until": _fmt_date(TODAY + datetime.timedelta(days=30))})
    assert future.json()["quote"]["effective_status"] == "received"

    listing = client.get("/estimates/%d/quotes" % eid).json()
    assert listing["summary"]["received"] == 1
    assert [w["code"] for w in listing["warnings"]] == []
    one = client.get("/estimates/%d/quotes/%d" % (eid, qid)).json()["quote"]
    assert one["effective_status"] == "received"
    assert client.get("/estimates/%d/quotes/999" % eid).status_code == 404

    rm = client.delete("/estimates/%d/quotes/%d" % (eid, qid))
    assert rm.status_code == 200
    assert client.delete("/estimates/%d/quotes/%d" % (eid, qid)).status_code == 404
    audit = client.get("/estimates/%d/audit" % eid).json()["audit"]
    actions = [e["action"] for e in audit]
    for action in ("quote.created", "quote.updated", "quote.status_changed",
                   "quote.deleted"):
        assert action in actions, action
    assert any(e["actor"] == "MB" for e in audit
               if e["action"] in ("quote.created", "quote.status_changed"))
    client.delete("/estimates/%d" % eid)


def test_quote_expiry_warns_in_recap_exports(client):
    est = _post(client, "/estimates").json()["estimate"]
    eid = est["id"]
    stale = client.post("/estimates/%d/quotes" % eid, json={
        "vendor": "STALE Co", "scope": "roof labor T&M",
        "amount": 95000, "status": "bound",
        "binding_until": _fmt_date(TODAY - datetime.timedelta(days=2))})
    assert stale.status_code == 200
    soon = client.post("/estimates/%d/quotes" % eid, json={
        "vendor": "Soon Co", "scope": "bowl lifts",
        "amount": 43000, "status": "received",
        "binding_until": _fmt_date(TODAY + datetime.timedelta(
            days=GOV["quote_binding_warn_days"] - 1))})
    assert soon.status_code == 200

    gov = client.get("/estimates/%d" % eid).json()["estimate"]["governance"]
    codes = sorted(w["code"] for w in gov["quote_warnings"])
    assert codes == ["QUOTE_BINDING_SOON", "QUOTE_EXPIRED"]
    assert len([w for w in gov["quote_warnings"]
                if w["code"] == "QUOTE_EXPIRED"]) == 1
    gov2 = client.get("/estimates/%d" % eid).json()["estimate"]["governance"]
    assert gov["quote_warnings"] == gov2["quote_warnings"]   # warn once

    csv_r = client.get("/exports/estimates/%d/recap.csv" % eid)
    raw = list(csv.reader(io.StringIO(csv_r.text)))
    gov_rows = [r for r in raw if r[:1] == ["governance"]]
    assert ["governance", "estimate_state", "draft"] in gov_rows
    assert ["governance", "revision", "1"] in gov_rows
    assert ["governance", "assumption_register",
            "open=24 verified=0 closed=0 total=24"] in gov_rows
    assert ["governance", "quote_register",
            "draft=0 received=1 bound=1 expired=0 (stored) total=2"] not in gov_rows
    expired_rows = [r for r in gov_rows if r[1] == "QUOTE_EXPIRED"]
    assert len(expired_rows) == 1
    assert "STALE Co" in expired_rows[0][2]

    xl = client.get("/exports/estimates/%d/recap.xlsx" % eid)
    zf = zipfile.ZipFile(io.BytesIO(xl.content))
    texts = [t.text or "" for t in ET.fromstring(
        zf.read("xl/worksheets/sheet1.xml").decode("utf-8")).iter()
        if t.tag.endswith("}t")]
    assert "draft" in texts and any("open=24" in t for t in texts)
    assert any("QUOTE_EXPIRED" in t for t in texts)
    client.delete("/estimates/%d" % eid)


# ------------------------------------------------------ API: bid tab

def test_bid_tab_endpoint_grouping_gaps_and_exclusion(client, recap):
    est = _post(client, "/estimates").json()["estimate"]
    eid = est["id"]
    carry_e06 = line_carry_usd(next(
        l for l in est["recap"]["lines"]
        if l["catalog_id"] == "E06" and l["location_id"] == "bowl-lower"))
    grand = est["recap"]["cost"]["grand_total_usd"]["mid"]

    futures = _fmt_date(TODAY + datetime.timedelta(days=40))
    quotes = [
        {"vendor": "Vendor A", "scope": "E06 bundle", "amount": 118000,
         "binding_until": futures, "status": "received",
         "linked_line_ids": ["bowl-lower:E06"]},
        {"vendor": "Vendor B", "scope": "E06 bundle", "amount": 120000,
         "binding_until": futures, "status": "received",
         "linked_line_ids": ["bowl-lower:E06"]},
        {"vendor": "Vendor C", "scope": "E06 bundle", "amount": 135000,
         "binding_until": futures, "status": "bound",
         "linked_line_ids": ["bowl-lower:E06"]},
        {"vendor": "STALE Co", "scope": "E06 bundle", "amount": 999999,
         "binding_until": _fmt_date(TODAY - datetime.timedelta(days=1)),
         "status": "received", "linked_line_ids": ["bowl-lower:E06"]},
        {"vendor": "Prime GC", "scope": "whole DAS build", "amount": 1200000,
         "binding_until": futures, "status": "bound", "linked_line_ids": []},
    ]
    made = [client.post("/estimates/%d/quotes" % eid, json=q).json()["quote"]["id"]
            for q in quotes]

    tab = client.get("/estimates/%d/bid_tab" % eid).json()
    assert tab["estimate"] == est["name"] and tab["as_of"] == _fmt_date(TODAY)
    scopes = [g["scope"] for g in tab["groups"]]
    assert scopes == sorted(scopes) == ["(whole estimate)", "bowl-lower:E06"]
    group = next(g for g in tab["groups"] if g["scope"] == "bowl-lower:E06")
    assert [q["vendor"] for q in group["quotes"]] == \
        ["Vendor A", "Vendor B", "Vendor C", "STALE Co"]   # ascending
    assert group["bid"]["low"] == 118000 and group["bid"]["mid"] == 120000
    assert group["bid"]["high"] == 135000 and group["bid"]["n_quotes"] == 3
    assert group["estimate_carry_usd"] == pytest.approx(carry_e06, abs=1e-6)
    assert group["bid"]["gaps_vs_carry_usd"]["low"] == pytest.approx(
        carry_e06 - 118000, abs=1e-6)
    assert group["bid"]["gaps_vs_carry_pct"]["low"] == pytest.approx(
        (carry_e06 - 118000) / 118000 * 100, abs=1e-3)
    assert any("BELOW the lowest active quote" in n for n in group["notes"])
    whole = next(g for g in tab["groups"] if g["scope"] == "(whole estimate)")
    assert whole["estimate_carry_usd"] == pytest.approx(grand, abs=1e-6)
    assert any("ABOVE the highest" in n for n in whole["notes"]) is False
    stale = next(q for q in group["quotes"] if q["vendor"] == "STALE Co")
    assert stale["effective_status"] == "expired"
    assert [w["code"] for w in tab["warnings"]].count("QUOTE_EXPIRED") == 1

    # removed quotes drop out of the tab
    assert client.delete("/estimates/%d/quotes/%d" % (eid, made[3])).status_code == 200
    tab2 = client.get("/estimates/%d/bid_tab" % eid).json()
    g2 = next(g for g in tab2["groups"] if g["scope"] == "bowl-lower:E06")
    assert [q["vendor"] for q in g2["quotes"]] == ["Vendor A", "Vendor B", "Vendor C"]
    assert tab2["warnings"] == []
    client.delete("/estimates/%d" % eid)


# --------------------------------------- API: inclusions/exclusions

def test_inclusions_exclusions_exports(client):
    est = _post(client, "/estimates").json()["estimate"]
    eid = est["id"]
    client.post("/estimates/%d/quotes" % eid, json={
        "vendor": "DAS Vendor A", "scope": "headend bundle", "amount": 185000,
        "binding_until": _fmt_date(TODAY + datetime.timedelta(days=45)),
        "status": "bound",
        "inclusions": ["All headend rack hardware installed"],
        "exclusions": ["Permit expediting fees"]})
    client.post("/estimates/%d/quotes" % eid, json={
        "vendor": "STALE Co", "scope": "roof labor", "amount": 90000,
        "binding_until": _fmt_date(TODAY - datetime.timedelta(days=3)),
        "status": "received"})

    sheet = client.get("/estimates/%d/inclusions_exclusions" % eid).json()
    inc = [t["text"] for t in sheet["inclusions"]]
    exc = [t["text"] for t in sheet["exclusions"]]
    assert DRAFT["inclusions_default"][0] in inc
    assert any("Night-shift" in t for t in inc)
    assert any("$3,560" in t for t in inc)
    assert any("All headend rack hardware installed" in t and
               "DAS Vendor A" in t for t in inc)
    assert any("Sales/use tax" in t for t in exc)
    assert any("Permit expediting fees" in t for t in exc)
    assert any("STALE Co" in t and "not honored" in t for t in exc)
    assert sheet["as_of"] == _fmt_date(datetime.date.today())

    xl = client.get("/exports/estimates/%d/inclusions_exclusions.xlsx" % eid)
    assert xl.status_code == 200
    assert xl.headers["content-type"].endswith("spreadsheetml.sheet")
    zf = zipfile.ZipFile(io.BytesIO(xl.content))
    assert "xl/workbook.xml" in zf.namelist()
    ie_texts = [t.text or "" for t in ET.fromstring(
        zf.read("xl/worksheets/sheet1.xml").decode("utf-8")).iter()
        if t.tag.endswith("}t")]
    for probe in ("INCLUSIONS", "EXCLUSIONS", "Sales/use tax", "Night-shift",
                  "DAS Vendor A", "VDC", "48"):
        assert any(probe in t for t in ie_texts), probe
    q_texts = [t.text or "" for t in ET.fromstring(
        zf.read("xl/worksheets/sheet2.xml").decode("utf-8")).iter()
        if t.tag.endswith("}t")]
    assert any("headend bundle" in t for t in q_texts)
    assert b"<v>185000.0</v>" in zf.read("xl/worksheets/sheet2.xml")
    assert any("expired" in t for t in q_texts)

    pdf = client.get("/exports/estimates/%d/inclusions_exclusions.pdf" % eid)
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-1.4")
    assert pdf.content.rstrip().endswith(b"%%EOF")
    texts = extract_pdf_text(pdf.content)
    assert any("INCLUSIONS" in t for t in texts)
    assert any("EXCLUSIONS" in t for t in texts)
    assert any("Sales/use tax" in t for t in texts)
    assert any("VENDOR QUOTES (register summary)" in t for t in texts)
    assert any("Metropolitan Stadium" in t for t in texts)
    assert any("not honored" in t for t in texts)  # expired quote disclaimer
    client.delete("/estimates/%d" % eid)


# ------------------------------------------------------------- misc

def test_list_estimates_shows_state_and_revision(client):
    est = _post(client, "/estimates").json()["estimate"]
    listing = client.get("/estimates").json()["estimates"]
    row = next(e for e in listing if e["id"] == est["id"])
    assert row["state"] == "draft" and row["revision"] == 1
    assert row["lines"] == len(est["recap"]["lines"])
    client.delete("/estimates/%d" % est["id"])


def test_docs_document_governance(client):
    body = client.get("/help").text
    for probe in ("governance", "draft", "review", "approved", "sent",
                  "superseded", "revisions", "audit log", "Assumption register",
                  "Quote register", "bid tab", "binding_until", "inclusions",
                  "assumptions.csv", "409", "inclusions_exclusions.xlsx"):
        assert probe in body, "missing on /help: %r" % probe
    hiw = client.get("/how-it-works").text
    for probe in ("governance", "revision", "assumption", "quote", "binding",
                  "line carry"):
        assert probe in hiw.lower() or probe in hiw, "missing on /how-it-works: %r" % probe
