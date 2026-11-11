"""Grades and budget brands: tier picks for a cellular job, fallbacks with notes,
connector and cable pairing per tier, ratio handling, the grades config (no
cost words), and the API parameters."""

import copy
import csv
import io
import json
import os
import re

import pytest

from romtool.bom import generate_bom
from romtool.builders import build_lines, load_parts_catalog
from romtool.config import load_config
from romtool.grades import (GradeError, check_bands, grade_items, load_grades,
                            project_items, resolve_pair, resolve_part, settings_for)
from romtool.ibwave import ibwave_table

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRADES_PATH = os.path.join(ROOT, "config", "grades.json")
PRICE_WORDS = re.compile(r"price|cost|usd|dollar|rate|amount|\$", re.I)

CELL = ["B12_700", "B5_850", "B2_1900", "B66_2100"]   # 751 to 2145 MHz
CELL_N77 = CELL + ["N77_CBAND"]                        # adds 3840 MHz
CELL_N71 = CELL + ["N71_600"]                          # adds 634.5 MHz


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def cat():
    return load_parts_catalog()


def _settings(cfg, bands=CELL, grade=None, brand=None):
    return settings_for({"bands": bands}, cfg, grade=grade, budget_brand=brand)


def _name(part):
    return part["manufacturer"] + " " + part["model"]


def _floor(fid, elements):
    return {"id": fid, "name": fid, "level": 0, "ppm": 10.0, "w_px": 100, "h_px": 100,
            "walls": [], "elements": elements}


# ------------------------------------------------------------ settings

def test_defaults_are_carrier_techwave_all_bands(cfg):
    s = settings_for({}, cfg)
    assert s["grade"] == "carrier"
    assert s["budget_brand"] == "techwave"
    assert s["bands"] == [b["id"] for b in cfg["bands"]]


def test_bad_grade_brand_and_band_raise(cfg):
    with pytest.raises(GradeError):
        settings_for({}, cfg, grade="platinum")
    with pytest.raises(GradeError):
        settings_for({}, cfg, budget_brand="acme")
    with pytest.raises(GradeError):
        check_bands(["NOT_A_BAND"], cfg)


def test_request_values_override_project_values(cfg):
    s = settings_for({"grade": "carrier", "budget_brand": "techwave"}, cfg,
                     grade="budget", budget_brand="cheapest")
    assert (s["grade"], s["budget_brand"]) == ("budget", "cheapest")


# ------------------------------------------------------------ tier picks, cellular job

def test_carrier_picks_the_carrier_family(cfg):
    s = _settings(cfg, grade="carrier")
    assert _name(resolve_part("tapper", 12.0, s)["part"]) == "Microlab/FXR DN-84FE"
    assert _name(resolve_part("tapper", 3.0, s)["part"]) == "Microlab/FXR DN-34FE"
    assert _name(resolve_part("coupler", 10.0, s)["part"]) == "Microlab/FXR CC-610E"
    assert _name(resolve_part("omni", None, s)["part"]) == "CommScope CMAX-O-43-UW-i53"


def test_enterprise_takes_the_median_compliant_part(cfg):
    s = _settings(cfg, grade="enterprise")
    # 13 dB compliant: ST-13-60 (rank 12) and DN-84FE (rank 19); lower median is rank 12.
    assert _name(resolve_part("tapper", 12.0, s)["part"]) == "Sym Technology ST-13-60"
    # 3 dB compliant ranks 5, 16, 17, 26, 33; median is rank 17.
    assert _name(resolve_part("tapper", 3.0, s)["part"]) == "Microlab/FXR DN-34FE"
    assert _name(resolve_part("omni", None, s)["part"]) == "CommScope CMAX-O-43-UW i53"


def test_budget_cheapest_takes_the_lowest_rank(cfg):
    s = _settings(cfg, grade="budget", brand="cheapest")
    assert _name(resolve_part("tapper", 3.0, s)["part"]) == "Westell CS05-494-114 (PT3/340-2.7K/N)"
    assert _name(resolve_part("omni", None, s)["part"]) == \
        "Techwave TC AI698-2700V05i360B (PS)"


def test_budget_techwave_uses_techwave_parts_where_they_exist(cfg):
    s = _settings(cfg, grade="budget", brand="techwave")
    assert _name(resolve_part("coupler", 10.0, s)["part"]) == "Techwave TC-PCCoM-10DI"
    assert _name(resolve_part("omni", None, s)["part"]) == \
        "Techwave TC AI698-2700V05i360B"


# ------------------------------------------------------------ fallbacks with notes

