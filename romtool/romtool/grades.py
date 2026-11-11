"""Grade and budget-brand selection for the ROMtool parts lists.

A project has a grade (carrier, enterprise or budget) and, for budget, a brand
option (techwave or cheapest). The grade picks the part for each graded role
from config/grades.json: tappers and couplers by modeled tap ratio, the DAS
omni antenna, and the 1/2 in cable and connector as a pair. Selection is
deterministic and never changes the modeled loss. When the chosen part does not
cover the project bands, or a tap ratio is not offered, the selection falls
back (cheapest compliant, then enterprise, then carrier) and the item carries a
note that says why.

Graded items come from generate_bom and builder_items with a "grade_kind" key.
grade_items() rewrites those items to a synthetic role ("graded.<kind>[.<ratio>]")
and returns the matching catalog overlay, so iBwave and the BOM JSON use the
same part.

This module reads no unit values. budget_rank is an ordering only.
"""

import json
import os
import re

from romtool.bom import generate_bom
from romtool.builders import builder_items, load_parts_catalog

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_GRADES_PATH = os.path.join(_REPO_ROOT, "config", "grades.json")

GRADES = ("carrier", "enterprise", "budget")
BRANDS = ("techwave", "cheapest")
GRADED_KINDS = ("tapper", "coupler", "omni", "connector", "cable")

IBWAVE_TYPE = {"tapper": "Tap", "coupler": "Coupler", "omni": "Antenna",
               "connector": "Connector", "cable": "Cable"}
CANDIDATE_KEY = {"tapper": "tapper", "coupler": "coupler", "omni": "omni",
                 "connector": "connector_half_male"}

_CACHE = {}


class GradeError(ValueError):
    """Unknown grade, brand or band, or a grades config entry that is not usable."""


def load_grades(path=None):
    """The grades config (read once per path). Do not mutate the result."""
    p = path or DEFAULT_GRADES_PATH
    if p not in _CACHE:
        with open(p, "r", encoding="utf-8") as fh:
            _CACHE[p] = json.load(fh)
    return _CACHE[p]


def check_grade(value):
    if value not in GRADES:
        raise GradeError("grade must be one of %s (got %r)" % (", ".join(GRADES), value))
    return value


def check_budget_brand(value):
    if value not in BRANDS:
        raise GradeError("budget_brand must be one of %s (got %r)"
                         % (", ".join(BRANDS), value))
    return value


def check_bands(value, config):
    """A list of band ids from config/default.json, or a GradeError."""
    if not isinstance(value, list) or not all(isinstance(b, str) for b in value):
        raise GradeError("bands must be a list of band ids")
    known = {b["id"] for b in config.get("bands", [])}
    unknown = [b for b in value if b not in known]
    if unknown:
        raise GradeError("unknown band id(s): %s" % ", ".join(unknown))
    return list(value)


def default_bands(model, config, grades):
    """Bands checked when the project lists none: the cellular set. A project
    whose system_type is listed in grades.public_safety_system_types also gets
    the public-safety bands from config autoplace."""
    ids = list(grades["default_bands"])
    if (model or {}).get("system_type") in grades["public_safety_system_types"]:
        ps = (config.get("autoplace") or {}).get("public_safety_band_ids", [])
        ids += [b for b in ps if b not in ids]
    return ids


def settings_for(model, config, grade=None, budget_brand=None, grades=None):
    """Resolve the grade settings for a project. Explicit arguments (request
    query) win over the project's stored values, which win over the defaults."""
    g = grades or load_grades()
    model = model or {}
    grade = grade or model.get("grade") or g["default_grade"]
    brand = budget_brand or model.get("budget_brand") or g["default_budget_brand"]
    check_grade(grade)
    check_budget_brand(brand)
    freq = {b["id"]: float(b["freq_mhz"]) for b in config.get("bands", [])}
    ids = check_bands(model["bands"], config) if model.get("bands") else default_bands(
        model, config, g)
    return {
        "grade": grade,
        "budget_brand": brand,
        "bands": ids,
        "freqs": [freq[i] for i in ids],
        "band_freq": freq,
        "grades": g,
    }


# ------------------------------------------------------------ candidate rules

