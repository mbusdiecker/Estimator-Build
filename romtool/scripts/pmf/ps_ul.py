"""PS uplink engine: talk-in link budget, noise coordination, squelch design,
and near-test margins per OEM per band. Config estimates (grade E). No prices,
no customer or job names.

Chain math (band-reciprocal, chain-loss-independent at fixed geometry):
  level at the remote/BDA PORT from a portable at point p
      ul_port = eirp_ue - port_dl + delivered_dl(p)
  where delivered_dl(p) is the predicted downlink at p and port_dl is the
  remote/BDA port power the DL model ran with. The distribution net uplink
  gain (fiber remotes) is a parameter, default 0 dB (conservative).

Arrival at the host site (reciprocal of the ambient cal):
      site_in  = ul_at_bda + ul_gain + g_donor + ambient - site_erp
  so the gain solve never needs the outdoor path loss separately.

Noise at the host site (BDA active, worst case):
      site_noise = kTB(bw) + nf + ul_gain + g_donor + ambient - site_erp
  With uplink squelch engaged the idle path is dormant, so the active-state
  figure above is the coordination case; the quiescent limits (-150 dBm LA
  rule, -43/-70 dBm ERP) are reported as plates.

Gain solve constraints (all must hold):
  c1  far-user site arrival >= site noise floor + arrival_margin_db (DC: +18)
  c2  site noise <= the rule limit (LA: -150 dBm at site, else DC: floor-15)
  c3  near-user input at BDA <= max_input_spec - 5 and <= damaging - 10 (DC)
  c4  ul_gain <= vendor ul_gain_max
Squelch threshold = distribution input noise at the BDA + 5 dB (DC).
"""
import argparse
import json
import math
import sys

