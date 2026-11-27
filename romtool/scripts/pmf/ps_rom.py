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

def even_split(total, parts):
    base = total // parts
    rem = total % parts
    return [base + 1] * rem + [base] * (parts - rem) if parts else []

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
    ap.add_argument("--caps-remote-sqft", type=float, default=None,
                    help="max building sq ft per remote/BDA (industry "
                         "practice about 250,000); report-only gate")
    ap.add_argument("--caps-near-density", action="store_true",
                    help="apply the near-test density from the ps_ul solve "
                         "(the DC OUC margins when that profile ran): the "
                         "plate takes max(coverage N, density N)")
    ap.add_argument("--caps-on", action="store_true",
                    help="apply the gates to the reported design numbers "
                         "(default: report-only)")
    args = ap.parse_args()
    d = args.dir
    floor_sqft = None
    floor_total_sqft = None
    grid_hint = None
    try:
        grid_hint = json.load(open(f"{d}/ps_grid_results.json"))
        floor_sqft = (grid_hint.get("caps_report") or {}).get("floor_area_sqft")
    except FileNotFoundError:
        pass
    if floor_sqft:
        floor_total_sqft = floor_sqft * args.floors
    rows = []
    area_sqft = None
    for prefix, oem, model, freq, port, meta in SPECS:
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
                "remote_sqft_cap": args.caps_remote_sqft,
                "applied": False}
        if args.caps_on and args.caps_k:
            gate["applied"] = True
            gate["k_now"] = k
            gate["min_ports_required"] = math.ceil(args.floors / args.caps_k)
            gate["note"] = "the floors-per-port gate would change the remote " \
                           "count to the minimum above; the plate k shown " \
                           "stays the engine's fit"
        if args.caps_remote_sqft and floor_total_sqft:
            gate["min_remotes_required"] = math.ceil(floor_total_sqft
                                                     / args.caps_remote_sqft)
            if args.caps_on and (gate["min_remotes_required"]
                                 > (remotes or 0)):
                gate["applied"] = True
        if args.caps_area_sqft:
            gate["area_note"] = "the antenna-area gate evaluates per floor in " \
                                "the grid module (caps_report): pass the same " \
                                "value there"
        # the near-density gate (owner 2026-11-26: 'add density until the
        # near-far rule passes, as well as the DC OUC'): when on, the plate
        # takes max(coverage N, the density N from the ps_ul solve)
        if args.caps_near_density:
            ul_art = {"adrf_fire": f"{d}/ps_ul_results.json",
                      "westell": f"{d}/ps_ul_westell.json",
                      "comba": f"{d}/ps_ul_comba.json",
                      "adrf_vu": f"{d}/ps_ul_vu.json"}.get(meta.get("oem"))
            dens_n = None
            if ul_art:
                try:
                    uj = json.load(open(ul_art))
                    vals = [b.get("density_solve", {}).get("antennas_per_floor")
                            for b in uj.get("bands", [])]
                    vals = [v for v in vals if v]
                    dens_n = max(vals) if vals else None
                except (FileNotFoundError, json.JSONDecodeError):
                    dens_n = None
            if dens_n and dens_n > plate["design_N"]:
                plate["near_density_gate"] = {
                    "coverage_n": plate["design_N"], "near_density_n": dens_n,
                    "design_N": dens_n,
                    "note": "plate raised to the near-test density (the DC "
                            "OUC margins); the pad alternative is reported in "
                            "the UL artifact"}
                plate["design_N"] = dens_n
                plate["antennas_per_floor"] = dens_n
                plate["antennas_bldg"] = dens_n * args.floors
        # BOM rebalance (owner 2026-11-26): equal per remote at BOM emission.
        # Example: the design says 21 antennas over 2 remotes -> 11 + 10, with
        # the splitters and coax split the same way. Day 3 note: re-running
        # the count with the floors split into equal zones per remote may
        # reduce the total; this block balances whatever the design said.
        n_r = plate["remotes_bldg"] or 0
        if n_r > 1:
            ant_total = plate["antennas_bldg"]
            spl_total = max(ant_total - n_r, 0)
            fl = even_split(args.floors, n_r)
            reb = {"remotes": n_r,
                   "antennas_per_remote": even_split(ant_total, n_r),
                   "floors_per_remote": fl,
                   "splitters_per_remote": even_split(spl_total, n_r),
                   "note": "antennas, tap plates, splitters and riser coax "
                           "balanced equally per remote in the emitted BOM; "
                           "day 3 may re-run the antennas needed by splitting "
                           "the square footage and floors equally per remote "
                           "to reduce the count; this balances the current "
                           "design"}
        else:
            reb = {"remotes": 1,
                   "note": "single remote: no rebalance needed"}
        # the ERP plate per retransmitted channel (90.219)
        erp = round(port_ch + args.ant_gain, 1)
        plate["erp_per_channel_dbm"] = erp
        plate["erp_pass_5w"] = bool(erp <= 37.0)
        if not plate["erp_pass_5w"]:
            plate["erp_pad_db"] = round(erp - 37.0, 1)
        rows.append({"oem": oem, "model": model, "freq": freq,
                     "prefix": prefix, "plate": plate, "gate": gate,
                     "bom_rebalance": reb})
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
