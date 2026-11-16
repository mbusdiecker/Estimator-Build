"""OEM packages: expansion of the generic OEM lines into one OEM's parts."""

import json
import math
import os
import re

import pytest

from romtool.config import load_config
from romtool.grades import GradeError, grade_items, settings_for
from romtool.oem import (DEFAULT_OEM_PATH, SITE, check_oem, expand_oem, list_packages,
                         load_oem_packages)

PKG = load_oem_packages()["packages"]


def _items(remotes=0, sources=0, repeaters=0, floor=None):
    out = [{"category": "antenna", "item": "Omni", "qty": 10, "unit": "ea", "role": "antenna.omni_das"}]
    for role, q in (("oem.remote", remotes), ("oem.source", sources), ("oem.repeater", repeaters)):
        if q:
            out.append({"category": role.split(".")[1], "item": role, "qty": q, "unit": "ea", "role": role})
    if floor is not None:
        for it in out:
            it["_floor"] = floor
    return out


def _by_role(items):
    return {it.get("oem_role"): it["qty"] for it in items if it.get("oem_role")}


def test_config_has_no_prices_or_job_numbers():
    text = open(DEFAULT_OEM_PATH, encoding="utf-8").read()
    data = json.loads(text)

    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)
    assert not [k for k in keys(data) if re.search(r"price|cost|usd|dollar|amount", k, re.I)]
    assert not re.search(r"\b2\d-\d{5}\b", text)


def test_every_ratio_marks_usable_by_job_count():
    min_jobs = load_oem_packages()["min_jobs"]
    for pkg in PKG.values():
        assert pkg["system_type"] in ("das", "dran", "bda")
        for r in pkg["ratios"].values():
            assert r["usable"] == (r["jobs"] >= min_jobs)


def test_check_oem_and_listing():
    assert check_oem(None) is None and check_oem("") is None
    assert check_oem("adrf_adx_v") == "adrf_adx_v"
    with pytest.raises(ValueError):
        check_oem("corning")
    keys = [p["key"] for p in list_packages()]
    assert "samsung_dran" in keys and "westell_ps_bda" in keys


def test_chassis_based_das_gets_chassis_band_modules_head_end_and_poi():
    pkg = PKG["adrf_adx_v"]
    out, overlay, notes = expand_oem(_items(remotes=4, sources=1), "adrf_adx_v", "das")
    got = _by_role(out)
    assert not [it for it in out if it["role"] in ("oem.remote", "oem.source")]
    bm = pkg["ratios"]["band_modules_per_remote_chassis"]["value"]
    rph = pkg["ratios"]["remote_units_per_head_end"]["value"]
    poi = pkg["ratios"]["poi_cards_per_head_end"]["value"]
    assert got["remote_chassis"] == 4
    assert got["remote_band_module"] == math.ceil(4 * bm)
    n_he = max(1, math.ceil(4 / rph))
    assert got["head_end"] == n_he
    assert got["poi_card"] == math.ceil(n_he * poi)
    assert "remote_unit" not in got
    for role in got:
        part = overlay["oem.adrf_adx_v.%s" % role]
        assert part["model"] and part["unit"] == "ea" and part["ibwave_type"]
    assert all("grade C" in it["note"] for it in out if it.get("oem_role"))


def test_dran_sizes_basebands_from_radios_per_baseband():
    pkg = PKG["samsung_dran"]
    out, _overlay, _notes = expand_oem(_items(remotes=12, sources=1), "samsung_dran", "dran")
    got = _by_role(out)
    rpb = pkg["ratios"]["remote_units_per_baseband"]["value"]
    assert got["remote_unit"] == 12
    assert got["baseband_radio_carrier"] == max(1, math.ceil(12 / rpb))
    assert "head_end" not in got


def test_bda_package_maps_repeater_and_donor():
    out, _overlay, _notes = expand_oem(_items(repeaters=1, sources=1), "westell_ps_bda", "bda")
    got = _by_role(out)
    assert got["bda_repeater"] == 1 and got["donor_antenna"] == 1
    assert "battery" not in got and "annunciator_monitoring" not in got   # builders own these


