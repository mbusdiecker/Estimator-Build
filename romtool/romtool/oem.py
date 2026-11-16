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
    it = {"category": "oem", "item": part["description"], "qty": int(qty), "unit": "ea",
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


def _expand_spec(items, pkg_key, pkg, area_sqft, add, note, has_floor):
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
    per_ru = int(lim["antennas_per_remote_unit_max"])
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
    need = math.ceil(ant / per_ru - 1e-9) if ant else 0
    if need > remotes:
        note("%s: %s need %d remote units at %d antennas each (data sheet limit: %d ports x %d antennas); "
             "the layout modeled %d." % (pkg["label"], basis, need, per_ru, lim["ports_per_remote_unit"],
                                          lim["antennas_per_port_max"], remotes))
    rus = max(remotes, need)
    if not rus and not sources:
        return out
    per_mu = int(lim["remote_units_per_master_max"])
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
    return out


def expand_oem(items, pkg_key, system_type=None, packages=None, area_sqft=None):
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
        kept = _expand_spec(items, pkg_key, pkg, area_sqft, add, note, has_floor)
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
