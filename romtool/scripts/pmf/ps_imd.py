"""Two-tone third-order intermodulation screen for converged cellular + PS
plates. Simplified (grade E): each band's center frequency stands for its
carriers; the real PIM analysis uses the actual TX carrier list and the
field PIM test decides. No prices, no customer or job names."

Products checked: 2f1 - f2 and 2f2 - f1 for every cellular TX carrier pair
(the DL the remotes radiate). A product landing inside a public-safety
passband is flagged: those are the frequencies AHJs cite for PIM testing
when cellular and ERCES share antennas, coax, or tap plates.

PS windows (US plan): PS700 DL 769-775, PS800 DL 851-861, B14 DL 758-768+775-785
UL 788-798+806-816+799-805 etc. Windows are CLI-configurable.
"""
import argparse
import itertools
import json
import sys

sys.path.insert(0, "..")

DEFAULT_PS_WINDOWS_MHZ = [
    ("PS700-DL", 769.0, 775.0), ("PS700-UL", 799.0, 805.0),
    ("PS800-DL", 851.0, 861.0), ("PS800-UL", 806.0, 816.0),
    ("B14-DL", 758.0, 775.0), ("B14-UL", 788.0, 805.0),
    ("VHF", 136.0, 174.0), ("UHF", 380.0, 512.0),
]

def main():
    a = argparse.ArgumentParser()
    a.add_argument("--tx-bands", required=True,
                   help="comma list of cellular band ids whose DL centers are "
                        "the TX carriers (read from the romtool config)")
    a.add_argument("--ps-windows", default=None,
                   help="optional comma list name:lo-hi overriding the default")
    a.add_argument("--out")
    args = a.parse_args()

    from romtool.config import load_config
    cfg = load_config()
    bands = {b["id"]: float(b["freq_mhz"]) for b in cfg["bands"]}
    tx = []
    for bid in args.tx_bands.split(","):
        if bid not in bands:
            raise SystemExit("unknown band id '%s'" % bid)
        tx.append((bid, bands[bid]))

    windows = []
    if args.ps_windows:
        for w in args.ps_windows.split(","):
            name, rng = w.split(":")
            lo, hi = rng.split("-")
            windows.append((name.strip(), float(lo), float(hi)))
    else:
        windows = DEFAULT_PS_WINDOWS_MHZ

    hits = []
    for (b1, f1), (b2, f2) in itertools.combinations(tx, 2):
        if f1 == f2:
            continue
        for prod, lbl in ((2 * f1 - f2, "2x%s-%s" % (b1, b2)),
                          (2 * f2 - f1, "2x%s-%s" % (b2, b1))):
            for wname, lo, hi in windows:
                if lo <= prod <= hi:
                    hits.append({"product_mhz": round(prod, 3), "label": lbl,
                                 "band_pair": sorted([b1, b2]),
                                 "lands_in": wname})
    doc = {"basis": "two-tone IM3 screen (grade E): band center frequencies "
                    "stand for the carriers; verify the actual TX list and run "
                    "the field PIM test where the converged plates carry both "
                    "cellular and ERCES. The DC OUC rule requires intermod "
                    "studies when other radio systems share the building.",
           "tx_carriers_mhz": {b: round(f, 3) for b, f in tx},
           "ps_windows_mhz": [{"name": n, "lo": lo, "hi": hi} for n, lo, hi in windows],
           "hits": hits,
           "verdict": ("PIM test trigger: %d IM3 products land in the PS "
                       "windows" % len(hits)) if hits else
                      "no 2-tone IM3 product lands in the PS windows from the "
                      "band centers; still run the field PIM test where the "
                      "plates are shared"}
    if args.out:
        json.dump(doc, open(args.out, "w"), indent=1)
    print(doc["verdict"])
    for h in hits:
        print("  %s = %.2f MHz -> %s" % (h["label"], h["product_mhz"], h["lands_in"]))

if __name__ == "__main__":
    main()
