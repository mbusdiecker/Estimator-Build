"""stack_check.py: trunked-tap stack check on top of the pmf run outputs.

pmf's loss_db(k) models the between-floors slice as a k-way star split plus riser
coax. Real multi-floor coax distribution is a trunked tap chain: the remote sits
mid-stack, 7/8 in riser segments run down and up, and each floor takes power off
a tap whose coupled ratio is the commercial value nearest its equal share (the
trunk end floor has no tap). This script re-solves k, floors per remote, against
those trunk losses and emits the counted stack BOM per OEM.

Inputs: the pmf run JSONs in --dir (P_min per band group at the design N, which
already carries each floor's splitter tree, branch cable and branch hardware
through autoplace_floor), plus oem_results.json for the flat-formula k.
Outputs: stack_results.json and a printed comparison table. Config estimates
(grade E), not measured. No prices, no customer or job names.
"""
import argparse, json, math, os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
from romtool.config import load_config
from romtool.autoplace import TRUNK_TAP_RATIOS_DB, TRUNK_TAP_EXCESS_DB
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from combine_oems import ROWS as OEM_ROWS

FLOORS = 45
KMAX = 48


def tap_loss_db(ratio_db):
    """Through loss of a tap/coupler with the given coupled ratio, plus the
    trunk tap insertion excess (grade E, the same value the trunk4 planner uses)."""
    return -10.0 * math.log10(1.0 - 10.0 ** (-ratio_db / 10.0)) + TRUNK_TAP_EXCESS_DB


def nearest_ratio(exact_db):
    return min(TRUNK_TAP_RATIOS_DB, key=lambda r: (abs(r - exact_db), r))


def trunk_losses(k, seg_db):
    """Per-floor between-floors losses for one remote port feeding k floors:
    the remote's own floor taps off first (ratio nearest its equal share of k),
    then a single trunk string serves the other k-1 floors, each tap ratio the
    commercial value nearest that floor's equal share, the trunk end floor fed
    straight through with no tap. losses[0] is the own floor, losses[1..k-1]
    the trunk floors in order from the remote. Delivered = port - loss."""
    m = k - 1
    r0 = nearest_ratio(10.0 * math.log10(k)) if m > 0 else 0.0
    through = tap_loss_db(r0) if m > 0 else 0.0
    losses = [r0]
    for i in range(1, m + 1):
        r = nearest_ratio(10.0 * math.log10(m - i + 1)) if i < m else 0.0
        losses.append(through + i * seg_db + r)
        if i < m:
            through += tap_loss_db(r)
    return losses


def stack_taps(k):
    """Counted tap devices for k floors on one port: the own-floor tap plus
    one tap per trunk floor except the trunk end."""
    return max(k - 1, 0)


def _tap_positions(k):
    """(floor_from_remote, ratio_db) for the tap devices of a k-floor trunk:
    position 0 is the own-floor tap at the remote floor, positions 1..k-2 the
    trunk taps; the trunk end floor has no tap."""
    m = k - 1
    out = [(0, nearest_ratio(10.0 * math.log10(k)))]
    for i in range(1, m):
        out.append((i, nearest_ratio(10.0 * math.log10(m - i + 1))))
    return out


