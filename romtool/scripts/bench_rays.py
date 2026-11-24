"""Ray-tracing benchmark: the old per-point ray loop vs the cached counts.

Takes a floor JSON file (as written by ``python -m romtool.plan_walls ...
--out floor.json``) or builds a synthetic floor itself (``--synthetic N``),
then runs the same autoplace request through three engines:

- old   : ROMTOOL_SLOW_RAYS=1, the pre-update-4 per-point ray loop
- numpy : new chunked vectorized counts (numba disabled)
- numba : new parallel JIT counts (the default when numba imports)

For every engine it reports the total time, the time per wall-loss value in
the sweep, whether the placement results are identical to the old engine's,
and the speed-up. Repeated loss values on the new path re-use the cached
crossing counts (the counts carry no loss); the old path retraces every ray
per value.

Usage:
  python scripts/bench_rays.py floor.json [--losses 4] [--losses "2,4,8"]
  python scripts/bench_rays.py --synthetic 3000 [--threshold -75]
      [--step 8] [--bands B2_1900] [--material drywall]
      [--skip-old|--skip-numpy|--skip-numba]
"""

import argparse
import copy
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from romtool import raycache                     # noqa: E402
from romtool.autoplace import autoplace_floor    # noqa: E402
from romtool.config import load_config           # noqa: E402

SRC = {"subtype": "direct", "injection_dbm": 15.0}
REP = {"max_dl_out_dbm": 33.0}


def synthetic_floor(n_walls, w_px=700.0, h_px=500.0, ppm=10.0,
                    material="drywall", seed=42):
    """A room lattice with door gaps: every interior room edge is two wall
    segments around a 1 m door, so walls per layout ~= 4*gx*gy - 2*(gx+gy)+4."""
    best = None
    for gx in range(5, 400):
        gy = max(2, int(round((n_walls - 4 + 2.0 * gx) / (4.0 * gx - 2.0))))
        count = 4 * gx * gy - 2 * gx - 2 * gy + 4
        if best is None or abs(count - n_walls) < abs(best[2] - n_walls):
            best = (gx, gy, count)
    gx, gy, count = best
    rng = random.Random(seed)
    walls = []
    wid = 0
    cw = w_px / gx
    ch = h_px / gy
    for c in range(1, gx):
        x = c * cw
        for r in range(gy):
            y0, y1 = r * ch, (r + 1) * ch
            door = rng.uniform(0.25, 0.75) * ch
            gap0, gap1 = y0 + door, y0 + min(door + 10.0, ch)
            for a, b in ((y0, gap0), (gap1, y1)):
                if b - a > 1.0:
                    wid += 1
                    walls.append({"id": "w%d" % wid, "x1": x, "y1": a,
                                  "x2": x, "y2": b, "material": material})
    for r in range(1, gy):
        y = r * ch
        for c in range(gx):
            x0, x1 = c * cw, (c + 1) * cw
            door = rng.uniform(0.25, 0.75) * cw
            gap0, gap1 = x0 + door, x0 + min(door + 10.0, cw)
            for a, b in ((x0, gap0), (gap1, x1)):
                if b - a > 1.0:
                    wid += 1
                    walls.append({"id": "w%d" % wid, "x1": a, "y1": y,
                                  "x2": b, "y2": y, "material": material})
    for a, b, vert in ((0.0, w_px, False), (0.0, h_px, True)):
        wid += 1
        if vert:
            walls.append({"id": "w%d" % wid, "x1": 0, "y1": 0, "x2": 0,
                          "y2": h_px, "material": material})
            wid += 1
            walls.append({"id": "w%d" % wid, "x1": w_px, "y1": 0, "x2": w_px,
                          "y2": h_px, "material": material})
        else:
            walls.append({"id": "w%d" % wid, "x1": 0, "y1": 0, "x2": w_px,
                          "y2": 0, "material": material})
            wid += 1
            walls.append({"id": "w%d" % wid, "x1": 0, "y1": h_px, "x2": w_px,
                          "y2": h_px, "material": material})
    floor = {"id": "f1", "name": "synthetic", "level": 0, "ppm": ppm,
             "w_px": w_px, "h_px": h_px, "walls": walls, "elements": []}
    return floor, count


def results_key(res):
    return {
        "antenna_count": res["antenna_count"],
        "positions": [(a["x_px"], a["y_px"]) for a in res["antennas"]],
        "coverage": res["coverage_pct_by_band"],
        "remotes": (res["remotes"]["count"] if res["remotes"] else None),
        "eirp": res["planning_eirp_dbm"],
    }


