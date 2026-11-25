"""combine_oems.py: merge pmf.py run outputs into one OEM comparison table.
usage: python combine_oems.py --dir RUN_DIR [--out RESULT.json]
Reads pmf_<prefix>.json files written by pmf.py. For split OEMs (per band group)
the combined floors-per-port at each N is the min k_fit across the group's runs,
naming the binding run (comma list on ties). The design row is picked by walking
the combined series with pmf.py's design rule: base = first N with coverage and
area, advance while the next N buys 5+ coverage points or one more floor per
port, stop at the first unjustified step or once one design feeds all floors.
The design row's k_cap reflects the binding runs; the all_n series keeps the
any-run-at-kmax flag. Standard library only.
"""
import argparse, json, math, os, sys

FLOORS = 45

# comparison row: oem, port_config, bands_in_run, source_label, run prefixes (pmf_<prefix>.json)
ROWS = [
    ("q4000c", "1 x 16 dBm per band", "B12, B5, B2, B66", "QUATRA 4000c CU datasheet", ("q4000c",)),
    ("signalmax", "4 x 17 dBm per band", "B12, B5, B2, B66", "SignalMax Fiber DAS datasheet", ("signalmax",)),
    ("surecall_5xmax", "1 x 16 dBm per band", "B12, B5, B2, B66", "5X Max datasheet", ("5xmax",)),
    ("adxv_mpr", "low bands 33 dBm, high bands 37 dBm", "B2, B66 at 37 dBm; B12, B5 at 33 dBm",
     "ADX V datasheet mid power remote", ("adxv_low33", "adxv_high37")),
    ("adxv_lpr", "low bands 17 dBm, high bands 20 dBm", "B2, B66 at 20 dBm; B12, B5 at 17 dBm",
     "ADX V datasheet low power remote", ("adxv_lpr_lo", "adxv_lpr_hi")),
    ("adxv_hpr", "low bands 43 dBm, high bands 46 dBm", "B2, B66 at 46 dBm; B12, B5 at 43 dBm",
     "ADX V datasheet high power remote", ("adxv_hpr_lo", "adxv_hpr_hi")),
    ("comba_mruc", "8 x 17 dBm per band", "B12, B5, B2, B66", "ComFlex NG MRUc DS 1-0-2", ("comba_mruc",)),
    ("jma_t19awe", "2 outputs x 46 dBm (one per band)", "B2, B66",
     "46 dBm per output, owner-confirmed in writing", ("jma_t19awe",)),
    ("sdrc_only", "1 x 17 dBm per band (one SDRC module per band)", "B12, B5, B2, B66",
     "Part 20 OEM configurator, SDRC-17 in BDA mode", ("sdrc_only",)),
]


def load_run(dir_path, prefix):
    path = os.path.join(dir_path, "pmf_%s.json" % prefix)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        print("warning: unreadable pmf_%s.json, treating as missing" % prefix, file=sys.stderr)
        return None


def combined_at(runs, n):
    """Conservative combination at N: min k_fit, min coverage; null stays null."""
    entries = []
    for prefix, data, by_n in runs:
        row = by_n.get(n)
        if row is None:
            return None
        entries.append((prefix, data, row))
    k = min(row["k_fit"] for _, _, row in entries)
    # binding: run(s) behind the min k; singles have no binding
    binding = "" if len(entries) == 1 else ",".join(p for p, _, row in entries if row["k_fit"] == k)
    k_cap = any(row["k_fit"] == data["kmax"] for _, data, row in entries)
    conf = [row["cov_confirmed"] for _, _, row in entries]
    nxt = [row["cov_next_floor"] for _, _, row in entries]
    ports = entries[0][1]["ports"]
    fpr = k * ports
    return {"k": k, "binding": binding, "k_cap": k_cap,
            # k_fit 0 or a null in the group means nothing confirmed at that N
            "cov_confirmed": None if (k == 0 or any(v is None for v in conf)) else min(conf),
            "cov_next": None if (k == 0 or any(v is None for v in nxt)) else min(nxt),
            "ports": ports, "floors_per_remote": fpr,
            "remotes": math.ceil(FLOORS / fpr) if fpr else None}


