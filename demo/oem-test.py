"""ROMtool OEM test: auto-placed antenna count and BOM vs the design, with leave-one-out calibration.

Usage: oem-test.py <jobs.json> <oem summary.csv> <out dir> [--clutter 0,0.1,...]

jobs.json: [{"job": "...", "building_type": "office", "bands": ["B2_1900", "B66_2100"],
             "floors": [{"sq_ft": 25000, "count": 3}], "threshold_dbm": -95, "rsrp_offset_db": -31,
             "coverage_target_pct": 95, "notes": "..."}]
Each floor is a 1.6:1 rectangle of its square footage (no walls), so the building type's default loss applies.
For every clutter value in the sweep, each distinct floor size is auto-placed once and multiplied by its count.
Leave-one-out: each job's prediction uses the clutter value that minimises the median absolute antenna-count
error over the OTHER jobs, so no job is scored with a value fitted on itself.
Pass bar (Matt): antenna count and BOM within 15% of the design on 8 of 10 jobs.
"""
import copy, csv, json, math, os, statistics, sys, time

ROMTOOL = r'C:\Users\MattBusdiecker\Documents\Estimator Build\romtool'
sys.path.insert(0, ROMTOOL)
from romtool.autoplace import AutoplaceError, autoplace_floor   # noqa: E402
from romtool.bom import generate_bom                            # noqa: E402
from romtool.config import load_config                          # noqa: E402

PPM = 2.0          # px per metre (0.5 m grid) keeps big floors fast; autoplace caps plan points anyway
SQFT_PER_M2 = 10.7639
SRC = {"subtype": "direct", "injection_dbm": 15.0}
REP = {"max_dl_out_dbm": 33.0}


def floor_model(sq_ft, building_type):
    m2 = sq_ft / SQFT_PER_M2
    h_m = math.sqrt(m2 / 1.6); w_m = 1.6 * h_m
    w_px, h_px = int(round(w_m * PPM)), int(round(h_m * PPM))
    floor = {"id": "f1", "name": "F1", "level": 0, "ppm": PPM, "w_px": w_px, "h_px": h_px, "walls": [], "elements": []}
    return {"environment": "office", "building_type": building_type, "floors": [floor]}


def run_floor(cfg, job, sq_ft):
    model = floor_model(sq_ft, job["building_type"])
    req = {"bands": job["bands"], "source": SRC, "repeater": REP, "verify_max_points": 20000,
           "coverage_target_pct": job.get("coverage_target_pct", 95)}
    if "threshold_dbm" in job: req["threshold_dbm"] = job["threshold_dbm"]
    if "rsrp_offset_db" in job: req["rsrp_offset_db"] = job["rsrp_offset_db"]
    res = autoplace_floor(model, cfg, "f1", req)
    bom = generate_bom(model, cfg)["items"]
    agg = {"antennas": len(res["antennas"]), "coax_half_ft": 0.0, "coax_other_ft": 0.0, "splitters": 0, "tappers": 0, "couplers": 0}
    for it in bom:
        if it["category"] == "cable" and it["unit"] == "ft":
            agg["coax_half_ft" if "1/2" in it["item"] else "coax_other_ft"] += it["qty"]
        elif it["category"] == "splitter": agg["splitters"] += it["qty"]
        elif it["category"] == "tapper": agg["tappers"] += it["qty"]
        elif it["category"] == "coupler": agg["couplers"] += it["qty"]
    return agg


def num(x):
    try: return float(x)
    except (TypeError, ValueError): return None


