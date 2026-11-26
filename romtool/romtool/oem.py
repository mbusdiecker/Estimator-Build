"""OEM packages: turn the generic OEM lines of the BOM into one OEM's equipment.

The element BOM carries three generic OEM lines: oem.source (head end or donor
feed), oem.remote and oem.repeater. With an OEM package selected (project
"oem", see config/oem_packages.json), expand_oem() replaces them with that
OEM's parts, counted from ANS quote-history ratios (grade C):

- per remote line: the remote unit, or for chassis-based OEMs (ADRF, SOLiD)
  the remote chassis plus band modules; SFPs and power supplies per remote.
- site level (once per project, floor label "Site"): head end, POI cards,
  fiber hubs and, for DRAN, basebands.
- BDA packages: the repeater line becomes the OEM BDA and the source line the
  OEM donor antenna.

Batteries and annunciators are left to the power and monitoring builders, so
nothing is counted twice. A ratio from fewer than min_jobs jobs is not applied:
the line it would size is left out and a note says why. No prices.

Spec packages (config/oem_specs.json, merged at load) come from OEM data
sheets and OEM designs instead of quote history. They carry hard limits
(remotes per master, antennas per remote), kit contents and, optionally, an
OEM design density. expand_oem() sizes them with _expand_spec(): remotes from
the density (when the covered area is known) or from the modeled antennas,
capped by the per-remote antenna limit; masters from the remotes-per-master
limit; and kit-supplied remotes, donors and antennas credited.
"""

import json
import math
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OEM_PATH = os.path.join(_REPO_ROOT, "config", "oem_packages.json")
DEFAULT_SPEC_PATH = os.path.join(_REPO_ROOT, "config", "oem_specs.json")
SITE = "Site"

# iBwave Type per role, using the types the estimating tool's v6 Type list knows.
IBWAVE_TYPE = {
    "head_end": "Network Equipment", "master_unit": "Network Equipment",
    "poi_card": "Network Equipment", "fiber_hub": "Network Equipment",
    "baseband_radio_carrier": "Network Equipment",
    "remote_unit": "Remote", "remote_chassis": "Remote",
    "remote_band_module": "Miscellaneous", "sfp_optic": "Miscellaneous",
    "power_supply": "Power Supply", "bda_repeater": "Repeater",
    "donor_antenna": "Antenna",
}

_cache = {}


class OEMError(ValueError):
    """Unknown OEM package."""


def load_oem_packages(path=None, spec_path=None):
    """The OEM package config (read once per path), with the hand-maintained
    spec packages merged in. A quote-history package of the same key wins.
    Do not mutate the result."""
    p = path or DEFAULT_OEM_PATH
    sp = spec_path or DEFAULT_SPEC_PATH
    key = (p, sp)
    if key not in _cache:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as f:
                spec = json.load(f)
            merged = dict(spec.get("packages", {}))
            merged.update(cfg.get("packages", {}))
            cfg = dict(cfg, packages=merged)
        _cache[key] = cfg
    return _cache[key]


def check_oem(value, packages=None):
    """None or '' clears the selection; otherwise the key must exist."""
    if value in (None, ""):
        return None
    pk = (packages or load_oem_packages())["packages"]
    if value not in pk:
        raise OEMError("oem must be one of %s (got %r)" % (", ".join(sorted(pk)), value))
    return value


def list_packages(packages=None):
    pk = (packages or load_oem_packages())["packages"]
    return [{"key": k, "label": v["label"], "system_type": v["system_type"], "jobs": v["jobs"]}
            for k, v in sorted(pk.items(), key=lambda kv: (kv[1]["system_type"], kv[1]["label"]))]


def _ratio(pkg, name):
    r = pkg.get("ratios", {}).get(name)
    return r if r and r.get("usable") else None


def _is_spec(pkg):
    return pkg.get("source") == "spec"