def _rank(p):
    r = p.get("budget_rank")
    return r if r is not None else 10 ** 6


def _in_scope(part, kind, scope, cfg):
    if scope == "carrier":
        m = cfg["carrier_match"][kind]
        if part["mfr"] != m["mfr"]:
            return False
        if "model" in m and part["model"] != m["model"]:
            return False
        if "model_regex" in m and not re.fullmatch(m["model_regex"], part["model"]):
            return False
        return True
    if scope == "techwave":
        return part["mfr"] == "Techwave"
    return True


def _covers(part, freqs):
    rng = part.get("bands_mhz")
    if not rng:
        return False
    return all(any(lo <= f <= hi for lo, hi in rng) for f in freqs)


def _uncovered(part, settings):
    """Project bands outside this part's stated ranges."""
    rng = part.get("bands_mhz") or []
    out = []
    for bid in settings["bands"]:
        f = settings["band_freq"][bid]
        if not any(lo <= f <= hi for lo, hi in rng):
            out.append(bid)
    return out


def _band_gap(pool, settings):
    """Project bands that no candidate in the pool covers at all."""
    gap = []
    for bid in settings["bands"]:
        f = settings["band_freq"][bid]
        if not any(any(lo <= f <= hi for lo, hi in (p.get("bands_mhz") or []))
                   for p in pool):
            gap.append(bid)
    return gap


