"""pmf.py: port power margin & floors-per-port. How many identical floors one DAS remote port can
feed, from a milliwatt power budget, then confirm by re-running autoplace at the delivered DL.
usage: pmf.py FLOOR_JSON OEM PORT_DBM --ports P [--nmin 3] [--nmax 8] [--kmax 4] [--floors 45]
       [--cov 98] [--thr -103] [--off -30.8] [--ops 3] [--res 0.25] [--out OUT.json]
Step 1: P_min(N) = lowest per-floor remote DL still hitting the coverage target (bisection on the
min band coverage, resolution --res; coverage assumed monotone in DL). Step 2: one port feeds k
floors if P_min_mw * 10**(loss/10) <= port_mw, loss = 10log10(k) + 0.5 (k>1) + riser coax for a
middle-floor remote ((k//2) * floor_height_m, coax_1_2in @2.0 GHz, worst band). Step 3: re-run at
the delivered DL to confirm, plus the next-floor DL. Config estimates (grade E), not measured.
Drywall loss_db = -1 always -> iBwave table via ibwloss.
"""
import argparse, json, math, os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
from romtool.config import load_config
from romtool.autoplace import AutoplaceError, autoplace_floor
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import ibwloss
from romtool import autoplace as A
NE = [4]
def seed_even(self):
    xs = self.grid.xs; ys = self.grid.ys; x0, x1 = min(xs), max(xs); yc = 0.5 * (min(ys) + max(ys)); N = NE[0]
    for i in range(N):
        x, y = self._nearest_point(x0 + (i + 0.5) * (x1 - x0) / N, yc); self.add(x, y)
    self.seed_n = N
A._Planner.seed = seed_even

BANDS = ["B12_700", "B5_850", "B2_1900", "B66_2100"]
ap = argparse.ArgumentParser()
ap.add_argument("floor"); ap.add_argument("oem"); ap.add_argument("port_dbm", type=float)
ap.add_argument("--ports", type=int, default=1); ap.add_argument("--nmin", type=int, default=3)
ap.add_argument("--nmax", type=int, default=8); ap.add_argument("--kmax", type=int, default=4)
ap.add_argument("--floors", type=int, default=45); ap.add_argument("--cov", type=float, default=98.0)
ap.add_argument("--thr", type=float, default=-103.0); ap.add_argument("--off", type=float, default=-30.8)
ap.add_argument("--ops", type=float, default=3.0); ap.add_argument("--res", type=float, default=0.25); ap.add_argument("--span", type=float, default=20.0)
ap.add_argument("--out"); ap.add_argument("--bands")
a = ap.parse_args()
if a.bands: BANDS = a.bands.split(",")
cfg = load_config()
cfg["wall_materials"]["drywall"]["loss_db"] = -1  # iBwave drywall table via ibwloss
fh = float(cfg["autoplace"]["floor_height_m"]) if "autoplace" in cfg and "floor_height_m" in cfg["autoplace"] else 4.0
riser_db_m = float(cfg["cables"]["coax_1_2in"]["loss_db_per_100m_ghz"]["2.0"]) / 100.0
floor = json.load(open(a.floor))
sqft = floor["w_px"] * floor["h_px"] / floor["ppm"] ** 2 * 10.7639
model = {"environment": "office", "building_type": "hotel_residential", "floors": [floor]}
CACHE = {}
def run(N, dl):
    dl = round(dl, 2)
    if (N, dl) in CACHE: return CACHE[(N, dl)]
    NE[0] = N
    req = {"bands": BANDS, "threshold_dbm": a.thr, "coverage_target_pct": a.cov, "rsrp_offset_db": a.off,
           "step_px": 2, "max_antennas": N,
           "remotes": {"count": 1, "max_dl_out_dbm": dl, "dl_gain_db": 60.0, "operators": a.ops}}
    try:
        r = autoplace_floor(model, cfg, floor["id"], req)
        covd = r.get("coverage_pct_by_band") or {}
        out = (min(covd.values()) if covd else 0.0, covd, len(r.get("antennas") or []))
    except AutoplaceError:
        out = (0.0, {}, 0)
    CACHE[(N, dl)] = out
    return out