def test_thin_ratios_are_not_applied_and_say_so():
    out, _overlay, notes = expand_oem(_items(remotes=3, sources=1), "solid", "das")
    got = _by_role(out)
    assert "poi_card" not in got           # 2 jobs only
    assert any("POI cards not sized" in n for n in notes)


def test_system_type_mismatch_is_noted():
    _out, _overlay, notes = expand_oem(_items(remotes=2), "samsung_dran", "das")
    assert any("DRAN package" in n for n in notes)


def test_floor_lines_keep_floor_and_site_lines_go_to_site():
    out, _overlay, _notes = expand_oem(_items(remotes=2, sources=1, floor="L1"), "adrf_adx_v", "das")
    floors = {it["oem_role"]: it["_floor"] for it in out if it.get("oem_role")}
    assert floors["remote_chassis"] == "L1"
    assert floors["head_end"] == SITE


def test_no_oem_leaves_generic_lines():
    cfg = load_config()
    s = settings_for({}, cfg)
    assert s["oem"] is None
    items, _overlay, _notes = grade_items(_items(remotes=2, sources=1), cfg, s)
    assert [it for it in items if it["role"] == "oem.remote"]


def test_grade_items_applies_project_oem_and_bad_oem_raises():
    cfg = load_config()
    s = settings_for({"oem": "jma_teko", "system_type": "das"}, cfg)
    items, overlay, _notes = grade_items(_items(remotes=6, sources=1), cfg, s)
    assert not [it for it in items if it["role"] == "oem.remote"]
    assert "oem.jma_teko.remote_unit" in overlay
    with pytest.raises(GradeError):
        settings_for({"oem": "nope"}, cfg)


# ------------------------------------------------ spec packages (SureCall FiberMax)

from romtool.oem import DEFAULT_SPEC_PATH

FM = "surecall_fibermax"


def _fm_items(antennas, remotes=1, sources=1, repeaters=0):
    out = [{"category": "antenna", "item": "Omni", "qty": antennas, "unit": "ea", "role": "antenna.omni_das"},
           {"category": "antenna", "item": "Donor", "qty": sources, "unit": "ea", "role": "antenna.donor"}]
    for role, q in (("oem.remote", remotes), ("oem.source", sources), ("oem.repeater", repeaters)):
        if q:
            out.append({"category": role.split(".")[1], "item": role, "qty": q, "unit": "ea", "role": role})
    return out


def test_spec_file_is_merged_and_has_no_prices_or_job_numbers():
    assert FM in PKG and PKG[FM]["source"] == "spec"
    assert any(p["key"] == FM for p in list_packages())
    assert check_oem(FM) == FM
    text = open(DEFAULT_SPEC_PATH, encoding="utf-8").read()
    assert not re.search(r'"[^"]*(price|cost|usd|dollar|amount)[^"]*"\s*:', text, re.I)
    assert not re.search(r"\b2\d-\d{5}\b", text)


def test_quote_history_package_wins_over_spec_of_same_key(tmp_path):
    hist = {"min_jobs": 3, "packages": {"x": {"label": "Hist", "system_type": "das", "jobs": 5, "ratios": {}, "parts": {}}}}
    spec = {"packages": {"x": {"label": "Spec", "system_type": "das", "jobs": 0, "source": "spec", "ratios": {}, "parts": {}},
                         "y": {"label": "OnlySpec", "system_type": "das", "jobs": 0, "source": "spec", "ratios": {}, "parts": {}}}}
    hp, sp = tmp_path / "h.json", tmp_path / "s.json"
    hp.write_text(json.dumps(hist)); sp.write_text(json.dumps(spec))
    cfg = load_oem_packages(str(hp), str(sp))
    assert cfg["packages"]["x"]["label"] == "Hist" and cfg["packages"]["y"]["label"] == "OnlySpec"


