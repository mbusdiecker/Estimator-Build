# ROMtool — In-Building RF ROM Estimator

Claude-driven rough-order-of-magnitude estimating tool for in-building wireless.
Headless RF core + REST API (Claude drives everything) + browser canvas UI (LAN).
Fully local, offline-capable after install. Owner: Matt Busdiecker. Built by Rowan/Hermes.

## Product definition (locked decisions, 2026-11-10)

- ROM estimator: coverage prediction + antenna count + parts BOM. Parts-only BOM
  (no $ cost fields in v1; user prices outside the tool).
- Full DAS cascade from day one: signal source / repeater + off-air donor budget,
  trunk, splitters/tappers/couplers, per-run cable, per-antenna EIRP. BOM falls out
  of the chain graph.
- Delivery order: (1) LTE, (2) public safety incl. legacy LMR + FirstNet,
  (3) 5G NR. Phase 2/3 only start after phase 1 verified.
- Not an iBwave clone, not ray tracing. Relative accuracy, empirical models only.
- Stack: Python 3 venv + FastAPI + SQLite (single-file project DB is fine),
  static web UI (vanilla TS/JS + canvas, minimal deps), all in ./romtool repo, git init.

## Non-goals (do not build)

Ray tracing, capacity/Monte Carlo, .ibw interop, authentication, cost/price fields,
systemd service (ask Matt first), anything cloud-hosted, vendor component catalogs.

## Physics (phase 1: LTE)

Bands: arbitrary frequency presets, default LTE set: 700 MHz (B12/B13), 850 (B5),
PCS 1900 (B2), AWS 2100 (B66). Config file editable.

Per band, per floor grid point:
1. Distance term: ITU-R P.1238 indoor
   PL = 20 log10(f_MHz) + N·log10(d_m) + L_f·n_floors − 28
   N per environment config (office 30, commercial 22, residential 28);
   L_f floor loss 15.4 dB/floor (n=1) etc. — config.
2. Wall crossings: for the straight segment antenna→grid point, sum per-material
   attenuation, frequency-scaled with ITU-R P.2040 curves (default table at 2 GHz,
   scale linearly in log domain): concrete 15, CMU 10, drywall 4, glass 5,
   low-E glass 8, wood 4, metal 30 (dB, editable).
3. RSSI per antenna = EIRP_dBm − PL_total. Composite = 10·log10(Σ linear mW)
   over co-channel antennas. Also expose per-antenna best-server grid.
4. Sanity anchor for tests: FSPL(d=1 m, f=2000 MHz) ≈ 38.5 dB; FSPL(10 m, 1900) ≈ 58 dB.

Cascade (per band): donor antenna gain + feeder loss → donor RSSI (input dBm) →
repeater (config DL gain, max DL out, ports) → trunk cable → splitter/tap/coupler
nodes (each: insertion loss per branch, editable part params) → branch cables →
antennas (each: #ports, port power derived from chain; cap at antenna port max).
Outputs: per-antenna port EIRP per band; chain validation errors (loss budget <
0 dBm out = warn). Every chain element lists its BOM parts.

KPI: threshold coverage, % floor area ≥ T (dBm) per band: single-tier and NFPA
two-tier presets (tier 1: −95 dBm @ 95% area general; tier 2: −95 dBm @ 99% area
critical). Editable thresholds. Report PNG heatmap + KPI badge + BOM CSV/JSON.

## BOM

From cascade graph edges: cable per type (meters AND feet), connectors (2 per run),
splitters/taps/couplers by count/way, antennas by type, sources/repeaters.
Exports: CSV, JSON. No prices.

## API (Claude drives these)

JSON REST: projects / floors (image upload + ppm scale) / walls / elements
(source, repeater, splitter, tapper, coupler, cable run, antenna) CRUD;
POST /projects/{id}/cascade/solve; POST /projects/{id}/floors/{fid}/predict?band=…;
GET /projects/{id}/bom; GET /exports/*. Single SQLite store, JSON model blob per project.

## UI scope (minimal)

Canvas: load floorplan image, calibrate scale (2 known points), draw walls w/
material picker, place elements from palette, heatmap overlay + KPI badge,
BOM panel. Served by the same FastAPI app. No auth. Bind 0.0.0.0:8807.

## Phase 1 acceptance (before any phase 2 work)

- pytest green: FSPL anchors, wall-crossing sum, cascade arithmetic hand example,
  BOM counts, KPI % on synthetic room, API smoke (create→solve→bom).
- Example project JSON ships preloaded (small office, 4 antennas, 2 floors),
  solve runs headless in <5 s for 3000 px² grid at 1 px step.
- README: venv setup, run command, LAN access URL for Windows machine.
- git history with logical commits.

## Phase 2 (after acceptance): legacy LMR + FirstNet

Bands VHF 150 / UHF 450 / 700 / 800 MHz + FirstNet Band 14 LTE. P25/FM sources.
Low-band caveat: document P.1238 validity limits; add config overrides for N and
wall-loss scaling. NFPA two-tier KPI report page per AHJ-editable tiers.

## Phase 3: 5G NR

n71 600 / n41 2500 / n77 C-band presets, NR SS-RSRP thresholds, multi-band
composite views.

## Runtime

cd /root/assistant/romtool && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8807
(for dev; systemd only after Matt approves).
