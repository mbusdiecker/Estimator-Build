"""All JSON REST endpoints for ROMtool (spec API surface, no auth):

projects / floors (image upload + ppm scale) / walls / elements
(source, repeater, splitter, tapper, coupler, cable run, antenna) CRUD,
POST /projects/{id}/cascade/solve, POST /projects/{id}/floors/{fid}/predict,
multiband composite (phase 3), GET /projects/{id}/bom, GET /exports/*,
heatmap / NFPA / composite PNG reports. Phase 4: Class 1 labor estimates —
POST /estimates (locations x catalog lines x access/condition/schedule flags
x travel/downtime/crew plan) -> full recap stored in SQLite, plus recap
CSV/XLSX exports. Phase E2 (v0.5.0): estimate governance + procurement —
versioned revisions with diff summaries + audit log, draft->review->
approved->sent->superseded state machine (409 on illegal transitions),
assumption register auto-populated from E-quality labor lines (open->
verified->closed close-out), vendor quote register with binding/expiry
date math + bid-tab comparison, and an inclusions/exclusions commercial
draft sheet (config/commercial_draft.json) exported to xlsx/pdf.
Phase E3 (v0.6.0): commercial wrap depth — the recap $ chain applies
config/commercial.json constants in one documented order (labor ->
downtime -> small tools -> escalation -> taxes -> insurance/bond -> PM ->
OH -> profit -> contingency), crew schedule with dated blackout windows +
early/late bounds + OT flag, milestone cash curve (+ CSV/XLSX), stored
what-if scenarios with line-level diffs against the live base, and a bid
form export (base bid + alternates priced by the same engine + per-
antenna-point unit prices + signature block).
Phase E4 (v0.7.0): intelligence layer — sanity gates with venue-archetype
missing-scope checklists (hard findings block approval), actuals drift loop
with guarded refit proposals, RF-to-estimate draft link, and gateway-agnostic
AI endpoints (503 with documented schemas when no gateway).
Phase E5 (v0.8.0): the Generic 60k-Seat Stadium capstone — stored RF project +
labor estimate + vendor quotes as real objects, the full morning bid package
(recap, bid form, exclusions sheet, assumptions, cashflow, crew plan, bid tab,
sanity findings, MORNING-REPORT.md) written by scripts/build_stadium_package.py,
and GET /reports/stadium serving the package manifest.
"""

import base64
import datetime
import json
import os
import struct

from fastapi import APIRouter, Body, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from romtool import ai as ai_mod
from romtool import actuals as actuals_mod
from romtool.autoplace import AutoplaceError, autoplace_floor
from romtool.clutter_map import (ClutterMapError, apply_overrides, build_clutter_map,
                                 decode_image)
from romtool.bom import bom_csv, generate_bom
from romtool.bidform import build_bid_form, bid_form_pdf, bid_form_xlsx
from romtool.cascade import solve_cascade
from romtool.composite import composite_heatmap_view, predict_floor_multiband
from romtool.commercial import (CommercialError, apply_quote_swap,
                                cash_curve, cash_curve_csv, cash_curve_xlsx,
                                load_commercial_config,
                                merge_factor_overrides,
                                scenario_definition, scenario_diff,
                                crew_schedule, validate_scenario_overlay)
from romtool.config import load_config, with_overrides
from romtool.estimate import (EstimateError, compute_estimate, load_estimating_config,
                              load_labor_catalog, recap_csv)
from romtool.governance import (GovernanceError, StateRuleError,
                                assert_estimate_transition,
                                assert_transition_rules, assumptions_csv,
                                bid_tab, build_commercial_sheet,
                                commercial_pdf, commercial_xlsx,
                                compute_diff, line_key_of,
                                load_commercial_draft, load_governance_config,
                                next_states, quote_warnings, quote_view,
                                validate_quote_payload)
from romtool.predict import PredictError, predict_floor
from romtool.rflink import RfLinkError, draft_for_project
from romtool.sanity import run_sanity
from romtool.report import render_heatmap_png
from romtool.drawings import (floor_layout_png, floor_layout_svg,
                                 one_line_png, one_line_svg)
from romtool.xlsx import encode_xlsx

from . import db

__version__ = "0.8.0"

router = APIRouter(prefix="", tags=["romtool"])

ELEMENT_TYPES = ("source", "repeater", "remote", "splitter", "tapper", "coupler",
                 "antenna")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _stadium_package_dir():
    """E5 morning-package folder (manifest + exports); env-overridable for
    tests and alternate deploys."""
    return os.path.abspath(os.environ.get("ROMTOOL_STADIUM_PACKAGE") or
                           os.path.join(_REPO_ROOT, "dist", "stadium-package"))


# ---------------------------------------------------------------- helpers

def _load_or_404(pid):
    proj = db.get_project(pid)
    if proj is None:
        raise HTTPException(status_code=404, detail="project %s not found" % pid)
    return proj


def _save(pid, name, model):
    return db.save_model(pid, name, model)


def _proj_config(model):
    return with_overrides(load_config(), model.get("config_overrides"))


def _public_project(proj):
    model = json.loads(json.dumps(proj["model"]))  # deep copy
    for fl in model.get("floors", []):
        img = fl.get("image")
        if img and img.get("data_b64"):
            fl["image"] = {
                "mime": img.get("mime", "image/png"),
                "w": img.get("w"),
                "h": img.get("h"),
                "url": "/projects/%s/floors/%s/image" % (proj["id"], fl["id"]),
            }
    return {
        "id": proj["id"],
        "name": proj["name"],
        "created_at": proj["created_at"],
        "updated_at": proj["updated_at"],
        "model": model,
    }


def _require(cond, msg):
    if not cond:
        raise HTTPException(status_code=422, detail=msg)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="expected a number, got %r" % (v,))


def _check_building_type(value):
    """None/'' clears; otherwise the id must exist in config building_types."""
    if value in (None, ""):
        return None
    known = (load_config().get("building_types") or {})
    _require(value in known, "unknown building_type '%s' (known: %s)"
             % (value, ", ".join(sorted(known))))
    return value


def _check_outline(value):
    """None/[] clears; otherwise a list of >= 3 [x, y] number pairs."""
    if not value:
        return None
    _require(isinstance(value, list) and len(value) >= 3,
             "outline must be a list of at least 3 [x, y] points")
    out = []
    for pt in value:
        _require(isinstance(pt, (list, tuple)) and len(pt) == 2,
                 "outline points must be [x, y] pairs")
        out.append([_num(pt[0]), _num(pt[1])])
    return out


def _find_floor(model, fid):
    for fl in model.get("floors", []):
        if fl.get("id") == fid:
            return fl
    raise HTTPException(status_code=404, detail="floor %s not found" % fid)


def _all_elements(model):
    for fl in model.get("floors", []):
        for el in fl.get("elements", []):
            yield fl, el


def _find_element(model, eid):
    for fl, el in _all_elements(model):
        if el.get("id") == eid:
            return fl, el
    raise HTTPException(status_code=404, detail="element %s not found" % eid)


def _new_id(prefix):
    import uuid

    return "%s_%s" % (prefix, uuid.uuid4().hex[:8])


def _auto_label(model, kind):
    counts = {"antenna": 0, "splitter": 0, "repeater": 0, "source": 0,
              "tapper": 0, "coupler": 0, "cable_run": 0, "remote": 0}
    for _, el in _all_elements(model):
        if el.get("type") in counts:
            counts[el["type"]] += 1
    if kind == "antenna":
        return "A%d" % (counts["antenna"] + 1)
    if kind == "splitter":
        return "SP%d" % (counts["splitter"] + 1)
    if kind == "cable_run":
        return "R%d" % (counts["cable_run"] + 1)
    if kind == "remote":
        return "RU%d" % (counts["remote"] + 1)
    if kind == "repeater":
        n = counts["repeater"]
        return "RPTR" if n == 0 else "RPTR%d" % (n + 1)
    return kind.upper()[:6]


# ---------------------------------------------------------------- root/meta

@router.get("/health")
def health():
    return {"ok": True, "tool": "ROMtool", "version": __version__,
            "phase": 4, "subphase": "E5"}


@router.get("/config")
def get_config():
    cfg = load_config()
    return cfg


# ---------------------------------------------------------------- projects

@router.get("/projects")
def list_projects():
    return {"projects": db.list_projects()}


@router.post("/projects")
def create_project(payload: dict = Body(default_factory=dict)):
    name = (payload or {}).get("name") or "Untitled Project"
    cfg = load_config()
    model = {
        "name": name,
        "environment": payload.get("environment") or cfg.get("default_environment", "office"),
        "config_overrides": payload.get("config_overrides") or {},
        "floors": [],
    }
    if payload.get("building_type"):
        model["building_type"] = _check_building_type(payload["building_type"])
    proj = db.create_project(name, model)
    return _public_project(proj)


@router.get("/projects/{pid}")
def get_project(pid: int):
    return _public_project(_load_or_404(pid))


@router.patch("/projects/{pid}")
def patch_project(pid: int, payload: dict = Body(...)):
    proj = _load_or_404(pid)
    model = proj["model"]
    if "name" in payload:
        _require(isinstance(payload["name"], str) and payload["name"].strip(),
                 "name must be a non-empty string")
        model["name"] = payload["name"].strip()
    if "environment" in payload:
        model["environment"] = payload["environment"]
    if "description" in payload:
        model["description"] = payload["description"]
    if "building_type" in payload:
        bt = _check_building_type(payload["building_type"])
        if bt is None:
            model.pop("building_type", None)
        else:
            model["building_type"] = bt
    if "config_overrides" in payload:
        model["config_overrides"] = payload["config_overrides"] or {}
    proj = _save(pid, model.get("name", proj["name"]), model)
    return _public_project(proj)


@router.delete("/projects/{pid}")
def delete_project(pid: int):
    if not db.delete_project(pid):
        raise HTTPException(status_code=404, detail="project %s not found" % pid)
    return {"deleted": pid}


# -------------------------------------- class 1 estimates (phase 4 + E2 gov)

def _gov_cfg():
    return load_governance_config()


def _actor(request):
    """Actor for the audit log: X-Romtool-Actor header, ?actor= query param,
    else config default (usually 'api')."""
    hdr = (request.headers.get("x-romtool-actor") or "").strip()
    if hdr:
        return hdr
    qp = (request.query_params.get("actor") or "").strip()
    if qp:
        return qp
    return _gov_cfg()["default_actor"]


def _known_line_ids(recap):
    return sorted({line_key_of(l) for l in (recap or {}).get("lines", [])})


def _governance_view(rec, today=None):
    today = today or datetime.date.today()
    eid = rec["id"]
    return {
        "state": rec["state"],
        "revision": rec["revision"],
        "as_of": today.isoformat(),
        "assumptions": db.assumption_summary(eid),
        "quotes": db.quote_summary(eid),
        "quote_warnings": quote_warnings(db.list_quotes(eid), today),
        "revision_count": len(db.list_revisions(eid)),
    }


