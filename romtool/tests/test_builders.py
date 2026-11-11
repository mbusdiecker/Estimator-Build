"""Parts catalog and assembly builders: catalog coverage, no price fields,
builder quantities for simple cases, rooftop donor coax, and the endpoints."""

import json
import math
import os
import re

import pytest

from romtool.builders import (BUILDER_NAMES, BuilderError, build_lines, builder_items,
                              load_parts_catalog)
from romtool.bom import generate_bom
from romtool.config import load_config
from romtool.estimate import power_plant_counts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_PATH = os.path.join(ROOT, "config", "parts_catalog.json")
PRICE_WORDS = re.compile(r"price|cost|usd|dollar|rate|amount|\$", re.I)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def cat():
    return load_parts_catalog()


def _model(*floors, builders=None):
    model = {"environment": "office", "floors": list(floors)}
    if builders is not None:
        model["builders"] = builders
    return model


def _floor(fid, elements, name=None, level=0):
    return {"id": fid, "name": name or fid, "level": level, "ppm": 10.0,
            "w_px": 100, "h_px": 100, "walls": [], "elements": elements}


def _by_role(lines):
    out = {}
    for ln in lines:
        out.setdefault(ln["role"], 0.0)
        out[ln["role"]] += ln["qty"]
    return out


# ------------------------------------------------------------ catalog

def test_catalog_covers_every_config_role(cfg, cat):
    """Each cable, splitter way, tapper, coupler, antenna and branch part in the
    config maps to a catalog role; the head-end OEM roles exist too."""
    needed = {"cable.%s" % cid for cid in cfg["cables"]}
    needed |= {"splitter.%s" % k for k in cfg["parts"]["splitters"]}
    needed |= {"tapper.%s" % k for k in cfg["parts"]["tappers"]}
    needed |= {"coupler.%s" % k for k in cfg["parts"]["couplers"]}
    needed |= {"antenna.%s" % k for k in cfg["antennas"]}
    needed |= {"connector.coax", "connector.fiber", "jumper.coax", "diplexer.das",
               "oem.source", "oem.repeater", "oem.remote"}
    missing = sorted(r for r in needed if r not in cat)
    assert missing == []


def test_catalog_covers_every_builder_role(cfg, cat):
    """Run each builder with a full set of inputs; every role it emits is in the catalog."""
    samples = {
        "rooftop": {"carriers": ["A", "B"], "mount": "wall"},
        "closet": {"racks": 2, "fiber_runs": 4, "ladder_ft": 10},
        "riser": {"floors_served": 3, "coax_runs": 1, "fiber_runs": 1, "shafts": 1},
        "gps": {"coax_ft": 50},
        "power": {},
        "monitoring": {"annunciators": 2},
    }
    model = _model(_floor("f1", [{"id": "s", "type": "source"},
                                 {"id": "r", "type": "repeater"}]))
    for name, inputs in samples.items():
        for ln in build_lines(name, inputs, cfg, model=model, catalog=cat):
            assert ln["role"] in cat, (name, ln["role"])


def test_catalog_units_and_shape(cat):
    for role, part in cat.items():
        assert part["unit"] in ("ea", "ft"), role
        assert part["ibwave_type"], role
        assert part["source"], role
        assert set(part) <= {"manufacturer", "model", "description", "ibwave_type",
                             "unit", "source", "note"}, role
        if part["source"].startswith("no ANS standard yet"):
            assert part["model"] == "", role
        else:
            assert re.match(r"ANS quote history, \d+ jobs?$", part["source"]), role
            assert part["model"], role


def test_catalog_has_no_price_or_cost_fields():
    with open(CATALOG_PATH, encoding="utf-8") as fh:
        raw = fh.read()
    data = json.loads(raw)

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                assert not PRICE_WORDS.search(k), "field %s%s" % (path, k)
                walk(v, path + k + ".")
        elif isinstance(node, list):
            for v in node:
                walk(v, path)
        elif isinstance(node, str):
            assert "$" not in node, path

    walk(data, "")
    for role, part in data["roles"].items():
        assert not PRICE_WORDS.search(" ".join(part.keys())), role