def run_engine(name, model, config, request, losses, slow, disable_numba,
               material):
    saved = {k: os.environ.get(k) for k in
             ("ROMTOOL_SLOW_RAYS", "ROMTOOL_NO_NUMBA")}
    os.environ["ROMTOOL_SLOW_RAYS"] = "1" if slow else "0"
    os.environ["ROMTOOL_NO_NUMBA"] = "1" if disable_numba else "0"
    times = []
    keys = []
    try:
        for value in losses:
            cfg = copy.deepcopy(config)
            cfg["wall_materials"][material]["loss_db"] = value
            raycache.clear_ray_cache()
            t0 = time.perf_counter()
            res = autoplace_floor(model, cfg, "f1", request)
            times.append(time.perf_counter() - t0)
            keys.append(results_key(res))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return times, keys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bench_rays")
    ap.add_argument("floor", nargs="?", default=None, help="floor JSON file")
    ap.add_argument("--synthetic", type=int, default=None,
                    help="build a synthetic floor with about N walls")
    ap.add_argument("--losses", default="4",
                    help="comma list of wall loss dB values to sweep "
                         "(default '4'; the default engine's own value)")
    ap.add_argument("--material", default="drywall",
                    help="wall material id whose loss_db the sweep rewrites")
    ap.add_argument("--threshold", type=float, default=-70.0)
    ap.add_argument("--step", type=int, default=8, help="request step_px")
    ap.add_argument("--bands", default="B2_1900")
    ap.add_argument("--skip-old", action="store_true")
    ap.add_argument("--skip-numpy", action="store_true")
    ap.add_argument("--skip-numba", action="store_true")
    args = ap.parse_args(argv)

    config = load_config()
    if args.synthetic:
        floor, count = synthetic_floor(args.synthetic, material=args.material)
        print("synthetic floor: %d walls requested, %d built (%s, %gx%g px)"
              % (args.synthetic, count, args.material,
                 floor["w_px"], floor["h_px"]))
    else:
        if not args.floor:
            ap.error("give a floor JSON file or --synthetic N")
        with open(args.floor, "r", encoding="utf-8") as fh:
            floor = json.load(fh)
        print("floor file: %s (%d walls)"
              % (args.floor, len(floor.get("walls") or [])))
    model = {"environment": "office", "floors": [floor]}
    request = {"bands": args.bands.split(","),
               "threshold_dbm": args.threshold, "source": SRC,
               "repeater": REP, "step_px": args.step}
    losses = [float(v) for v in args.losses.split(",") if v.strip()]
    print("request: bands=%s threshold=%.1f step=%dpx, wall loss sweep %s dB"
          % (request["bands"], args.threshold, args.step, losses))

    engines = []
    if not args.skip_old:
        engines.append(("old", True, False))
    if not args.skip_numpy:
        engines.append(("numpy", False, True))
    if not args.skip_numba:
        engines.append(("numba", False, False))
    totals = {}
    keys_by_engine = {}
    for name, slow, disnumba in engines:
        times, keys = run_engine(name, model, config, request, losses,
                                 slow, disnumba, args.material)
        keys_by_engine[name] = keys
        totals[name] = sum(times)
        print("%-5s total %9.2f s  (%s)"
              % (name, totals[name], ", ".join("%.2f s" % t for t in times)))
        for value, key in zip(losses, keys):
            rem = (" remotes %s" % key["remotes"]) if key["remotes"] else ""
            print("   loss %g: %d antenna(s)%s, coverage %s"
                  % (value, key["antenna_count"], rem, key["coverage"]))
    base = keys_by_engine.get("old")
    if base is not None:
        for name in ("numpy", "numba"):
            if name in keys_by_engine:
                keys = keys_by_engine[name]
                same = base == keys
                print("%-5s results identical to old: %s" % (name, same))
                if not same:
                    for li, (b, k) in enumerate(zip(base, keys)):
                        if b != k:
                            print("   loss %g differs: old %s new %s"
                                  % (losses[li], b, k))
        for name, total in totals.items():
            if name != "old" and total > 0:
                print("%-5s speed-up vs old: %.1fx" % (name, totals["old"] / total))
    elif len(totals) > 1:
        ref = keys_by_engine.get("numba") or keys_by_engine.get("numpy")
        for name, keys in keys_by_engine.items():
            print("%-5s results identical to %s: %s"
                  % (name, "numba" if ref is keys_by_engine.get("numba") else "numpy",
                     keys == ref))


if __name__ == "__main__":
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