def _line(pkg_key, pkg, role, qty, floor, has_floor, why):
    part = pkg["parts"][role]
    basis = ("grade %s, %s" % (pkg.get("grade", "B"), pkg.get("source_label", "OEM data sheet"))
             if _is_spec(pkg) else "grade C, ANS quote history")
    it = {"category": "oem", "item": part["description"], "qty": int(qty),
          "unit": part.get("unit", "ea"),
          "role": "oem.%s.%s" % (pkg_key, role), "oem_role": role,
          "note": "%s %s: %s (%s)." % (pkg["label"], role.replace("_", " "), why, basis)}
    if has_floor:
        it["_floor"] = floor
    return it


def _overlay_part(pkg, role):
    part = pkg["parts"][role]
    src = (pkg.get("source_label", "OEM data sheet") if _is_spec(pkg)
           else "ANS quote history, most-quoted %s on %d jobs" % (role, part["jobs"]))
    return {"manufacturer": part["manufacturer"], "model": part["model"],
            "description": part["description"], "ibwave_type": IBWAVE_TYPE.get(role, "Miscellaneous"),
            "unit": "ea", "source": src}


def _service_antennas(items):
    """Service antennas in the element BOM (donor antennas excluded)."""
    n = 0
    for it in items:
        role = it.get("role") or ""
        if role.startswith("antenna.") and role != "antenna.donor":
            n += int(round(float(it.get("qty", 0))))
    return n


def _expand_spec(items, pkg_key, pkg, area_sqft, add, note, has_floor,
                 autonomy_hours=12, ps_mode=False):
    """Size a spec package (for example SureCall FiberMax) from its limits.

    Remotes = the larger of the modeled remotes and the antennas divided by the
    per-remote antenna limit. The antenna basis is the OEM design density times
    the covered area when both are known, otherwise the modeled service
    antennas. Masters = remotes / remotes-per-master (at least one per off-air
    source). Each master kit already holds the remotes, donor and antennas
    listed under kit.master_unit_includes, so those are credited."""
    lim = pkg["limits"]
    kit = (pkg.get("kit") or {}).get("master_unit_includes", {})
    dens = pkg.get("design_density")
    out, remotes, sources = [], 0, 0
    for it in items:
        role = it.get("role")
        q = int(round(float(it.get("qty", 0))))
        if role == "oem.remote":
            remotes += q
            continue
        if role == "oem.source":
            sources += q
            if not kit.get("donor_antenna"):
                out.append(it)
            continue
        if role == "oem.repeater":
            note("%s is a fiber DAS; the generic repeater line is replaced by its master units." % pkg["label"])
            continue
        out.append(it)

    modeled = _service_antennas(items)
    per_ru = int(lim.get("antennas_per_remote_unit_max", 0))
    if dens and area_sqft:
        ant = math.ceil(float(area_sqft) / float(dens["sq_ft_per_antenna"]) - 1e-9)
        basis = ("%d antennas from %s sq ft at %s sq ft per antenna (grade %s: %s)"
                 % (ant, format(int(round(float(area_sqft))), ","), format(dens["sq_ft_per_antenna"], ","),
                    dens["grade"], dens["ref"]))
        if modeled and abs(modeled - ant) > 0.15 * ant:
            note("%s: the layout models %d service antennas but the OEM design density gives %d; "
                 "equipment is sized on the density, check the antenna and cable lines." % (pkg["label"], modeled, ant))
    else:
        ant = modeled
        basis = "%d modeled service antennas" % ant
        if dens:
            note("%s: no covered area given, so the OEM design density (%s sq ft per antenna) was not applied."
                 % (pkg["label"], format(dens["sq_ft_per_antenna"], ",")))
    need = math.ceil(ant / per_ru - 1e-9) if (ant and per_ru) else 0
    if need > remotes:
        note("%s: %s need %d remote units at %d antennas each (data sheet limit: %s ports x %s antennas); "
             "the layout modeled %d." % (pkg["label"], basis, need, per_ru, lim.get("ports_per_remote_unit", "?"),
                                          lim.get("antennas_per_port_max", "?"), remotes))
    rus = max(remotes, need)
    if not rus and not sources:
        return out
    per_mu = int(lim.get("remote_units_per_master_max", 1))
    if pkg.get("head_end"):
        # The confirmed head end kit owns the master split and the site parts:
        # quantities come from the kit's own per rules, not the generic model.
        note("%s limits (grade %s, %s): %s %s"
             % (pkg["label"], lim.get("grade", "B"), lim.get("ref", "OEM data sheet"),
                lim.get("ref", ""), str(lim.get("fcc", "") or "")))
        _head_end_entries(pkg, rus, add, note, autonomy_hours=autonomy_hours, ps_mode=ps_mode)
        kept = [it for it in out if it.get("role") != "oem.source"] if rus else out
        if len(kept) != len(out):
            note("%s: the kit carries its own donor side (donor antennas + protection), so the generic off-air "
                 "source line is dropped." % pkg["label"])
        return kept
    mus = max(math.ceil(rus / per_mu - 1e-9), sources, 1)
    kit_ru = int(kit.get("remote_unit", 0)) * mus
    rus = max(rus, kit_ru)
    site = SITE if has_floor else ""
    add("master_unit", mus, site, "%d remote units at up to %d per master" % (rus, per_mu))
    add("remote_unit", rus - kit_ru, site,
        "%d remote units less %d supplied in the master kits" % (rus, kit_ru))
    inc = ", ".join("%s %s" % (v, k.replace("_", " ")) for k, v in kit.items())
    if inc:
        note("%s: each master kit includes %s. Buy %d service antennas beyond the %d in the kits; "
             "remote locations must sit within the kit fiber length or need longer fiber."
             % (pkg["label"], inc, max(0, ant - int(kit.get("service_antenna", 0)) * mus),
                int(kit.get("service_antenna", 0)) * mus))
    note("%s limits (grade %s, %s): %d remotes per master, %d antennas per remote, fiber reach %d m, %s."
         % (pkg["label"], lim["grade"], lim["ref"], per_mu, per_ru, lim["fiber_reach_m"], lim.get("fcc", "")))
    _head_end_entries(pkg, rus, add, note, autonomy_hours=autonomy_hours, ps_mode=ps_mode)
    return out


