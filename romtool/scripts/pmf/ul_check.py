"""ul_check.py: end-to-end uplink verification on top of the stack results.

The user (UE) stands at the far point of the design, at the edge of acceptable
power (the RSRP target the design was built to, -103 dBm by default here, -95
as the alternative). By reciprocity the UE's uplink back to the unit input pays
the same one-way loss the downlink paid, so:

    UL input at the unit = UE_Tx - L_far (dBm)
    L_far = port_design - margin - RSRP_edge ... (geometrically locked: the
    -95 vs -103 choice moves the design port by +8 dB and leaves L_far, and
    therefore this UL input, unchanged)

Then: unit UL gain/limits (datasheet), uplink SNR at the unit input against the
input-referred noise floor at several uplink bandwidths, the uplink output caps
(OEM spec values and the 47 CFR 20.21(e)(8)(i)(D) 1 W composite cap for
consumer boosters), the donor side (feeder, donor antenna gain) and the tower
path as an allowed-path-loss line the survey must fill (no invented defaults).

Config estimates (grade E). No prices, no customer or job names.
"""
import argparse, json, math, os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
from romtool.config import load_config

UE_TX_DBM = 23.0          # LTE power class 3 handset (assumption, survey replaces)
EDGE_DESIGN_DBM = -103.0  # the RSRP edge the pmf designs were solved to
EDGE_ALT_DBM = -95.0      # the alternative edge (adds 8 dB to the DL design)
TOWER_SENS_10MHZ_DBM = -101.5  # 3GPP TS 36.104 wide-area eNB reference sensitivity,
                               # QPSK at 10 MHz; scaled to each allocation by the noise
                               # floor: sens(BW) = -101.5 + 10log10(BW/10 MHz), so
                               # 5 MHz -104.0 dBm and 1.4 MHz -110.0 dBm. (3GPP's own
                               # 1.4 MHz row is -106.8 dBm, 3.2 dB more conservative
                               # than the pure noise-floor scaling.)
BW_MHZ = (10.0, 5.0, 1.4) # uplink allocation widths to test
KTB = -174.0              # thermal noise dBm/Hz
DONOR_GAIN_DB = {"B12_700": 7.0, "B5_850": 8.0, "B2_1900": 9.0, "B66_2100": 9.0}  # SC-530W
OPS = 3.0                 # operators sharing one port's composite (design rule)
OP_SPLIT_DB = 10.0 * math.log10(OPS)  # 4.77 dB: the DL edge is set by ONE operator's share
                          # of the composite (cascade.remote_per_operator_split), while the
                          # far UE's uplink is single-carrier and pays no split, so the
                          # uplink input improves by this amount over a composite-to-edge read