def _estimate_response(rec):
    return {
        "id": rec["id"],
        "name": rec["name"],
        "state": rec["state"],
        "revision": rec["revision"],
        "created_at": rec["created_at"],
        "updated_at": rec["updated_at"],
        "definition": rec["definition"],
        "recap": rec["recap"],
        "governance": _governance_view(rec),
    }


def _load_estimate_or_404(eid):
    est = db.get_estimate(eid)
    if est is None:
        raise HTTPException(status_code=404, detail="estimate %s not found" % eid)
    return est


def _compute_from_body(payload):
    try:
        return compute_estimate(payload or {})
    except EstimateError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})


def _gov_exception_response(exc, state=None, to=None, allowed=None):
    """Map governance rule errors: illegal transitions -> 409, unknown
    states/statuses -> 422. `state`/`allowed` describe the machine the
    request addressed (estimate states, assumption or quote statuses)."""
    detail = {"error": "; ".join(exc.errors)}
    if state:
        detail["state"] = state
    if to:
        detail["to"] = to
    if isinstance(exc, StateRuleError):
        if to is not None and state is not None and allowed is not None:
            detail["allowed"] = allowed
        raise HTTPException(status_code=409, detail=detail)
    raise HTTPException(status_code=422, detail={"errors": exc.errors})


def _ensure_revision_allowed(state, gov):
    if state not in gov["states_allowing_revision"]:
        raise HTTPException(status_code=409, detail={
            "error": "estimate is in state '%s'; revisions are allowed only in %s"
                     " - supersede it or open a new estimate"
                     % (state, gov["states_allowing_revision"]),
            "state": state})


def _ensure_editor_allowed(state, gov):
    if state not in gov["states_allowing_editor_changes"]:
        raise HTTPException(status_code=409, detail={
            "error": "estimate is in state '%s'; register edits (assumptions /"
                     " quotes) are allowed only in %s"
                     % (state, gov["states_allowing_editor_changes"]),
            "state": state})


def _validated_quote_payload(payload, est, gov, creating=False):
    """Validate a quote create/update payload; GovernanceError -> 422."""
    try:
        return validate_quote_payload(payload or {},
                                      _known_line_ids(est["recap"]), gov,
                                      creating=creating)
    except GovernanceError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})


@router.post("/estimates")
def create_estimate(request: Request, payload: dict = Body(...)):
    """Validate + compute + store an estimate (revision 1, state draft) and
    auto-populate the assumption register from E-quality labor lines."""
    recap = _compute_from_body(payload)
    gov = _gov_cfg()
    actor = _actor(request)
    rec = db.create_estimate(recap["name"], payload or {}, recap, actor)
    if rec is None:  # pragma: no cover - row just inserted
        raise HTTPException(status_code=500, detail="estimate insert failed")
    sync = db.sync_assumptions(rec["id"], recap["lines"], 1,
                               gov["default_assumption_owner"],
                               gov.get("auto_assumption_source_quality", "E"),
                               actor=actor)
    rec = db.get_estimate(rec["id"]) or rec
    out = _estimate_response(rec)
    out["governance"]["assumptions"]["auto_populated"] = len(sync["added"])
    return {"estimate": out}


@router.post("/estimates/compute")
def compute_estimate_dry_run(payload: dict = Body(...)):
    """Dry run: compute the recap without storing it (no revision, no
    assumptions, no audit)."""
    return {"recap": _compute_from_body(payload)}


@router.get("/estimates")
def list_estimates():
    return {"estimates": db.list_estimates()}


@router.get("/estimates/{eid}")
def get_estimate(eid: int):
    return {"estimate": _estimate_response(_load_estimate_or_404(eid))}


@router.patch("/estimates/{eid}")
def revise_estimate(eid: int, request: Request, payload: dict = Body(...)):
    """Create revision N+1: replace the definition, recompute, freeze an
    immutable revision snapshot with a diff summary, re-sync assumptions.
    Only draft/review estimates accept revisions (409 otherwise)."""
    est = _load_estimate_or_404(eid)
    gov = _gov_cfg()
    _ensure_revision_allowed(est["state"], gov)
    body = payload or {}
    if isinstance(body.get("definition"), dict):
        definition, note = body["definition"], body.get("note")
    elif isinstance(body.get("locations"), list):
        definition, note = body, body.get("note")
    else:
        raise HTTPException(status_code=422,
                            detail={"errors": ['body must be {"definition":'
                                               " {...}} or a definition"
                                               ' object with locations[]']})
    new_recap = _compute_from_body(definition)
    diff = compute_diff(est["definition"], est["recap"], definition, new_recap)
    actor = _actor(request)
    est2, _ = db.revise_estimate(eid, new_recap["name"], definition, new_recap,
                                 diff, note, actor)
    if est2 is None:
        raise HTTPException(status_code=404, detail="estimate %s not found" % eid)
    sync = db.sync_assumptions(eid, new_recap["lines"], est2["revision"],
                               gov["default_assumption_owner"],
                               gov.get("auto_assumption_source_quality", "E"),
                               actor=actor)
    est2 = db.get_estimate(eid) or est2
    revision = est2["revision"]
    return {"estimate": _estimate_response(est2),
            "revision": {"revision": revision, "note": note,
                         "diff": diff,
                         "assumptions_sync": {"added": len(sync["added"]),
                                              "removed": len(sync["removed"]),
                                              "kept": sync["kept"]}}}


# ------------------------------------------------------ sanity gates (E4)

def _sanity_report(est, archetype=None, today=None):
    """Run the sanity report against the stored estimate + its quotes."""
    return run_sanity(
        est["recap"], est["definition"], db.list_quotes(est["id"]),
        archetype=archetype, today=today)


@router.get("/estimates/{eid}/sanity")
def estimate_sanity(eid: int, archetype: str = Query(None)):
    """Sanity gates on demand: labor-share bands per archetype, crew
    duration consistency, E-density, unit-cost outliers, quote expiry and
    the archetype's missing-scope checklist. Hard findings vs warnings
    separated; hard findings block the approved transition."""
    est = _load_estimate_or_404(eid)
    today = datetime.date.today()
    report = _sanity_report(est, archetype, today)
    report.update({"estimate_id": eid, "estimate_name": est["name"],
                   "state": est["state"], "revision": est["revision"],
                   "as_of": today.isoformat()})
    return report


def _sanity_approval_guard(est, to_state, gov, actor):
    """Recompute the sanity report for transitions into a sanity-blocked
    state; hard findings reject with 409 carrying the findings."""
    if not gov.get("sanity_blocks_approval") or \
            to_state not in (gov.get("sanity_block_states") or []):
        return
    report = _sanity_report(est)
    if report["blocks_approval"]:
        raise HTTPException(status_code=409, detail={
            "error": "sanity hard findings block the %r transition: %d hard"
                     " finding(s). Resolve them (missing checklist scope /"
                     " crew math) or re-run GET /estimates/%d/sanity"
                     % (to_state, report["counts"]["hard"], est["id"]),
            "state": est["state"], "to": to_state,
            "hard_findings": report["hard_findings"],
            "counts": report["counts"],
            "sanity_endpoint": "/estimates/%d/sanity" % est["id"]})


@router.post("/estimates/{eid}/state")
def change_estimate_state(eid: int, request: Request, payload: dict = Body(...)):
    """Estimate state machine transition draft->review->approved->sent->
    superseded; illegal transitions are rejected with 409. Transitions into
    sanity_block_states also recompute the sanity report on demand and are
    rejected while any HARD finding is open (409 with the findings)."""
    est = _load_estimate_or_404(eid)
    gov = _gov_cfg()
    body = payload or {}
    to = body.get("to")
    if not to:
        raise HTTPException(status_code=422,
                            detail={"errors": ["body needs {'to': state,"
                                               " 'note'?, 'from'?}"]})
    try:
        assert_estimate_transition(est["state"], to, gov)
    except (StateRuleError, GovernanceError) as exc:
        _gov_exception_response(exc, est["state"], to,
                                allowed=list(next_states(est["state"], gov)))
    provided_from = body.get("from")
    if provided_from and provided_from != est["state"]:
        raise HTTPException(status_code=409, detail={
            "error": "estimate is in state %r, request expected %r"
                     % (est["state"], provided_from),
            "state": est["state"]})
    actor = _actor(request)
    _sanity_approval_guard(est, to, gov, actor)
    est2, from_state = db.set_estimate_state(eid, to, est["state"], actor,
                                             body.get("note") or "")
    if est2 is None:
        raise HTTPException(status_code=409, detail={
            "error": "estimate state changed concurrently", "state": est["state"]})
    return {"transition": {"from": from_state, "to": to,
                           "note": body.get("note") or "", "actor": actor},
            "estimate": _estimate_response(est2)}


@router.get("/estimates/{eid}/revisions")
def list_estimate_revisions(eid: int):
    est = _load_estimate_or_404(eid)
    return {"estimate_id": eid, "current_revision": est["revision"],
            "revisions": db.list_revisions(eid)}


@router.get("/estimates/{eid}/revisions/{rn}")
def get_estimate_revision(eid: int, rn: int):
    _load_estimate_or_404(eid)
    rev = db.get_revision(eid, rn)
    if rev is None:
        raise HTTPException(status_code=404,
                            detail="revision %s of estimate %s not found" % (rn, eid))
    return {"estimate_id": eid, "revision": rev}


@router.get("/estimates/{eid}/audit")
def estimate_audit(eid: int):
    _load_estimate_or_404(eid)
    return {"estimate_id": eid, "audit": db.list_events(eid)}


# --------------------------------------------------- assumption register

@router.get("/estimates/{eid}/assumptions")
def list_assumptions(eid: int):
    _load_estimate_or_404(eid)
    return {"estimate_id": eid, "assumptions": db.list_assumptions(eid),
            "summary": db.assumption_summary(eid)}


def _review_date_ok(value, errors, field="review_date"):
    if value is None:
        return None
    try:
        datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        errors.append("%s must be an ISO date YYYY-MM-DD, got %r" % (field, value))
        return None
    return str(value)[:10]


@router.post("/estimates/{eid}/assumptions")
def create_assumption(eid: int, request: Request, payload: dict = Body(default_factory=dict)):
    """Manually add an open assumption row (auto rows come from the compute
    sync; this is for estimator-added items)."""
    est = _load_estimate_or_404(eid)
    gov = _gov_cfg()
    _ensure_editor_allowed(est["state"], gov)
    body = payload or {}
    errors = []
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        errors.append("text must be a non-empty string")
    owner = body.get("owner")
    if owner is not None and (not isinstance(owner, str) or not owner.strip()):
        errors.append("owner must be a non-empty string when provided")
    owner = (owner or gov["default_assumption_owner"]).strip()
    review_date = _review_date_ok(body.get("review_date"), errors)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    row = db.add_assumption(eid, text.strip(), owner, review_date,
                            _actor(request), est["revision"])
    return {"assumption": row, "summary": db.assumption_summary(eid)}