def test_n77_makes_budget_techwave_omni_fall_back_with_note(cfg):
    s = _settings(cfg, bands=CELL_N77, grade="budget", brand="techwave")
    res = resolve_part("omni", None, s)
    assert _name(res["part"]) == "JMA Wireless iV01OMI136-U4"
    assert res["tier"] == "budget cheapest"
    assert res["notes"] and "budget Techwave" in res["notes"][0]
    assert "N77_CBAND" in res["notes"][0]


def test_600_mhz_band_also_falls_back_for_techwave_omni(cfg):
    s = _settings(cfg, bands=CELL_N71, grade="budget", brand="techwave")
    res = resolve_part("omni", None, s)
    assert "TC AI698" not in _name(res["part"])
    assert "N71_600" in res["notes"][0]


def test_missing_tap_ratio_falls_back_to_nearest_cheapest(cfg):
    s = _settings(cfg, grade="budget", brand="techwave")
    res = resolve_part("coupler", 12.0, s)   # Techwave offers 6, 10 and 15 dB only
    assert _name(res["part"]) == "Microlab/FXR CC-613E"
    assert res["ratio_used"] == 13.0
    assert "no 12 dB coupler" in res["notes"][0]
    assert "modeled 12 dB, part 13 dB" in res["notes"][0]


def test_techwave_has_no_tapper_so_the_role_falls_back(cfg):
    s = _settings(cfg, grade="budget", brand="techwave")
    res = resolve_part("tapper", 3.0, s)
    assert res["tier"] == "budget cheapest"
    assert "no tapper candidate" in res["notes"][0]


def test_carrier_keeps_its_part_and_says_when_bands_are_not_covered(cfg):
    s = _settings(cfg, bands=CELL + ["VHF_150"], grade="carrier")
    res = resolve_part("tapper", 3.0, s)
    assert res["part"] is not None
    assert "VHF_150" in res["notes"][0]


# ------------------------------------------------------------ connector and cable pair

PAIR_KEYS = {
    ("carrier", "techwave"): "carrier",
    ("enterprise", "techwave"): "enterprise",
    ("budget", "cheapest"): "budget_cheapest",
    ("budget", "techwave"): "budget_techwave",
}


@pytest.mark.parametrize("grade,brand", sorted(PAIR_KEYS))
def test_connector_matches_cable_family_in_every_tier(cfg, cat, grade, brand):
    g = load_grades()
    s = _settings(cfg, grade=grade, brand=brand)
    pair = resolve_pair(s, cat)
    assert pair["key"] == PAIR_KEYS[(grade, brand)]
    spec = g["pairs"][pair["key"]]
    conn = pair["connector"]
    assert conn is not None
    if "connector_family" in spec:
        cand = [p for p in g["candidates"]["connector_half_male"]
                if p["mfr"] == conn["manufacturer"] and p["model"] == conn["model"]]
        assert cand and cand[0]["cable_family"] == spec["connector_family"]
        assert cand[0]["interface"] == spec["connector_interface"]
    else:
        assert conn["manufacturer"] == "Techwave" and conn["model"].startswith("TC-CN-N")


def test_carrier_pair_is_the_catalog_cable_and_jma_connector(cfg, cat):
    pair = resolve_pair(_settings(cfg, grade="carrier"), cat)
    assert pair["cable"]["model"] == "HL4RPV-50"
    assert _name(pair["connector"]) == "JMA Wireless UPL-4MT-12"


def test_enterprise_and_cheapest_use_the_lcf12_family(cfg, cat):
    ent = resolve_pair(_settings(cfg, grade="enterprise"), cat)
    assert _name(ent["connector"]) == "Windy City Wire WCW-43M-LCF12-D01"
    assert ent["cable"]["model"] == "" and ent["cable"]["source"] == "no ANS standard yet"
    cheap = resolve_pair(_settings(cfg, grade="budget", brand="cheapest"), cat)
    assert _name(cheap["connector"]) == "RFS 43M-LCF12-E01"
    assert cheap["cable"]["model"] == ""


def test_budget_techwave_pair_asks_for_confirmation(cfg, cat):
    pair = resolve_pair(_settings(cfg, grade="budget", brand="techwave"), cat)
    assert pair["connector"]["manufacturer"] == "Techwave"
    assert any("confirm" in n for n in pair["notes"])


# ------------------------------------------------------------ item rewrite

def test_grade_items_keeps_modeled_ratio_and_reports_part_ratio(cfg, cat):
    s = _settings(cfg, grade="carrier")
    item = {"category": "tapper", "item": "Tap", "qty": 2, "unit": "ea",
            "role": "tapper.default", "ratio_db": 12.0, "grade_kind": "tapper"}
    out, overlay, notes = grade_items([item], cfg, s, cat)
    assert item.get("graded_role") is None          # input not mutated
    assert out[0]["ratio_db"] == 12.0
    assert out[0]["ratio_used"] == 13.0
    assert out[0]["graded_role"] == "graded.tapper.12"
    assert overlay["graded.tapper.12"]["model"] == "DN-84FE"
    assert notes and "modeled 12 dB" in notes[0]


