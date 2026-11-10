# HANDOFF — ANS Rebuild Notes (2026-11-10)

Purpose: quick orientation for engineers rebuilding/porting ROMtool.
Author: Rowan (AI assistant) for Matt Busdiecker. All code in this archive was
written fresh for this project (no third-party application code); the physics is
standard published material (FSPL, ITU-R P.1238, ITU-R P.2040, COST 231
Multi-Wall, 3GPP TR 38.901, NFPA/IFC two-tier coverage concept).

## What it is
In-building RF rough-order-of-magnitude (ROM) estimator: floorplan import,
wall drawing with per-material attenuation, full passive DAS cascade
(donor/repeater, trunk, splitters/tappers/couplers, branch cable, antennas),
per-band grid prediction with heatmap PNG, KPI evaluation (threshold %,
NFPA two-tier with critical-area polygons, NR SS-RSRP classes), multi-band
best-band composite, parts-only BOM (CSV/JSON). No ray tracing, empirical-grade,
all tuning params editable.

## Stack
- Python 3 venv + FastAPI + uvicorn (port 8807), SQLite single-file store
  (JSON blob per project), pure-Python RF engine (no numpy), zero-dep PNG writer.
- Frontend: plain HTML/TS-free JS canvas UI, served by the same app.
- Deployment: /etc/systemd/system/romtool.service (included in deploy/).

## Run / test
```
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8807
.venv/bin/pytest -q          # 141 tests, incl. hand-math anchors
```

## Repo layout
- romtool/          RF engine: config.py, cascade.py, predict.py, geometry.py,
                    composite.py, report.py, example_project.py;
                    estimate.py (Class 1 labor engine, phase 4),
                    governance.py (v0.5.0 estimate governance + procurement),
                    xlsx.py + pdf.py (zero-dep writers)
- app/              FastAPI app (main.py, routes.py), SQLite store
                    (projects, estimates + revisions/audit/assumptions/quotes)
- static/           Canvas UI (index.html, app.js, style.css)
                    + docs: help.html (/help), how-it-works.html (/how-it-works)
- config/default.json  ALL bands/materials/cables/parts/KPI presets (data-driven;
                    add bands by config edit, e.g. n78)
- config/estimating.json  phase-4 estimate constants (factors, rates, contingency)
- config/labor_units.json  labor-unit catalog store (134 rows; regenerate with
                    scripts/build_labor_units.py from the research CSV)
- config/governance.json  v0.5.0 governance rules (state machine, register
                    statuses, binding warn window, default actor/owner)
- config/commercial_draft.json  inclusions/exclusions sheet templates
- tests/            pytest incl. numeric anchors (FSPL 1m/2GHz ~= 38.5 dB, etc.)
- SPEC.md           Original build spec (product decisions + phase plan)
- README.md         Run + modeling notes
- deploy/           systemd unit

## Delivered capability
- LTE: B12_700, B5_850, B2_1900, B66_2100
- Public safety: VHF_150, UHF_450, PS700, PS800 (P25, off-air or direct-inject
  sources), B14_FIRSTNET (LTE), NFPA two-tier KPIs + critical-area polygons
- 5G NR: N71_600, N41_2500, N77_CBAND (+ n78 via config), SS-RSRP KPI presets
- Composite: per-point best-band + OR union-KPI, composite.png
- Phase 4: Class 1 labor estimates — POST /estimates + recap.csv/.xlsx
  exports; access/condition/schedule factor stack, supervision wrap, OH&P,
  line-risk contingency; 134-row labor catalog (regenerable)
- v0.5.0 (phase E2): estimate governance + procurement — versioned revisions
  with diff summaries, full audit log, draft->review->approved->sent->
  superseded state machine (409 on illegal transitions), assumption register
  auto-populated from E-grade norms (open->verified->closed close-out),
  vendor quote register with inclusive binding_until expiry math + one
  expired-warning per quote, bid-tab comparison vs estimate carry,
  inclusions/exclusions commercial sheet (xlsx + PDF via romtool/pdf.py)

## Notes / limitations (documented on /how-it-works)
- P.1238 validity 300 MHz-6 GHz (VHF warns; per-band N + wall-loss-scale
  overrides provided for calibration)
- Composite is per-point max across selected bands, not a cross-carrier RF sum
- Governance deviations (v0.5.0, documented on /how-it-works 10.6):
  binding_until is inclusive (expiry strictly after); a no-op PATCH still
  records a revision (empty diff) so the audit trail shows the re-compute;
  verified/closed assumption rows persist when their line is removed
  (only open rows are pruned); DELETE /estimates/{id} wipes the estimate's
  whole governance history; the PDF writer wraps at ~0.55 em/char
  (deliberate over-estimate so lines never clip)
- KPI area = full floor canvas rectangle; critical areas are tier-2 scope
- No capacity/Monte Carlo, no ray tracing, no vendor component catalog