@router.patch("/estimates/{eid}/assumptions/{aid}")
def patch_assumption(eid: int, aid: int, request: Request, payload: dict = Body(...)):
    est = _load_estimate_or_404(eid)
    gov = _gov_cfg()
    _ensure_editor_allowed(est["state"], gov)
    if db.get_assumption(eid, aid) is None:
        raise HTTPException(status_code=404,
                            detail="assumption %s on estimate %s not found" % (aid, eid))
    body = payload or {}
    errors, changes = [], {}
    if "text" in body:
        if not isinstance(body["text"], str) or not body["text"].strip():
            errors.append("text must be a non-empty string")
        else:
            changes["text"] = body["text"].strip()
    if "owner" in body:
        if not isinstance(body["owner"], str) or not body["owner"].strip():
            errors.append("owner must be a non-empty string")
        else:
            changes["owner"] = body["owner"].strip()
    if "review_date" in body:
        rd = _review_date_ok(body.get("review_date"), errors)
        if rd is not None or body.get("review_date") is None:
            changes["review_date"] = rd
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    row = db.update_assumption(eid, aid, changes, _actor(request))
    return {"assumption": row, "summary": db.assumption_summary(eid)}


@router.post("/estimates/{eid}/assumptions/{aid}/status")
def change_assumption_status(eid: int, aid: int, request: Request,
                             payload: dict = Body(...)):
    """Assumption close-out machine: open->verified->closed (verified->open
    reopens). Skipped steps are 409."""
    est = _load_estimate_or_404(eid)
    gov = _gov_cfg()
    _ensure_editor_allowed(est["state"], gov)
    row = db.get_assumption(eid, aid)
    if row is None:
        raise HTTPException(status_code=404,
                            detail="assumption %s on estimate %s not found" % (aid, eid))
    to = (payload or {}).get("to") or (payload or {}).get("status")
    if not to:
        raise HTTPException(status_code=422,
                            detail={"errors": ["body needs {'to': status}"]})
    try:
        assert_transition_rules(row["status"], to,
                                gov["assumption_status_transitions"],
                                gov["assumption_statuses"])
    except (StateRuleError, GovernanceError) as exc:
        _gov_exception_response(exc, row["status"], to,
                                allowed=list(
                                    gov["assumption_status_transitions"]
                                    .get(row["status"], [])))
    updated, from_status = db.set_assumption_status(eid, aid, to,
                                                    row["status"],
                                                    _actor(request))
    if updated is None:
        raise HTTPException(status_code=409, detail={
            "error": "assumption %s status moved concurrently" % aid})
    return {"transition": {"from": from_status, "to": to},
            "assumption": updated, "summary": db.assumption_summary(eid)}


@router.delete("/estimates/{eid}/assumptions/{aid}")
def remove_assumption(eid: int, aid: int, request: Request):
    est = _load_estimate_or_404(eid)
    gov = _gov_cfg()
    _ensure_editor_allowed(est["state"], gov)
    if not db.delete_assumption(eid, aid, _actor(request)):
        raise HTTPException(status_code=404,
                            detail="assumption %s on estimate %s not found" % (aid, eid))
    return {"deleted": aid, "summary": db.assumption_summary(eid)}


@router.get("/exports/estimates/{eid}/assumptions.csv")
def export_assumptions_csv(eid: int):
    est = _load_estimate_or_404(eid)
    return Response(
        content=assumptions_csv(db.list_assumptions(eid)),
        media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="assumptions-%s.csv"' % eid},
    )


# --------------------------------------------------------- quote register

@router.post("/estimates/{eid}/quotes")
def create_quote(eid: int, request: Request, payload: dict = Body(...)):
    """Register a vendor quote (scope $, binding_until date, inclusions/
    exclusions, linked estimate line ids). Status defaults to draft."""
    est = _load_estimate_or_404(eid)
    gov = _gov_cfg()
    _ensure_editor_allowed(est["state"], gov)
    fields = _validated_quote_payload(payload, est, gov, creating=True)
    q = db.create_quote(eid, fields, _actor(request), est["revision"])
    return {"quote": quote_view(q)}


@router.get("/estimates/{eid}/quotes")
def list_quotes(eid: int):
    _load_estimate_or_404(eid)
    today = datetime.date.today()
    return {"estimate_id": eid,
            "quotes": [quote_view(q, today) for q in db.list_quotes(eid)],
            "warnings": quote_warnings(db.list_quotes(eid), today),
            "summary": db.quote_summary(eid),
            "as_of": today.isoformat()}


@router.get("/estimates/{eid}/quotes/{qid}")
def get_quote(eid: int, qid: int):
    _load_estimate_or_404(eid)
    q = db.get_quote(eid, qid)
    if q is None:
        raise HTTPException(status_code=404,
                            detail="quote %s on estimate %s not found" % (qid, eid))
    return {"quote": quote_view(q)}


@router.patch("/estimates/{eid}/quotes/{qid}")
def patch_quote(eid: int, qid: int, request: Request, payload: dict = Body(...)):
    """Edit quote commercial term; status changes go through the status
    endpoint (409/422 rules there)."""
    est = _load_estimate_or_404(eid)
    gov = _gov_cfg()
    _ensure_editor_allowed(est["state"], gov)
    if db.get_quote(eid, qid) is None:
        raise HTTPException(status_code=404,
                            detail="quote %s on estimate %s not found" % (qid, eid))
    fields = _validated_quote_payload(payload, est, gov, creating=False)
    q = db.update_quote(eid, qid, fields, _actor(request), est["revision"])
    if q is None:
        raise HTTPException(status_code=404,
                            detail="quote %s on estimate %s not found" % (qid, eid))
    return {"quote": quote_view(q)}


@router.post("/estimates/{eid}/quotes/{qid}/status")
def change_quote_status(eid: int, qid: int, request: Request,
                        payload: dict = Body(...)):
    """Quote workflow: draft->received->bound->expired (expired->received
    re-issues). Illegal transitions are 409."""
    est = _load_estimate_or_404(eid)
    gov = _gov_cfg()
    _ensure_editor_allowed(est["state"], gov)
    q = db.get_quote(eid, qid)
    if q is None:
        raise HTTPException(status_code=404,
                            detail="quote %s on estimate %s not found" % (qid, eid))
    to = (payload or {}).get("to") or (payload or {}).get("status")
    if not to:
        raise HTTPException(status_code=422,
                            detail={"errors": ["body needs {'to': status}"]})
    try:
        assert_transition_rules(q["status"], to, gov["quote_status_transitions"],
                                gov["quote_statuses"])
    except (StateRuleError, GovernanceError) as exc:
        _gov_exception_response(exc, q["status"], to,
                                allowed=list(gov["quote_status_transitions"]
                                             .get(q["status"], [])))
    updated = db.set_quote_status(eid, qid, to, q["status"], _actor(request),
                                  est["revision"])
    if updated is None:
        raise HTTPException(status_code=409, detail={
            "error": "quote %s status moved concurrently" % qid})
    return {"quote": quote_view(updated)}


@router.delete("/estimates/{eid}/quotes/{qid}")
def remove_quote(eid: int, qid: int, request: Request):
    est = _load_estimate_or_404(eid)
    gov = _gov_cfg()
    _ensure_editor_allowed(est["state"], gov)
    if not db.delete_quote(eid, qid, _actor(request), est["revision"]):
        raise HTTPException(status_code=404,
                            detail="quote %s on estimate %s not found" % (qid, eid))
    return {"deleted": qid}


@router.get("/estimates/{eid}/bid_tab")
def estimate_bid_tab(eid: int):
    """Bid-tab comparison: quotes grouped by linked lines (or whole
    estimate), low/median/high over active quotes, gap analysis vs the
    estimate carry, expiry warnings."""
    est = _load_estimate_or_404(eid)
    return bid_tab(db.list_quotes(eid), est["recap"])


# ----------------------------------------------------- actuals loop (E4)

def _drift_payload(est, today=None):
    rows = db.list_actual_rows(est["id"])
    drift = actuals_mod.drift_report(rows, load_labor_catalog(),
                                     actuals_mod.load_actuals_config(),
                                     today=today)
    return rows, drift


@router.post("/estimates/{eid}/actuals")
def import_actuals(eid: int, request: Request, payload: dict = Body(...)):
    """Import actual production rows {rows: [{catalog_id, location_id?,
    qty, hours, cost?, date, note?}]} against this estimate. qty/hours must
    be > 0, date ISO. One audit event per import; per-card drift is read
    from GET /estimates/{id}/actuals/drift."""
    est = _load_estimate_or_404(eid)
    body = payload or {}
    try:
        rows = actuals_mod.validate_actual_rows(body.get("rows"),
                                                load_labor_catalog())
    except actuals_mod.ActualsError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
    stored = db.add_actual_rows(eid, rows, _actor(request), est["revision"])
    _, drift = _drift_payload(est)
    return {"estimate_id": eid, "imported": len(stored), "rows": stored,
            "drift": drift}


@router.get("/estimates/{eid}/actuals")
def list_actuals(eid: int):
    est = _load_estimate_or_404(eid)
    rows, drift = _drift_payload(est)
    return {"estimate_id": eid, "rows": rows, "drift": drift}


@router.get("/estimates/{eid}/actuals/drift")
def actuals_drift(eid: int):
    """Per catalog card: effective norm-card mid vs actual unit hours -
    qty-weighted ratio + per-row ratios, drift pct, quality flag."""
    est = _load_estimate_or_404(eid)
    _, drift = _drift_payload(est)
    drift.update({"estimate_id": eid, "estimate_name": est["name"],
                  "revision": est["revision"]})
    return drift


@router.post("/estimates/{eid}/actuals/propose-refit")
def propose_refit(eid: int, request: Request):
    """Generate proposed new low/high labor cards from actual-driven drift
    (weighted median ratio, guarded bounds). PROPOSALS ONLY - never applied;
    frozen as a refit_proposals row with an audit event, the estimator
    reviews and applies explicitly via .../apply-refit."""
    est = _load_estimate_or_404(eid)
    rows, drift = _drift_payload(est)
    try:
        proposal = actuals_mod.propose_refit(drift, load_labor_catalog(),
                                             actuals_mod.load_actuals_config())
    except actuals_mod.ActualsError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
    saved = db.save_refit_proposal(est["id"], proposal, _actor(request))
    return {"estimate_id": est["id"], "refit_proposal_id": saved["id"],
            "proposal": proposal, "created_at": saved["created_at"],
            "actor": saved["actor"],
            "note": "stored for review - nothing was applied"}


@router.get("/estimates/{eid}/actuals/refit-proposals")
def list_refit_proposals(eid: int):
    est = _load_estimate_or_404(eid)
    return {"estimate_id": eid, "proposals": db.list_refit_proposals(eid)}