def walk_design(runs):
    """Pick the design N by walking the combined series with pmf.py's design rule:
    base = first N where every run has coverage and area; advance while the next N
    buys 5+ coverage points (min across runs) or one more floor per port; stop at
    the first unjustified step or once one design feeds all FLOORS."""
    common = sorted(set.intersection(*[set(by_n) for _, _, by_n in runs]))
    qual = [n for n in common
            if all(by_n[n]["P_min"] is not None and by_n[n]["area_ok"] for _, _, by_n in runs)]
    if not qual:
        return None
    d = qual[0]
    for n in qual[1:]:
        c_now = combined_at(runs, n)
        c_prev = combined_at(runs, d)
        if c_now is None or c_prev is None:
            break
        gain = min(by_n[n]["cov_at_port"] for _, _, by_n in runs) \
             - min(by_n[d]["cov_at_port"] for _, _, by_n in runs)
        if gain >= 5.0 or c_now["k"] >= c_prev["k"] + 1:
            d = n
            if c_now["floors_per_remote"] >= FLOORS:
                break  # a design that feeds the whole building needs no more antennas
        else:
            break
    return d


def binding_cap(runs, dn, binding):
    """True when the binding run(s) sit on their own kmax."""
    prefixes = set(binding.split(",")) if binding else {runs[0][0]}
    return any(by_n[dn]["k_fit"] == data["kmax"]
               for prefix, data, by_n in runs
               if prefix in prefixes and dn in by_n)


def pct(v):
    return None if v is None else round(v, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()

    design, all_n, skipped = [], [], []
    for oem, pcfg, bands, src, prefixes in ROWS:
        runs = []
        for p in prefixes:
            d = load_run(a.dir, p)
            if d is not None:
                runs.append((p, d, {r["N"]: r for r in d.get("rows", [])}))
        if len(runs) != len(prefixes):
            skipped.append(oem)
            continue
        ns = sorted(set().union(*[set(by_n) for _, _, by_n in runs]))
        dn = walk_design(runs)
        comb = combined_at(runs, dn) if dn is not None else None
        cap = binding_cap(runs, dn, comb["binding"]) if comb else False
        design.append({"oem": oem, "design_N": dn,
                       "thr_dbm": runs[0][1].get("thr"),
                       "k_floors_per_port": comb["k"] if comb else None,
                       "k_cap": cap,
                       "binding": comb["binding"] if comb else "",
                       "floors_per_remote": comb["floors_per_remote"] if comb else None,
                       "remotes_bldg": comb["remotes"] if comb else None,
                       "cov_confirmed_pct": pct(comb["cov_confirmed"]) if comb else None,
                       "cov_next_pct": pct(comb["cov_next"]) if comb else None,
                       "ports": comb["ports"] if comb else None,
                       "port_config": pcfg, "bands_in_run": bands, "source_label": src})
        for n in ns:
            c = combined_at(runs, n)
            if c is None:
                continue
            all_n.append({"oem": oem, "N": n, "k": c["k"], "k_cap": c["k_cap"],
                          "binding": c["binding"], "floors_per_remote": c["floors_per_remote"],
                          "remotes_bldg": c["remotes"],
                          "cov_confirmed_pct": pct(c["cov_confirmed"])})

    cols = ["oem", "N", "k", "floors/remote", "remotes(45fl)", "cov_conf %",
            "cov_next %", "port_config", "band powers", "source"]

    def cell(v):
        return "" if v is None else str(v)

    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for d in design:
        cells = [d["oem"], cell(d["design_N"]), cell(d["k_floors_per_port"]),
                 cell(d["floors_per_remote"]), cell(d["remotes_bldg"]),
                 "" if d["cov_confirmed_pct"] is None else "%.1f" % d["cov_confirmed_pct"],
                 "" if d["cov_next_pct"] is None else "%.1f" % d["cov_next_pct"],
                 d["port_config"], d["bands_in_run"], d["source_label"]]
        print("| " + " | ".join(cells) + " |")

    if skipped:
        print("skipped: " + ", ".join(skipped), file=sys.stderr)
    result = {"floors": FLOORS, "design": design, "all_n": all_n, "skipped": skipped}
    if a.out:
        with open(a.out, "w") as f:
            json.dump(result, f, indent=1)


if __name__ == "__main__":
    main()
