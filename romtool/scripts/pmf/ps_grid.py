"""PS grid test module: IFC 510.5.4 style acceptance check from the same floor
model the ROM machinery runs. Design self-check, not a substitute for the
field walk (grade E). No prices, no customer or job names.

What it does per band:
  - partitions the floor into 20 approximately equal cells honoring the 80 ft
    (24.4 m) maximum grid dimension rule,
  - predicts the downlink at each cell center with the same propagation engine
    the planner uses (_lin_from/_contrib over a mini grid),
  - pass/fail per cell against the AHJ thresholds (RSSI -95 dBm, or the SINR /
    BER equivalents), flags the two-fail 40-cell retest zone,
  - predicts the critical-area discrete points when the floor model carries
    zone polygons,
  - computes the calculated uplink per cell (the LA County method) from the
    DL prediction plus the link budget, and
  - reports the per-floor level spread (min/p5/p95/max) and the near-ring
    level per antenna (1.5 m ring, real propagation points, no shortcuts).

Run pattern:
  uv run --no-project python -I ps_grid.py sugarhouse-26-01038/floor4_soup.json \
      --bands PS700,PS800 --port 33 --design-n 3 --k 48 --nfloors 45 \
      --out sugarhouse-26-01038/ps_grid_results.json
"""
import argparse
import json
import math
import sys

sys.path.insert(0, "..")
from romtool import autoplace as ap_mod  # noqa: E402
from romtool.config import load_config  # noqa: E402
from romtool.autoplace import (  # noqa: E402
    _inside, _lin_from, _Grid, _Band, WallRays, autoplace_floor, AutoplaceError)


def fspl_db(f_mhz, d_m):
    return 32.45 + 20.0 * math.log10(f_mhz) + 20.0 * math.log10(d_m / 1000.0)


def cell_layout(w_px, h_px, ppm, max_dim_m):
    """Rows x cols for 20 cells honoring the max grid dimension."""
    w_m, h_m = w_px / ppm, h_px / ppm
    best = None
    for rows in range(1, 21):
        if 20 % rows:
            continue
        cols = 20 // rows
        d = max(w_m / cols, h_m / rows)
        ok = d <= max_dim_m + 1e-9
        if best is None or (abs(rows * (w_m / cols) - cols * (h_m / rows)),
                            not ok, d) < best[0]:
            best = ((abs(rows * (w_m / cols) - cols * (h_m / rows)),
                     not ok or d, d), rows, cols, d)
    score, rows, cols, worst_dim = best
    return rows, cols, round(worst_dim, 1), bool(worst_dim <= max_dim_m + 1e-9)


def even_split(total, parts):
    base = total // parts
    rem = total % parts
    return [base + 1] * rem + [base] * (parts - rem) if parts else []


def _factor_pair(n):
    """Rows x cols for n antennas: the factor pair with the smallest max
    dimension (1=1x1, 2=1x2, 4=2x2, 6=2x3, 9=3x3, 12=3x4, ...)."""
    best = (n, 1)
    bd = None
    for rows in range(1, n + 1):
        if n % rows:
            continue
        cols = n // rows
        d = max(rows, cols)
        if bd is None or d < bd:
            bd = d
            best = (rows, cols)
    return best