@router.post("/estimates/{eid}/actuals/apply-refit")
def apply_refit(eid: int, request: Request, payload: dict = Body(...)):
    """Owner-approved refit: {catalog_id, low, high, note?} -> writes the
    company override layer (config/company_labor_overrides.json). The
    engine prefers the override card on every later compute and stamps
    provenance on affected recap lines. Audit event on this estimate."""
    est = _load_estimate_or_404(eid)
    actor = _actor(request)
    rows, drift = _drift_payload(est)
    catalog = load_labor_catalog()
    try:
        cid_ok = actuals_mod._validate_refit_payload(
            {"catalog_id": (payload or {}).get("catalog_id"),
             "low": (payload or {}).get("low"),
             "high": (payload or {}).get("high"),
             "note": (payload or {}).get("note")}, catalog)
    except actuals_mod.ActualsError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
    cid = (payload or {}).get("catalog_id")
    rows_for_card = [r for r in rows if r.get("catalog_id") == str(cid).strip()]
    median_ratio = next((c["ratio_weighted_median"] for c in drift["cards"]
                         if c["catalog_id"] == cid_ok["catalog_id"]), None)
    layer = actuals_mod.apply_refit_to_file(payload or {}, {
        "estimate_id": est["id"], "n_rows": len(rows_for_card),
        "median_ratio": median_ratio, "actor": actor,
        "revision": est["revision"], "catalog": catalog})
    db.add_event(est["id"], actor, "refit.applied", est["revision"],
                 ["company_overrides#%s" % cid_ok["catalog_id"]],
                 "company labor override applied for %s: low=%s high=%s"
                 " (n_rows=%d median_ratio=%s)"
                 % (cid_ok["catalog_id"], cid_ok["low"], cid_ok["high"],
                    len(rows_for_card), median_ratio))
    return {"estimate_id": est["id"], "applied": layer["applied"],
            "overrides": layer["overrides"],
            "note": "company override written; every later compute prefers"
                    " this card (provenance stamped on affected recap lines)"}


# ------------------------------------------- inclusions/exclusions sheet

def _commercial_sheet(est, today=None):
    return build_commercial_sheet(
        est["recap"], db.list_quotes(est["id"]), load_estimating_config(),
        load_commercial_draft(), today or datetime.date.today())


@router.get("/estimates/{eid}/inclusions_exclusions")
def estimate_inclusions_exclusions(eid: int):
    est = _load_estimate_or_404(eid)
    return _commercial_sheet(est)


@router.get("/exports/estimates/{eid}/inclusions_exclusions.xlsx")
def export_inclusions_exclusions_xlsx(eid: int):
    """Commercial draft sheet: template + definition-derived + vendor-quote
    inclusions/exclusions (sheets: Inclusions_Exclusions + Quotes)."""
    est = _load_estimate_or_404(eid)
    blob = commercial_xlsx(_commercial_sheet(est), _governance_view(est))
    return Response(
        content=blob,
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={"Content-Disposition":
                 'attachment; filename="inclusions_exclusions-%s.xlsx"' % eid},
    )


@router.get("/exports/estimates/{eid}/inclusions_exclusions.pdf")
def export_inclusions_exclusions_pdf(eid: int):
    est = _load_estimate_or_404(eid)
    blob = commercial_pdf(_commercial_sheet(est), _governance_view(est))
    return Response(
        content=blob,
        media_type="application/pdf",
        headers={"Content-Disposition":
                 'attachment; filename="inclusions_exclusions-%s.pdf"' % eid},
    )


# ------------------------------- commercial wrap: schedule / cashflow / bid
# form / what-if scenarios (phase E3, v0.6.0)

def _commercial_cfg():
    return load_commercial_config()


@router.get("/estimates/{eid}/schedule")
def estimate_schedule(eid: int):
    """Crew-loading plan: per-location crew-days from wrapped mid hours over
    single-crew daily capacity, on a working-day calendar blocked by dated
    downtime windows; early bound = all configured crews, late bound =
    single-crew serial; weekly hours above the OT threshold flagged."""
    est = _load_estimate_or_404(eid)
    try:
        sched = crew_schedule(est["recap"], est["definition"], _commercial_cfg())
    except CommercialError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
    sched["estimate_id"] = eid
    sched["state"] = est["state"]
    sched["revision"] = est["revision"]
    return sched


@router.get("/estimates/{eid}/cashflow")
def estimate_cashflow(eid: int):
    """Monthly cash curve from the config milestone splits (must sum to 100)
    of the wrapped grand total (mid band); INSTALL spans the schedule months."""
    est = _load_estimate_or_404(eid)
    try:
        curve = cash_curve(est["recap"], est["definition"], _commercial_cfg())
    except CommercialError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
    curve["estimate_id"] = eid
    return {"estimate_id": eid, "cashflow": curve}


@router.get("/exports/estimates/{eid}/cashflow.csv")
def export_cashflow_csv(eid: int):
    est = _load_estimate_or_404(eid)
    try:
        curve = cash_curve(est["recap"], est["definition"], _commercial_cfg())
    except CommercialError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
    return Response(
        content=cash_curve_csv(curve),
        media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="cashflow-%s.csv"' % eid},
    )


@router.get("/exports/estimates/{eid}/cashflow.xlsx")
def export_cashflow_xlsx(eid: int):
    est = _load_estimate_or_404(eid)
    try:
        curve = cash_curve(est["recap"], est["definition"], _commercial_cfg())
    except CommercialError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
    return Response(
        content=cash_curve_xlsx(curve),
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={"Content-Disposition":
                 'attachment; filename="cashflow-%s.xlsx"' % eid},
    )


def _compute_scenario(est, scenario):
    """Validate + compute one scenario recap against the estimate's current
    base (overlay applied fresh at read time; quote swaps flow through the
    full wrap chain via compute_estimate's pre-chain hook)."""
    overlay = scenario["overlay"]
    definition = est["definition"]
    quotes = db.list_quotes(est["id"])
    try:
        validate_scenario_overlay(overlay, definition,
                                  est["recap"]["lines"], quotes)
    except CommercialError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
    defn2 = scenario_definition(definition, overlay)
    est_cfg = merge_factor_overrides(load_estimating_config(),
                                     overlay.get("factor_overrides") or {})
    swap = overlay.get("quote_swap")
    swapper = None
    if swap:
        def swapper(lines):  # noqa: E306 - closure over swap
            apply_quote_swap({"lines": lines}, swap, quotes)
    try:
        return compute_estimate(defn2, estimating=est_cfg, quote_swap=swapper)
    except EstimateError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})


@router.post("/estimates/{eid}/scenarios")
def create_scenario_endpoint(eid: int, request: Request,
                             payload: dict = Body(...)):
    """Store a named what-if scenario: {name, notes?, overlay} with
    access_overrides / factor_overrides / qty_overrides / quote_swap. The
    overlay is stored; results are recomputed against the current base at
    diff time so scenarios never go stale."""
    est = _load_estimate_or_404(eid)
    body = payload or {}
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422,
                            detail={"errors": ["scenario name is required"]})
    overlay = body.get("overlay")
    try:
        validate_scenario_overlay(overlay, est["definition"],
                                  est["recap"]["lines"], db.list_quotes(eid))
    except CommercialError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
    scenario = db.create_scenario(eid, name, body.get("notes"), overlay,
                                  est["revision"], _actor(request))
    return {"scenario": scenario}


@router.get("/estimates/{eid}/scenarios")
def list_scenarios_endpoint(eid: int):
    _load_estimate_or_404(eid)
    return {"estimate_id": eid, "scenarios": db.list_scenarios(eid)}


@router.get("/estimates/{eid}/scenarios/{sid}")
def get_scenario_endpoint(eid: int, sid: int):
    _load_estimate_or_404(eid)
    sc = db.get_scenario(eid, sid)
    if sc is None:
        raise HTTPException(status_code=404,
                            detail="scenario %s not found" % sid)
    return {"scenario": sc}


def _scenario_or_404(eid, sid):
    sc = db.get_scenario(eid, sid)
    if sc is None:
        raise HTTPException(status_code=404,
                            detail="scenario %s not found" % sid)
    return sc


@router.get("/estimates/{eid}/scenarios/{sid}/diff")
def scenario_diff_endpoint(eid: int, sid: int):
    """Line-level what-if report: base vs scenario recap, only changed lines
    (qty / hours mid / labor mid moved) plus the totals delta."""
    est = _load_estimate_or_404(eid)
    sc = _scenario_or_404(eid, sid)
    scen_recap = _compute_scenario(est, sc)
    return {
        "estimate_id": eid,
        "scenario": {"id": sc["id"], "name": sc["name"],
                     "notes": sc["notes"],
                     "base_revision": sc["base_revision"],
                     "overlay": sc["overlay"]},
        "diff": scenario_diff(est["recap"], scen_recap),
    }


@router.delete("/estimates/{eid}/scenarios/{sid}")
def delete_scenario_endpoint(eid: int, sid: int, request: Request):
    _load_estimate_or_404(eid)
    if not db.delete_scenario(eid, sid, _actor(request)):
        raise HTTPException(status_code=404,
                            detail="scenario %s not found" % sid)
    return {"deleted": sid, "note": "scenario overlay removed (audit event"
                                    " logged)"}


def _bid_form_data(est, today=None):
    try:
        return build_bid_form(
            est["definition"], est["recap"], db.list_quotes(est["id"]),
            load_estimating_config(), _commercial_cfg(),
            load_commercial_draft(), today)
    except (EstimateError, CommercialError) as exc:
        errors = getattr(exc, "errors", None) or [str(exc)]
        raise HTTPException(status_code=422, detail={"errors": errors})


@router.get("/exports/estimates/{eid}/bid_form.pdf")
def export_bid_form_pdf(eid: int):
    """Bid form PDF: base bid, named alternates (engine-priced), unit
    prices per antenna point, condensed SOV, exclusions summary, signature
    block. Mid-band dollars."""
    est = _load_estimate_or_404(eid)
    data = _bid_form_data(est)
    return Response(
        content=bid_form_pdf(data),
        media_type="application/pdf",
        headers={"Content-Disposition":
                 'attachment; filename="bid_form-%s.pdf"' % eid},
    )


@router.get("/exports/estimates/{eid}/bid_form.xlsx")
def export_bid_form_xlsx(eid: int):
    est = _load_estimate_or_404(eid)
    data = _bid_form_data(est)
    return Response(
        content=bid_form_xlsx(data),
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={"Content-Disposition":
                 'attachment; filename="bid_form-%s.xlsx"' % eid},
    )


@router.delete("/estimates/{eid}")
def delete_estimate(eid: int):
    """Hard delete of the estimate and its whole governance history; blocked
    from approved/sent (supersede first) per config/governance.json."""
    est = _load_estimate_or_404(eid)
    gov = _gov_cfg()
    if est["state"] not in gov["states_allowing_delete"]:
        raise HTTPException(status_code=409, detail={
            "error": "estimate is in state '%s'; delete is allowed only in %s"
                     " (supersede approved/sent estimates instead)"
                     % (est["state"], gov["states_allowing_delete"]),
            "state": est["state"]})
    if not db.delete_estimate(eid):
        raise HTTPException(status_code=404, detail="estimate %s not found" % eid)
    return {"deleted": eid, "note": "estimate + revisions + audit +"
                                    " assumptions + quotes removed"}