KTB_12K5 = -174.0 + 10.0 * math.log10(12500.0)  # -133.03 dBm
# vendor uplink specs (from data sheets on disk; placeholders flagged)
OEM_HW = {
    "adrf_fire": {"label": "ADRF FiRe-78-8-U1 (ADXV-R-3378P-U2 remotes)",
                  "ul_gain_max": 90.0, "ul_nf": 4.5, "ul_out_cap_dbm": -21.0,
                  "max_input_dbm": -10.0, "damaging_dbm": 0.0,
                  "spec_note": "UL NF 4.5 dB at max gain from the FiRe-78 SS; "
                               "UL composite output cap -21 dBm class from the "
                               "SS composite table; max/damaging input not "
                               "published: placeholders flagged grade D"},
    "westell": {"label": "Westell CS45-734-834-A0",
                "ul_gain_max": 80.0, "ul_nf": 7.0, "ul_out_cap_dbm": 27.0,
                "max_input_dbm": -10.0, "damaging_dbm": 0.0,
                "spec_note": "80 dB UL gain, 0.5 W UL output, NF under 7 dB "
                             "(CS40 figure) per the CS45 DS; max/damaging "
                             "input placeholders flagged grade D"},
    "comba": {"label": "Comba ComFlex NG V3 PS BDA",
              "ul_gain_max": 90.0, "ul_nf": 5.0, "ul_out_cap_dbm": 27.0,
              "max_input_dbm": -10.0, "damaging_dbm": 0.0,
              "spec_note": "UL NF under 5 dB at 90 dB gain per the V3 manual; "
                           "UL output cap assumed the common 0.5 W class "
                           "(grade D); max/damaging input placeholders"},
    "adrf_vu": {"label": "ADRF PSR-VU-9537-X",
                "ul_gain_max": 95.0, "ul_nf": 3.5, "ul_out_cap_dbm": 0.0,
                "max_input_dbm": -10.0, "damaging_dbm": 0.0,
                "spec_note": "gain 85-95 dB range (top used), NF under 3.5 dB "
                             "without duplexing per the PSR-VU DS; UL output "
                             "cap and input limits placeholders grade D"},
}
# FSPL in dB at 1.5 m (the near ring) per band frequency
def fspl_150(f_mhz):
    return 32.45 + 20.0 * math.log10(f_mhz) + 20.0 * math.log10(0.0015)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", required=True)
    ap.add_argument("--bands", default="PS700,PS800")
    ap.add_argument("--port", type=float, default=33.0, help="DL port dBm the DL model ran")
    ap.add_argument("--oem", default="adrf_fire", choices=sorted(OEM_HW))
    ap.add_argument("--k", type=int, default=48, help="design floors per port (loss basis)")
    ap.add_argument("--nfloors", type=int, default=45)
    # AHJ / host site inputs (overridable; documented assumptions)
    ap.add_argument("--site-erp", type=float, default=50.0, help="host site ERP dBm (AHJ input)")
    ap.add_argument("--site-nf", type=float, default=6.0, help="host site receiver NF dB (AHJ input)")
    ap.add_argument("--ambient", type=float, default=-85.0, help="site DL ambient at the donor dBm (cal basis)")
    ap.add_argument("--ue-tx", type=float, default=34.8, help="portable TX dBm (3 W class)")
    ap.add_argument("--body-loss", type=float, default=5.0, help="hip-height body loss dB")
    ap.add_argument("--arrival-margin", type=float, default=18.0,
                    help="arrival target above the site noise floor (DC: 18 dB)")
    ap.add_argument("--noise-rule", choices=["la", "dc"], default="la",
                    help="site noise coordination rule: la -150 dBm flat, dc floor-15")
    ap.add_argument("--g-donor", type=float, default=15.5, help="donor antenna gain dB")
    ap.add_argument("--das-ul-net", type=float, default=0.0,
                    help="distribution net UL gain dB (fiber remote UL gain; 0 conservative)")
    ap.add_argument("--loss-coeff", type=float, default=None,
                    help="override trunk tap loss dB at k (default: same formula as pmf)")
    ap.add_argument("--out")
    a = ap.parse_args()
    cfg_path = None
    sys.path.insert(0, "..")
    try:
        from romtool.config import load_config
        cfg = load_config()
        fh = float(cfg["autoplace"]["floor_height_m"])
    except Exception:
        fh = 4.0

    # trunk tap loss at the design k: same formula as pmf.loss_db
    def loss_db(k):
        return ((10 * math.log10(k) + 0.5) if k > 1 else 0.0) + (k // 2) * fh * 0.22
    loss = a.loss_coeff if a.loss_coeff is not None else loss_db(a.k)

    hw = OEM_HW[a.oem]
    floor = json.load(open(a.floor))
    try:
        from romtool.config import load_config as _lc
        bands_cfg = {b["id"]: b for b in _lc()["bands"]}
    except Exception:
        bands_cfg = {}
    site_floor = KTB_12K5 + a.site_nf
    target_arrival = site_floor + a.arrival_margin
    noise_limit = -150.0 if a.noise_rule == "la" else site_floor - 15.0
    # far user: deepest plate point delivered = port - loss; ul_port = eirp - port + delivered
    eirp_ue = a.ue_tx - a.body_loss
    rows = []
    for bid in a.bands.split(","):
        fd = a.port - loss
        ul_far_at_bda = eirp_ue - a.port + fd + a.das_ul_net
        # near ring: antenna EIRP at 1.5 m ring, then up the distribution
        f = float((bands_cfg.get(bid) or {}).get("freq_mhz", 772.0))
        eirp_ant = a.port - 0.0  # first antenna; ring level
        ring_dl = eirp_ant - fspl_150(f)
        ul_near_at_bda = eirp_ue - a.port + ring_dl + a.das_ul_net
        # gain constraints
        g_far_min = target_arrival - (ul_far_at_bda + a.g_donor + a.ambient - a.site_erp)
        g_noise_max = noise_limit - (KTB_12K5 + hw["ul_nf"] + a.g_donor + a.ambient - a.site_erp)
        g_rec = max(g_far_min, 0.0)
        if g_rec > g_noise_max:
            raise SystemExit("%s: no feasible UL gain (far needs %.1f dB, noise allows %.1f dB)"
                             % (bid, g_far_min, g_noise_max))
        noise_at_site = KTB_12K5 + hw["ul_nf"] + g_rec + a.g_donor + a.ambient - a.site_erp
        arrival = ul_far_at_bda + g_rec + a.g_donor + a.ambient - a.site_erp
        # squelch threshold: distribution noise estimate at the BDA input (+3 dB NF)
        # plus the DC 5 dB offset; absolute level in dBm
        squelch_in = KTB_12K5 + 3.0 + 5.0
        near_ok = ul_near_at_bda <= hw["max_input_dbm"] - 5.0
        near_dmg = ul_near_at_bda <= hw["damaging_dbm"] - 10.0
        pad_db = max(0.0, round(ul_near_at_bda - (hw["max_input_dbm"] - 5.0), 1))
        rows.append({
            "band": bid, "freq_mhz": f,
            "delivered_far_dl_dbm": round(fd, 2),
            "ul_port_far_dbm": round(ul_far_at_bda, 2),
            "near_ring_dl_dbm": round(ring_dl, 2),
            "near_input_bda_dbm": round(ul_near_at_bda, 2),
            "ul_gain_recommended_db": round(g_rec, 1),
            "ul_gain_far_min_db": round(g_far_min, 1),
            "ul_gain_noise_max_db": round(g_noise_max, 1),
            "site_arrival_far_dbm": round(arrival, 2),
            "site_arrival_target_dbm": round(target_arrival, 2),
            "site_noise_active_dbm": round(noise_at_site, 2),
            "site_noise_limit_dbm": round(noise_limit, 2),
            "noise_margin_db": round(noise_limit - noise_at_site, 2),
            "squelch_threshold_dbm": round(squelch_in, 2),
            "input_pad_recommendation_db": pad_db,
            "near_test_operating_pass": bool(near_ok),
            "near_test_damage_pass": bool(near_dmg),
            "passes": bool(arrival >= target_arrival and noise_at_site <= noise_limit
                           and near_ok and near_dmg)})
    doc = {"basis": "PS uplink engine (grade E): band-reciprocal chain at the "
                    "design k, AHJ site inputs parameterized. Assumptions: 3 W "
                    "portable, 5 dB hip-height body loss, distribution net UL "
                    "gain 0 dB (conservative), site ERP/NF/ambient defaults "
                    "are AHJ placeholders until the site sheet arrives; the "
                    "squelch floor uses a 3 dB distribution noise figure "
                    "estimate (grade D)." + " Vendor notes: " + hw["spec_note"],
           "oem": hw["label"], "site_nf_db": a.site_nf, "site_floor_dbm": round(site_floor, 2),
           "trunk_loss_at_k_db": round(loss, 2), "bands": rows}
    out = a.out or "sugarhouse-26-01038/ps_ul_results.json"
    json.dump(doc, open(out, "w"), indent=1)
    for r in rows:
        print("%-14s arrival %7.2f (target %.1f) noise %7.2f (limit %.1f) "
              "G=%.1f sq=%.1f near=%s%s" % (
                  r["band"], r["site_arrival_far_dbm"], r["site_arrival_target_dbm"],
                  r["site_noise_active_dbm"], r["site_noise_limit_dbm"],
                  r["ul_gain_recommended_db"], r["squelch_threshold_dbm"],
                  "PASS" if r["passes"] else "FAIL",
                  "" if r["passes"] else "  <- see bands for the failing constraint"))

if __name__ == "__main__":
    main()
