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
"""

import json
import math
import os

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OEM_PATH = os.path.join(_REPO_ROOT, "config", "oem_packages.json")
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


def load_oem_packages(path=None):
    """The OEM package config (read once per path). Do not mutate the result."""
    p = path or DEFAULT_OEM_PATH
    if p not in _cache:
        with open(p, encoding="utf-8") as f:
            _cache[p] = json.load(f)
    return _cache[p]


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


def _line(pkg_key, pkg, role, qty, floor, has_floor, why):
    part = pkg["parts"][role]
    it = {"category": "oem", "item": part["description"], "qty": int(qty), "unit": "ea",
          "role": "oem.%s.%s" % (pkg_key, role), "oem_role": role,
          "note": "%s %s: %s (grade C, ANS quote history)." % (pkg["label"], role.replace("_", " "), why)}
    if has_floor:
        it["_floor"] = floor
    return it


def _overlay_part(pkg, role):
    part = pkg["parts"][role]
    return {"manufacturer": part["manufacturer"], "model": part["model"],
            "description": part["description"], "ibwave_type": IBWAVE_TYPE.get(role, "Miscellaneous"),
            "unit": "ea", "source": "ANS quote history, most-quoted %s on %d jobs" % (role, part["jobs"])}


def expand_oem(items, pkg_key, system_type=None, packages=None):
    """Replace the generic OEM lines with the package's parts.

    Returns (items, overlay, notes). overlay maps each new role to a catalog
    part for the iBwave export. Items keep their order; site-level lines are
    appended at the end."""
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
