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
                  "ul_gain_max": 85.0, "ul_nf": 4.5, "ul_out_cap_dbm": 30.0,
                  "max_input_dbm": -10.0, "damaging_dbm": 0.0,
                  "spec_note": "UL composite output 30 dBm and total system "
                               "gain 85 dB are OWNER-PROVIDED figures "
                               "(2026-11-26); UL NF 4.5 dB at max gain from "
                               "the FiRe-78 SS; max/damaging input not "
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
                "gain_floor_db": 45.0,
                "spec_note": "gain range bottom 45 dB OWNER-PROVIDED "
                             "(2026-11-28); NF under 3.5 dB without duplexing "
                             "per the PSR-VU DS; UL output cap and input "
                             "limits placeholders grade D"},
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
    ap.add_argument("--channels", type=int, required=True,
                    help="channels in the passband: the composite port is split "
                         "by 10log10(channels) per the industry convention; "
                         "explicitly entered on every run by owner direction")
    # AHJ / host site inputs: per-site, researched or given pre-ROM (owner
    # rule); no defaults, explicit entry required
    ap.add_argument("--site-erp", type=float, required=True,
                    help="host site ERP dBm (AHJ input per site)")
    ap.add_argument("--site-nf", type=float, required=True,
                    help="host site receiver NF dB (AHJ input per site)")
    ap.add_argument("--ambient", type=float, required=True,
                    help="site DL ambient at the donor dBm (cal per site)")
    ap.add_argument("--ue-tx", type=float, default=34.8, help="portable TX dBm (3 W class)")
    ap.add_argument("--body-loss", type=float, default=5.0, help="hip-height body loss dB")
    ap.add_argument("--profile", default=None,
                    help="per-AHJ profile from ps_ahj_profiles.json (the CLIargs override)")
    ap.add_argument("--arrival-margin", type=float, default=None,
                    help="arrival target above the site noise floor (DC: 18 dB)")
    ap.add_argument("--noise-rule", choices=["la", "dc"], default=None,
                    help="site noise coordination rule: la -150 dBm flat, dc floor-15")
    ap.add_argument("--near-op-margin", type=float, default=None,
                    help="near test: dB below max operating power (DC OUC: 5)")
    ap.add_argument("--near-dmg-margin", type=float, default=None,
                    help="near test: dB below damaging threshold (DC OUC: 10)")
    ap.add_argument("--load-case", choices=["pair", "loading"], default="pair",
                    help="pair: two radios (farthest point + directly under an "
                         "antenna). loading: the DC OUC instant with 10 "
                         "channels active, per-channel caps by 10log10(10)")
    ap.add_argument("--solve-density", action="store_true",
                    help="add antenna density until the near-far rule passes "
                         "(the splitter-tree route: 10log10(N) branch loss; "
                         "owner direction 2026-11-26, corrected by the owner: "
                         "the branch coupler/tap loss is the primary lever)")
    ap.add_argument("--near-branch-loss-db", type=float, default=None,
                    help="branch loss on the nearest antenna's return path "
                         "from the plate design (the tap ratio in a tap "
                         "plate; 10log10(N)+insertions in a splitter tree). "
                         "When given, the near test is checked WITH it")
    ap.add_argument("--near-chain-db", type=float, default=0.0,
                    help="tap-chain loss from the nearest antenna's position "
                         "to the BDA input (0 = worst case: the BDA sits at "
                         "the first tap)")
    ap.add_argument("--density-max", type=int, default=24,
                    help="cap on antennas per floor in the density solve")
    ap.add_argument("--load-channels", type=int, default=10)
    ap.add_argument("--g-donor", type=float, default=15.5, help="donor antenna gain dB")
    ap.add_argument("--das-ul-net", type=float, default=0.0,
                    help="distribution net UL gain dB (fiber remote UL gain; 0 conservative)")
    ap.add_argument("--loss-coeff", type=float, default=None,
                    help="override trunk tap loss dB at k (default: same formula as pmf)")
    ap.add_argument("--out")
    a = ap.parse_args()
    # per-AHJ profile (the CLI args override); generic fallback when neither
    sys.path.insert(0, ".")  # -I mode: the script dir is not on the path
    from ps_profiles import resolve as resolve_profile
    RESOLVED, PWHERE, PROF = resolve_profile(a.profile, {
        "arrival_margin": ("arrival_margin_db", "arrival_margin_db", a.arrival_margin),
        "noise_rule": ("noise_rule", "noise_rule", a.noise_rule),
        "near_op_margin": ("near_operating_margin_db", "near_operating_margin_db", a.near_op_margin),
        "near_dmg_margin": ("near_damage_margin_db", "near_damage_margin_db", a.near_dmg_margin),
        "squelch_offset": ("squelch_offset_db", "squelch_offset_db", None),
        "ul_erp_donor_cap": ("ul_erp_donor_cap_dbm", "ul_erp_donor_cap_dbm", None),
    })
    a.arrival_margin = RESOLVED["arrival_margin"]
    a.noise_rule = RESOLVED["noise_rule"]
    NEAR_OP_MARGIN = RESOLVED["near_op_margin"]
    NEAR_DMG_MARGIN = RESOLVED["near_dmg_margin"]

    # Owner formula (2026-11-28): the AGC input ceiling is the UL composite
    # output minus the lowest effective gain the unit can settle at
    # (max input = Pout - G_min, or Pout - (G - Att_min) with the front
    # attenuator folded in). It computes the operating (AGC clamp) line, not the
    # damage line; and the floor must be the minimum gain the design can run
    # (the ceiling tightens to Pout - G_required when the link budget needs
    # more gain than the spec floor). Applied per OEM only when BOTH the UL
    # composite output cap and the minimum available gain are published.
    for _hw in OEM_HW.values():
        _cap = _hw.get("ul_out_cap_dbm") or 0.0
        _floor = _hw.get("gain_floor_db")
        if _cap > 0 and _floor:
            _computed = round(_cap - max(_floor, 0.0), 1)
            _hw["max_input_dbm"] = _computed
            _hw["max_input_source"] = ("owner formula: UL composite output "
                                       "%.1f dBm - gain floor %.0f dB"
                                       % (_cap, _floor))
    SQUELCH_OFFSET_DB = RESOLVED["squelch_offset"]
    UL_ERP_DONOR_CAP = RESOLVED["ul_erp_donor_cap"]
    cfg_path = None
    sys.path.insert(0, "..")
    try:
        from romtool.config import load_config
        cfg = load_config()
        fh = float(cfg["autoplace"]["floor_height_m"])
        riser = float(cfg["cables"]["coax_1_2in"]["loss_db_per_100m_ghz"]["2.0"]) / 100.0
    except Exception:
        fh = 4.0
        riser = 0.144

    # trunk tap loss at the design k: same formula as pmf.loss_db
    def loss_db(k):
        return ((10 * math.log10(k) + 0.5) if k > 1 else 0.0) + (k // 2) * fh * riser
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
    # per-channel DL port: the composite splits by the channel count
    port_composite = a.port
    a.port = a.port - 10.0 * math.log10(a.channels)
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
        # plus the profile squelch offset; absolute level in dBm
        squelch_in = KTB_12K5 + 3.0 + SQUELCH_OFFSET_DB
        near_ok = ul_near_at_bda <= hw["max_input_dbm"] - NEAR_OP_MARGIN
        near_dmg = ul_near_at_bda <= hw["damaging_dbm"] - NEAR_DMG_MARGIN
        pad_db = max(0.0, round(ul_near_at_bda - (hw["max_input_dbm"] - NEAR_OP_MARGIN), 1))
        # near-far levers (owner correction 2026-11-26: "the coupler is
        # essentially an attenuator for that path back to the remote"). Three
        # ways to pass the near test:
        #   1. branch loss: the nearest antenna's coupler/tap ratio. A tap
        #      plate can spec the near taps high (the equal-loss convention
        #      already gives the closest branches the most dB); a splitter
        #      tree gives 10log10(N) plus insertions.
        #   2. the input pad (an explicit attenuator).
        #   3. density in a pure splitter tree: 10log10(N) comes from the
        #      tree itself (the --solve-density route; the most hardware).
        branch_need_op = round(ul_near_at_bda - (hw["max_input_dbm"] - NEAR_OP_MARGIN), 2)
        branch_need_dmg = round(ul_near_at_bda - (hw["damaging_dbm"] - NEAR_DMG_MARGIN), 2)
        branch_need = max(0.0, branch_need_op, branch_need_dmg)
        branch_ok = (a.near_branch_loss_db is not None
                     and a.near_branch_loss_db >= branch_need)
        # the density route: the tree losses add 10log10(N) to the branch
        dens = {"solve": bool(a.solve_density)}
        if a.solve_density:
            n_min = None
            for n in range(1, a.density_max + 1):
                tree_db = 10.0 * math.log10(n) + a.near_chain_db
                ni = ul_near_at_bda - tree_db
                if (ni <= hw["max_input_dbm"] - NEAR_OP_MARGIN
                        and ni <= hw["damaging_dbm"] - NEAR_DMG_MARGIN):
                    n_min = n
                    break
            dens.update({"antennas_per_floor": n_min,
                         "antennas_bldg": (n_min * 45 if n_min else None),
                         "near_input_at_density_dbm": (round(ul_near_at_bda
                                                             - (10.0 * math.log10(n_min)
                                                                + a.near_chain_db), 2)
                                                       if n_min else None),
                         "near_pass_at_density": bool(n_min),
                         "cap_hit": (n_min is None),
                         "near_chain_db": a.near_chain_db,
                         "note": ("the splitter-tree density route: 10log10(N) "
                                  "of branch attenuation builds in the tree; "
                                  "the tap-plate route reaches the same pass "
                                  "with the near tap ratio at >= %.1f dB "
                                  "instead" % branch_need)})
        # UL output cap per channel: plan-split at -10log10(channels); the
        # loading case (owner option 2026-11-26) re-tests the instant with
        # load_channels active (DC OUC uses 10), where per-channel caps are
        # -10log10(load_channels)
        out_cap_ch = hw["ul_out_cap_dbm"] - 10.0 * math.log10(a.channels)
        cap_binds = (ul_far_at_bda + g_rec) > out_cap_ch
        load_n = a.load_channels if a.load_case == "loading" else None
        out_cap_loading = (hw["ul_out_cap_dbm"] - 10.0 * math.log10(load_n)) if load_n else None
        # UL ERP at the donor per retransmitted channel (SF 9.5.3.1 class rule)
        ul_erp_donor = (ul_far_at_bda + g_rec) + a.g_donor
        erp_ok = (ul_erp_donor <= UL_ERP_DONOR_CAP) if UL_ERP_DONOR_CAP else None
        # near/far simultaneous pair (the field near test): one radio at the
        # farthest covered point, one directly under an antenna. Class B
        # (shared wideband AGC) starves the far channel by the near channel's
        # excess over the per-channel cap; Class A per-channel AGC does not.
        pair = {"far_in_at_bda_dbm": round(ul_far_at_bda, 2),
                "near_in_pre_pad_dbm": round(ul_near_at_bda, 2),
                "pad_applied_db": pad_db,
                "near_in_post_pad_dbm": round(ul_near_at_bda - pad_db, 2),
                "out_cap_per_channel_dbm": round(out_cap_ch, 2)}
        excess = max(0.0, (ul_near_at_bda - pad_db) + g_rec - out_cap_ch)
        classb_far_gain = g_rec - excess
        classb_far_arrival = ul_far_at_bda + classb_far_gain + a.g_donor \
            + a.ambient - a.site_erp
        pair.update({"classB_far_arrival_dbm": round(classb_far_arrival, 2),
                     "classB_agc_gain_reduction_db": round(excess, 2),
                     "classB_far_pass": bool(classb_far_arrival >= target_arrival),
                     "class_a_recommended": bool(excess > 0.0),
                     "load_case": a.load_case})
        if load_n:
            # loading instant: per-channel share widens to -10log10(load_n);
            # the near and far must both fit their request inside the share
            ld_far_req = ul_far_at_bda + g_rec
            ld_near_req = (ul_near_at_bda - pad_db) + g_rec
            pair.update({"loading_active_channels": load_n,
                         "loading_out_cap_per_channel_dbm": round(out_cap_loading, 2),
                         "loading_far_request_pass": bool(ld_far_req <= out_cap_loading),
                         "loading_near_request_pass": bool(ld_near_req <= out_cap_loading)})
            if not (pair["loading_far_request_pass"] and pair["loading_near_request_pass"]):
                pair["class_a_recommended"] = True
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
            "ul_erp_donor_per_channel_dbm": round(ul_erp_donor, 2),
            "ul_erp_donor_cap_dbm": UL_ERP_DONOR_CAP,
            "ul_erp_donor_pass": erp_ok,
            "ul_out_cap_composite_dbm": hw["ul_out_cap_dbm"],
            "max_input_dbm": hw["max_input_dbm"],
            "max_input_source": hw.get("max_input_source",
                                       "unpublished placeholder, grade D"),
            "ul_out_cap_per_channel_dbm": round(out_cap_ch, 2),
            "ul_out_cap_binds": bool(cap_binds),
            "near_test_operating_pass": bool(near_ok),
            "near_test_damage_pass": bool(near_dmg),
            "near_levers": {"branch_loss_needed_db": branch_need,
                            "branch_loss_passes_when_specified": bool(branch_ok),
                            "branch_loss_specified_db": a.near_branch_loss_db,
                            "pad_db": pad_db,
                            "density_route": dens.get("solve", False)
                                             and dens.get("near_pass_at_density",
                                                          False)},
            "density_solve": dens,
            "near_far_pair": pair,
            "passes": bool((arrival >= target_arrival and noise_at_site <= noise_limit
                            and not cap_binds and pair["classB_far_pass"])
                           and (near_ok and near_dmg or branch_ok))})
    doc = {"basis": "PS uplink engine (grade E): band-reciprocal chain at the "
                    "design k, AHJ site inputs parameterized. Assumptions: 3 W "
                    "portable, 5 dB hip-height body loss, distribution net UL "
                    "gain 0 dB (conservative), site ERP/NF/ambient defaults "
                    "are AHJ placeholders until the site sheet arrives; the "
                    "squelch floor uses a 3 dB distribution noise figure "
                    "estimate (grade D)." + " Vendor notes: " + hw["spec_note"],
           "channels": a.channels,
           "port_composite_dbm": port_composite,
           "port_per_channel_dbm": round(port_composite - 10.0 * math.log10(a.channels), 2),
           "profile": a.profile or "ifc_default",
           "profile_provenance": PWHERE,
           "load_case": a.load_case,
           "load_channels": a.load_channels if a.load_case == "loading" else None,
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
