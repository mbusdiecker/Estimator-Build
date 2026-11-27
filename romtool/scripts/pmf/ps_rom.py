"""PS ROM assembler: builds ps_rom_results.json from the run artifacts and
applies the owner design gates when they are turned on.

Inputs (all under sugarhouse-26-01038/ by default):
  pmf_ps_*.json           the DL coverage runs (per OEM per frequency)
  ps_ul_results.json      the uplink engine output
  ps_grid_results.json    the grid-test self-check
  ps_compliance_*.json    the FPC 90.219 and delay plates per OEM
  ps_imd_results.json     the intermod screen for converged plates

Design gates (the owner 2026-11-26: 'build it in but do not turn it on yet'):
  --caps-area-sqft  maximum square feet per service antenna; the tool reports
                    the minimum antenna count the gate would require
  --caps-k          maximum floors per remote port; the tool reports the
                    minimum remote/port count the gate would require
Neither gate changes the runs while off; they report only.
"""
import argparse
import json
import math

SPECS = [
    ("ps_fire700", "ADRF", "FiRe-78-8-U1 + ADXV-R-3378P-U2", "700 PS", 33.0, {"oem": "adrf_fire"}),
    ("ps_fire800", "ADRF", "FiRe-78-8-U1 + ADXV-R-3378P-U2", "800 PS", 33.0, {"oem": "adrf_fire"}),
    ("ps_fire_b14", "ADRF", "FiRe-78-8-U1 (FirstNet)", "B14", 30.0, {"oem": "adrf_fire"}),
    ("ps_vu_vhf", "ADRF", "PSR-VU-9537-X", "VHF 136-174", 28.0, {"oem": "adrf_vu"}),
    ("ps_vu_uhf", "ADRF", "PSR-VU-9537-X", "UHF 380-512", 37.0, {"oem": "adrf_vu"}),
    ("ps_westell700", "Westell", "CS45-734-834-A0", "700 PS", 34.0, {"oem": "westell"}),
    ("ps_westell800", "Westell", "CS45-734-834-A0", "800 PS", 34.0, {"oem": "westell"}),
    ("ps_comba700", "Comba", "ComFlex NG V3 PS", "700 PS", 33.0, {"oem": "comba"}),
    ("ps_comba800", "Comba", "ComFlex NG V3 PS", "800 PS", 33.0, {"oem": "comba"}),
]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="sugarhouse-26-01038")
    ap.add_argument("--floors", type=int, default=45)
    ap.add_argument("--ant-gain", type=float, default=5.0)
    ap.add_argument("--channels", type=int, required=True,
                    help="channels in each passband: the composite port splits "
                         "by 10log10(channels); explicitly entered every run")
    ap.add_argument("--caps-area-sqft", type=float, default=None)
    ap.add_argument("--caps-k", type=int, default=None)
    ap.add_argument("--caps-on", action="store_true",
                    help="apply the gates to the reported design numbers "
                         "(default: report-only)")
    args = ap.parse_args()
    d = args.dir
    rows = []
    area_sqft = None
    for prefix, oem, model, freq, port, _ in SPECS:
        try:
            run = json.load(open(f"{d}/pmf_{prefix}.json"))
        except FileNotFoundError:
            run = None
        if run is None:
            continue
        dN = run["design_N"]
        dr = next(r for r in run["rows"] if r["N"] == dN)
        ant_n = dr["ant_bldg"]
        remotes = dr["remotes_bldg"]
        k = dr["k_fit"]
        # per-channel plate: the composite splits by the channel count, and the
        # coverage runs (pmf_ps_*) were executed AT the per-channel port
        port_ch = round(port - 10.0 * math.log10(args.channels), 2)
        plate = {"port_composite_dbm": port, "channels": args.channels,
                 "port_per_channel_dbm": port_ch,
                 "design_N": dN, "p_min_dbm": dr["P_min"],
                 "headroom_db": dr["headroom_db"], "k": k,
                 "cov_pct": dr["cov_confirmed"], "antennas_per_floor": dN,
                 "antennas_bldg": ant_n, "remotes_bldg": remotes,
                 "floors_per_port": k}
        # the owner gates (report-only unless --caps-on)
        gate = {"area_cap_sqft": args.caps_area_sqft, "k_cap": args.caps_k,
                "applied": False}
        if args.caps_on and args.caps_k:
            gate["applied"] = True
            gate["k_now"] = k
            gate["min_ports_required"] = math.ceil(args.floors / args.caps_k)
            gate["note"] = "the floors-per-port gate would change the remote " \
                           "count to the minimum above; the plate k shown " \
                           "stays the engine's fit"
        if args.caps_area_sqft:
            gate["area_note"] = "the antenna-area gate evaluates per floor in " \
                                "the grid module (caps_report): pass the same " \
                                "value there"
        # the ERP plate per retransmitted channel (90.219)
        erp = round(port_ch + args.ant_gain, 1)
        plate["erp_per_channel_dbm"] = erp
        plate["erp_pass_5w"] = bool(erp <= 37.0)
        if not plate["erp_pass_5w"]:
            plate["erp_pad_db"] = round(erp - 37.0, 1)
        rows.append({"oem": oem, "model": model, "freq": freq,
                     "prefix": prefix, "plate": plate, "gate": gate})
    ul = None
    try:
        ul = json.load(open(f"{d}/ps_ul_results.json"))
    except FileNotFoundError:
        pass
    grid = None
    try:
        grid = json.load(open(f"{d}/ps_grid_results.json"))
    except FileNotFoundError:
        pass
    comp = {}
    for name in ("ps_compliance_fire.json", "ps_compliance_comba.json",
                 "ps_compliance_westell.json", "ps_compliance_vu.json"):
        try:
            comp[name.replace("ps_compliance_", "").replace(".json", "")] = \
                json.load(open(f"{d}/{name}"))
        except FileNotFoundError:
            pass
    imd = None
    try:
        imd = json.load(open(f"{d}/ps_imd_results.json"))
    except FileNotFoundError:
        pass
    caps_block = {
        "enabled": bool(args.caps_on),
        "area_cap_sqft": args.caps_area_sqft,
        "k_cap": args.caps_k,
        "note": "Owner direction 2026-11-26: build the antenna square-footage "
                "and remote-coverage gates in but leave them off. While off, "
                "the gates report what they WOULD require; --caps-on applies "
                "them to the reported design numbers. Per-antenna area and "
                "floors-per-port as built live in the grid block and the "
                "plates."},
    # SBC architecture guidance (owner message 2026-11-26)
    floor_sqft = None
    if grid:
        floor_sqft = (grid.get("caps_report") or {}).get("floor_area_sqft")
    total = (floor_sqft * args.floors) if floor_sqft else None
    if total is not None:
        passive_fav = total < 500000.0
        fiber_backbone = total > 250000.0
        arch = {
            "floor_area_sqft": floor_sqft, "floors": args.floors,
            "total_building_sqft": round(total, 0),
            "passive_vs_active": {
                "sbc_guidance": "passive favored generally under about "
                                "500,000 sq ft; active common for 500,000 to "
                                "1,000,000+ sq ft or dense/steel-heavy "
                                "buildings",
                "verdict": ("passive-simple side of the boundary" if passive_fav
                            else "active DAS territory"),
                "this_building": ("about %.0f sq ft: under the 500K passive "
                                  "boundary, but a 45-floor dense/steel "
                                  "high-rise is the active case the guidance "
                                  "names" % total)
                               if passive_fav else
                               ("about %.0f sq ft: active DAS territory" % total)},
            "fiber_vs_coax": {
                "sbc_guidance": "fiber optic backbones recommended over coax "
                                "for buildings over 250,000 sq ft or campus "
                                "layouts (lower signal loss)",
                "verdict": "fiber backbone recommended" if fiber_backbone
                           else "coax backbone permissible",
                "this_building": ("about %.0f sq ft is over the 250,000 sq ft "
                                  "line: the SBC recommends the fiber "
                                  "backbone, which favors the fiber-star "
                                  "architectures (ADRF FiRe remotes, the "
                                  "Westell V2 fiber DAS kit, the Comba MU+RU "
                                  "fiber DAS) over the pure coax trunk "
                                  "standalone BDAs" % total)
                                 if fiber_backbone else "coax acceptable"}}
    else:
        arch = {"note": "floor area unknown: run ps_grid first to get "
                        "floor_area_sqft"}
    converged_note = {
        "requirement": "CONVERGED SYSTEMS: the order MUST include the "
                       "channel-specific (Class A channelized) filters for "
                       "every PS channel, on every shared plate and every "
                       "BDA carrying PS bands (owner direction 2026-11-26; "
                       "SBC practice)",
        "applies_to": "any kit combining cellular and ERCES on shared coax, "
                      "tap plates, or donor-side hardware"}
    doc = {"basis": "PS ROM per OEM and frequency: the DL plate from the pmf "
                    "runs, the uplink from the ps_ul noise-coordination "
                    "engine, the grid-method self-check from the floor model, "
                    "the 90.219 and delay plates, and the converged-plate "
                    "intermod screen. Config estimates (grade E). No prices.",
           "floors": args.floors, "ant_gain_dbi": args.ant_gain,
           "architecture_sbc": arch, "converged_order_requirement": converged_note,
           "rom": rows, "uplink": ul, "grid": grid, "compliance": comp,
           "intermod": imd, "caps": caps_block}
    out = f"{d}/ps_rom_results.json"
    json.dump(doc, open(out, "w"), indent=1)
    print("wrote", out, "| rows:", len(rows))
    for r in rows:
        p = r["plate"]
        print("%-22s %-10s comp %4.1f %-2dch -> %5.2f ch  N=%d  k=%d  rem=%d  ERP %s" % (
            r["model"], r["freq"], p["port_composite_dbm"], p["channels"],
            p["port_per_channel_dbm"], p["design_N"], p["k"],
            p["remotes_bldg"],
            "%.1f dBm %s" % (p["erp_per_channel_dbm"],
                             "PASS" if p["erp_pass_5w"] else
                             "FAIL pad %.1f" % p.get("erp_pad_db", 0))))

if __name__ == "__main__":
    main()