# verified uplink specs per OEM (datasheets checked directly 2026-11-25);
# None = not published, carried as an open item rather than a guess
UL_SPECS = {
    "comba_mruc": {"ul_gain": 35.0, "ul_out_per_band": None, "ul_out_cap": 19.0, "system": True,
                   "mid_cap_dbm": -15.0, "host_gain": 80.0, "eirp_cap": True,
                   "nf": 6.0, "src": "ComFlex NG system chain (MRUc DS 1-0-2 + Consumer Signal Booster DS 0-0-5, 11-25): MRUc UL gain 35 dB with UL output -15 dBm (mid-stage cap), then the MU BDA card UL gain 80 dB with UL output +19 dBm per band (MU-BDA20-2B sheets); NF 6 dB; Part 20 consumer class, so the 1 W (30 dBm EIRP incl. donor antenna) composite ceiling applies" + " - the model previously omitted the whole MU/BDA stage, which is why the row looked absurd"},
    "q4000c": {"eirp_cap": True, "ul_gain": 100.0, "ul_out_per_band": {"B2_1900": 22.0, "B66_2100": 22.0,
                                                    "B12_700": 22.0, "B5_850": 20.0},
               "ul_out_cap": 30.0, "system": True, "nf": 7.0,
               "src": "Nextivity QUATRA 4000c Bands table (image, 11-25): Uplink Power per band 22 dBm (B2, B4, B12, B13, B25, B48, B71), 20 dBm (B5), 19 dBm (B30, industrial only); carriers 10-20 MHz; NF 7 dB; System Gain 100 dB max. Per-band values are loaded composite ceilings; far-user line is gain-limited (a wideband AGC'd booster, not a per-UE regenerator toward the donor)"},
    "signalmax": {"eirp_cap": True, "ul_gain": 72.0, "ul_out_per_band": {"B12_700": 23.49, "B5_850": 23.31,
                                                       "B2_1900": 20.89, "B66_2100": 21.41},
                  "ul_out_cap": 30.0, "system": True, "nf": 8.0,
                  "src": "SignalMax_Fiber_DAS_User_Guide (verified 11-25): Noise Figure <8 dB (8.0 used), Maximum Uplink Power 30 dBm EIRP, Maximum Gain 64 dB <1 GHz / 72 dB >1 GHz (72 used, high-band binding); per-band UL values from SignalMax_Fiber_DAS_Data_Sheet are loaded ceilings; far-user line is gain-limited"},
    "adxv_mpr": {"adrf_ant": True, "ul_gain": 10.0, "ul_out_per_band": None, "ul_out_cap": None, "system": True, "sdrc": True,
                 "nf": 5.0, "src": "ADX V Series Gain table: UL -20..+10 dB END-TO-END (head end included), NF 5 dB at max gain; plus SDRC donor front end (owner-provided 11-25: UL gain 57/62/70/71 dB for 700/850/PCS/AWS, matching its DL max gains, composite UL out +22 dBm)"},
    "adxv_lpr": {"adrf_ant": True, "ul_gain": 10.0, "ul_out_per_band": None, "ul_out_cap": None, "system": True, "sdrc": True,
                 "nf": 5.0, "src": "ADX V Series Gain table: UL -20..+10 dB END-TO-END (head end included), NF 5 dB at max gain; plus SDRC donor front end (owner-provided 11-25: UL gain 57/62/70/71 dB for 700/850/PCS/AWS, matching its DL max gains, composite UL out +22 dBm)"},
    "adxv_hpr": {"adrf_ant": True, "ul_gain": 10.0, "ul_out_per_band": None, "ul_out_cap": None, "system": True, "sdrc": True,
                 "nf": 5.0, "src": "ADX V Series Gain table: UL -20..+10 dB END-TO-END (head end included), NF 5 dB at max gain; plus SDRC donor front end (owner-provided 11-25: UL gain 57/62/70/71 dB for 700/850/PCS/AWS, matching its DL max gains, composite UL out +22 dBm)"},
    "5xmax": {"eirp_cap": True, "ul_gain": 72.0, "ul_out_per_band": None, "ul_out_cap": 26.0, "system": True,
              "nf": 8.0, "src": "surecall.com 5X Max spec sheet (sheet-verified 11-25): Maximum Gain 72 dB, Max Uplink Power 26.0 dBm, Noise Figure <8 dB (8.0 used), Maximum Output Power 1 W EIRP; NF conservatively taken at the spec edge"},
    "jma_t19awe": {"adrf_ant": True, "ul_gain": 47.0, "ul_out_per_band": None, "ul_out_cap": 26.0, "system": True, "host_gain": 72.0,
                   "nf": 15.0, "src": "JMA TEKO TRU/TRL datasheets (TRU35T35TWM, TRL7S8SC8A19AW23): MU UL RF gain out 47 dB, remote UL NF 14-15 dB per band; max UL output at the POI assumed +10 dBm (owner assumption 11-25; never binds for a single far user); host path = 5X Max (72 dB gain and 26.0 dBm max uplink power, surecall.com spec sheet 11-25); cap shown is the host's; Optical uplink output power 6 dBm is the 1550 nm LIGHT level (optical block: nominal input +6 down to -4 dBm, 10 dB optical link budget with AGC holding constant RF gain over the span) - a fiber-link spec, not an RF UL cap; T19AWE power still unpublished"},
}
UL_SPECS["surecall_5xmax"] = UL_SPECS["5xmax"]  # run label alias
UL_SPECS["sdrc_only"] = {"adrf_ant": True, "ul_gain": 0.0, "ul_out_per_band": None, "ul_out_cap": 22.0, "system": True, "sdrc": True,
                 "nf": None, "src": "SDRC_Datasheet_v3_SS.pdf (v0.2, 09/23/2024, sheet-verified 11-25): Maximum Gain (DL/UL) per band 57 dB (700 B12/13), 62 dB (Cellular 850 B5), 70 dB (PCS B25), 71 dB (AWS-1 B4); Composite UL Output Power 22 dBm max; Noise Figure 11 dB at 700/850 and 6 dB at PCS/AWS-1 at max gain; Part 20 certified (22 dBm within the 30 dBm consumer composite cap)"}
FCC_CONSUMER_COMPOSITE_DBM = 30.0  # 47 CFR 20.21(e)(8)(i)(D): 1 W composite uplink
CONSUMER_EIRP_CAP_DBM = 30.0       # Part 20 consumer class: 1 W composite EIRP
                                   # INCLUDING the donor antenna; applies only to
                                   # rows flagged eirp_cap, never to industrial chain