def test_metre_rows_and_ungraded_lines_are_untouched(cfg, cat):
    s = _settings(cfg, grade="enterprise")
    items = [{"category": "cable", "item": "c", "qty": 3.0, "unit": "m",
              "role": "cable.coax_1_2in", "grade_kind": "cable"},
             {"category": "splitter", "item": "s", "qty": 1, "unit": "ea",
              "role": "splitter.2way"}]
    out, overlay, _ = grade_items(items, cfg, s, cat)
    assert out == items and overlay == {}


def test_generate_bom_splits_tappers_by_element_loss(cfg):
    model = {"environment": "office", "floors": [_floor("f1", [
        {"id": "t1", "type": "tapper", "part_id": "default"},
        {"id": "t2", "type": "tapper", "part_id": "default", "tap_loss_db": 6.0},
    ])]}
    tappers = [it for it in generate_bom(model, cfg)["items"] if it["category"] == "tapper"]
    assert sorted((it["ratio_db"], it["qty"]) for it in tappers) == [(6.0, 1), (12.0, 1)]


def test_rooftop_connectors_split_into_outdoor_and_plenum_legs(cfg, cat):
    lines = build_lines("rooftop", {"carriers": ["A"]}, cfg, catalog=cat)
    conn = [ln for ln in lines if ln["role"] == "connector.coax"]
    assert sum(ln["qty"] for ln in conn) == 4
    assert [ln.get("grade_kind") for ln in conn] == [None, "connector"]
    cable = [ln for ln in lines if ln["role"] == "cable.coax_1_2in_plenum"]
    assert cable[0]["grade_kind"] == "cable"
    outdoor = [ln for ln in lines if ln["role"] == "cable.coax_1_2in_outdoor"]
    assert outdoor[0].get("grade_kind") is None


def test_ibwave_table_enterprise_has_lcf12_pair(cfg, cat):
    model = {"environment": "office", "floors": [_floor("f1", [
        {"id": "r1", "type": "cable_run", "cable_id": "coax_1_2in", "length_m": 10.0},
        {"id": "a1", "type": "antenna", "antenna_id": "omni_das"},
    ])]}
    header, rows = ibwave_table(model, cfg, catalog=cat,
                                settings=_settings(cfg, grade="enterprise"))
    cable = [r for r in rows if r[0] == "Cable"]
    assert len(cable) == 1 and cable[0][1] == "RFS" and cable[0][3] == ""
    conn = [r for r in rows if r[0] == "Connector"]
    assert conn and conn[0][3] == "WCW-43M-LCF12-D01"
    ant = [r for r in rows if r[0] == "Antenna"]
    assert ant and ant[0][3] == "CMAX-O-43-UW i53"


def test_ibwave_table_without_settings_is_unchanged(cfg, cat):
    model = {"environment": "office", "floors": [_floor("f1", [
        {"id": "r1", "type": "cable_run", "cable_id": "coax_1_2in", "length_m": 10.0},
        {"id": "a1", "type": "antenna", "antenna_id": "omni_das"},
    ])]}
    _header, rows = ibwave_table(model, cfg, catalog=cat)
    assert [r[3] for r in rows if r[0] == "Cable"] == ["HL4RPV-50"]
    assert [r[3] for r in rows if r[0] == "Antenna"] == ["CMAX-O-43-UW-i53"]


def test_project_items_grade_the_bom(cfg):
    model = {"environment": "office", "floors": [_floor("f1", [
        {"id": "t1", "type": "tapper", "part_id": "default"},
        {"id": "a1", "type": "antenna", "antenna_id": "omni_das"},
    ])]}
    items, _overlay, _notes = project_items(model, cfg, _settings(cfg, grade="carrier"))
    tap = [it for it in items if it["category"] == "tapper"][0]
    assert tap["graded_role"] == "graded.tapper.12"


# ------------------------------------------------------------ config

def test_grades_config_has_no_cost_words():
    with open(GRADES_PATH, encoding="utf-8") as fh:
        data = json.load(fh)

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                assert not PRICE_WORDS.search(k), "key %s%s" % (path, k)
                walk(v, path + k + ".")
        elif isinstance(node, list):
            for v in node:
                walk(v, path)
        elif isinstance(node, str):
            assert not PRICE_WORDS.search(node), "value at %s: %r" % (path, node)

    walk(copy.deepcopy(data), "")


def test_every_candidate_has_a_source_and_rank(cfg):
    g = load_grades()
    for kind, rows in g["candidates"].items():
        for p in rows:
            assert p["source"].startswith("ANS quote history, "), (kind, p["model"])
            assert p["budget_rank"] is not None, (kind, p["model"])