# The code table published with every public safety package (owner 2026-11-26:
# "publish all code tables with the PS BOM package so the standards followed are
# known"). Row: topic | IFC 2024 cite | NFPA 1225-2022 cite | requirement | how
# the tool enforces it. Sources: codes.iccsafe.org (IFC 2024, read verbatim) and
# the NFPA 1225-2022 chapter 18 PDF on disk (scripts/pmf/codes), cross-checked
# against the Safer Buildings Coalition Quick Code Reference (August 2026).
PS_CODE_TABLE = [
    {"topic": "Equipment listing", "ifc": "510.4", "nfpa": "18.12.1.3",
     "requirement": "ERCES components listed to UL 2524.",
     "enforcement": "the PS packages carry UL 2524 listed parts only (data sheet listings on file)"},
    {"topic": "Coverage", "ifc": "510.4.1", "nfpa": "18.8.3, 18.8.4",
     "requirement": "95 percent of general areas and 99 percent of critical areas per floor.",
     "enforcement": "design side; the acceptance test runs per the grid method (IFC 510.5.4 / NFPA 1225 18.5, 20.3.10.1)"},
    {"topic": "Audio quality", "ifc": "510.4.1.1", "nfpa": "18.9",
     "requirement": "DAQ 3.0 downlink and uplink (or the equivalent BER or SINR).",
     "enforcement": "design side; carried into the link budget checks"},
    {"topic": "Signal strength", "ifc": "510.4.1.1 (2015/2021 wording)", "nfpa": "none in 1225-2022",
     "requirement": "the -95 dBm figure appears only in the IFC 2015 and 2021 editions; the 2024 IFC and NFPA 1225 use DAQ 3.0.",
     "enforcement": "assumption noted in the ROM when a jurisdiction adopts the older wording"},
    {"topic": "Standby power", "ifc": "510.4.2.3", "nfpa": "18.13.2",
     "requirement": "dedicated 12 h at 100 percent, or a 2 h battery with generator power carrying the full load for 12 h.",
     "enforcement": "the sized battery (the autonomy_hours setting: 12 h default, 24 h owner option)"},
    {"topic": "Cabinets and batteries", "ifc": "510.4.2.4(1),(2)", "nfpa": "18.3.1",
     "requirement": "NEMA 4 (or 4X) equipment cabinets; batteries in NEMA 3R or better.",
     "enforcement": "the parts carry the enclosure ratings on their data sheets (NEMA 4 sheets on file)"},
    {"topic": "Donor isolation", "ifc": "510.4.2.4(4)", "nfpa": "18.3.3.2, 18.10",
     "requirement": "at least 20 dB above system gain between the donor antenna and all inside antennas under all operating conditions.",
     "enforcement": "the packages note the isolation check routines; verified at commissioning"},
    {"topic": "Oscillation", "ifc": "510.4.2.4(5)", "nfpa": "18.3.2",
     "requirement": "built-in oscillation detection and control, supervisory signal, shutdown when uncorrectable.",
     "enforcement": "the selected parts carry oscillation management (data sheet features)"},
    {"topic": "Monitoring", "ifc": "510.4.2.5", "nfpa": "18.14, NFPA 72 ch. 10",
     "requirement": "monitored by a listed fire alarm control unit with the 8 supervisory signals (AC loss, charger failure, signal source, RF device failure, 70 percent low battery, critical components, the alarm link, oscillation).",
     "enforcement": "the annunciator and alarm wiring parts; the alarm configuration per UL 2524 second edition"},
    {"topic": "Dedicated annunciator", "ifc": "510.4.2.5", "nfpa": "18.14.2.1",
     "requirement": "a dedicated annunciator in the fire command center where one exists.",
     "enforcement": "the annunciator part (PSR-ANN / CS40-ANNUNC class), included by the package"},
    {"topic": "Approvals", "ifc": "510.5.2, 510.5.5, FCC 90.219", "nfpa": "18.2.1, 18.6.1, 18.7.2",
     "requirement": "fire code official plus frequency license holder approval before installation and activation; FCC Class B registration where applicable; FirstNet Band 14 retransmission needs FirstNet Authority consent.",
     "enforcement": "process items in the ROM notes"},
    {"topic": "Noise and non-interference", "ifc": "510.4.2.9", "nfpa": "18.6, 18.9.3",
     "requirement": "uplink noise coordinated with all affected license holders; no interference to the public safety system.",
     "enforcement": "design side; the uplink check in the comparison runs"},
    {"topic": "Cable and survivability", "ifc": "510.4 via NFPA 1225", "nfpa": "18.12.3.1, 18.12.3.3, 18.12.3.4",
     "requirement": "plenum or riser cable per Article 820; backbone fire rating only in nonsprinklered, partially sprinklered or high-rise buildings.",
     "enforcement": "the coax parts (plenum riser in the kits) and the sprinkler-status switch at the ROM inputs"},
    {"topic": "Lightning protection", "ifc": "via NFPA 1225", "nfpa": "18.4 (NFPA 70 Article 820, UL 497C)",
     "requirement": "listed antenna discharge units on the donor feed.",
     "enforcement": "the arrestor parts (Polyphaser IS-50NX-C2 class per the ROM precedent)"},
    {"topic": "Testing", "ifc": "510.5.4, 510.6.1", "nfpa": "18.5, 20.3.10.1",
     "requirement": "initial and acceptance, then annual and 5-year inspections.",
     "enforcement": "process items in the ROM notes"},
    {"topic": "Personnel", "ifc": "510.5.3", "nfpa": "18.2, 3.3.115",
     "requirement": "FCC GROL plus approved in-building training, or demonstrated skills to the fire code official.",
     "enforcement": "process item in the ROM notes"},
]


