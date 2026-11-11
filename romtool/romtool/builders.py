"""Assembly builders: BOM lines for the parts a site needs beyond the cascade.

Each builder takes simple inputs and returns deterministic BOM lines. A line is
{"role", "qty", "unit", "note"}: the role resolves to a part through
config/parts_catalog.json, the unit comes from that part, and every quantity
rule comes from the "builders" block in config/default.json (each rule carries
its grade and reference). Nothing here carries a price or cost.

Builders: rooftop (donor antennas and coax), closet (2-post rack, backboard,
ladder rack, ground, PDU, fiber panel and cassettes, jumpers, labels, head-end
shelves), riser (vertical coax and fiber footage, penetrations, J-hooks),
gps (antenna, mount, coax run, surge arrestor), power (O-zone battery strings
and rectifiers for a public safety plant), monitoring (annunciator and alarm
wiring).

The head-end units themselves (source, repeater, remote) are not repeated here;
they come from the element BOM lines, so each unit is counted once.
"""

import json
import math
import os

from romtool.estimate import load_estimating_config, power_plant_counts
from romtool.rflink import load_rflink_config

M_PER_FT = 0.3048

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CATALOG_PATH = os.path.join(_REPO_ROOT, "config", "parts_catalog.json")

BUILDER_NAMES = ("rooftop", "closet", "riser", "gps", "power", "monitoring")
HEAD_END_TYPES = ("source", "repeater", "remote")


class BuilderError(ValueError):
    """Bad builder input, a missing config rule, or a role with no catalog part."""


def load_parts_catalog(path=None):
    """Role id -> part dict (manufacturer, model, description, ibwave_type, unit, source)."""
    with open(path or DEFAULT_CATALOG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)["roles"]


class _Rules:
    """Reads one builders.<name> block; each rule is {value, grade, ref}."""

    def __init__(self, config, name):
        block = (config.get("builders") or {}).get(name)
        if block is None:
            raise BuilderError("config has no builders.%s block" % name)
        self._name = name
        self._block = block

    def __call__(self, key):
        rule = self._block.get(key)
        if not isinstance(rule, dict) or "value" not in rule:
            raise BuilderError("builders.%s.%s is missing" % (self._name, key))
        return rule["value"]


def _whole(inputs, key, default, minimum=0):
    raw = inputs.get(key)
    if raw is None or raw == "":
        raw = default
    if raw is None:
        raise BuilderError("%s is required" % key)
    if isinstance(raw, bool):
        raise BuilderError("%s must be a whole number" % key)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise BuilderError("%s must be a whole number, got %r" % (key, raw))
    if value != int(value):
        raise BuilderError("%s must be a whole number, got %r" % (key, raw))
    if int(value) < minimum:
        raise BuilderError("%s must be at least %d" % (key, minimum))
    return int(value)


def _amount(inputs, key, default, minimum=0.0):
    raw = inputs.get(key)
    if raw is None or raw == "":
        raw = default
    if raw is None:
        raise BuilderError("%s is required" % key)
    if isinstance(raw, bool):
        raise BuilderError("%s must be a number" % key)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise BuilderError("%s must be a number, got %r" % (key, raw))
    if value < minimum:
        raise BuilderError("%s must be at least %g" % (key, minimum))
    return value


def _ceil_div(n, d):
    return int(math.ceil(n / float(d))) if n > 0 else 0


class _Ctx:
    """What a builder may read beyond its own inputs."""

    def __init__(self, config, catalog, head_end_units):
        self.config = config
        self.catalog = catalog
        self.head_end_units = head_end_units

    def part(self, role):
        if role not in self.catalog:
            raise BuilderError("no catalog part for role %r; add it to config/parts_catalog.json"
                               % role)
        return self.catalog[role]


def _lines_out(ctx, raw):
    """raw: list of (role, qty, note[, grade_kind]). Adds the unit from the
    catalog, rounds feet to 0.1 ft, and drops zero-quantity lines. grade_kind
    ("cable" or "connector") marks lines the grade selection may replace."""
    out = []
    for entry in raw:
        role, qty, note = entry[0], entry[1], entry[2]
        kind = entry[3] if len(entry) > 3 else None
        unit = ctx.part(role)["unit"]
        if unit == "ft":
            qty = round(float(qty), 1)
        else:
            qty = int(round(float(qty)))
        if qty:
            line = {"role": role, "qty": qty, "unit": unit, "note": note}
            if kind:
                line["grade_kind"] = kind
            out.append(line)
    return out


# ------------------------------------------------------------------ builders