# ------------------------------------------------------------ endpoints

def _new_project_with_gps(client):
    pid = client.post("/projects", json={"name": "grades test"}).json()["id"]
    r = client.post("/projects/%d/builders/gps" % pid, json={"coax_ft": 60})
    assert r.status_code == 200, r.text
    return pid


def test_bom_endpoint_uses_project_grade_and_query_overrides(client):
    pid = _new_project_with_gps(client)
    body = client.get("/projects/%d/bom" % pid).json()
    assert (body["grade"], body["budget_brand"]) == ("carrier", "techwave")
    cable = [it for it in body["items"] if it["role"] == "cable.coax_1_2in_plenum"][0]
    assert cable["graded_role"] == "graded.cable" and cable["grade_tier"] == "carrier"

    body = client.get("/projects/%d/bom?grade=enterprise" % pid).json()
    assert body["grade"] == "enterprise"
    conn = [it for it in body["items"] if it["category"] == "builder: gps"
            and it["role"] == "connector.coax"][0]
    assert conn["graded_role"] == "graded.connector"


def test_patch_project_stores_grade_and_brand(client):
    pid = _new_project_with_gps(client)
    r = client.patch("/projects/%d" % pid, json={"grade": "budget", "budget_brand": "cheapest"})
    assert r.status_code == 200, r.text
    body = client.get("/projects/%d/bom" % pid).json()
    assert (body["grade"], body["budget_brand"]) == ("budget", "cheapest")
    rows = list(csv.reader(io.StringIO(
        client.get("/projects/%d/bom_ibwave.csv" % pid).text)))
    assert any(r[0] == "Connector" and r[3] == "43M-LCF12-E01" for r in rows)


def test_invalid_grade_values_are_422(client):
    pid = _new_project_with_gps(client)
    assert client.patch("/projects/%d" % pid, json={"grade": "gold"}).status_code == 422
    assert client.patch("/projects/%d" % pid, json={"budget_brand": "acme"}).status_code == 422
    assert client.patch("/projects/%d" % pid, json={"bands": ["NOPE"]}).status_code == 422
    assert client.get("/projects/%d/bom?grade=gold" % pid).status_code == 422
    assert client.get("/projects/%d/bom_ibwave.csv?budget_brand=acme" % pid).status_code == 422


def test_bands_patch_is_stored_and_used(client):
    pid = _new_project_with_gps(client)
    r = client.patch("/projects/%d" % pid, json={"bands": CELL})
    assert r.status_code == 200, r.text
    body = client.get("/projects/%d/bom" % pid).json()
    assert body["bands"] == CELL
    client.patch("/projects/%d" % pid, json={"bands": None})
    assert client.get("/projects/%d/bom" % pid).json()["bands"] != CELL


def test_ibwave_endpoints_take_grade_parameters(client):
    pid = _new_project_with_gps(client)
    text = client.get("/projects/%d/bom_ibwave.csv?grade=enterprise" % pid).text
    rows = list(csv.reader(io.StringIO(text)))
    assert any(r[0] == "Connector" and r[3] == "WCW-43M-LCF12-D01" for r in rows)
    assert any(r[0] == "Cable" and r[1] == "RFS" and r[3] == "" for r in rows)
    r = client.get("/projects/%d/bom_ibwave.xlsx?grade=budget&budget_brand=techwave" % pid)
    assert r.status_code == 200


def test_builder_endpoints_apply_the_grade(client):
    pid = _new_project_with_gps(client)
    lines = client.get("/projects/%d/builders?grade=enterprise" % pid).json()
    gps = lines["builders"]["gps"]["lines"]
    assert any(ln["role"] == "connector.coax" and ln.get("graded_role") == "graded.connector"
               for ln in gps)
    r = client.post("/projects/%d/builders/gps?grade=budget&budget_brand=cheapest" % pid,
                    json={"coax_ft": 30})
    assert r.status_code == 200
    assert r.json()["lines"]
    assert client.post("/projects/%d/builders/gps?grade=gold" % pid,
                       json={"coax_ft": 30}).status_code == 422


def test_example_project_bom_still_builds_for_every_grade(client):
    pid = next(p["id"] for p in client.get("/projects").json()["projects"] if p["example"])
    for q in ("", "?grade=enterprise", "?grade=budget&budget_brand=techwave",
              "?grade=budget&budget_brand=cheapest"):
        assert client.get("/projects/%d/bom%s" % (pid, q)).status_code == 200
        assert client.get("/exports/projects/%d/bom.csv%s" % (pid, q)).status_code == 200
        assert client.get("/projects/%d/bom_ibwave.csv%s" % (pid, q)).status_code == 200