def loss_db(k):
    return ((10 * math.log10(k) + 0.5) if k > 1 else 0.0) + (k // 2) * fh * riser_db_m
def r1(x): return round(x, 1) if x is not None else None
def r2(x): return round(x, 2) if x is not None else None
port_mw = 10 ** (a.port_dbm / 10)
rows = []; prev_cov = None
for N in range(a.nmin, a.nmax + 1):
    cov_at, covd = run(N, a.port_dbm)[:2]
    P_min = None
    if cov_at >= a.cov:
        lo, hi = a.port_dbm - a.span, a.port_dbm
        while hi - lo > a.res:
            m = 0.5 * (lo + hi)
            if run(N, m)[0] >= a.cov: hi = m
            else: lo = m
        P_min = round(hi, 2)
    P_mw = 10 ** (P_min / 10) if P_min is not None else None
    gain = None if prev_cov is None else cov_at - prev_cov
    five = prev_cov is None or gain >= 5.0 or prev_cov < a.cov
    k_fit = 0
    if P_min is not None:
        for k in range(1, a.kmax + 1):
            if P_mw * 10 ** (loss_db(k) / 10) <= port_mw: k_fit = k  # split loss already in loss_db; no extra k factor
            else: break
    k = k_fit; cov_conf = None
    while k >= 1:
        cov_conf = run(N, a.port_dbm - loss_db(k))[0]
        if cov_conf >= a.cov: break
        k -= 1
    k_fit = k
    cov_next = run(N, a.port_dbm - loss_db(k_fit + 1))[0] if k_fit >= 1 else None
    fpr = k_fit * a.ports
    row = {"N": N, "cov_at_port": r1(cov_at), "cov_by_band": {b: r1(v) for b, v in covd.items()},
           "P_min": r2(P_min), "P_min_mw": r2(P_mw),
           "headroom_db": r2(a.port_dbm - P_min) if P_min is not None else None,
           "headroom_mw": r2(port_mw - P_mw) if P_min is not None else None,
           "sqft_per_ant": round(sqft / N), "area_ok": sqft / N >= 1000,
           "gain_vs_prev": r1(gain), "five_pct_ok": five,
           "k_fit": k_fit, "loss_db": r2(loss_db(k_fit)) if k_fit else None,
           "delivered_dbm": r2(a.port_dbm - loss_db(k_fit)) if k_fit else None,
           "cov_confirmed": r1(cov_conf), "confirmed": cov_conf is not None and cov_conf >= a.cov,
           "cov_next_floor": r1(cov_next), "floors_per_remote": fpr,
           "remotes_bldg": math.ceil(a.floors / fpr) if fpr else None, "ant_bldg": N * a.floors}
    rows.append(row); print(json.dumps(row), flush=True)
    prev_cov = cov_at
d = next((r for r in rows if r["P_min"] is not None and r["area_ok"]), None)
print("\nsummary: " + " ".join(f"{c:>9}" for c in ["N", "P_min", "head_dB", "head_mW", "area", "5pct", "k_fit", "cov_conf", "cov_next", "f/rem", "rem_bldg", "ant_bldg"]))
for r in rows:
    print(" ".join(f"{v!s:>9}" for v in [r["N"], r["P_min"], r["headroom_db"], r["headroom_mw"], r["area_ok"], r["five_pct_ok"], r["k_fit"], r["cov_confirmed"], r["cov_next_floor"], r["floors_per_remote"], r["remotes_bldg"], r["ant_bldg"]]))
print("design_N: " + (f"N={d['N']} k_fit={d['k_fit']} cov_confirmed={d['cov_confirmed']} floors_per_remote={d['floors_per_remote']} remotes_bldg={d['remotes_bldg']} ant_bldg={d['ant_bldg']}" if d else "none (no N with P_min and area_ok)"))
if a.out:
    json.dump({"oem": a.oem, "port_dbm": a.port_dbm, "ports": a.ports, "cov_target": a.cov, "thr": a.thr,
               "off": a.off, "ops": a.ops, "floor_height_m": fh, "riser_db_m": riser_db_m, "floors": a.floors,
               "sqft_floor": round(sqft), "nmin": a.nmin, "nmax": a.nmax, "kmax": a.kmax, "res_db": a.res,
               "rows": rows, "design_N": d["N"] if d else None, "design_k_fit": d["k_fit"] if d else None},
              open(a.out, "w"), indent=1)