def _pick(group, how):
    if how == "max_jobs":
        return sorted(group, key=lambda p: (-(p.get("jobs") or 0), _rank(p), p["model"]))[0]
    if how == "cheapest":
        return sorted(group, key=lambda p: (_rank(p), p["model"]))[0]
    if how == "median":
        ordered = sorted(group, key=lambda p: (_rank(p), p["model"]))
        return ordered[(len(ordered) - 1) // 2]
    raise GradeError("unknown selection rule %r" % how)


def _fmt(x):
    return "%g" % x


def _label(part):
    return "%s %s" % (part["mfr"], part["model"]) if part["model"] else part["description"]


def _ibwave_part(kind, p):
    return {"manufacturer": p["mfr"], "model": p["model"], "description": p["description"],
            "ibwave_type": IBWAVE_TYPE[kind], "unit": "ea", "source": p["source"]}


def target_interface(settings):
    """Connector interface of the chosen 1/2 in pair. Every tapper, coupler and
    omni in the same grade and brand is preferred to match it."""
    return settings["grades"]["pairs"][_pair_key(settings)]["connector_interface"]


def _prefer_interface(comp, target):
    """Parts whose stated interface matches the target when any do; otherwise
    the compliant set as it is (an adapter line is added later)."""
    match = [p for p in comp if p.get("interface") == target]
    return match or comp


def _select(kind, scope, how, ratio, settings, band_check, exact_only, target=None):
    """(part, ratio_used) or (None, reason)."""
    cfg = settings["grades"]
    pool = [p for p in cfg["candidates"][CANDIDATE_KEY[kind]] if _in_scope(p, kind, scope, cfg)]
    if not pool:
        return None, "no %s candidate in this family" % kind
    comp = [p for p in pool if _covers(p, settings["freqs"])] if band_check else pool
    if not comp:
        gap = _band_gap(pool, settings) if band_check else []
        detail = ", ".join(gap) if gap else "no candidate covers all project bands"
        return None, "no band-compliant %s (%s)" % (kind, detail)
    if target is not None and scope != "carrier":
        comp = _prefer_interface(comp, target)
    if ratio is None:
        return _pick(comp, how), None
    exact = [p for p in comp if p.get("ratio_db") == ratio]
    if exact:
        return _pick(exact, how), ratio
    if exact_only:
        return None, "no %s dB %s" % (_fmt(ratio), kind)
    offered = {p["ratio_db"] for p in comp if p.get("ratio_db") is not None}
    if not offered:
        return None, "no ratio stated for %s" % kind
    used = min(offered, key=lambda r: (abs(r - ratio), r))
    group = [p for p in comp if p.get("ratio_db") == used]
    return _pick(group, how), used


# Steps are (label, scope, pick rule, exact ratio only). Carrier is always the
# last resort; it is re-run without the band check when no step is compliant.
def _chain(grade, brand):
    if grade == "carrier":
        return [("carrier grade", "carrier", "max_jobs", False)]
    if grade == "enterprise":
        return [("enterprise grade", "all", "median", False),
                ("carrier grade", "carrier", "max_jobs", False)]
    if brand == "techwave":
        return [("budget Techwave", "techwave", "max_jobs", True),
                ("budget cheapest", "all", "cheapest", False),
                ("enterprise grade", "all", "median", False),
                ("carrier grade", "carrier", "max_jobs", False)]
    return [("budget cheapest", "all", "cheapest", False),
            ("enterprise grade", "all", "median", False),
            ("carrier grade", "carrier", "max_jobs", False)]


def _finish(kind, part, used, label, ratio, reasons, settings):
    pieces = list(reasons)
    if ratio is not None and used != ratio:
        pieces.append("modeled %s dB, part %s dB" % (_fmt(ratio), _fmt(used)))
    target = target_interface(settings)
    adapter = bool(part.get("interface")) and part["interface"] != target
    if adapter:
        pieces.append("no %s part fits, so %s (%s) needs an adapter to the %s pair"
                      % (target, _label(part), part["interface"], target))
    note = ""
    if pieces:
        note = "%s. Selected %s (%s)." % ("; ".join(pieces), _label(part), label)
    return {"part": _ibwave_part(kind, part), "ratio_used": used, "tier": label,
            "adapter": adapter, "notes": [note] if note else []}


def resolve_part(kind, ratio, settings):
    """Pick the part for one graded role and modeled tap ratio.

    Returns {part, ratio_used, tier, adapter, notes}. part is None when no
    candidate exists at all for the role."""
    reasons = []
    target = target_interface(settings)
    for label, scope, how, exact_only in _chain(settings["grade"], settings["budget_brand"]):
        part, used = _select(kind, scope, how, ratio, settings, True, exact_only, target)
        if part is not None:
            return _finish(kind, part, used, label, ratio, reasons, settings)
        reasons.append("%s: %s" % (label, used))
    part, used = _select(kind, "carrier", "max_jobs", ratio, settings, False, False)
    if part is None:
        return {"part": None, "ratio_used": None, "tier": None, "adapter": False,
                "notes": ["%s; no candidate for this role" % "; ".join(reasons)]}
    missing = ", ".join(_uncovered(part, settings)) or "none"
    reasons.append("no compliant part; carrier part kept, does not cover %s" % missing)
    return _finish(kind, part, used, "carrier grade", ratio, reasons, settings)


# ------------------------------------------------------------ connector and cable pair

def _pair_key(settings):
    if settings["grade"] == "carrier":
        return "carrier"
    if settings["grade"] == "enterprise":
        return "enterprise"
    return "budget_techwave" if settings["budget_brand"] == "techwave" else "budget_cheapest"


def resolve_pair(settings, catalog):
    """The 1/2 in cable and connector for this grade. They are chosen together
    so the connector always matches the cable family.

    Returns {key, cable, connector, notes}; cable and connector are ibwave parts
    or None."""
    g = settings["grades"]
    key = _pair_key(settings)
    spec = g["pairs"][key]
    notes = []

    if "catalog_role" in spec["cable"]:
        role = spec["cable"]["catalog_role"]
        cable = dict(catalog[role]) if role in catalog else None
    else:
        c = spec["cable"]
        cable = {"manufacturer": c["mfr"], "model": c["model"], "description": c["description"],
                 "ibwave_type": c["ibwave_type"], "unit": c["unit"], "source": c["source"]}
        if c["model"] == "":
            notes.append("cable model not on ANS file (no ANS standard yet)")
    if key == "budget_techwave":
        notes.append(spec.get("note", ""))

    if spec.get("connector_source") == "techwave_connectors":
        pool = [p for p in g["techwave_connectors"]]
        pick = pool[0] if pool else None
    else:
        pool = [p for p in g["candidates"]["connector_half_male"]
                if p.get("cable_family") == spec["connector_family"]
                and p.get("interface") == spec["connector_interface"]]
        pick = _pick(pool, spec["connector_pick"]) if pool else None
    connector = None
    if pick is not None:
        connector = {"manufacturer": pick["mfr"], "model": pick["model"],
                     "description": pick["description"], "ibwave_type": "Connector",
                     "unit": "ea", "source": pick["source"]}
    else:
        notes.append("no connector candidate for the %s cable family" % key)
    if cable is not None:
        cable.setdefault("ibwave_type", "Cable")
        cable.setdefault("unit", "ft")
    return {"key": key, "cable": cable, "connector": connector,
            "notes": [n for n in notes if n]}


# ------------------------------------------------------------ item rewrite

def _ratio_key(ratio):
    return "na" if ratio is None else _fmt(ratio)


def grade_items(items, config, settings, catalog=None):
    """Apply the grade to items that carry a grade_kind.

    Returns (items, overlay, notes). Each graded item is a copy with
    graded_role (the overlay key), grade_tier, ratio_used and note. Items with no
    part get a note and keep their catalog role. Metre rows are not graded."""
    cat = catalog or load_parts_catalog()
    cache = {}
    pair = None
    overlay = {}
    notes_all = []
    out = []
    for it in items:
        new = dict(it)
        kind = it.get("grade_kind")
        if not kind or it.get("unit") == "m":
            out.append(new)
            continue
        if kind in ("cable", "connector"):
            if pair is None:
                pair = resolve_pair(settings, cat)
                notes_all.extend(pair["notes"])
            part = pair[kind]
            if part is None:
                out.append(new)
                continue
            role_key = "graded.%s" % kind
            overlay[role_key] = part
            new["graded_role"] = role_key
            new["grade_tier"] = pair["key"]
            _add_note(new, pair["notes"])
            out.append(new)
            continue
        ratio = it.get("ratio_db") if kind in ("tapper", "coupler") else None
        ck = (kind, ratio)
        if ck not in cache:
            cache[ck] = resolve_part(kind, ratio, settings)
        res = cache[ck]
        if res["part"] is None:
            _add_note(new, res["notes"])
            notes_all.extend(res["notes"])
            out.append(new)
            continue
        role_key = "graded.%s.%s" % (kind, _ratio_key(ratio))
        overlay[role_key] = res["part"]
        new["graded_role"] = role_key
        new["grade_tier"] = res["tier"]
        if ratio is not None:
            new["ratio_used"] = res["ratio_used"]
        _add_note(new, res["notes"])
        notes_all.extend(res["notes"])
        out.append(new)
        if res.get("adapter"):
            out.append(_adapter_item(it, 1 if kind == "omni" else 2, target_interface(settings)))

    uniq = []
    for n in notes_all:
        if n and n not in uniq:
            uniq.append(n)
    return out, overlay, uniq


ADAPTER_ROLE = "adapter.n_to_4310"


def _adapter_item(src, ports, target):
    """An interface adapter line for a graded part that does not match the pair.
    One adapter per port the part uses (a tap or coupler has two ports, an omni
    one). The line keeps the source item's other keys (for example the floor)."""
    a = dict(src)
    for k in ("grade_kind", "ratio_db", "ratio_used", "graded_role", "grade_tier"):
        a.pop(k, None)
    a.update({
        "category": "adapter",
        "item": "Adapter, N to 4.3-10 (interface mismatch with the %s pair)" % target,
        "qty": int(round(float(src["qty"]))) * ports,
        "unit": "ea",
        "role": ADAPTER_ROLE,
        "note": "%d per %s, %d port(s): the part is %s and the pair is %s"
                % (ports, src.get("item", "part"), ports,
                   "4.3-10" if target == "N" else "N", target),
    })
    return a


def _add_note(item, notes):
    text = _join_note(item.get("note"), notes)
    if text:
        item["note"] = text


def _join_note(existing, notes):
    parts = [p for p in ([existing] if existing else []) + list(notes) if p]
    return " ".join(parts) if parts else None


def project_items(model, config, settings=None, catalog=None):
    """Element BOM lines plus stored builder lines, graded when settings is given.

    Returns (items, overlay, notes). Without settings the catalog roles are used
    unchanged (overlay is empty)."""
    cat = catalog or load_parts_catalog()
    raw = generate_bom(model, config)["items"] + builder_items(model, config, cat)
    if settings is None:
        return raw, {}, []
    return grade_items(raw, config, settings, cat)