def _emit_code_tables(pkg, note):
    """Publish the standards-followed matrix with the PS BOM package (owner
    2026-11-26). One note line per row, prefixed so it reads as a table."""
    for row in PS_CODE_TABLE:
        note("%s code table | %s | IFC %s | NFPA %s | %s | enforced: %s"
             % (pkg["label"], row["topic"], row["ifc"], row["nfpa"], row["requirement"], row["enforcement"]))


def _head_end_entries(pkg, rus, add, note, autonomy_hours=12, ps_mode=False):
    """Emit the confirmed head end kit (oem_specs.head_end): site_parts sized by
    their per rule and per_remote lines (owner wiring 2026-11-26).

    per rules: 'project' (fixed qty, once remotes exist), 'when_remotes' (qty
    when any remote exists), 'beyond_master_max' (ceil((rus - per master max) /
    per master max)), 'remotes_per_<N>' (ceil(rus / N)), and 'battery' (sized
    from limits.battery at autonomy_hours: 12 h is the code default, NFPA 1225
    18.13.2 / IFC 510.4.2.3; 24 h is the owner option). per_remote entries are
    qty times the modeled remote count. Entries marked variant 'converged only'
    emit only in the public safety pass (ps_mode). Quantities are the confirmed
    kit values; no prices."""
    he = pkg.get("head_end") or {}
    for it in (he.get("site_parts") or []):
        role, per, why = it["role"], it.get("per", "project"), it.get("why", "")
        if not rus:
            continue
        if it.get("variant") == "converged only" and not ps_mode:
            continue
        if per == "project" or per == "when_remotes":
            qty = int(it.get("qty", 0))
        elif per == "beyond_master_max":
            pm = int((pkg.get("limits") or {}).get("remote_units_per_master_max", 1))
            qty = int(math.ceil(max(rus - pm, 0) / pm - 1e-9))
        elif per.startswith("remotes_per_"):
            n = int(per.split("_")[-1])
            qty = int(math.ceil(rus / n - 1e-9)) if n else 0
        elif per == "battery":
            bat = (pkg.get("limits") or {}).get("battery") or {}
            load = bat.get("load_w") or {}
            total_w = float(load.get("bda_mu", 0)) + float(load.get("per_remote", 0)) * rus
            ratio = float(bat.get("ah_per_w_hour", 0))
            options = sorted(bat.get("options", []), key=lambda o: o.get("ah", 0))
            if not ratio or not options:
                note("%s: the battery option list or the Ah sizing ratio is missing; the battery line is left out."
                     % pkg["label"])
                continue
            needed = total_w * autonomy_hours / ratio if total_w else 0
            pick = next((o for o in options if o.get("ah", 0) >= needed), None)
            if pick is None:
                big = options[-1]
                qty = int(math.ceil(needed / big.get("ah", 1) - 1e-9))
                pick = big
            else:
                qty = 1
            why = ("%d h autonomy at %.0f W (head end %s plus %s per remote x%d): needs %.0f Ah "
                   "(Ah = load x hours / %.1f); %s"
                   % (autonomy_hours, total_w, load.get("bda_mu"), load.get("per_remote"), rus,
                      needed, ratio, "x%d of the %g Ah %s option"
                      % (qty, pick.get("ah"), pick.get("model_label", "battery")) if qty > 1
                      else "one %g Ah %s covers it" % (pick.get("ah"), pick.get("model_label", "battery"))))
            add(pick["role"], qty, SITE, "12 h code minimum per NFPA 1225 18.13.2 / IFC 510.4.2.3: " + why)
            continue
        else:
            note("%s: unknown head end sizing rule %r; the line is skipped." % (pkg["label"], per))
            continue
        v = (" [%s]" % it["variant"]) if it.get("variant") else ""
        add(role, qty, SITE if it.get("_floor", True) is not False else "", "%s%s" % (why, v))
    for it in (he.get("per_remote") or []):
        if not rus:
            continue
        add(it["role"], int(it.get("qty", 0)) * rus, SITE if it.get("_floor", True) is not False else "",
            "per remote x%d: %s" % (rus, it.get("why", "")))
    for role, qty in (he.get("donor_parts") or {}).items():
        if role == "arrestor_why" or not rus:
            continue
        note("%s donor side: %s" % (pkg["label"], he["donor_parts"].get("arrestor_why", "")))
        add(role, int(qty), SITE, "%s" % he["donor_parts"].get("arrestor_why", "donor side per the design look"))
    if rus:
        add_note = ("%s code basis (IFC 2024 / NFPA 1225 2022, re-read 2026-11-26): DAQ 3.0 at 95 percent general and "
                    "99 percent critical coverage (510.4.1.1 / 18.8, 18.9); UL 2524 listed components (510.4 / "
                    "18.12.1.3); standby power 12 h at 100 percent or a 2 h battery with generator power carrying the "
                    "full load for 12 h (510.4.2.3 / 18.13.2); NEMA 4 cabinets and NEMA 3R batteries (510.4.2.4 / "
                    "18.3.1); donor isolation 20 dB above system gain (510.4.2.4(4) / 18.3.3.2, 18.10); oscillation "
                    "detection (18.3.2); fire alarm monitoring with the annunciator where a fire command center exists "
                    "(510.4.2.5 / 18.14). Assumptions: the -95 dBm figure appears only in the IFC 2015 and 2021 "
                    "wording, not in IFC 2024 or NFPA 1225; the one-story buildings under 12,000 sq ft exception "
                    "applies only where the fire code official accepts it (510.1 exception 4)."
                    % pkg["label"])
        note(add_note)
    if pkg.get("public_safety"):
        _emit_code_tables(pkg, note)