def main():
    a = argparse.ArgumentParser()
    a.add_argument("floor")
    a.add_argument("--bands", default="PS700,PS800")
    a.add_argument("--port", type=float, required=True,
                   help="COMPOSITE DL port dBm from the vendor sheet (B14: the "
                        "passband is one channel, Class B)")
    a.add_argument("--channels", type=int, required=True,
                   help="channels in the passband: the composite port splits by "
                        "10log10(channels); explicitly entered every run")
    a.add_argument("--design-n", type=int, required=True)
    a.add_argument("--k", type=int, default=48)
    a.add_argument("--nfloors", type=int, default=45)
    a.add_argument("--ports", type=int, default=1)
    a.add_argument("--cov", type=float, default=95.0)
    a.add_argument("--thr", type=float, default=-95.0)
    a.add_argument("--sinr-threshold", type=float, default=None)
    a.add_argument("--nf-portable", type=float, default=8.0)
    a.add_argument("--profile", default=None,
                   help="per-AHJ profile; the CLI args override")
    a.add_argument("--solve-density", action="store_true",
                   help="the owner's sequential loop: minimal coverage plate "
                        "first, then add antennas until the near-far check "
                        "passes (the per-antenna coverage cell shrinks, the "
                        "under-antenna coupling drops 10log10(N), and the "
                        "spread tightens). Reports the whole curve so the "
                        "tap/pad alternative stays visible")
    a.add_argument("--spread-target-db", type=float, default=20.0)
    a.add_argument("--density-max", type=int, default=12)
    a.add_argument("--near-max-input-db", type=float, default=-10.0,
                   help="BDA max operating input (vendor spec; placeholder "
                        "where the sheets are silent: grade D)")
    a.add_argument("--near-damaging-db", type=float, default=0.0,
                   help="BDA damaging input threshold (vendor spec placeholder)")
    a.add_argument("--noise-rule", default="la")
    a.add_argument("--site-erp", type=float, required=True,
                   help="host site ERP dBm (AHJ input per site)")
    a.add_argument("--site-nf", type=float, required=True,
                   help="host site receiver NF dB (AHJ input per site)")
    a.add_argument("--ambient", type=float, required=True,
                   help="site DL ambient at the donor dBm (cal per site)")
    a.add_argument("--g-donor", type=float, default=15.5)
    a.add_argument("--ue-tx", type=float, default=34.8)
    a.add_argument("--body-loss", type=float, default=5.0)
    a.add_argument("--arrival-margin", type=float, default=18.0)
    a.add_argument("--ul-gain", type=float, default=None,
                   help="recommended UL gain dB (from ps_ul); default recomputed")
    a.add_argument("--caps-area-sqft", type=float, default=None,
                   help="antenna square footage cap (design gate; None = off)")
    a.add_argument("--caps-remote-sqft", type=float, default=None,
                   help="max building sq ft per remote/BDA (industry practice "
                        "about 250,000); report-only gate")
    a.add_argument("--caps-k", type=int, default=None,
                   help="floors-per-port cap; None = off")
    a.add_argument("--out", default="sugarhouse-26-01038/ps_grid_results.json")
    args = a.parse_args()
    sys.path.insert(0, ".")  # -I mode: the script dir is not on the path
    from ps_profiles import resolve as resolve_profile
    RESOLVED, PWHERE, PROF = resolve_profile(args.profile, {
        "sinr_threshold": ("sinr_threshold_db", "sinr_threshold_db", args.sinr_threshold),
        "noise_rule": ("noise_rule", "noise_rule", args.noise_rule),
    })
    args.sinr_threshold = RESOLVED["sinr_threshold"]

    cfg = load_config()
    cfg["wall_materials"]["drywall"]["loss_db"] = -1  # iBwave drywall via ibwloss
    bands_cfg = {b["id"]: b for b in cfg["bands"]}
    floor = json.load(open(args.floor))
    ppm = float(floor["ppm"])
    model = {"environment": "office", "building_type": "hotel_residential",
             "floors": [floor]}
    env = "office"
    st = "hotel_residential"

    bands = args.bands.split(",")
    crit_src = (floor.get("critical_areas") or floor.get("zones") or [])
    crit_polys = [ap_mod._prep_poly(c.get("points", [])) for c in crit_src
                  if len(c.get("points", [])) >= 3]
    bmap = {bid: _Band(model, cfg, floor, bid, env, ppm, None,
                       system_type=None) for bid in bands}
    dv_fn = ap_mod.height_offset_fn(floor, cfg)
    dv_lo, dv_hi = ap_mod.height_offset_range(floor, cfg)
    for b in bmap.values():
        b.dv_at, b.dv_min, b.dv_max = dv_fn, dv_lo, dv_hi

    # the design the pmf run chose: place antennas once at the design N.
    # The composite port splits by the channel count before the planner runs;
    # the uplink budget below keeps the composite-port form (the coupling
    # invariant delivered-minus-port is unchanged by the split).
    port_ch = args.port - 10.0 * math.log10(args.channels)
    req = {"bands": bands, "threshold_dbm": args.thr, "coverage_target_pct": args.cov,
           "rsrp_offset_db": 0.0, "step_px": 2, "max_antennas": args.design_n,
           "remotes": {"count": 1, "max_dl_out_dbm": port_ch, "dl_gain_db": 60.0,
                       "operators": 1}}
    try:
        r = autoplace_floor(model, cfg, floor["id"], req)
    except AutoplaceError as exc:
        raise SystemExit("autoplace failed: %s" % exc)
    ants = []
    for row in r["antennas"]:
        aid = row["element_id"]
        x = float(row["x_px"]); y = float(row["y_px"])
        ants.append((aid, x, y, {bid: row["eirp_dbm"][bid] for bid in bands}))
    placement = {"returned": len(ants), "forced": False}
    if len(ants) < args.design_n:
        # the placement optimizer prunes when margins are huge; chase the
        # design plate (N antennas per floor) with a tighter coverage target
        tgt = max(args.cov, 99.0)
        for _t in range(4):
            req2 = dict(req)
            req2["coverage_target_pct"] = tgt
            try:
                r2 = autoplace_floor(model, cfg, floor["id"], req2)
            except AutoplaceError:
                break
            cand = r2["antennas"]
            if len(cand) >= args.design_n:
                r = r2
                ants = [(row["element_id"], float(row["x_px"]), float(row["y_px"]),
                         {bid: row["eirp_dbm"][bid] for bid in bands})
                        for row in cand[:args.design_n]]
                placement = {"returned": len(cand), "forced": True,
                             "target_pct": tgt}
                break
            tgt = min(tgt + 0.5, 99.9)
        else:
            placement = {"returned": len(ants), "forced": False,
                         "note": "planner would not place the full design plate; "
                                 "grid uses the pruned placement"}
    eirps = {bid: {aid: (aw[bid], None, False) for aid, _x, _y, aw in ants}
             for bid in bands}

    # cells
    rows_n, cols_n, worst_dim, fits = cell_layout(int(floor["w_px"]), int(floor["h_px"]),
                                                  ppm, 24.4)
    w_px, h_px = int(floor["w_px"]), int(floor["h_px"])
    cx = [((ci + 0.5) * w_px / cols_n) for ci in range(cols_n)]
    cy = [((ri + 0.5) * h_px / rows_n) for ri in range(rows_n)]
    outline = ap_mod._wall_bbox_outline(floor)
    if outline and len(outline) >= 3:
        pts = [(float(x), float(y)) for y in cy for x in cx if _inside(x, y, outline)]
    else:
        pts = [(float(x), float(y)) for y in cy for x in cx]

    class _Mini:
        def __init__(self, xs, ys):
            self.xs = xs; self.ys = ys; self.n = len(xs)
    cellgrid = _Mini([p[0] for p in pts], [p[1] for p in pts])

    ahj = {"site_erp": args.site_erp, "site_nf": args.site_nf,
           "ambient": args.ambient, "g_donor": args.g_donor,
           "arrival_margin": args.arrival_margin, "noise_rule": args.noise_rule}
    site_floor = -174.0 + 10.0 * math.log10(12500.0) + args.site_nf
    target_arr = site_floor + args.arrival_margin
    eirp_ue = args.ue_tx - args.body_loss
    # UL gain solve mirrors ps_ul: G = target - (ul_port_far + G_donor + ambient - site_erp)
    loss_at_k = ((10 * math.log10(args.k) + 0.5) if args.k > 1 else 0.0) + \
        (args.k // 2) * float(cfg["autoplace"]["floor_height_m"]) * \
        (float(cfg["cables"]["coax_1_2in"]["loss_db_per_100m_ghz"]["2.0"]) / 100.0)
    use_cell_gain = args.ul_gain is None
    if args.ul_gain is None:
        args.ul_gain = 0.0

    band_out = {}
    for bid in bands:
        b = bmap[bid]
        lin = _lin_from(cellgrid, {bid: b}, floor, [], {bid: eirps[bid]},
                        auto_ants=[(aid, x, y) for aid, x, y, _ in ants])
        dl = [10.0 * math.log10(v) if v > 0 else -200.0 for v in lin[bid]]
        # LA County method: derive the UL gain from the worst cell first, then
        # report per-cell arrivals at that gain
        ul_ports = [eirp_ue - args.port + lvl for lvl in dl]
        if use_cell_gain:
            worst = min(ul_ports)
            args.ul_gain = max(0.0, target_arr - (worst + args.g_donor
                                                  + args.ambient - args.site_erp))
        cells = []
        nfails = 0
        ndl_fail = nsinr_fail = nul_fail = 0
        for i, (x, y) in enumerate(pts):
            lvl = dl[i]
            sinr = lvl - (-174.0 + 10.0 * math.log10(12500.0) + args.nf_portable)
            arrival = ul_ports[i] + args.ul_gain + args.g_donor + args.ambient - args.site_erp
            dl_pass = lvl >= args.thr
            sinr_pass = sinr >= args.sinr_threshold
            ul_pass = arrival >= target_arr
            if not dl_pass:
                ndl_fail += 1
            if not sinr_pass:
                nsinr_fail += 1
            if not ul_pass:
                nul_fail += 1
            if not (dl_pass and sinr_pass and ul_pass):
                nfails += 1
            cells.append({"cell": i + 1, "x_m": round(x / ppm, 1),
                          "y_m": round(y / ppm, 1),
                          "dl_dbm": round(lvl, 1), "dl_pass": dl_pass,
                          "sinr_db_est": round(sinr, 1), "sinr_pass": sinr_pass,
                          "calc_ul_site_dbm": round(arrival, 1), "ul_pass": ul_pass})
        # spread stats over the full-lattice prediction
        full = _Grid(floor, 2, outline, crit_polys)
        linf = _lin_from(full, {bid: b}, floor, [], {bid: eirps[bid]},
                         auto_ants=[(aid, x, y) for aid, x, y, _ in ants])
        lv = sorted(10.0 * math.log10(v) for v in linf[bid] if v > 0)
        def pct(p):
            return lv[min(len(lv) - 1, int(len(lv) * p / 100.0))] if lv else None
        smin, p5, p95, smax = (lv[0] if lv else None), pct(5), pct(95), (lv[-1] if lv else None)
        spread = (smax - smin) if (smax is not None and smin is not None) else None
        # near ring: 4 points 1.5 m from each antenna
        ring_pts = []
        for aid, ax, ay, _ in ants:
            d_px = 1.5 * ppm
            ring_pts += [(ax + d_px, ay), (ax - d_px, ay), (ax, ay + d_px), (ax, ay - d_px)]
        ringgrid = _Mini([p[0] for p in ring_pts], [p[1] for p in ring_pts])
        linr = _lin_from(ringgrid, {bid: b}, floor, [], {bid: eirps[bid]},
                         auto_ants=[(aid, x, y) for aid, x, y, _ in ants])
        per_ant = []
        for j in range(0, len(ring_pts), 4):
            seg = linr[bid][j:j + 4]
            lvl = 10.0 * math.log10(max(seg)) if max(seg) > 0 else -200.0
            near_in = eirp_ue - port_ch + lvl
            per_ant.append({"antenna": ants[j // 4][0],
                            "x_m": round(ants[j // 4][1] / ppm, 1),
                            "y_m": round(ants[j // 4][2] / ppm, 1),
                            "near_ring_dl_dbm": round(lvl, 1),
                            "near_input_bda_dbm": round(near_in, 1)})
        band_out[bid] = {"cells": cells, "n_cells": len(cells), "n_fail": nfails,
                         "floor_pass": nfails <= 1,
                         "fail_breakdown": {"dl": ndl_fail, "sinr": nsinr_fail,
                                            "calc_ul": nul_fail},
                         "retest_zone": nfails == 2,
                         "spread": {"min_dbm": round(smin, 1) if smin is not None else None,
                                    "p5_dbm": round(p5, 1) if p5 is not None else None,
                                    "p95_dbm": round(p95, 1) if p95 is not None else None,
                                    "max_dbm": round(smax, 1) if smax is not None else None,
                                    "spread_db": round(spread, 1) if spread is not None else None},
                         "near_ring": per_ant}

    # critical areas: the floor model's zones if it carries them
    crit = None
    # the owner's sequential density loop (2026-11-26): minimal coverage plate
    # first, then add antennas until the near-far check passes: the per-floor
    # cell shrinks with each antenna (the under-antenna coupling drops
    # 10log10(N) in the balanced tree) and the spread tightens. The curve
    # reports every step so the tap/pad equivalent stays visible.
    density_loop = None
    firsts = {}
    if args.solve_density:
        from ps_profiles import resolve as resolve_profile
        RM, PW2, _p2 = resolve_profile(args.profile, {
            "near_op": ("near_operating_margin_db", "near_operating_margin_db", None),
            "near_dmg": ("near_damage_margin_db", "near_damage_margin_db", None)})
        near_op = RM["near_op"]
        near_dmg = RM["near_dmg"]
        max_in = args.near_max_input_db
        dmg_in = args.near_damaging_db
        eirp_ue = args.ue_tx - args.body_loss
        curve = []
        rec_n = None
        full = _Grid(floor, 2, outline, crit_polys)
        for n in range(1, args.density_max + 1):
            rows_n_d, cols_n_d = _factor_pair(n)
            cx = [((ci + 0.5) * w_px / cols_n_d) for ci in range(cols_n_d)]
            cy = [((ri + 0.5) * h_px / rows_n_d) for ri in range(rows_n_d)]
            if outline and len(outline) >= 3:
                dpts = [(float(x), float(y)) for y in cy for x in cx
                        if _inside(x, y, outline)]
            else:
                dpts = [(float(x), float(y)) for y in cy for x in cx]
            dgrid = _Mini([p[0] for p in dpts], [p[1] for p in dpts])
            share = 10.0 * math.log10(n)
            dens_eirps = {bid: {"d%d" % i:
                                (args.port - 10.0 * math.log10(args.channels)
                                 - share, None, False)
                                for i in range(len(dpts))}
                          for bid in bands}
            ant_list = [("d%d" % i, x, y) for i, (x, y) in enumerate(dpts)]
            firsts = {}
            per_band_all = {}
            for bid in bands:
                b = bmap[bid]
                lv = _lin_from(full, {bid: b}, floor, [], {bid: dens_eirps[bid]},
                               auto_ants=[(a0, x, y) for a0, x, y in ant_list])
                vals = sorted(10.0 * math.log10(v) for v in lv[bid] if v > 0)
                smin = vals[0] if vals else -200.0
                smax = vals[-1] if vals else -200.0
                f = float((bands_cfg or {}).get(bid, {}).get("freq_mhz", 772.0))
                near_in = eirp_ue - fspl_db(f, 1.5) - share
                spread = smax - smin
                near_pass = (near_in <= max_in - near_op
                             and near_in <= dmg_in - near_dmg)
                cov_ok = bool(vals and smin >= args.thr)
                per_band_all[bid] = {"min_dbm": round(smin, 1),
                                     "max_dbm": round(smax, 1),
                                     "spread_db": round(spread, 1),
                                     "near_input_at_bda_dbm": round(near_in, 2),
                                     "near_pass": bool(near_pass),
                                     "coverage_min_ok": cov_ok}
                if near_pass and cov_ok and spread <= args.spread_target_db:
                    firsts[bid] = n
            curve.append({"n": n, "bands": per_band_all})
        density_loop = {"solve": True, "spread_target_db": args.spread_target_db,
                        "recommended_n": (max(firsts.values()) if firsts else None),
                        "first_pass_by_band": firsts,
                        "equiv_branch_loss_db": (round(10.0 * math.log10(max(firsts.values())), 1)
                                                 if firsts else None),
                        "equivalence_note": "the density route builds the same "
                                            "dB of branch loss the taps or pad "
                                            "would; each doubling of antennas "
                                            "is about 3 dB",
                        "curve": curve}

    # critical areas: the floor model's zones if it carries them
    crit = None
    zones = floor.get("zones") or []
    if zones:
        crit = []
        critpts = []
        for z in zones:
            poly = [[p[0], p[1]] for p in z["points"]]
            bx = [p[0] for p in poly]; by = [p[1] for p in poly]
            candidates = [((bx[0] + bx[1]) / 2.0 if len(bx) == 2 else sum(bx) / len(bx),
                           (by[0] + by[1]) / 2.0 if len(by) == 2 else sum(by) / len(by))]
            for (px_, py_) in [(bx[0], by[0]), (bx[0], by[-1]), (bx[-1], by[0]), (bx[-1], by[-1]),
                               (min(bx), min(by)), (max(bx), max(by))]:
                candidates.append((float(px_), float(py_)))
            seen = []
            for x, y in candidates:
                if _inside(x, y, poly) and (x, y) not in seen:
                    seen.append((x, y))
            critpts.append((z.get("name", z.get("id", "zone")), seen, z.get("class")))
        cgrid = _Mini([x for _n, ps, _c in critpts for (x, y) in ps],
                      [y for _n, ps, _c in critpts for (x, y) in ps])
        if cgrid.n == 0:
            crit = None
        else:
            critlin = _lin_from(cgrid, bmap, floor, [], eirps,
                                auto_ants=[(aid, x, y) for aid, x, y, _ in ants])
            idx = 0
            for name, ps, zc in critpts:
                rows_z = []
                for _ in ps:
                    lvl = (10.0 * math.log10(critlin[bands[0]][idx])
                           if critlin[bands[0]][idx] > 0 else -200.0)
                    rows_z.append({"dl_dbm": round(lvl, 1), "pass": bool(lvl >= args.thr)})
                    idx += 1
                crit.append({"zone": name, "class": zc, "points": rows_z,
                             "pass": all(r["pass"] for r in rows_z)})

    # caps (built in, off unless provided)
    sqft = w_px * h_px / ppm ** 2 * 10.7639
    area_per_ant = sqft / max(len(ants), 1)
    caps = {"enabled": bool(args.caps_area_sqft or args.caps_k),
            "floor_area_sqft": round(sqft, 0),
            "area_sqft_per_antenna_now": round(area_per_ant, 1),
            "floors_per_port_now": args.k,
            "area_cap_sqft": args.caps_area_sqft,
            "k_cap": args.caps_k,
            "would_require_min_antennas": (math.ceil(sqft / args.caps_area_sqft)
                                           if args.caps_area_sqft else None),
            "remote_sqft_cap": args.caps_remote_sqft,
            "total_building_sqft_at_cap": (round(sqft * args.nfloors, 0)
                                           if args.caps_remote_sqft else None),
            "would_require_remotes_at_cap": (math.ceil(sqft * args.nfloors / args.caps_remote_sqft)
                                             if args.caps_remote_sqft else None),
            "would_require_min_ports": (math.ceil(args.nfloors / args.caps_k)
                                        if args.caps_k else None),
            "note": ("gate is ON for this run: the 'would_require_min_*' "
                     "count is the design the gate demands; the placed "
                     "design above is the unlocked engine fit, and the "
                     "owner turns the gate into the build rule when ready")
            if (args.caps_area_sqft or args.caps_k) else
            "caps are off (the owner default): pass --caps-area-sqft or "
            "--caps-k to see what they would require; the runs themselves "
            "never change until the owner makes them the build rule"}

    doc = {"basis": "IFC 510.5.4 grid-method self-check from the floor model "
                    "(grade E): 20 cells honoring the 80 ft max grid dimension, "
                    "cell-center predictions with the planner's propagation "
                    "engine, AHJ thresholds (RSSI %.0f dBm, SINR %.0f dB), the "
                    "calculated uplink per cell (LA County method) to the site "
                    "target (floor %.1f + %.0f dB), critical-area discrete "
                    "points when the floor carries zones, spread and near-ring "
                    "metrics per band. Design estimate, not a substitute for "
                    "the field walk." % (args.thr, args.sinr_threshold,
                                         site_floor, args.arrival_margin),
           "grid": {"rows": rows_n, "cols": cols_n, "worst_cell_dim_m": worst_dim,
                    "max_dim_rule_m": 24.4, "dimension_rule_ok": fits},
           "design": {"port_composite_dbm": args.port,
                      "port_per_channel_dbm": round(port_ch, 2),
                      "channels": args.channels,
                      "n": args.design_n, "k": args.k,
                      "ul_gain_db": round(args.ul_gain, 1), "sites": len(ants),
                      "placement": placement,
                      "ahj_profile": args.profile or "ifc_default",
                      "ul_gain_trunk_model_db": round(max(0.0, target_arr
                                                          - ((eirp_ue - loss_at_k)
                                                             + args.g_donor
                                                             + args.ambient
                                                             - args.site_erp)), 1)},
           "uplink": {"site_floor_dbm": round(site_floor, 2),
                      "target_arrival_dbm": round(target_arr, 2), **ahj},
           "bands": band_out, "critical_areas": crit, "caps_report": caps,
           "density_loop": density_loop}
    json.dump(doc, open(args.out, "w"), indent=1)
    for bid, bo in band_out.items():
        sp = bo["spread"]
        print("%s: cells %d, fail %d, floor %s | spread min/p5/p95/max "
              "%.1f/%.1f/%.1f/%.1f (%.1f dB) | %d antennas" % (
                  bid, bo["n_cells"], bo["n_fail"],
                  "PASS" if bo["floor_pass"] else "FAIL",
                  sp["min_dbm"], sp["p5_dbm"], sp["p95_dbm"], sp["max_dbm"],
                  sp["spread_db"] or 0.0, len(bo["near_ring"])))


if __name__ == "__main__":
    main()