def test_fibermax_sized_from_design_density_when_area_known():
    # 251,974 sq ft at 4,230 sq ft per antenna = 60 antennas -> 4 remotes at 16 -> 1 master (kit holds 1 RU) + 3 RU kits
    out, overlay, notes = expand_oem(_fm_items(126, remotes=1, sources=1), FM, "das", area_sqft=251974)
    got = _by_role(out)
    assert got == {"master_unit": 1, "remote_unit": 3}
    assert not [it for it in out if it["role"] in ("oem.remote", "oem.source")]
    assert any("126 service antennas" in n and "gives 60" in n for n in notes)
    assert any("4 remotes per master, 16 antennas per remote" in n for n in notes)
    assert "SureCall" in overlay["oem.%s.master_unit" % FM]["manufacturer"]
    assert all("grade B" in it["note"] for it in out if it.get("oem_role"))


def test_fibermax_without_area_uses_modeled_antennas_and_caps_per_remote():
    # 126 antennas / 16 = 8 remotes -> 2 masters (4 each) -> 2 kit RUs + 6 RU kits
    out, _overlay, notes = expand_oem(_fm_items(126, remotes=1, sources=1), FM, "das")
    assert _by_role(out) == {"master_unit": 2, "remote_unit": 6}
    assert any("no covered area" in n for n in notes)


def test_fibermax_one_master_per_offair_source_and_repeater_dropped():
    out, _overlay, notes = expand_oem(_fm_items(40, remotes=3, sources=2, repeaters=1), FM, "das")
    got = _by_role(out)
    assert got["master_unit"] == 2                  # two donors, one per master
    assert got["remote_unit"] == 1                  # 3 remotes less 2 in the kits
    assert any("repeater" in n for n in notes)
    assert [it for it in out if it["role"] == "antenna.donor"]   # passive donor line untouched


def test_fibermax_small_job_still_gets_one_kit():
    out, _overlay, _notes = expand_oem(_fm_items(4, remotes=1, sources=1), FM, "das", area_sqft=15000)
    assert _by_role(out) == {"master_unit": 1}       # 4 antennas, 1 RU, in the kit


def test_grade_items_passes_covered_area_to_spec_package():
    cfg = load_config()
    s = settings_for({"oem": FM, "system_type": "das", "covered_area_sqft": 251974}, cfg)
    assert s["covered_area_sqft"] == 251974
    items, overlay, _notes = grade_items(_fm_items(126, remotes=1, sources=1), cfg, s)
    assert "oem.%s.master_unit" % FM in overlay
    assert {it["oem_role"]: it["qty"] for it in items if it.get("oem_role")} == {"master_unit": 1, "remote_unit": 3}


def test_api_covered_area_validation(client):
    pid = [p for p in client.get("/projects").json()["projects"] if p["example"]][0]["id"]
    assert client.patch("/projects/%d" % pid, json={"covered_area_sqft": "abc"}).status_code == 422
    assert client.patch("/projects/%d" % pid, json={"covered_area_sqft": 0}).status_code == 422
    assert client.patch("/projects/%d" % pid, json={"covered_area_sqft": 251974}).status_code == 200
    assert client.patch("/projects/%d" % pid, json={"covered_area_sqft": None}).status_code == 200


def test_api_sets_oem_and_bom_reports_it(client):
    pid = [p for p in client.get("/projects").json()["projects"] if p["example"]][0]["id"]
    assert client.get("/oem_packages").json()["packages"]
    r = client.patch("/projects/%d" % pid, json={"oem": "commscope_ion_era"})
    assert r.status_code == 200, r.text
    bom = client.get("/projects/%d/bom" % pid).json()
    assert bom["oem"] == "commscope_ion_era"
    assert client.patch("/projects/%d" % pid, json={"oem": "bogus"}).status_code == 422
    client.patch("/projects/%d" % pid, json={"oem": None})
    assert client.get("/projects/%d/bom" % pid).json()["oem"] is None