def rooftop(inputs, rules, ctx):
    carriers = inputs.get("carriers") or []
    if not isinstance(carriers, list) or not all(isinstance(c, str) for c in carriers):
        raise BuilderError("carriers must be a list of carrier names")
    mount = inputs.get("mount") or "roof"
    if mount not in ("roof", "wall"):
        raise BuilderError("mount must be 'roof' or 'wall'")
    if inputs.get("donor_count") not in (None, ""):
        donors = _whole(inputs, "donor_count", None, 0)
        basis = "donor_count"
    elif carriers:
        donors = len(carriers) * rules("donors_per_carrier")
        basis = "per carrier (%s)" % ", ".join(carriers)
    else:
        raise BuilderError("rooftop needs carriers or donor_count")
    outdoor_ft = rules("donor_outdoor_ft_per_carrier") * donors
    plenum_ft = rules("donor_plenum_ft_per_carrier") * donors
    per = "per donor line, %s" % basis
    mount_role = "mount.roof" if mount == "roof" else "mount.wall"
    raw = [
        ("antenna.donor", donors, per),
        ("mount.donor", donors, per),
        (mount_role, donors, "%s base, %s" % (mount, per)),
        ("roof.entry", rules("roof_entries"), "rooftop cable entry"),
        ("surge.coax", donors * rules("surge_per_donor_line"), "building entry, " + per),
        ("grounding.kit", donors * rules("grounding_kits_per_donor_line"),
         "outdoor coax run, " + per),
        ("weatherproof.kit", donors * rules("weatherproof_kits_per_donor_line"),
         "outdoor connections, " + per),
        ("cable.coax_1_2in_outdoor", outdoor_ft,
         "%g ft outdoor-rated 1/2 in per donor line, %s"
         % (rules("donor_outdoor_ft_per_carrier"), basis)),
        ("cable.coax_1_2in_plenum", plenum_ft,
         "%g ft indoor plenum 1/2 in per donor line, %s"
         % (rules("donor_plenum_ft_per_carrier"), basis), "cable"),
        # The connector rule counts both cable legs. The outdoor leg keeps the
        # catalog connector; the plenum leg follows the grade's 1/2 in pair.
        ("connector.coax", donors * (rules("connectors_per_donor_line") // 2),
         per + ", outdoor leg"),
        ("connector.coax",
         donors * (rules("connectors_per_donor_line") - rules("connectors_per_donor_line") // 2),
         per + ", plenum leg", "connector"),
        ("jumper.coax", donors * rules("jumpers_per_donor_line"), per),
    ]
    return _lines_out(ctx, raw)


def closet(inputs, rules, ctx):
    racks = _whole(inputs, "racks", rules("racks"), 0)
    fiber_runs = _whole(inputs, "fiber_runs", 0, 0)
    ladder_ft = _amount(inputs, "ladder_ft", 0.0)
    ground_cable_ft = _amount(inputs, "ground_cable_ft", rules("ground_cable_ft"))
    head = ctx.head_end_units
    cassettes = fiber_runs * rules("cassettes_per_fiber_run")
    panels = _ceil_div(cassettes, rules("cassettes_per_panel"))
    raw = [
        ("rack.two_post", racks, "head-end rack"),
        ("rack.backboard", racks * rules("backboards_per_rack"), "per rack"),
        ("rack.ladder", ladder_ft, "cable ladder rack, from the request"),
        ("ground.bar", rules("ground_bars"), "closet ground"),
        ("ground.cable", ground_cable_ft, "ground bar to building ground"),
        ("pdu.default", racks * rules("pdus_per_rack"), "per rack"),
        ("fiber.panel", panels, "%d cassettes per panel, %d fiber run(s)"
         % (rules("cassettes_per_panel"), fiber_runs)),
        ("fiber.splice_cassette", cassettes, "one per fiber run"),
        ("fiber.housing", rules("housings_per_closet") if fiber_runs else 0,
         "closet fiber housing"),
        ("jumper.fiber", fiber_runs * rules("fiber_jumpers_per_fiber_run"),
         "one per fiber end"),
        ("label.cable", fiber_runs * rules("labels_per_fiber_run")
         + head * rules("labels_per_head_end_unit"), "fiber ends and head-end units"),
        ("rack.shelf", head * rules("rack_shelves_per_head_end_unit"),
         "one per head-end unit in the element BOM (%d)" % head),
    ]
    return _lines_out(ctx, raw)


def riser(inputs, rules, ctx):
    floors = _whole(inputs, "floors_served", None, 2)
    shafts = _whole(inputs, "shafts", 1, 1)
    coax_runs = _whole(inputs, "coax_runs", 1, 0)
    fiber_runs = _whole(inputs, "fiber_runs", 0, 0)
    ap = ctx.config.get("autoplace") or {}
    coax_id = inputs.get("coax_cable_id") or ap.get("trunk_cable_id", "coax_7_8in")
    floor_h = _amount(inputs, "floor_height_m", ap.get("floor_height_m", 4.0), 0.0)
    slack_m = float(ap.get("slack_m_per_run", 3.0))
    run_m = floor_h * floors + slack_m
    run_ft = run_m / M_PER_FT
    transitions = floors - 1
    penetrations = shafts * transitions
    jhooks = _ceil_div(run_m, rules("jhook_spacing_m")) * (coax_runs + fiber_runs)
    note = "%d floors at %g m, %g m slack per run (%d transitions)" % (
        floors, floor_h, slack_m, transitions)
    raw = [
        ("cable.%s" % coax_id, coax_runs * run_ft, "%d vertical run(s), %s" % (coax_runs, note),
         "cable" if coax_id == "coax_1_2in" else None),
        ("cable.fiber_sm", fiber_runs * run_ft, "%d vertical fiber run(s), %s"
         % (fiber_runs, note)),
        ("sleeve.penetration", penetrations * rules("sleeves_per_penetration"),
         "%d shaft(s), %d transitions" % (shafts, transitions)),
        ("firestop.penetration", penetrations * rules("firestop_per_penetration"),
         "%d shaft(s), %d transitions" % (shafts, transitions)),
        ("jhook.default", jhooks, "vertical runs at %g m spacing" % rules("jhook_spacing_m")),
    ]
    return _lines_out(ctx, raw)


def gps(inputs, rules, ctx):
    coax_ft = _amount(inputs, "coax_ft", 0.0)
    raw = [
        ("antenna.gps", 1, "GPS antenna"),
        ("mount.gps", 1, "GPS rooftop mount"),
        ("cable.coax_1_2in_plenum", coax_ft, "GPS coax run, 1/2 in plenum assumed", "cable"),
        ("surge.coax", rules("surge_per_gps"), "GPS coax entry"),
        ("connector.coax", rules("connectors_per_gps"), "GPS coax ends", "connector"),
    ]
    return _lines_out(ctx, raw)


def power(inputs, rules, ctx):
    rflink = load_rflink_config()
    est = load_estimating_config()
    spec = est["power_plant"]
    load = _amount(inputs, "load_amps", (rflink.get("booster_load") or {}).get("load_amps"))
    runtime = _amount(inputs, "runtime_hours", rules("runtime_hours"))
    string_ah = _amount(inputs, "string_ah", spec["battery_string_ah"])
    sizing = power_plant_counts(load, runtime, string_ah, spec["rectifier_size_amps"])
    basis = "%.1f A x %.1f h = %.1f Ah, %g Ah strings" % (
        load, runtime, sizing["ah"], string_ah)
    raw = [
        ("battery.string", sizing["strings"], basis),
        ("charger.rectifier", sizing["rectifiers"], sizing["rectifier_note"]),
    ]
    return _lines_out(ctx, raw)


def monitoring(inputs, rules, ctx):
    n = _whole(inputs, "annunciators", rules("annunciators"), 0)
    raw = [
        ("annunciator.panel", n, "public safety annunciator"),
        ("alarm.wiring", n * rules("alarm_wiring_ft_per_annunciator"),
         "alarm and supervision wiring per annunciator"),
    ]
    return _lines_out(ctx, raw)


BUILDERS = {"rooftop": rooftop, "closet": closet, "riser": riser,
            "gps": gps, "power": power, "monitoring": monitoring}


def head_end_count(model):
    """Source, repeater and remote elements across the project (autoplaced or drawn)."""
    n = 0
    for fl in (model or {}).get("floors", []):
        for el in fl.get("elements", []):
            if el.get("type") in HEAD_END_TYPES:
                n += 1
    return n


def build_lines(name, inputs, config, model=None, catalog=None):
    """Validate inputs and return the builder's BOM lines for this project."""
    if name not in BUILDERS:
        raise BuilderError("unknown builder '%s' (known: %s)"
                           % (name, ", ".join(BUILDER_NAMES)))
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict):
        raise BuilderError("builder inputs must be a JSON object")
    cat = catalog or load_parts_catalog()
    ctx = _Ctx(config, cat, head_end_count(model))
    return BUILDERS[name](dict(inputs), _Rules(config, name), ctx)


def builder_items(model, config, catalog=None):
    """Lines for every builder stored on the project, shaped as BOM items.

    Builder lines have no floor; the iBwave export files them under Site."""
    stored = (model or {}).get("builders") or {}
    cat = catalog or load_parts_catalog()
    items = []
    for name in BUILDER_NAMES:
        if name not in stored:
            continue
        for ln in build_lines(name, stored[name], config, model=model, catalog=cat):
            part = cat[ln["role"]]
            item = {
                "category": "builder: %s" % name,
                "item": part["description"],
                "qty": ln["qty"],
                "unit": ln["unit"],
                "role": ln["role"],
                "note": ln["note"],
            }
            if ln.get("grade_kind"):
                item["grade_kind"] = ln["grade_kind"]
            items.append(item)
    return items