def _recap_xlsx_bytes(recap, governance=None, sanity=None):
    name = recap.get("name") or "Estimate"
    header = [
        ["ROMtool %s Class 1 recap - %s" % (__version__, name)],
        ["engine", recap.get("engine"), "catalog rows", recap["catalog"]["row_count"]],
        ["schedule", "night=%s" % recap["schedule"]["night"],
         "weekend=%s" % recap["schedule"]["weekend"]],
    ]
    if governance:
        a, q = governance["assumptions"], governance["quotes"]
        header.append(["state", governance["state"], "revision",
                       governance["revision"], "as_of", governance["as_of"]])
        header.append(["assumptions", "open=%d verified=%d closed=%d" %
                       (a["open"], a["verified"], a["closed"]), "total", a["total"]])
        header.append(["quotes", "draft=%d received=%d bound=%d expired=%d" %
                       (q["draft"], q["received"], q["bound"], q["expired"]),
                       "total", q["total"]])
        header.extend(["quote_warning", w["code"], w["message"]]
                      for w in governance["quote_warnings"])
    if sanity:
        from romtool.sanity import sanity_export_rows
        header.append(["sanity", "archetype", sanity.get("archetype") or "none",
                       "hard=%d warnings=%d" % (sanity["counts"]["hard"],
                                                sanity["counts"]["warnings"])])
        header.extend(["sanity_row", r[0], r[1], r[2] or "", r[3] if len(r) > 3 else ""]
                      for r in sanity_export_rows(sanity))
    header.append([])
    cols = ["location", "zone", "id", "item", "unit", "qty", "quality", "kind",
            "base_min_mid", "cond", "access", "sched", "stacked_mid",
            "n01", "n02", "task_h_mid", "foreman_h_mid", "super_h_mid",
            "hours_low", "hours_mid", "hours_high", "labor_usd_mid",
            "cont_pct", "cont_usd_mid", "cost_carry_usd", "derivation"]
    rows = list(header) + [cols]
    for ln in recap["lines"]:
        f = ln["factors"]
        rows.append([
            ln["location_name"], ln["zone"], ln["catalog_id"], ln["item"],
            ln["unit"], ln["qty"], ln["source_quality"], ln["kind"],
            ln["base_minutes"]["mid"],
            "+".join("%s=%s" % (c["key"], c["value"]) for c in f["condition"]) or "",
            ("%s=%s" % (f["access"]["row"], f["access"]["value"])
             if f["access"] else "none"),
            "+".join("%s=%s" % (c["key"], c["value"]) for c in f["schedule"]) or "",
            ln["stacked_factor"]["mid"],
            ln["supervision"]["n01_foreman_ratio"],
            ln["supervision"]["n02_pct"],
            ln["task_hours"]["mid"],
            ln["supervision"]["n01_foreman_hours"]["mid"],
            ln["supervision"]["n02_super_hours"]["mid"],
            ln["hours"]["low"], ln["hours"]["mid"], ln["hours"]["high"],
            ln["labor_usd"]["total"]["mid"],
            ln["contingency_pct"], ln["contingency_usd"]["mid"],
            (ln.get("cost_carry_usd") or {"mid": ""})["mid"],
            ln.get("derivation") or "",
        ])
    t = recap["totals"]
    rows.append([])
    rows.append(["legend", "stack", "base minutes x condition x access x schedule "
                 "= task hours; + N01 foreman + N02 super = wrapped hours"])
    rows.append(["legend", "rates", "tech $%.0f/h; foreman $%.0f/h (also bills N02)"
                 % (recap["cost"]["rate_card"]["tech_rate"],
                    recap["cost"]["rate_card"]["foreman_rate"])])
    rows.append(["legend", "contingency", next(
        m["basis"] for m in recap["money_lines"] if m["code"] == "CONTINGENCY_N05")])
    rows.append(["legend", "wrap_order",
                 "labor -> downtime -> small tools -> escalation -> taxes ->"
                 " insurance/bond -> PM -> OH -> profit -> contingency"])
    rows.append([])
    rows.append(["rollup", "code", "label", "basis", "low", "mid", "high"])
    rows.append(["hours", "CREW_HOURS", "Total crew-hours",
                 "task + foreman (N01) + super (N02)",
                 t["hours"]["low"], t["hours"]["mid"], t["hours"]["high"]])
    rows.append(["hours", "CREW_DAYS/WEEKS", "Crew-days; crew-weeks; avg h/week",
                 "ceil(direct h / crew-day cap); ceil(h / crew-week cap)",
                 t["crew_days"], t["crew_weeks"], t["hours_per_crew_week"]])
    rows.append(["zones", "", "", "", "", "", ""])
    for z in recap["zone_rollups"]:
        rows.append(["zone_rollup", z["key"], "%d lines" % z["line_count"], "",
                     z["hours"]["low"], z["hours"]["mid"], z["hours"]["high"]])
    for m in recap["money_lines"]:
        rows.append(["money", m["code"], m["label"], m["basis"],
                     m["amount"]["low"], m["amount"]["mid"], m["amount"]["high"]])
    return encode_xlsx([{"name": "Recap", "rows": rows}])


@router.get("/exports/estimates/{eid}/recap.csv")
def export_estimate_csv(eid: int):
    est = _load_estimate_or_404(eid)
    return Response(
        content=recap_csv(est["recap"], _governance_view(est),
                          _sanity_report(est)),
        media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="recap-%s.csv"' % eid},
    )


@router.get("/exports/estimates/{eid}/recap.xlsx")
def export_estimate_xlsx(eid: int):
    est = _load_estimate_or_404(eid)
    return Response(
        content=_recap_xlsx_bytes(est["recap"], _governance_view(est),
                                  _sanity_report(est)),
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={"Content-Disposition":
                 'attachment; filename="recap-%s.xlsx"' % eid},
    )


# ---------------------------------------------------- RF link: draft from RF project

@router.post("/projects/{pid}/draft-estimate")
def draft_estimate_from_project(pid: int, request: Request,
                                store: bool = Query(False)):
    """Build a Class 1 estimate definition from the RF project (antenna
    points per floor, coax footage per size from cascade cable runs,
    riser/horizontal split, hub closets, headend + booster DC load block
    feeding the O-zone auto-sizer, L-zone allowances, H-zone testing per
    floor/band). Returns the REVIEW DRAFT; with ?store=true the draft is
    computed and stored as a real estimate (revision 1, state draft)."""
    proj = _load_or_404(pid)
    cfg = _proj_config(proj["model"])
    try:
        draft = draft_for_project(proj, cfg, catalog=load_labor_catalog())
    except RfLinkError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
    if not store:
        draft["stored"] = False
        draft["note"] = ("review draft - not stored; re-POST with ?store=true"
                         " to compute + store as an estimate revision 1")
        return draft
    definition = draft["definition"]
    recap = _compute_from_body(definition)
    gov = _gov_cfg()
    actor = _actor(request)
    rec = db.create_estimate(recap["name"], definition, recap, actor)
    if rec is None:  # pragma: no cover - row just inserted
        raise HTTPException(status_code=500, detail="estimate insert failed")
    sync = db.sync_assumptions(rec["id"], recap["lines"], 1,
                               gov["default_assumption_owner"],
                               gov.get("auto_assumption_source_quality", "E"),
                               actor=actor)
    rec = db.get_estimate(rec["id"]) or rec
    out = _estimate_response(rec)
    out["governance"]["assumptions"]["auto_populated"] = len(sync["added"])
    return {"draft_basis": draft["basis"], "project_id": pid,
            "project_name": proj["name"], "stored": True, "estimate": out}


# ---------------------------------------------------------------- AI layer (E4)

def _ai_503(schema, reason):
    return JSONResponse(
        status_code=503,
        content={"detail": "ai gateway unreachable",
                 "schema": schema, "reason": reason})


def _ai_502(schema, reason):
    return JSONResponse(
        status_code=502,
        content={"detail": "ai gateway response did not match the"
                           " documented schema", "schema": schema,
                 "reason": reason})


@router.get("/reports/stadium")
def reports_stadium():
    """E5 morning-package manifest for the Generic 60k-Seat Stadium: package
    dir, file sizes, estimate/project ids, headline stats and the API links.
    404 (with the rebuild command) when the package has not been built."""
    pkg_dir = _stadium_package_dir()
    mpath = os.path.join(pkg_dir, "manifest.json")
    if not os.path.exists(mpath):
        raise HTTPException(status_code=404, detail={
            "error": "no stadium package manifest at %s" % mpath,
            "rebuild": "cd <repo> && .venv/bin/python "
                       "scripts/build_stadium_package.py",
            "package_dir": pkg_dir})
    try:
        with open(mpath, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail={
            "error": "stadium package manifest is not valid JSON: %s" % exc})
    return {"package_dir": pkg_dir, "manifest": manifest}


@router.post("/ai/rfp-scan")
def ai_rfp_scan(payload: dict = Body(...)):
    """RFP text -> requirements / scope matrix + gaps (structured JSON),
    through the configured LLM gateway. 503 + documented schema when no
    gateway is configured or reachable."""
    schema = ai_mod.SCHEMAS["rfp_scan"]
    body = payload or {}
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=422,
                            detail={"errors": ['body needs {"text": '
                                               '"RFP text"}']})
    try:
        cfg = ai_mod.load_ai_config()
        out = ai_mod.rfp_scan(text, cfg)
    except ai_mod.AiUnavailable as exc:
        return _ai_503(schema, str(exc))
    except ai_mod.AiResponseError as exc:
        return _ai_502(schema, str(exc))
    return {"schema": schema, **out}


def _recap_or_from_estimate(payload, with_assumptions=False):
    body = payload or {}
    rec, est = body.get("recap"), None
    eid = body.get("estimate_id")
    if rec is None:
        if not eid:
            raise HTTPException(status_code=422, detail={"errors": [
                'body needs "recap" (an estimate recap object) or'
                ' "estimate_id" (a stored estimate id)']})
        est = _load_estimate_or_404(int(eid))
        rec = est["recap"]
    assumptions = []
    if with_assumptions:
        if est is not None:
            rows = db.list_assumptions(int(eid))
        else:
            rows = []
        assumptions = [{"text": r["text"], "owner": r["owner"],
                        "status": r["status"], "review_date": r["review_date"]}
                       for r in rows]
    return rec, assumptions, est