def expand_oem(items, pkg_key, system_type=None, packages=None, area_sqft=None,
               autonomy_hours=12, ps_mode=False):
    """Replace the generic OEM lines with the package's parts.

    Returns (items, overlay, notes). overlay maps each new role to a catalog
    part for the iBwave export. Items keep their order; site-level lines are
    appended at the end. area_sqft (covered area) lets a spec package with an
    OEM design density size its equipment from the area."""
    cfg = packages or load_oem_packages()
    pkg = cfg["packages"][pkg_key]
    parts = pkg["parts"]
    has_floor = any("_floor" in it for it in items)
    out, overlay, notes = [], {}, []

    def note(text):
        if text not in notes:
            notes.append(text)

    if system_type and system_type != pkg["system_type"]:
        note("%s is a %s package but the project system type is %s."
             % (pkg["label"], pkg["system_type"].upper(), system_type.upper()))

    def add(role, qty, floor, why):
        if qty <= 0:
            return
        if role not in parts:
            note("%s has no quoted %s part; that line is left out." % (pkg["label"], role.replace("_", " ")))
            return
        out.append(_line(pkg_key, pkg, role, qty, floor, has_floor, why))
        overlay["oem.%s.%s" % (pkg_key, role)] = _overlay_part(pkg, role)

    def per_remote(role, ratio_name, q, floor):
        r = _ratio(pkg, ratio_name)
        if r is None:
            if role in parts:
                note("%s: %s not sized (fewer than %d jobs quote it)." % (pkg["label"], ratio_name.replace("_", " "), cfg["min_jobs"]))
            return
        add(role, math.ceil(q * r["value"] - 1e-9), floor,
            "%g per remote, median of %d jobs" % (r["value"], r["jobs"]))

    if _is_spec(pkg) and pkg.get("limits"):
        kept = _expand_spec(items, pkg_key, pkg, area_sqft, add, note, has_floor,
                            autonomy_hours=autonomy_hours, ps_mode=ps_mode)
        return kept + out, overlay, notes

    remotes = sources = repeaters = 0
    chassis_based = _ratio(pkg, "band_modules_per_remote_chassis") is not None and "remote_chassis" in parts
    for it in items:
        role = it.get("role")
        floor = it.get("_floor", "")
        q = int(round(float(it.get("qty", 0))))
        if role == "oem.remote" and pkg["system_type"] in ("das", "dran", "bda") and q:
            remotes += q
            if chassis_based:
                add("remote_chassis", q, floor, "one chassis per remote")
                bm = _ratio(pkg, "band_modules_per_remote_chassis")
                add("remote_band_module", math.ceil(q * bm["value"] - 1e-9), floor,
                    "%g band modules per chassis, median of %d jobs; the band mix follows the carrier and band setup"
                    % (bm["value"], bm["jobs"]))
            elif "remote_unit" in parts:
                add("remote_unit", q, floor, "one per modeled remote")
            else:
                out.append(it)
                note("%s has no quoted remote part; the generic remote line stays." % pkg["label"])
            per_remote("sfp_optic", "sfp_per_remote_unit", q, floor)
            per_remote("power_supply", "power_supplies_per_remote_unit", q, floor)
            continue
        if role == "oem.source" and q:
            sources += q
            if pkg["system_type"] == "bda":
                add("donor_antenna", q, floor, "one per modeled off-air source")
                if "donor_antenna" not in parts:
                    out.append(it)
            continue
        if role == "oem.repeater" and q:
            repeaters += q
            if "bda_repeater" in parts:
                add("bda_repeater", q, floor, "one per modeled repeater")
            else:
                out.append(it)
                note("%s has no quoted BDA part; the generic repeater line stays." % pkg["label"])
            continue
        out.append(it)

    site = SITE if has_floor else ""
    if pkg["system_type"] == "dran":
        r = _ratio(pkg, "remote_units_per_baseband")
        n = max(sources, math.ceil(remotes / r["value"] - 1e-9) if r and remotes else (1 if remotes or sources else 0))
        add("baseband_radio_carrier", n, site,
            "%g radios per baseband, median of %d jobs" % (r["value"], r["jobs"]) if r
            else "one per site (too few jobs for a ratio)")
    elif pkg["system_type"] == "das" and (remotes or sources):
        he_role = "head_end" if "head_end" in parts else ("master_unit" if "master_unit" in parts else None)
        r = _ratio(pkg, "remote_units_per_head_end")
        n_he = max(sources, math.ceil(remotes / r["value"] - 1e-9) if r and remotes else 1)
        if he_role:
            add(he_role, n_he, site, "%g remotes per head end, median of %d jobs" % (r["value"], r["jobs"]) if r
                else "one per modeled source (too few jobs for a remotes-per-head-end ratio)")
        else:
            note("%s has no quoted head-end part; the generic source line stays." % pkg["label"])
            out.extend(it for it in items if it.get("role") == "oem.source")
        p = _ratio(pkg, "poi_cards_per_head_end")
        if p:
            add("poi_card", math.ceil(n_he * p["value"] - 1e-9), site,
                "%g per head end, median of %d jobs; the count follows the carrier and band setup" % (p["value"], p["jobs"]))
        elif "poi_card" in parts:
            note("%s: POI cards not sized (fewer than %d jobs quote them with a head end)." % (pkg["label"], cfg["min_jobs"]))
    if pkg["system_type"] in ("das", "dran") and remotes:
        h = _ratio(pkg, "remote_units_per_fiber_hub")
        if h:
            add("fiber_hub", math.ceil(remotes / h["value"] - 1e-9), site,
                "%g remotes per hub, median of %d jobs" % (h["value"], h["jobs"]))
    return out, overlay, notes