def main():
    jobs = json.load(open(sys.argv[1], encoding='utf-8'))
    oem = {r['job']: r for r in csv.DictReader(open(sys.argv[2], encoding='utf-8'))}
    out = sys.argv[3]; os.makedirs(out, exist_ok=True)
    sweep = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.5]
    if '--clutter' in sys.argv: sweep = [float(x) for x in sys.argv[sys.argv.index('--clutter') + 1].split(',')]
    base = load_config()
    raw = []   # (job, clutter, totals)
    for job in jobs:
        for c in sweep:
            cfg = copy.deepcopy(base)
            cfg['building_types'][job['building_type']]['clutter_db_per_m'] = c
            tot = {"antennas": 0, "coax_half_ft": 0.0, "coax_other_ft": 0.0, "splitters": 0, "tappers": 0, "couplers": 0}
            err = None; t0 = time.time()
            try:
                for fl in job['floors']:
                    a = run_floor(cfg, job, fl['sq_ft'])
                    for k in tot: tot[k] += a[k] * fl.get('count', 1)
            except AutoplaceError as e:
                err = '; '.join(getattr(e, 'errors', [str(e)]))[:200]
            raw.append(dict(job=job['job'], clutter=c, error=err, secs=round(time.time() - t0, 1), **tot))
            print(job['job'], c, 'ERR ' + err if err else tot['antennas'], flush=True)
    with open(os.path.join(out, 'sweep.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(raw[0].keys())); w.writeheader(); w.writerows(raw)

    def ant_err(job, c):
        r = next(x for x in raw if x['job'] == job and x['clutter'] == c)
        d = num(oem.get(job, {}).get('service_antennas'))
        if r['error'] or not d: return None
        return abs(r['antennas'] - d) / d

    scored = [j['job'] for j in jobs if num(oem.get(j['job'], {}).get('service_antennas'))]
    rows = []
    for j in jobs:
        jid = j['job']
        others = [o for o in scored if o != jid]
        best, best_med = None, None
        for c in sweep:
            errs = [e for e in (ant_err(o, c) for o in others) if e is not None]
            if len(errs) < max(1, len(others) // 2): continue
            med = statistics.median(errs)
            if best_med is None or med < best_med: best, best_med = c, med
        r = next((x for x in raw if x['job'] == jid and x['clutter'] == best), None)
        o = oem.get(jid, {})
        def pct(pred, key):
            d = num(o.get(key))
            return None if (pred is None or not d) else round((pred - d) / d * 100, 1)
        rows.append(dict(job=jid, building_type=j['building_type'], loo_clutter=best,
                         pred_antennas=r and r['antennas'], design_antennas=o.get('service_antennas'),
                         antenna_err_pct=r and pct(r['antennas'], 'service_antennas'),
                         pred_coax_half_ft=r and round(r['coax_half_ft']), design_coax_half_ft=o.get('coax_half_ft'),
                         coax_err_pct=r and pct(r['coax_half_ft'], 'coax_half_ft'),
                         pred_splitters=r and (r['splitters'] + r['tappers'] + r['couplers']),
                         design_splitters=(num(o.get('splitters')) or 0) + (num(o.get('tappers')) or 0) + (num(o.get('couplers')) or 0) or None,
                         error=r and r['error']))
    for r in rows:
        d = r['design_splitters']
        r['splitter_err_pct'] = None if not (d and r['pred_splitters'] is not None) else round((r['pred_splitters'] - d) / d * 100, 1)
        r['antenna_pass'] = r['antenna_err_pct'] is not None and abs(r['antenna_err_pct']) <= 15
    with open(os.path.join(out, 'results.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    passed = sum(1 for r in rows if r['antenna_pass'])
    print(f"\nantenna count within 15%: {passed} of {sum(1 for r in rows if r['antenna_err_pct'] is not None)} scored jobs (bar: 8 of 10)")
    for r in rows:
        print(r['job'], '| type', r['building_type'], '| clutter', r['loo_clutter'], '| ant', r['pred_antennas'], 'vs', r['design_antennas'],
              f"({r['antenna_err_pct']}%)", '| 1/2in ft', r['pred_coax_half_ft'], 'vs', r['design_coax_half_ft'], f"({r['coax_err_pct']}%)",
              '| split', r['pred_splitters'], 'vs', r['design_splitters'], f"({r['splitter_err_pct']}%)", '|', r['error'] or '')


if __name__ == '__main__':
    main()