@router.post("/ai/scenario-suggest")
def ai_scenario_suggest(payload: dict = Body(...)):
    """Recap (or stored estimate id) -> suggested what-if overlays with
    rationale. Suggestion overlays are validated against the estimate when
    one is referenced; invalid overlays are a 422 from the scenario rules."""
    schema = ai_mod.SCHEMAS["scenario_suggest"]
    body = payload or {}
    rec, _, est = _recap_or_from_estimate(body)
    eid = est["id"] if est is not None else body.get("estimate_id")
    try:
        cfg = ai_mod.load_ai_config()
        out = ai_mod.scenario_suggest(rec, cfg)
    except ai_mod.AiUnavailable as exc:
        return _ai_503(schema, str(exc))
    except ai_mod.AiResponseError as exc:
        return _ai_502(schema, str(exc))
    if est is not None:
        for i, sug in enumerate(out["result"].get("suggestions") or []):
            try:
                validate_scenario_overlay(sug.get("overlay") or {},
                                          est["definition"],
                                          est["recap"]["lines"],
                                          db.list_quotes(est["id"]))
            except CommercialError as exc:
                raise HTTPException(status_code=422, detail={
                    "errors": ["suggestion[%d] %r overlay invalid: %s"
                               % (i, sug.get("name"), exc.errors)]})
    return {"estimate_id": eid, "schema": schema, **out}


@router.post("/ai/bid-memo")
def ai_bid_memo(payload: dict = Body(...)):
    """Recap + assumption register -> narrative bid memo (JSON with memo +
    highlights) through the configured gateway. 503 + schema when the
    gateway is unavailable."""
    schema = ai_mod.SCHEMAS["bid_memo"]
    body = payload or {}
    rec, assumptions, est = _recap_or_from_estimate(body,
                                                    with_assumptions=True)
    try:
        cfg = ai_mod.load_ai_config()
        out = ai_mod.bid_memo(rec, assumptions, cfg)
    except ai_mod.AiUnavailable as exc:
        return _ai_503(schema, str(exc))
    except ai_mod.AiResponseError as exc:
        return _ai_502(schema, str(exc))
    out["assumptions_included"] = len(assumptions)
    if est is not None:
        out["estimate_id"] = est["id"]
    return {"schema": schema, **out}


# ---------------------------------------------------------------- floors

@router.post("/projects/{pid}/floors")
def create_floor(pid: int, payload: dict = Body(...)):
    proj = _load_or_404(pid)
    model = proj["model"]
    level_default = len(model.get("floors", []))
    level_val = payload.get("level")
    floor = {
        "id": _new_id("f"),
        "name": payload.get("name") or "Floor %d" % (level_val if level_val is not None else level_default),
        "level": int(level_val) if level_val is not None else level_default,
        "ppm": float(_num(payload.get("ppm", 10.0))),
        "w_px": int(_num(payload.get("w_px", 200))),
        "h_px": int(_num(payload.get("h_px", 150))),
        "image": None,
        "walls": [],
        "elements": [],
    }
    if payload.get("building_type"):
        floor["building_type"] = _check_building_type(payload["building_type"])
    if payload.get("outline"):
        floor["outline"] = _check_outline(payload["outline"])
    model.setdefault("floors", []).append(floor)
    _save(pid, proj["name"], model)
    return {"floor": {k: v for k, v in floor.items() if k != "image"}}


@router.patch("/projects/{pid}/floors/{fid}")
def patch_floor(pid: int, fid: str, payload: dict = Body(...)):
    proj = _load_or_404(pid)
    model = proj["model"]
    floor = _find_floor(model, fid)
    for key in ("name", "level", "ppm", "w_px", "h_px"):
        if key in payload:
            floor[key] = payload[key]
    if "ppm" in floor:
        floor["ppm"] = float(_num(floor["ppm"]))
    if "building_type" in payload:
        bt = _check_building_type(payload["building_type"])
        if bt is None:
            floor.pop("building_type", None)
        else:
            floor["building_type"] = bt
    if "outline" in payload:
        ol = _check_outline(payload["outline"])
        if ol is None:
            floor.pop("outline", None)
        else:
            floor["outline"] = ol
    _save(pid, proj["name"], model)
    return {"floor": {k: v for k, v in floor.items() if k != "image"}}


@router.delete("/projects/{pid}/floors/{fid}")
def delete_floor(pid: int, fid: str):
    proj = _load_or_404(pid)
    model = proj["model"]
    before = len(model["floors"])
    model["floors"] = [fl for fl in model["floors"] if fl.get("id") != fid]
    if len(model["floors"]) == before:
        raise HTTPException(status_code=404, detail="floor %s not found" % fid)
    _save(pid, proj["name"], model)
    return {"deleted": fid}


@router.post("/projects/{pid}/floors/{fid}/image")
def upload_floor_image(pid: int, fid: str, file: UploadFile = File(...)):
    proj = _load_or_404(pid)
    model = proj["model"]
    floor = _find_floor(model, fid)
    data = file.file.read()
    _require(len(data) > 8, "empty upload")
    mime = "image/png"
    w = h = None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        mime = "image/png"
    elif data[:2] == b"\xff\xd8":
        mime = "image/jpeg"
        w, h = _jpg_dims(data)
    else:
        _require(False, "only PNG or JPEG images supported")
    floor["image"] = {
        "mime": mime,
        "data_b64": base64.b64encode(data).decode("ascii"),
        "w": w,
        "h": h,
    }
    floor["w_px"] = int(w)
    floor["h_px"] = int(h)
    _save(pid, proj["name"], model)
    return {"floor": _floor_public(floor)}


def _floor_public(floor):
    img = floor.get("image")
    out = {k: v for k, v in floor.items() if k != "image"}
    out["image"] = ({"mime": img.get("mime"), "w": img.get("w"), "h": img.get("h")}
                    if img else None)
    return out


def _jpg_dims(data):
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + seglen
    raise HTTPException(status_code=422, detail="could not parse JPEG dimensions")


@router.get("/projects/{pid}/floors/{fid}/image")
def get_floor_image(pid: int, fid: str):
    proj = _load_or_404(pid)
    model = proj["model"]
    floor = _find_floor(model, fid)
    img = floor.get("image")
    if not img or not img.get("data_b64"):
        raise HTTPException(status_code=404, detail="floor has no image")
    return Response(
        content=base64.b64decode(img["data_b64"]),
        media_type=img.get("mime", "image/png"),
    )


# ------------------------------------------------------------- clutter map

def _clutter_error(exc):
    return HTTPException(status_code=422, detail={"errors": [str(exc)]})


def _map_summary(cmap):
    return {k: v for k, v in cmap.items() if k not in ("auto_grid",)}


@router.post("/projects/{pid}/floors/{fid}/clutter-map")
def build_floor_clutter_map(
        pid: int, fid: str, file: UploadFile = File(None),
        cell_m: float = Query(None), dark_threshold: int = Query(None),
        min_wall_len_m: float = Query(None), smooth_cells: int = Query(None),
        core_fill_pct: float = Query(None), light_cpm: float = Query(None),
        dense_cpm: float = Query(None), detect_exterior: bool = Query(None),
        pdf_page: int = Query(0), keep_overrides: bool = Query(True)):
    """Score a floor plan image (PNG; JPEG needs Pillow, PDF needs PyMuPDF)
    into a clutter class per square at the floor's scale (ppm) and store it on
    the floor. With no file, the floor's own image is used. Manual overrides of
    the previous map are kept when the grid is unchanged."""
    proj = _load_or_404(pid)
    model = proj["model"]
    floor = _find_floor(model, fid)
    cfg = _proj_config(model)
    if file is not None:
        data = file.file.read()
    else:
        img = floor.get("image")
        if not img or not img.get("data_b64"):
            raise HTTPException(status_code=422, detail={
                "errors": ["no file uploaded and the floor has no image"]})
        data = base64.b64decode(img["data_b64"])
    try:
        iw, ih, gray = decode_image(data, floor_w_px=floor.get("w_px"), pdf_page=pdf_page)
        cmap = build_clutter_map(
            gray, iw, ih, floor, cfg, outline=floor.get("outline"),
            cell_m=cell_m, dark_threshold=dark_threshold, min_wall_len_m=min_wall_len_m,
            smooth_cells=smooth_cells, core_fill_pct=core_fill_pct, light_cpm=light_cpm,
            dense_cpm=dense_cpm, detect_exterior=detect_exterior)
        old = floor.get("clutter_map")
        if (keep_overrides and old and old.get("overrides")
                and (old["rows"], old["cols"], old["cell_px"])
                == (cmap["rows"], cmap["cols"], cmap["cell_px"])):
            cmap["overrides"] = dict(old["overrides"])
            from romtool.clutter_map import regrid
            regrid(cmap)
    except ClutterMapError as exc:
        raise _clutter_error(exc)
    floor["clutter_map"] = cmap
    _save(pid, proj["name"], model)
    return {"floor_id": fid, "clutter_map": _map_summary(cmap)}


@router.get("/projects/{pid}/floors/{fid}/clutter-map")
def get_floor_clutter_map(pid: int, fid: str):
    floor = _find_floor(_load_or_404(pid)["model"], fid)
    cmap = floor.get("clutter_map")
    if not cmap:
        raise HTTPException(status_code=404, detail="floor has no clutter map")
    return {"floor_id": fid, "clutter_map": _map_summary(cmap)}


@router.patch("/projects/{pid}/floors/{fid}/clutter-map")
def patch_floor_clutter_map(pid: int, fid: str, payload: dict = Body(...)):
    """Manual overrides per square. Body: {"set": [{"row", "col"} | {"x_m",
    "y_m"} | {"rect_m": [x0, y0, x1, y1]}, "class": id or null/"auto" to clear],
    "clear": [{...cell spec}], "clear_all": bool}."""
    proj = _load_or_404(pid)
    model = proj["model"]
    floor = _find_floor(model, fid)
    cmap = floor.get("clutter_map")
    if not cmap:
        raise HTTPException(status_code=404, detail="floor has no clutter map")
    try:
        apply_overrides(cmap, _proj_config(model), float(floor.get("ppm", 10.0)),
                        set_cells=payload.get("set"), clear_cells=payload.get("clear"),
                        clear_all=bool(payload.get("clear_all", False)))
    except (ClutterMapError, TypeError, ValueError) as exc:
        raise _clutter_error(exc)
    _save(pid, proj["name"], model)
    return {"floor_id": fid, "clutter_map": _map_summary(cmap)}


@router.delete("/projects/{pid}/floors/{fid}/clutter-map")
def delete_floor_clutter_map(pid: int, fid: str):
    proj = _load_or_404(pid)
    model = proj["model"]
    floor = _find_floor(model, fid)
    if not floor.pop("clutter_map", None):
        raise HTTPException(status_code=404, detail="floor has no clutter map")
    _save(pid, proj["name"], model)
    return {"deleted": "clutter_map", "floor_id": fid}


# ---------------------------------------------------------------- walls

@router.post("/projects/{pid}/floors/{fid}/walls")
def add_walls(pid: int, fid: str, payload: dict = Body(...)):
    proj = _load_or_404(pid)
    model = proj["model"]
    floor = _find_floor(model, fid)
    cfg = _proj_config(model)
    batch = payload.get("walls") if isinstance(payload.get("walls"), list) else [payload]
    created = []
    for wl in batch:
        mat = wl.get("material")
        _require(mat in cfg["wall_materials"], "unknown wall material '%s'" % mat)
        wall = {
            "id": _new_id("w"),
            "x1": _num(wl.get("x1", 0)), "y1": _num(wl.get("y1", 0)),
            "x2": _num(wl.get("x2", 0)), "y2": _num(wl.get("y2", 0)),
            "material": mat,
        }
        floor["walls"].append(wall)
        created.append(wall)
    _save(pid, proj["name"], model)
    return {"walls": created}