# SDRC donor front end (SDRC_Datasheet_v3_SS.pdf v0.2, sheet-verified 11-25):
# max UL gain matches its DL max gains per band; composite UL output +22 dBm;
# noise figure 11 dB at 700/850 and 6 dB at PCS/AWS-1. Sits on the donor side of
# the ADX V system, so ADX V rows add: far-UE input + end-to-end gain + SDRC gain,
# with the SDRC noise figure composed through Friis after the unit's own stage.
SDRC_UL_GAIN_DB = {"B12_700": 57.0, "B5_850": 62.0, "B2_1900": 70.0, "B66_2100": 71.0}
SDRC_UL_OUT_CAP_DBM = 22.0
SDRC_NF_DB = {"B12_700": 11.0, "B5_850": 11.0, "B2_1900": 6.0, "B66_2100": 6.0}
# AD-PA donor antennas (owner direction 11-25: SDRC, ADX V, Comba and JMA rows use
# ADRF donor antennas instead of the SC-530W). Sheet-verified:
# AD-PA-617-960-D (low-band high-isolation): 15.1 dBi (617-698), 15.5 dBi (698-806,
# our B12/B700 uplink segment), 16.8 dBi (806-894); AD-PA-1900-2600-DIN-X
# (PCS/AWS/WCS/BRS panel): 19.1 dBi (1710-2170, our B2/B66 uplink segments).
AD_PA_GAIN_DB = {"B12_700": 15.5, "B5_850": 16.8, "B2_1900": 19.1, "B66_2100": 19.1}
# UL center frequencies (MHz) per band key, for the free-space max-distance line
UL_CENTER_MHZ = {"B12_700": 707.0, "B5_850": 836.5, "B2_1900": 1882.5, "B66_2100": 1732.5}
# radio-horizon cap (owner challenge 11-25: FSPL-only distances of hundreds of km
# are impossible; beyond LOS the Earth blocks the path). Assumed heights, survey
# replaces: donor yagi 25 m AGL on the building roof, macro tower 50 m AGL.
DONOR_H_M = 25.0
TOWER_H_M = 50.0
HORIZON_KM = 3.57 * (math.sqrt(DONOR_H_M) + math.sqrt(TOWER_H_M))  # ~43 km at K=4/3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out")
    ap.add_argument("--edge", type=float, default=EDGE_DESIGN_DBM)
    a = ap.parse_args()

    cfg = load_config()
    cables = cfg["cables"]
    conn = float(((cfg.get("branch_hardware") or {}).get("connector") or {}).get("loss_db", 0.1))
    # donor feeder (owner question 11-25: the old 2 m LMR-400 was a jumper
    # assumption with no roof run): 30 m of 1/2 inch corrugated from the 8F head
    # end to the roof donor antenna + 2 mated pairs, at each band's loss
    # (assumption, survey replaces the length)
    DONOR_LINE_M = 30.0
    BAND_FREQ = {"B12_700": "0.7", "B5_850": "0.9", "B2_1900": "2.0", "B66_2100": "2.0"}
    feeder_db_band = {
        k: DONOR_LINE_M * float(cables["coax_1_2in"]["loss_db_per_100m_ghz"][g]) / 100.0 + 2.0 * conn
        for k, g in BAND_FREQ.items()}

    with open(os.path.join(a.dir, "stack_results.json")) as f:
        stack = json.load(f)

    # each OEM uses its own design edge from its runs (ADX V mid/high and JMA at
    # -95 dBm RSRP at 98%, the rest at -103); the far-point loss is port - edge
    # (geometrically locked: raising the edge only moves the DL design port)
    rows, results = [], []
    for s in stack["stack"]:
        oem = s["oem"]
        spec = UL_SPECS.get(oem, {})
        bind = next(g for g in s["groups"] if g["prefix"] == s["binding_group"])
        edge = s.get("design_thr_dbm")
        if edge is None:
            edge = EDGE_DESIGN_DBM
        port = bind["port_dbm"]
        margin = s["worst_margin_db_at_k_stack"] or 0.0
        # far-point loss: the DL edge is delivered by ONE operator's share of the
        # composite (port - OP_SPLIT_DB); the UE's single-carrier uplink pays the
        # same physical loss back with no split, so l_far = port - op split - edge
        l_far = (port - OP_SPLIT_DB) - edge  # remote port -> the UE on the design edge contour
        ul_in = UE_TX_DBM - l_far
        band_key = {
            "adxv_low33": "B12_700", "adxv_high37": "B2_1900", "adxv_lpr_lo": "B12_700",
            "adxv_lpr_hi": "B2_1900", "adxv_hpr_lo": "B12_700", "adxv_hpr_hi": "B2_1900",
            "jma_t19awe": "B2_1900"}.get(bind["prefix"]) or "B66_2100"
        dant = (AD_PA_GAIN_DB.get(band_key, 9.0) if spec.get("adrf_ant")
                else DONOR_GAIN_DB.get(band_key, 9.0))
        feeder_db = feeder_db_band.get(band_key, 0.0)
        nf = spec.get("nf")
        ul_gain_pre = spec.get("ul_gain")
        eff_nf = nf
        if spec.get("sdrc") and band_key in SDRC_NF_DB:
            if nf is None:
                eff_nf = SDRC_NF_DB[band_key]  # the SDRC is the first active stage
            else:  # Friis: the SDRC acts on the signal after the unit's own stage(s)
                eff_nf = 10.0 * math.log10(
                    10 ** (nf / 10.0) +
                    (10 ** (SDRC_NF_DB[band_key] / 10.0) - 1.0) /
                    (10 ** ((ul_gain_pre or 0.0) / 10.0)))
        floors = {}
        for bw in BW_MHZ:
            if eff_nf is None:
                floors[bw] = None
            else:
                fl = KTB + 10.0 * math.log10(bw * 1e6) + eff_nf
                floors[bw] = round(ul_in - fl, 2)  # UL SNR at the unit input
        out_cap = spec.get("ul_out_cap")
        out_band = spec.get("ul_out_per_band")
        ul_gain = spec.get("ul_gain")
        system = spec.get("system", False)
        band_cap = None
        if out_band and ul_gain is not None:
            # far user's own payload: gain-limited, capped by the binding band's
            # published output (a loaded ceiling, not a per-UE service level)
            band_cap = out_band.get(band_key, FCC_CONSUMER_COMPOSITE_DBM)
            gained = ul_in + ul_gain + spec.get("host_gain", 0.0)
            out_value = min(gained, band_cap)
            ul_out_kind = "far user out"
        elif out_band:
            out_value = min(out_band.values())
            ul_out_kind = "per band"
        elif ul_gain is not None and spec.get("sdrc"):
            out_value = min(ul_in + ul_gain + SDRC_UL_GAIN_DB.get(band_key, 0.0), SDRC_UL_OUT_CAP_DBM)
            ul_out_kind = "system out (SDRC)"
        elif ul_gain is not None:
            # analog or host-chained units: the far user's own uplink payload,
            # gain-limited through every stage, bounded by each stage's cap and
            # finally by the published composite cap (or the FCC 1 W)
            after_first = ul_in + ul_gain
            mid_cap = spec.get("mid_cap_dbm")
            if mid_cap is not None:
                after_first = min(after_first, mid_cap)
            gained = after_first + spec.get("host_gain", 0.0)
            cap = out_cap if out_cap is not None else FCC_CONSUMER_COMPOSITE_DBM
            out_value = min(gained, cap)
            ul_out_kind = "far user out"
        elif out_cap is not None:
            out_value = out_cap  # service line at the published uplink output
            ul_out_kind = "capped"
        else:
            out_value = None
            ul_out_kind = "open"
        banded = {b: min(v, FCC_CONSUMER_COMPOSITE_DBM) for b, v in out_band.items()} if out_band else None
        # tower line: the UE's signal must arrive at or above the tower's
        # reference sensitivity; per-allocation lines follow. Part 20 consumer
        # rows are EIRP-capped at 1 W INCLUDING the donor antenna.
        ul_ant = None
        if out_value is not None and system:
            ul_ant = out_value + dant - feeder_db
            if spec.get("eirp_cap"):
                ul_ant = min(ul_ant, CONSUMER_EIRP_CAP_DBM)
        # per-allocation tower lines: 3GPP eNB reference sensitivity scaled by the
        # noise floor to each width (owner direction 11-25); distance from
        # free-space path loss out of the donor antenna at the band's UL center
        tower_pl = {}
        tower_dist = {}
        if ul_ant is not None:
            f_ul = UL_CENTER_MHZ.get(band_key, 1882.5)
            for bw in BW_MHZ:
                sens_w = TOWER_SENS_10MHZ_DBM + 10.0 * math.log10(bw / 10.0)
                pl_w = ul_ant - sens_w
                d_w = 10.0 ** ((pl_w - 32.45 - 20.0 * math.log10(f_ul)) / 20.0)
                tower_pl[bw] = round(pl_w, 1)
                tower_dist[bw] = round(d_w, 3)
        opens = []
        if out_cap is None and not out_band and not ul_gain:
            opens.append("no UL output cap on file; FCC composite cap not checkable here")
        if eff_nf is None:
            opens.append("uplink noise figure not published (SNR not checkable)")
        opens.append("tower path loss is a survey input; sensitivity is the 3GPP eNB reference (-101.5 dBm at 10 MHz) scaled by allocation width")
        row = {"oem": oem, "binding_group": s["binding_group"], "port_design_dbm": port,
               "far_point_loss_db": round(l_far, 2), "ul_input_dbm": round(ul_in, 2),
               "edge_dbm": edge, "op_split_db": round(OP_SPLIT_DB, 2),
               "snr_db": floors, "ul_out_cap_dbm": out_cap, "ul_out_per_band": banded,
               "donor_feeder_db": round(feeder_db, 2), "donor_ant_gain_db": dant,
               "ul_at_donor_ant_dbm": None if ul_ant is None else round(ul_ant, 2),
               "tower_pl_per_bw_db": None if ul_ant is None else {str(k): v for k, v in tower_pl.items()},
               "fspl_max_km_per_bw": None if ul_ant is None else {str(k): v for k, v in tower_dist.items()},
               "nf_db": None if eff_nf is None else round(eff_nf, 2), "source": spec.get("src"), "open_items": opens}
        results.append(row)

        snr_s = " ".join("%.1f" % floors[b] if floors[b] is not None else "open" for b in BW_MHZ)
        if out_value is not None and ul_out_kind == "far user out":
            ul_out_s = "far-user out %.1f dBm (gain %.0f, band cap %.2f)" % (
                out_value, ul_gain, band_cap if band_cap is not None else (out_cap if out_cap is not None else FCC_CONSUMER_COMPOSITE_DBM))
        elif banded and ul_out_kind == "per band":
            ul_out_s = "per band, min %.2f dBm" % min(banded.values())
        else:
            ul_out_s = ("system out %.1f dBm (SDRC included, cap +22 composite)" % out_value) if (out_value is not None and ul_out_kind == "system out (SDRC)") else (
                ("far-user out %.1f dBm (gain %.0f, composite cap %.0f)" % (out_value, ul_gain, out_cap if out_cap is not None else FCC_CONSUMER_COMPOSITE_DBM)) if (out_value is not None and ul_out_kind == "far user out") else (
                    ("cap %.2f dBm" % out_value) if (out_value is not None and ul_out_kind == "capped") else "open"))
        def dist_s(v_km):
            if v_km >= 1.0:
                return "%.1f km" % v_km
            return "%.0f m" % (v_km * 1000.0)
        if ul_ant is None:
            tower_s = "PL tower open (survey)"
        else:
            pl_s = "/".join("%.0f" % tower_pl[b] for b in BW_MHZ)
            d_s = "/".join(dist_s(tower_dist[b]) for b in BW_MHZ)
            tower_s = "PL tower 10/5/1.4 MHz <= %s dB; FSPL dmax %s (3GPP sens)" % (pl_s, d_s)
        rows.append("| %s | %.2f | %.2f | %.1f | %s | %s | %.2f | %s |" % (
            oem, port, l_far, ul_in, snr_s, ul_out_s, dant, tower_s))

    print("Up-link verification, UE at the far point on each OEM's design edge, UE Tx %.0f dBm" % UE_TX_DBM)
    print("Design edges from the runs: ADX V mid/high and JMA at -95 dBm RSRP at 98%%, the rest at -103 dBm RSRP at 98%%.")
    print()
    print("| OEM | DL port dBm | far-point loss dB | UL in at unit dBm | SNR at 10/5/1.4 MHz dB | UL out | donor ant dBi | tower line |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(r)
    print()
    print("Open items per OEM:")
    for r in results:
        print("  %s: %s" % (r["oem"], "; ".join(r["open_items"])))
    print()
    print("FCC notes: consumer composite uplink cap 1 W (30 dBm) per 47 CFR 20.21(e)(8)(i)(D);")
    print("uplink noise and gain rules per 20.21(e)(9) need the survey's tower path loss;")
    print("industrial boosters run under 20.21(c) licensee consent.")
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"ue_tx_dbm": UE_TX_DBM, "edge_dbm": a.edge, "tower_sens_dbm": TOWER_SENS_DBM,
                       "bw_mhz": list(BW_MHZ), "results": results}, f, indent=1)


if __name__ == "__main__":
    main()
