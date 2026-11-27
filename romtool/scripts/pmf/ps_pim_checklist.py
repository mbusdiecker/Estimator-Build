"""PIM test gate checklist for converged cellular + public safety plates.
Field procedure generator (owner build request 2026-11-26). It folds the
IM3 screen results into the field procedure the crew runs at and after
install. Grade E planning artifact; the acceptance values are project inputs
(the project spec overrides the IEC 62037 practice defaults noted below).

Usage: uv run --no-project python -I ps_pim_checklist.py \
        --tx-bands B12_700,B5_850,... [--imd-results ps_imd_results.json] \
        [--out sugarhouse-26-01038/ps_pim_checklist.json]
"""
import argparse
import json
import sys

def main():
    a = argparse.ArgumentParser()
    a.add_argument("--tx-bands", default="B12_700,B5_850,B2_1900,B66_2100,N71_600,N41_2500,N77_CBAND")
    a.add_argument("--imd-results", default=None,
                   help="ps_imd_results.json with the two-tone IM3 screen")
    a.add_argument("--pim-test-level-dbm", type=float, default=43.0,
                   help="PIM test tone level, IEC 62037 field practice 2x43 dBm")
    a.add_argument("--pim-pass-dbm", type=float, default=-100.0,
                   help="PIM acceptance limit; project spec overrides the "
                        "practice values (industry runs -97 to -110 by band)")
    a.add_argument("--ps-band", default="700/800/VHF/UHF as ordered",
                   help="the PS bands the order carries")
    a.add_argument("--out", default=None)
    x = a.parse_args()

    hits = []
    if x.imd_results:
        try:
            hits = json.load(open(x.imd_results)).get("hits", [])
        except (FileNotFoundError, json.JSONDecodeError):
            hits = []
    checklist = [
        {"step": 1, "when": "order + pre-install",
         "item": "Run the two-tone IM3 screen and attach the result to the "
                 "order." if not hits else
                 "CONVERGED PLATE: the IM3 screen found %d products landing in "
                 "the PS passbands (%s). The channel-specific PS filters MUST "
                 "be order lines (owner direction 2026-11-26)."
                 % (len(hits), "; ".join("%s=%.1f MHz -> %s"
                                         % (h["label"], h["product_mhz"],
                                            h["lands_in"]) for h in hits)),
         "source": "ps_imd.py run"},
        {"step": 2, "when": "pre-activation on the plate",
         "item": "PIM test every shared trunk and tap plate before the "
                 "cellular carrier activation with 2x%.0f dBm tones; pass is "
                 "at or below %.0f dBm IM3 unless the project spec says "
                 "otherwise (IEC 62037 field practice: values are band "
                 " dependent)." % (x.pim_test_level_dbm, x.pim_pass_dbm),
         "source": "AHJ trigger: intermod studies required when other systems "
                   "share the building (DC OUC 2.2.3); PIM testing may be "
                   "required on the results"},
        {"step": 3, "when": "donor and head end",
         "item": "PIM test the donor antenna, its jumpers and connectors, and "
                 "the BDA/head-end ports; donor-side PIM is the loudest path "
                 "into the PS receive window."},
        {"step": 4, "when": "hunt procedure",
         "item": "On a failing line: half-split the trunk (the divide-and-"
                 "conquer tap isolation), test each half, and repeat to the "
                 "section; standard PIM hunting practice."},
        {"step": 5, "when": "hardware and workmanship",
         "item": "PIM-rated jumpers and connectors only on shared lines; "
                 "torque to the connector spec, no dissimilar metals, no "
                 "ferrous hardware near the 700/800 lines; re-torque after "
                 "any flex. The residential-ceiling congestion and PIM note "
                 "in the MCF design spec applies."},
        {"step": 6, "when": "after cellular activation",
         "item": "Re-run the ERCES coverage check (grid cells) for %s with "
                 "the cellular carriers active; verify the PS SINR does not "
                 "collapse at the instrument and record the before/after."
                 % x.ps_band,
         "source": "the converged-plate risk: cellular activation can raise "
                   "the noise and PIM into the PS receive path"},
        {"step": 7, "when": "acceptance",
         "item": "Record tone level, IM3 limit, per-line results, and the "
                 "corrective actions in the acceptance package; re-test fails "
                 "after the fixes and before the AHJ witness."},
    ]
    doc = {"basis": "PIM test gate checklist for converged cellular + ERCES "
                    "plates (grade E planning artifact): IM3 screen hits plus "
                    "the field procedure. PIM limits are project inputs; the "
                    "practice defaults cited come from IEC 62037-style field "
                    "testing.",
           "tx_bands": x.tx_bands, "im3_hits": hits,
           "pim_test_level_dbm": x.pim_test_level_dbm,
           "pim_pass_dbm": x.pim_pass_dbm,
           "ps_band": x.ps_band,
           "checklist": checklist}
    if x.out:
        json.dump(doc, open(x.out, "w"), indent=1)
    print("PIM checklist: %d steps, %d IM3 hits carried" % (len(checklist), len(hits)))
    for c in checklist[:2]:
        print("  %d. %s" % (c["step"], c["item"][:110]))

if __name__ == "__main__":
    main()