@router.patch("/projects/{pid}/walls/{wid}")
def patch_wall(pid: int, wid: str, payload: dict = Body(...)):
    proj = _load_or_404(pid)
    model = proj["model"]
    for fl in model["floors"]:
        for wl in fl.get("walls", []):
            if wl.get("id") == wid:
                if "material" in payload:
                    cfg = _proj_config(model)
                    _require(payload["material"] in cfg["wall_materials"],
                             "unknown wall material '%s'" % payload["material"])
                    wl["material"] = payload["material"]
                for key in ("x1", "y1", "x2", "y2"):
                    if key in payload:
                        wl[key] = _num(payload[key])
                _save(pid, proj["name"], model)
                return {"wall": wl}
    raise HTTPException(status_code=404, detail="wall %s not found" % wid)


@router.delete("/projects/{pid}/walls/{wid}")
def delete_wall(pid: int, wid: str):
    proj = _load_or_404(pid)
    model = proj["model"]
    for fl in model["floors"]:
        before = len(fl.get("walls", []))
        fl["walls"] = [wl for wl in fl.get("walls", []) if wl.get("id") != wid]
        if len(fl["walls"]) != before:
            _save(pid, proj["name"], model)
            return {"deleted": wid}
    raise HTTPException(status_code=404, detail="wall %s not found" % wid)


@router.get("/projects/{pid}/floors/{fid}/walls")
def list_walls(pid: int, fid: str):
    proj = _load_or_404(pid)
    floor = _find_floor(proj["model"], fid)
    return {"walls": floor.get("walls", [])}


# ---------------------------------------------------------------- critical areas

def _norm_poly_points(raw):
    """Validate + normalize a polygon point list to [[x, y], ...] floats."""
    if not isinstance(raw, list) or len(raw) < 3:
        raise HTTPException(status_code=422,
                            detail="polygon needs at least 3 points, got %r" % (raw,))
    pts = []
    for p in raw:
        if isinstance(p, dict):
            _require("x" in p and "y" in p, "polygon point needs x and y: %r" % (p,))
            px, py = p["x"], p["y"]
        elif isinstance(p, (list, tuple)) and len(p) == 2:
            px, py = p[0], p[1]
        else:
            raise HTTPException(status_code=422,
                                detail="polygon point must be [x, y] or {x, y}: %r" % (p,))
        pts.append([round(_num(px), 3), round(_num(py), 3)])
    return pts


@router.post("/projects/{pid}/floors/{fid}/critical_areas")
def add_critical_area(pid: int, fid: str, payload: dict = Body(...)):
    proj = _load_or_404(pid)
    model = proj["model"]
    floor = _find_floor(model, fid)
    batch = payload.get("critical_areas") if isinstance(payload.get("critical_areas"), list) else [payload]
    created = []
    floor.setdefault("critical_areas", [])
    for ca in batch or []:
        _require(isinstance(ca, dict),
                 "critical area entry must be an object: %r" % (ca,))
        rec = {
            "id": _new_id("ca"),
            "label": ca.get("label"),
            "points": _norm_poly_points(ca.get("points")),
        }
        floor["critical_areas"].append(rec)
        created.append(rec)
    _save(pid, proj["name"], model)
    return {"critical_areas": created}


@router.get("/projects/{pid}/floors/{fid}/critical_areas")
def list_critical_areas(pid: int, fid: str):
    proj = _load_or_404(pid)
    floor = _find_floor(proj["model"], fid)
    return {"critical_areas": floor.get("critical_areas", [])}


@router.patch("/projects/{pid}/critical_areas/{caid}")
def patch_critical_area(pid: int, caid: str, payload: dict = Body(...)):
    proj = _load_or_404(pid)
    model = proj["model"]
    for fl in model.get("floors", []):
        for ca in fl.get("critical_areas", []):
            if ca.get("id") == caid:
                if "label" in payload:
                    ca["label"] = payload["label"]
                if "points" in payload:
                    ca["points"] = _norm_poly_points(payload["points"])
                _save(pid, proj["name"], model)
                return {"critical_area": ca}
    raise HTTPException(status_code=404, detail="critical area %s not found" % caid)


@router.delete("/projects/{pid}/critical_areas/{caid}")
def delete_critical_area(pid: int, caid: str):
    proj = _load_or_404(pid)
    model = proj["model"]
    for fl in model.get("floors", []):
        before = len(fl.get("critical_areas", []))
        fl["critical_areas"] = [ca for ca in fl.get("critical_areas", [])
                                if ca.get("id") != caid]
        if len(fl["critical_areas"]) != before:
            _save(pid, proj["name"], model)
            return {"deleted": caid}
    raise HTTPException(status_code=404, detail="critical area %s not found" % caid)


# ---------------------------------------------------------------- NFPA report


# ---------------------------------------------------------------- elements

def _validate_element(model, cfg, el, creating):
    t = el.get("type")
    if t == "cable_run":
        nodes = {e["id"] for _, e in _all_elements(model)
                 if e.get("type") in ("source", "repeater", "remote", "splitter", "tapper",
                                      "coupler", "antenna")}
        for ref in ("from_id", "to_id"):
            _require(el.get(ref) in nodes,
                     "cable run %s must reference an existing chain element (%s)"
                     % (ref, el.get(ref)))
        _require(el.get("from_id") != el.get("to_id"), "cable run endpoints must differ")
        cable_id = el.get("cable_id")
        _require(cable_id in cfg["cables"], "unknown cable_id '%s'" % cable_id)
        el["length_m"] = _num(el.get("length_m", 0))
        _require(el["length_m"] >= 0, "length_m must be >= 0")
        if el.get("port") is not None:
            _require(el["port"] in ("through", "tap", "coupled"),
                     "port role must be through|tap|coupled")
        return
    _require(t in ELEMENT_TYPES, "unknown element type '%s'" % t)
    if t == "source":
        sub_cfg = (cfg.get("source") or {}).get("subtypes", {})
        sub = el.get("subtype")
        if sub is not None:
            known = set(sub_cfg) | {"off_air"}
            _require(str(sub) in known,
                     "unknown source subtype '%s' (known: %s)" % (sub, ", ".join(sorted(known))))
        if el.get("injection_dbm") is not None:
            el["injection_dbm"] = _num(el["injection_dbm"])
    if t == "splitter":
        ways = int(el.get("ways", 2))
        _require(ways in (2, 3, 4), "splitter ways must be 2, 3 or 4")
        el["ways"] = ways
        if el.get("il_db") is not None:
            el["il_db"] = _num(el["il_db"])
    elif t == "antenna":
        _require(el.get("antenna_id") in cfg["antennas"],
                 "unknown antenna_id '%s'" % el.get("antenna_id"))
    elif t in ("tapper", "coupler"):
        for key in ("through_il_db", "tap_loss_db", "coupled_loss_db"):
            if key in el and el[key] is not None:
                el[key] = _num(el[key])
    for key in ("x", "y", "donor_gain_db", "feeder_loss_db", "dl_gain_db",
                "max_dl_out_dbm", "gain_db", "max_port_dbm", "ports",
                "injection_dbm"):
        if key in el and el[key] is not None:
            el[key] = _num(el[key])


@router.post("/projects/{pid}/floors/{fid}/elements")
def add_element(pid: int, fid: str, payload: dict = Body(...)):
    proj = _load_or_404(pid)
    model = proj["model"]
    floor = _find_floor(model, fid)
    cfg = _proj_config(model)
    t = payload.get("type")
    _require(t in ELEMENT_TYPES + ("cable_run",), "unknown element type '%s'" % t)
    el = {"id": _new_id("e"), "type": t, "label": payload.get("label")}
    for key in ("x", "y", "antenna_id", "ways", "il_db", "cable_id", "length_m",
                "from_id", "to_id", "port", "subtype", "donor_gain_db",
                "feeder_loss_db", "dl_gain_db", "max_dl_out_dbm", "ports",
                "gain_db", "max_port_dbm", "through_il_db", "tap_loss_db",
                "coupled_loss_db", "ambient_dbm", "dl_gain_db_per_band", "max_dl_out_dbm_per_band",
                "injection_dbm"):
        if key in payload:
            el[key] = payload[key]
    if t in ELEMENT_TYPES and payload.get("x") is None:
        el["x"], el["y"] = 20, 20
    if t == "antenna":
        el["x"] = _num(el.get("x", 20))
        el["y"] = _num(el.get("y", 20))
    if not el.get("label"):
        el["label"] = _auto_label(model, t)
    _validate_element(model, cfg, el, creating=True)
    floor["elements"].append(el)
    _save(pid, proj["name"], model)
    return {"element": el}


@router.get("/projects/{pid}/floors/{fid}/elements")
def list_elements(pid: int, fid: str):
    proj = _load_or_404(pid)
    floor = _find_floor(proj["model"], fid)
    return {"elements": floor.get("elements", [])}


@router.patch("/projects/{pid}/elements/{eid}")
def patch_element(pid: int, eid: str, payload: dict = Body(...)):
    proj = _load_or_404(pid)
    model = proj["model"]
    _, el = _find_element(model, eid)
    _require("type" not in payload or payload["type"] == el["type"],
             "element type cannot change")
    editable = ("x", "y", "antenna_id", "ways", "il_db", "cable_id", "length_m",
                "from_id", "to_id", "port", "label", "donor_gain_db",
                "feeder_loss_db", "dl_gain_db", "max_dl_out_dbm", "ports",
                "gain_db", "max_port_dbm", "through_il_db", "tap_loss_db",
                "coupled_loss_db", "ambient_dbm", "dl_gain_db_per_band", "max_dl_out_dbm_per_band",
                "subtype", "injection_dbm")
    for key in editable:
        if key in payload:
            el[key] = payload[key]
    cfg = _proj_config(model)
    _validate_element(model, cfg, el, creating=False)
    _save(pid, proj["name"], model)
    return {"element": el}


@router.delete("/projects/{pid}/elements/{eid}")
def delete_element(pid: int, eid: str):
    proj = _load_or_404(pid)
    model = proj["model"]
    _find_element(model, eid)  # 404 if missing
    removed = 0
    for fl in model.get("floors", []):
        before = len(fl.get("elements", []))
        fl["elements"] = [el for el in fl.get("elements", [])
                          if el.get("id") != eid
                          and el.get("from_id") != eid and el.get("to_id") != eid]
        removed += before - len(fl["elements"])
    _save(pid, proj["name"], model)
    return {"deleted": eid, "removed_including_runs": removed}


# ---------------------------------------------------------------- solve/predict