def test_builders_config_rules_are_graded(cfg):
    block = cfg["builders"]
    for name in BUILDER_NAMES:
        if name == "power":
            assert "runtime_hours" in block[name]
            continue
        assert name in block, name
    for name, rules in block.items():
        if name == "power" or not isinstance(rules, dict):
            continue
        for key, rule in rules.items():
            assert rule["grade"] in ("E", "C"), (name, key)
            assert rule["ref"], (name, key)
            assert "value" in rule, (name, key)


# ------------------------------------------------------------ rooftop

def test_rooftop_donor_coax_is_150_ft_outdoor_plus_150_ft_plenum_per_carrier(cfg, cat):
    lines = build_lines("rooftop", {"carriers": ["Carrier A", "Carrier B"]}, cfg, catalog=cat)
    by = _by_role(lines)
    assert by["antenna.donor"] == 2
    assert by["cable.coax_1_2in_outdoor"] == pytest.approx(300.0)
    assert by["cable.coax_1_2in_plenum"] == pytest.approx(300.0)
    assert by["connector.coax"] == 8
    assert by["jumper.coax"] == 4
    assert by["surge.coax"] == 2
    assert by["grounding.kit"] == 2
    assert by["weatherproof.kit"] == 4
    assert by["mount.roof"] == 2
    assert by["roof.entry"] == 1


def test_rooftop_one_carrier_one_donor_line(cfg, cat):
    by = _by_role(build_lines("rooftop", {"carriers": ["X"]}, cfg, catalog=cat))
    assert by["cable.coax_1_2in_outdoor"] == pytest.approx(150.0)
    assert by["cable.coax_1_2in_plenum"] == pytest.approx(150.0)


def test_rooftop_donor_count_and_wall_mount(cfg, cat):
    by = _by_role(build_lines("rooftop", {"donor_count": 3, "mount": "wall"}, cfg, catalog=cat))
    assert by["antenna.donor"] == 3
    assert by["mount.wall"] == 3
    assert "mount.roof" not in by
    assert by["cable.coax_1_2in_plenum"] == pytest.approx(450.0)


def test_rooftop_needs_carriers_or_count(cfg, cat):
    with pytest.raises(BuilderError):
        build_lines("rooftop", {}, cfg, catalog=cat)
    with pytest.raises(BuilderError):
        build_lines("rooftop", {"carriers": ["A"], "mount": "tower"}, cfg, catalog=cat)


# ------------------------------------------------------------ closet

def test_closet_quantities_for_fiber_and_racks(cfg, cat):
    model = _model(_floor("f1", [{"id": "s", "type": "source"},
                                 {"id": "r", "type": "repeater"}]))
    by = _by_role(build_lines("closet", {"racks": 2, "fiber_runs": 5, "ladder_ft": 12},
                              cfg, model=model, catalog=cat))
    assert by["rack.two_post"] == 2
    assert by["rack.backboard"] == 2
    assert by["pdu.default"] == 2
    assert by["rack.ladder"] == pytest.approx(12.0)
    assert by["ground.bar"] == 1
    assert by["ground.cable"] == pytest.approx(20.0)
    assert by["fiber.splice_cassette"] == 5
    assert by["fiber.panel"] == math.ceil(5 / 3)
    assert by["fiber.housing"] == 1
    assert by["jumper.fiber"] == 10
    assert by["label.cable"] == 5 * 2 + 2
    assert by["rack.shelf"] == 2


def test_closet_without_fiber_or_head_end(cfg, cat):
    by = _by_role(build_lines("closet", {}, cfg, catalog=cat))
    assert by["rack.two_post"] == 1
    assert "fiber.panel" not in by
    assert "fiber.housing" not in by
    assert "rack.shelf" not in by


# ------------------------------------------------------------ riser