def worst_loss_db(k, seg_db):
    if k <= 1:
        return 0.0
    return max(trunk_losses(k, seg_db))



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out")
    ap.add_argument("--res", type=float, default=0.25)
    a = ap.parse_args()

    cfg = load_config()
    cables = cfg.get("cables", {})
    fh = float((cfg.get("autoplace") or {}).get("floor_height_m", 4.0))
    riser_cable = "coax_1_2in"  # Matt (2026-11-25): riser is always 1/2 in, never 7/8 in
    riser_db_m = float(cables[riser_cable]["loss_db_per_100m_ghz"]["2.0"]) / 100.0
    conn = float(((cfg.get("branch_hardware") or {}).get("connector") or {}).get("loss_db", 0.1))
    seg_db = fh * riser_db_m + 2.0 * conn  # one riser segment between two floors

    with open(os.path.join(a.dir, "oem_results.json")) as f:
        combined = json.load(f)
    flat = {r["oem"]: r for r in combined["design"]}

    results, table = [], []
    for oem, pcfg, bands, src, prefixes in OEM_ROWS:
        groups = []
        for p in prefixes:
            with open(os.path.join(a.dir, "pmf_%s.json" % p)) as f:
                run = json.load(f)
            row4 = next(r for r in run["rows"] if r["N"] == 4)
            groups.append({"prefix": p, "port_dbm": run["port_dbm"], "p_min": row4["P_min"],
                           "thr": run["thr"],
                           "bands": ",".join(sorted(json.loads(json.dumps(
                               list(row4["cov_by_band"])))))})
        k_flat = flat.get(oem, {}).get("k_floors_per_port")
        flat_cap = flat.get(oem, {}).get("k_cap")
        design_thr = flat.get(oem, {}).get("thr_dbm")
        # per band group: the largest k whose worst-floor delivered power still
        # covers that group's P_min (P_min already carries the floor chain)
        k_group = {}
        for g in groups:
            head = g["port_dbm"] - g["p_min"]
            best = 0
            for k in range(1, KMAX + 1):
                if head - worst_loss_db(k, seg_db) >= -1e-9:
                    best = k
            k_group[g["prefix"]] = best
        k_stack = min(k_group.values()) if k_group else 0
        remotes = math.ceil(FLOORS / (k_stack * flat.get(oem, {}).get("ports", 1))) if k_stack else None
        # the binding group's margin at k_stack
        bind = min(k_group, key=lambda p: k_group[p])
        bg = next(g for g in groups if g["prefix"] == bind)
        margin = bg["port_dbm"] - bg["p_min"] - worst_loss_db(k_stack, seg_db) if k_stack else None
        taps_total = stack_taps(k_stack)
        segs_total = max(k_stack - 1, 0)
        riser_m = round(segs_total * fh, 1)
        # counted stack BOM: floor tree counts come from the aggregate floor
        # BOM (x45), trunk parts from the tap chain; remotes from the stack
        floor_bom = {"tapper": 0, "riser_segment": 0}
        stack = {"oem": oem, "design_N": 4, "ports": flat.get(oem, {}).get("ports", 1),
                 "k_flat": k_flat, "k_flat_cap": flat_cap,
                 "k_stack": k_stack, "remotes_bldg": remotes,
                 "groups": [{"prefix": g["prefix"], "port_dbm": g["port_dbm"],
                             "p_min": g["p_min"], "k_stack_group": k_group[g["prefix"]]}
                            for g in groups],
                 "binding_group": bind,
                 "worst_margin_db_at_k_stack": None if margin is None else round(margin, 2),
                 "design_thr_dbm": design_thr,
                 "tappers": taps_total, "riser_segments": segs_total,
                 "riser_cable_1_2in_m": riser_m,
                 "riser_connectors": 2 * segs_total,
                 "taps_detail": [], "floor_bom": None, "notes": []}
        if k_stack >= 2:
            stack["taps_detail"] = [{"floor_from_remote": idx, "ratio_db": r,
                                     "through_loss_db": round(tap_loss_db(r), 2)}
                                    for idx, r in _tap_positions(k_stack)]
        if a.out:
            pass
        results.append(stack)
        table.append((oem, k_flat, flat_cap, k_stack, remotes, margin, taps_total, segs_total, riser_m))

        # one autoplace run per OEM for the real floor-tree counts (even N=4)
        try:
            import importlib
            autoplace = importlib.import_module("romtool.autoplace")
            NE = [4]
            def seed_even(self, _planner=autoplace._Planner, _NE=NE):
                xs = self.grid.xs; ys = self.grid.ys
                x0, x1 = min(xs), max(xs); yc = 0.5 * (min(ys) + max(ys))
                for i in range(_NE[0]):
                    x, y = self._nearest_point(x0 + (i + 0.5) * (x1 - x0) / _NE[0], yc)
                    self.add(x, y)
                self.seed_n = _NE[0]
            autoplace._Planner.seed = seed_even
            autoplace._Planner.prune = lambda self: None  # fixed layout, same as pmf.py
            floor = json.load(open(os.path.join(a.dir, "floor4_soup.json")))
            cfg2 = dict(cfg)
            cfg2["wall_materials"] = dict(cfg.get("wall_materials") or {})
            cfg2["wall_materials"]["drywall"] = dict((cfg.get("wall_materials") or {}).get("drywall") or {})
            cfg2["wall_materials"]["drywall"]["loss_db"] = -1
            model = {"environment": "office", "building_type": "hotel_residential", "floors": [floor]}
            union_bands = sorted({b for g in groups for b in g["bands"].split(",")})
            # plan the tree at the design delivery, not at the port maximum: a
            # floor in the stack receives about P_min plus the trunk margin, and
            # planning at the port power would prune the tree to one antenna
            margin_bom = margin if margin is not None else (max(g["port_dbm"] for g in groups) - bg["p_min"])
            dl_bom = min(max(g["port_dbm"] for g in groups),
                         bg["p_min"] + margin_bom + a.res)
            req = {"bands": union_bands, "threshold_dbm": -103.0, "coverage_target_pct": 98.0,
                   "rsrp_offset_db": -30.8, "step_px": 2, "max_antennas": 4,
                   "remotes": {"count": 1, "max_dl_out_dbm": dl_bom,
                               "dl_gain_db": 60.0, "operators": 3.0}}
            r = autoplace.autoplace_floor(model, cfg2, floor["id"], req)
            assert min((r.get("coverage_pct_by_band") or {0: 0}).values()) >= 98.0
            from romtool.bom import generate_bom
            items = generate_bom(model, cfg2)["items"]
            agg, per_remote = {}, {}
            for it in items:
                if it["category"] in ("remote", "source", "repeater"):
                    continue
                label = it["item"]
                if "LMR-400" in label:
                    continue  # donor-side feeder, not part of the floor distribution
                key = (it["category"], label, it["unit"])
                if "Fiber" in label:
                    per_remote[key] = per_remote.get(key, 0.0) + float(it["qty"])
                    continue  # the remote's fiber run repeats per remote, not per floor
                agg[key] = agg.get(key, 0.0) + float(it["qty"])
            stack["floor_bom"] = {"%s | %s (%s)" % (c, i, u): q for (c, i, u), q in sorted(agg.items())}
            stack["per_remote_fiber_bom"] = {
                "%s | %s (%s)" % (c, i, u): round(q * (remotes or 0), 1)
                for (c, i, u), q in sorted(per_remote.items())}
        except Exception as exc:  # keep the loss numbers even if the BOM run fails
            stack["notes"].append("floor BOM run failed: %s" % exc)

    print("| OEM | N | k flat (pmf) | k stack | remotes (45 fl) | worst margin dB at k stack | tappers | riser segments | riser 1/2 m |")
    print("|---|---|---|---|---|---|---|---|---|")
    for oem, kf, cap, ks, rem, mrg, taps, segs, rm in table:
        kf_s = "%s%s" % (kf, " (cap)" if cap else "")
        m_s = "" if mrg is None else "%.2f" % mrg
        print("| %s | 4 | %s | %s | %s | %s | %s | %s | %s |" % (oem, kf_s, ks, rem, m_s, taps, segs, rm))

    if a.out:
        with open(a.out, "w") as f:
            json.dump({"floors": FLOORS, "seg_db_per_floor": round(seg_db, 3),
                       "connector_loss_db_each": conn, "riser_1_2in_db_per_m": round(riser_db_m, 4),
                       "riser_cable_id": riser_cable,
                       "stack": results}, f, indent=1)


if __name__ == "__main__":
    main()