@router.post("/projects/{pid}/cascade/solve")
def solve(pid: int):
    proj = _load_or_404(pid)
    model = proj["model"]
    cfg = _proj_config(model)
    result = solve_cascade(model, cfg)
    if result["errors"]:
        raise HTTPException(status_code=422, detail={"errors": result["errors"]})
    return result


def _run_predict(pid, fid, band, step):
    proj = _load_or_404(pid)
    model = proj["model"]
    cfg = _proj_config(model)
    try:
        return predict_floor(model, cfg, fid, band, step_px=step)
    except PredictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # CascadeError and friends
        errors = getattr(exc, "errors", None)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/projects/{pid}/floors/{fid}/predict")
def predict(pid: int, fid: str, band: str = Query(...), step: int = Query(1, ge=1, le=64)):
    return _run_predict(pid, fid, band, step)


@router.get("/projects/{pid}/floors/{fid}/predict")
def predict_get(pid: int, fid: str, band: str = Query(...), step: int = Query(1, ge=1, le=64)):
    return _run_predict(pid, fid, band, step)


# ------------------------------------------------------------ autoplace

@router.post("/projects/{pid}/floors/{fid}/autoplace")
def autoplace(pid: int, fid: str, payload: dict = Body(default_factory=dict)):
    """Place antennas + build the splitter/cable chain for one floor so the
    coverage target is met with as few antennas as practical. Replaces any
    earlier auto-placed elements on the floor. `dry_run: true` returns the
    result without saving. The saved model feeds /predict and /bom as usual."""
    proj = _load_or_404(pid)
    payload = payload or {}
    dry = bool(payload.get("dry_run", False))
    model = json.loads(json.dumps(proj["model"])) if dry else proj["model"]
    _find_floor(model, fid)  # 404 if missing
    cfg = _proj_config(model)
    try:
        result = autoplace_floor(model, cfg, fid, payload)
    except AutoplaceError as exc:
        raise HTTPException(status_code=422, detail={"errors": exc.errors})
    if not dry:
        _save(pid, proj["name"], model)
    result["saved"] = not dry
    try:
        result["bom"] = generate_bom(model, cfg)["items"]
    except ValueError as exc:  # pragma: no cover - chain just solved
        result["bom"] = []
        result["warnings"].append("bom: %s" % exc)
    return result


# ------------------------------------------------- multiband composite (phase 3)

def _run_composite(pid, fid, bands, step):
    proj = _load_or_404(pid)
    model = proj["model"]
    cfg = _proj_config(model)
    try:
        return predict_floor_multiband(model, cfg, fid, bands, step_px=step)
    except PredictError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # CascadeError and friends
        errors = getattr(exc, "errors", None)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        raise HTTPException(status_code=422, detail=str(exc))


def _composite_bands_param(bands: str):
    bids = [p.strip() for p in (bands or "").split(",") if p.strip()]
    if not bids:
        raise HTTPException(status_code=400,
                            detail="no bands selected (pass bands=n41_2500,n77_cband,..)")
    return bids


@router.post("/projects/{pid}/floors/{fid}/composite")
def composite_post(pid: int, fid: str, bands: str = Query(...),
                   step: int = Query(1, ge=1, le=64)):
    return _run_composite(pid, fid, _composite_bands_param(bands), step)


@router.get("/projects/{pid}/floors/{fid}/composite")
def composite_get(pid: int, fid: str, bands: str = Query(...),
                  step: int = Query(1, ge=1, le=64)):
    return _run_composite(pid, fid, _composite_bands_param(bands), step)


@router.get("/projects/{pid}/floors/{fid}/composite.png")
def composite_png(pid: int, fid: str, bands: str = Query(...),
                  step: int = Query(1, ge=1, le=64)):
    multi = _run_composite(pid, fid, _composite_bands_param(bands), step)
    payload = render_heatmap_png(composite_heatmap_view(multi),
                                 _proj_config(_load_or_404(pid)["model"]),
                                 title_extra="P%d" % pid)
    return Response(
        content=payload,
        media_type="image/png",
        headers={"Content-Disposition": 'inline; filename="composite-%s-%s.png"'
                 % (pid, fid)},
    )


@router.get("/projects/{pid}/floors/{fid}/heatmap.png")
def heatmap_png(pid: int, fid: str, band: str = Query(...), step: int = Query(1, ge=1, le=64)):
    result = _run_predict(pid, fid, band, step)
    payload = render_heatmap_png(result, _proj_config(_load_or_404(pid)["model"]),
                                 title_extra="P%d" % pid)
    return Response(
        content=payload,
        media_type="image/png",
        headers={"Content-Disposition": 'inline; filename="heatmap-%s-%s-%s.png"'
                 % (pid, fid, band)},
    )


# ----------------------------------------------------------- NFPA two-tier report

def _nfpa_report(pid, fid, band, step, cfg, predict_result, proj):
    """Assemble the NFPA two-tier KPI report body for one floor + band."""
    kpi = predict_result["kpi"]
    tiers = [p for p in kpi["presets"] if p["preset"] == "nfpa_two_tier"]
    critical_block = kpi.get("critical_areas") or {}
    return {
        "project_id": pid,
        "project_name": proj["name"],
        "floor": predict_result["floor"],
        "band": predict_result["band"],
        "grid": predict_result["grid"],
        "environment": predict_result["project_environment"],
        "n_exponent": predict_result["n_exponent"],
        "n_exponent_source": predict_result.get("n_exponent_source"),
        "wall_loss_scale_db": predict_result.get("wall_loss_scale_db", 0.0),
        "tiers": tiers,
        "single_tier": next((p for p in kpi["presets"]
                             if p["preset"] == "single_tier"), None),
        "critical_areas": {
            "evaluated": critical_block.get("evaluated", False),
            "count": critical_block.get("count", 0),
            "polygons": critical_block.get("polygons", []),
            "n_points_union": critical_block.get("n_points_union"),
            "covered_pct_union": critical_block.get("covered_pct_union"),
            "pass_union": critical_block.get("pass_union"),
            "defined": [
                {"id": ca.get("id"), "label": ca.get("label"), "n_vertices": len(ca.get("points", []))}
                for ca in (predict_result["floor_geom"].get("critical_areas") or [])
            ],
        },
        "model_warnings": predict_result.get("model_warnings", []),
        "antennas": predict_result.get("antennas", []),
        "generated_with": "ROMtool %s" % __version__,
    }


@router.get("/projects/{pid}/floors/{fid}/nfpa_report")
def nfpa_report(pid: int, fid: str, band: str = Query(...), step: int = Query(1, ge=1, le=64)):
    proj = _load_or_404(pid)
    result = _run_predict(pid, fid, band, step)
    return _nfpa_report(pid, fid, band, step, _proj_config(proj["model"]), result, proj)


@router.get("/projects/{pid}/floors/{fid}/nfpa_report.json")
def nfpa_report_json(pid: int, fid: str, band: str = Query(...), step: int = Query(1, ge=1, le=64)):
    proj = _load_or_404(pid)
    result = _run_predict(pid, fid, band, step)
    body = _nfpa_report(pid, fid, band, step, _proj_config(proj["model"]), result, proj)
    return Response(
        content=json.dumps(body, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="nfpa-report-%s-%s-%s.json"'
                 % (pid, fid, band)},
    )


@router.get("/projects/{pid}/floors/{fid}/nfpa_report.png")
def nfpa_report_png(pid: int, fid: str, band: str = Query(...), step: int = Query(1, ge=1, le=64)):
    result = _run_predict(pid, fid, band, step)
    payload = render_heatmap_png(result, _proj_config(_load_or_404(pid)["model"]),
                                 title_extra="NFPA P%d" % pid)
    return Response(
        content=payload,
        media_type="image/png",
        headers={"Content-Disposition": 'inline; filename="nfpa-report-%s-%s-%s.png"'
                 % (pid, fid, band)},
    )


# ---------------------------------------------------------------- drawings

def _solved_model(pid):
    proj = _load_or_404(pid)
    model = proj["model"]
    cfg = _proj_config(model)
    result = solve_cascade(model, cfg)
    if result["errors"]:
        raise HTTPException(status_code=422, detail={"errors": result["errors"]})
    return proj, model, cfg, result


@router.get("/projects/{pid}/one_line.svg")
def one_line_svg_endpoint(pid: int):
    proj, model, cfg, result = _solved_model(pid)
    svg = one_line_svg(model, cfg, result, project_name=proj["name"])
    return Response(content=svg.encode("utf-8"), media_type="image/svg+xml",
                    headers={"Content-Disposition": 'inline; filename="one-line-%s.svg"' % pid})


@router.get("/projects/{pid}/one_line.png")
def one_line_png_endpoint(pid: int):
    proj, model, cfg, result = _solved_model(pid)
    payload = one_line_png(model, cfg, result, project_name=proj["name"])
    return Response(content=payload, media_type="image/png",
                    headers={"Content-Disposition": 'inline; filename="one-line-%s.png"' % pid})


@router.get("/projects/{pid}/floors/{fid}/layout.svg")
def floor_layout_svg_endpoint(pid: int, fid: str, include_image: bool = Query(True)):
    proj = _load_or_404(pid)
    model = proj["model"]
    _find_floor(model, fid)
    svg = floor_layout_svg(model, _proj_config(model), fid,
                           include_image=include_image, project_name=proj["name"])
    return Response(content=svg.encode("utf-8"), media_type="image/svg+xml",
                    headers={"Content-Disposition": 'inline; filename="layout-%s-%s.svg"'
                             % (pid, fid)})


@router.get("/projects/{pid}/floors/{fid}/layout.png")
def floor_layout_png_endpoint(pid: int, fid: str, include_image: bool = Query(True)):
    proj = _load_or_404(pid)
    model = proj["model"]
    _find_floor(model, fid)
    payload = floor_layout_png(model, _proj_config(model), fid,
                               include_image=include_image, project_name=proj["name"])
    return Response(content=payload, media_type="image/png",
                    headers={"Content-Disposition": 'inline; filename="layout-%s-%s.png"'
                             % (pid, fid)})


# ---------------------------------------------------------------- BOM + exports

@router.get("/projects/{pid}/bom")
def project_bom(pid: int):
    proj = _load_or_404(pid)
    model = proj["model"]
    cfg = _proj_config(model)
    try:
        return {"project_id": pid, **generate_bom(model, cfg)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/exports/projects/{pid}/bom.csv")
def export_bom_csv(pid: int):
    proj = _load_or_404(pid)
    try:
        text = bom_csv(generate_bom(proj["model"], _proj_config(proj["model"]))["items"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return Response(
        content=text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="bom-%s.csv"' % pid},
    )


@router.get("/exports/projects/{pid}/bom.json")
def export_bom_json(pid: int):
    proj = _load_or_404(pid)
    try:
        body = {"project_id": pid, "project": proj["name"],
                **generate_bom(proj["model"], _proj_config(proj["model"]))}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return Response(
        content=json.dumps(body, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="bom-%s.json"' % pid},
    )