def test_riser_footage_penetrations_and_jhooks(cfg, cat):
    # 3 floors x 4 m + 3 m slack = 15 m per run = 49.2 ft
    by = _by_role(build_lines("riser", {"floors_served": 3, "coax_runs": 2, "fiber_runs": 1,
                                        "coax_cable_id": "coax_1_2in"}, cfg, catalog=cat))
    run_ft = round(15.0 / 0.3048, 1)
    assert by["cable.coax_1_2in"] == pytest.approx(round(2 * 15.0 / 0.3048, 1))
    assert by["cable.fiber_sm"] == pytest.approx(run_ft)
    assert by["sleeve.penetration"] == 2
    assert by["firestop.penetration"] == 2
    assert by["jhook.default"] == math.ceil(15.0 / 1.5) * 3


def test_riser_needs_two_floors(cfg, cat):
    with pytest.raises(BuilderError):
        build_lines("riser", {"floors_served": 1}, cfg, catalog=cat)


# ------------------------------------------------------------ gps

def test_gps_lines(cfg, cat):
    by = _by_role(build_lines("gps", {"coax_ft": 100}, cfg, catalog=cat))
    assert by["antenna.gps"] == 1
    assert by["mount.gps"] == 1
    assert by["cable.coax_1_2in_plenum"] == pytest.approx(100.0)
    assert by["surge.coax"] == 1
    assert by["connector.coax"] == 2


# ------------------------------------------------------------ power

def test_power_matches_o_zone_sizing(cfg, cat):
    est = json.load(open(os.path.join(ROOT, "config", "estimating.json"), encoding="utf-8"))
    spec = est["power_plant"]
    # 12 h x 30 A = 360 Ah -> 3 strings of 120 Ah; rectifiers ceil(30/50)+1 = 2
    by = _by_role(build_lines("power", {"load_amps": 30}, cfg, catalog=cat))
    assert by["battery.string"] == 3
    assert by["charger.rectifier"] == 2
    sizing = power_plant_counts(30, 12, spec["battery_string_ah"], spec["rectifier_size_amps"])
    assert sizing["strings"] == 3 and sizing["rectifiers"] == 2


def test_power_default_load_from_rflink(cfg, cat):
    # rflink booster_load is 10 A: 12 h x 10 A = 120 Ah -> 1 string; ceil(10/50)+1 = 2
    by = _by_role(build_lines("power", {}, cfg, catalog=cat))
    assert by["battery.string"] == 1
    assert by["charger.rectifier"] == 2


# ------------------------------------------------------------ monitoring

def test_monitoring_lines(cfg, cat):
    by = _by_role(build_lines("monitoring", {"annunciators": 2}, cfg, catalog=cat))
    assert by["annunciator.panel"] == 2
    assert by["alarm.wiring"] == pytest.approx(200.0)


# ------------------------------------------------------------ inputs and project BOM

def test_bad_inputs_raise(cfg, cat):
    with pytest.raises(BuilderError):
        build_lines("closet", {"racks": -1}, cfg, catalog=cat)
    with pytest.raises(BuilderError):
        build_lines("closet", {"racks": 1.5}, cfg, catalog=cat)
    with pytest.raises(BuilderError):
        build_lines("nope", {}, cfg, catalog=cat)
    with pytest.raises(BuilderError):
        build_lines("gps", ["not", "an", "object"], cfg, catalog=cat)


def test_builder_items_join_the_project_bom(cfg, cat):
    model = _model(_floor("f1", [{"id": "a", "type": "antenna", "antenna_id": "omni_das"}]),
                   builders={"gps": {"coax_ft": 40}})
    element_items = generate_bom(model, cfg)["items"]
    items = builder_items(model, cfg, cat)
    assert element_items[0]["category"] == "antenna"
    assert {it["category"] for it in items} == {"builder: gps"}
    assert all(it["unit"] in ("ea", "ft") for it in items)
    assert all("role" in it and it["item"] for it in items)


def test_builder_items_empty_without_stored_inputs(cfg, cat):
    model = _model(_floor("f1", []))
    assert builder_items(model, cfg, cat) == []
