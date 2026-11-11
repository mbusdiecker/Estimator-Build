"""Area zones (romtool/zones.py): zone classes, clutter overrides, overlap, a
zones-only grid, ceiling height in predict and autoplace, the zone API and the
floor drawing. Floors are 300 x 200 px at 10 px/m (30 m x 20 m) unless noted.
"""

import math

import pytest

from romtool.autoplace import autoplace_floor
from romtool.clutter_map import FALLBACK_CODE, ClutterField
from romtool.config import load_config
from romtool.drawings import floor_layout_png, floor_layout_svg
from romtool.predict import predict_floor
from romtool.zones import (ZoneError, effective_clutter_map, height_offset_range,
                           normalize_zone, resolve_zone, resolve_zones, zone_at)

SRC = {"subtype": "direct", "injection_dbm": 15.0}
REP = {"max_dl_out_dbm": 33.0}
THR = -60.0
ZONE_CLASSES = ["warehouse_racked", "high_bay_open", "office_open", "office_standard",
                "office_dense", "hospital_rooms", "hotel_rooms", "retail",
                "parking_garage", "stadium_bowl"]


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _floor(w=300, h=200, **extra):
    fl = {"id": "f1", "name": "F1", "level": 0, "ppm": 10.0, "w_px": w, "h_px": h,
          "walls": [], "elements": []}
    fl.update(extra)
    return fl


def _model(floor, building_type="office"):
    model = {"environment": "office", "floors": [floor]}
    if building_type:
        model["building_type"] = building_type
    return model


def _zone(cfg, zid, cls, rect=None, points=None, **extra):
    raw = {"name": zid, "class": cls}
    if rect is not None:
        raw["rect"] = rect
    else:
        raw["points"] = points
    raw.update(extra)
    z = normalize_zone(raw, cfg)
    z["id"] = zid
    return z


def _antenna_model(floor, x, y, building_type="office"):
    floor["elements"] = [
        {"id": "s", "type": "source", "subtype": "direct", "injection_dbm": 10.0},
        {"id": "a", "type": "antenna", "antenna_id": "omni_das", "x": x, "y": y},
        {"id": "r", "type": "cable_run", "from_id": "s", "to_id": "a",
         "cable_id": "coax_1_2in", "length_m": 5.0}]
    return _model(floor, building_type)


# ============================================================ 1. zone classes

def test_zone_classes_take_calibrated_values_or_graded_estimates(cfg):
    cl = cfg["clutter_map"]["classes"]
    bt = cfg["building_types"]
    for cid in ZONE_CLASSES:
        assert cid in cl, cid
        assert cl[cid]["code"] and cl[cid]["ref"] and cl[cid]["label"], cid
        assert cl[cid]["height_band"] in cfg["clutter_map"]["ceiling_height_bands"], cid
        assert cl[cid]["pathway"] in cfg["clutter_map"]["cable_pathways"], cid
    # calibrated sources: grade C with the building type value
    assert cl["warehouse_racked"]["db_per_m"] == bt["warehouse_open"]["clutter_db_per_m"]
    assert cl["warehouse_racked"]["grade"] == "C"
    assert cl["office_standard"]["db_per_m"] == bt["office_standard"]["clutter_db_per_m"]
    assert cl["office_standard"]["grade"] == "C"
    assert cl["hospital_rooms"]["db_per_m"] == bt["hospital"]["clutter_db_per_m"]
    assert cl["hotel_rooms"]["db_per_m"] == bt["hotel_residential"]["clutter_db_per_m"]
    assert cl["hotel_rooms"]["grade"] == "C"
    # no calibrated source: grade E with the starting value named
    for cid, ref_key in (("office_open", "office_open"), ("office_dense", "office_dense"),
                         ("retail", "retail_mall"), ("parking_garage", "parking_garage"),
                         ("stadium_bowl", "stadium_venue")):
        assert cl[cid]["grade"] == "E", cid
        assert ref_key in cl[cid]["ref"], cid
        assert cl[cid]["db_per_m"] == bt[ref_key]["clutter_db_per_m"], cid
    assert cl["high_bay_open"]["grade"] == "E" and cl["high_bay_open"]["db_per_m"] == 0.1
    # the ceiling bands the brief names are the defaults
    assert cl["warehouse_racked"]["height_band"] == "Over 20 ft (lift)"
    assert cl["office_standard"]["height_band"] == "Up to 10 ft"
    codes = [c["code"] for c in cl.values()]
    assert len(codes) == len(set(codes)) and FALLBACK_CODE not in codes


# ============================================================ 2. validation

def test_zone_payload_validation(cfg):
    with pytest.raises(ZoneError):
        normalize_zone({"name": "x", "class": "nope", "rect": [0, 0, 10, 10]}, cfg)
    with pytest.raises(ZoneError):   # band and metres together
        normalize_zone({"class": "open", "rect": [0, 0, 10, 10],
                        "height_band": "Up to 10 ft", "ceiling_height_m": 3}, cfg)
    with pytest.raises(ZoneError):   # unknown band
        normalize_zone({"class": "open", "rect": [0, 0, 10, 10], "height_band": "Tall"}, cfg)
    with pytest.raises(ZoneError):   # unknown pathway
        normalize_zone({"class": "open", "rect": [0, 0, 10, 10], "pathway": "Ladder"}, cfg)
    with pytest.raises(ZoneError):   # flat rectangle
        normalize_zone({"class": "open", "rect": [0, 0, 10, 0]}, cfg)
    with pytest.raises(ZoneError):   # two-corner polygon
        normalize_zone({"class": "open", "points": [[0, 0], [5, 5]]}, cfg)
    with pytest.raises(ZoneError):   # height out of range
        normalize_zone({"class": "open", "rect": [0, 0, 10, 10], "ceiling_height_m": 50}, cfg)
    z = normalize_zone({"class": "warehouse_racked", "rect": [10, 0, 0, 10]}, cfg)
    assert z["points"][0] == [0.0, 0.0] and z["shape"] == "rect"
    assert z["name"] == "Warehouse, racked storage"


def test_resolved_height_and_pathway_sources(cfg):
    z = _zone(cfg, "w", "warehouse_racked", rect=[0, 0, 50, 50])
    r = resolve_zone(z, cfg)
    assert r["ceiling_height_m"] == cfg["clutter_map"]["ceiling_height_bands"][
        "Over 20 ft (lift)"]["height_m"]
    assert r["height_source"] == "class" and r["pathway_source"] == "class"
    o = resolve_zone(_zone(cfg, "w", "warehouse_racked", rect=[0, 0, 50, 50],
                           ceiling_height_m=5.0, pathway="Cable tray"), cfg)
    assert o["ceiling_height_m"] == 5.0 and o["height_source"] == "override_m"
    assert o["pathway"] == "Cable tray" and o["pathway_source"] == "override"


# ============================================ 3. zones override automatic squares

def test_zone_overrides_stored_map_squares_and_keeps_the_map(cfg):
    floor = _floor(420, 30)   # 14 squares of 30 px in one row
    floor["clutter_map"] = {
        "version": 1, "cell_m": 3.0, "cell_px": 30.0, "cols": 14, "rows": 1, "ppm": 10.0,
        "codes": {"o": "open", "l": "light_office", "d": "dense_rooms", "c": "core",
                  "x": "exterior"},
        "grid": ["o" * 14], "auto_grid": ["o" * 14], "overrides": {}}
    floor["zones"] = [_zone(cfg, "rack", "warehouse_racked", rect=[60, 0, 120, 30])]
    cm = effective_clutter_map(floor, cfg, 0.0)
    # centres 75 and 105 px lie in the zone (cols 2 and 3)
    assert cm["grid"][0] == "oorr" + "o" * 10
    assert cm["stats"]["warehouse_racked"] == 2 and cm["stats"]["open"] == 12
    assert cm["zones_applied"][0]["squares"] == 2
    # the stored map is not modified
    assert floor["clutter_map"]["grid"] == ["o" * 14]


# ============================================================ 4. overlap

def test_later_zone_wins_where_zones_overlap(cfg):
    floor = _floor(420, 30)
    floor["zones"] = [_zone(cfg, "office", "office_dense", rect=[0, 0, 120, 30]),
                      _zone(cfg, "rack", "warehouse_racked", rect=[60, 0, 180, 30])]
    cm = effective_clutter_map(floor, cfg, 0.0)
    # office_dense (e) in cols 0 and 1; the later rack zone (r) in cols 2 to 5;
    # squares outside every zone are the building-type fallback (_)
    assert cm["grid"][0] == "ee" + "rrrr" + FALLBACK_CODE * 8
    # the same point resolves to the later zone
    resolved = resolve_zones(floor["zones"], cfg)
    assert zone_at(resolved, 100, 15)["id"] == "rack"
    assert zone_at(resolved, 30, 15)["id"] == "office"
    assert zone_at(resolved, 250, 15) is None


# ============================================ 5. zones-only grid, no image

def test_zones_only_grid_uses_building_type_outside_zones(cfg):
    floor = _floor(300, 200)   # no image, no clutter map
    floor["zones"] = [_zone(cfg, "rack", "warehouse_racked", rect=[150, 0, 300, 200])]
    cm = effective_clutter_map(floor, cfg, fallback_db=0.3)
    assert cm["source"]["zones_only"] is True
    assert cm["cols"] == 10 and cm["rows"] == 7   # 300 / 30 and ceil(200 / 30)
    field = ClutterField(cm, cfg, 10.0)
    # left half at the building type value (0.3), right half the rack class (1.0)
    assert field.db_at(20, 100) == pytest.approx(0.3)
    assert field.db_at(250, 100) == pytest.approx(1.0)
    assert field.mean_db == pytest.approx(0.65, abs=0.05)
    # predict runs on the zones-only grid and reports the clutter-map source
    model = _antenna_model(floor, 60.0, 100.0)
    res = predict_floor(model, cfg, "f1", "B2_1900", step_px=4)
    assert res["loss_model"]["source"] == "clutter_map"


def test_zone_too_small_for_a_square_is_reported(cfg):
    floor = _floor(300, 200)
    floor["zones"] = [_zone(cfg, "tiny", "office_dense", rect=[100, 100, 104, 104])]
    cm = effective_clutter_map(floor, cfg, 0.0)
    assert any("covers no grid square" in w for w in cm["warnings"])


# ================================================ 6. height in prediction

def _point_rssi(res, x, y):
    cols = res["grid"]["cols"]
    return res["values_dbm"][y * cols + x]


def test_zone_height_changes_the_distance_term(cfg):
    # same class (same clutter) in both; only the ceiling height differs
    def run(height_m):
        floor = _floor(300, 200)
        floor["zones"] = [_zone(cfg, "z", "office_standard", rect=[0, 0, 300, 200],
                                ceiling_height_m=height_m)]
        model = _antenna_model(floor, 50.0, 100.0)
        return predict_floor(model, cfg, "f1", "B2_1900", step_px=1)
    high, low = run(7.6), run(3.0)
    # office N = 30; the point is 100 px (10 m) away on the same row
    n = cfg["building_types"]["office"]["n"]
    dv_hi, dv_lo = 7.6 - 4.0, 3.0 - 4.0
    expected = n * (math.log10(math.hypot(10.0, dv_hi)) - math.log10(math.hypot(10.0, dv_lo)))
    diff = _point_rssi(low, 150, 100) - _point_rssi(high, 150, 100)
    assert diff == pytest.approx(expected, abs=0.15)
    assert _point_rssi(high, 150, 100) < _point_rssi(low, 150, 100)


def test_zone_at_default_height_matches_no_zone(cfg):
    # office_standard clutter (0.3) at the floor default height (4.0 m): dv = 0,
    # so the prediction equals the building type path (no zones)
    base = _antenna_model(_floor(300, 200), 50.0, 100.0)
    a = predict_floor(base, cfg, "f1", "B2_1900", step_px=2)
    floor = _floor(300, 200)
    floor["zones"] = [_zone(cfg, "z", "office_standard", rect=[0, 0, 300, 200],
                            ceiling_height_m=4.0)]
    b = predict_floor(_antenna_model(floor, 50.0, 100.0), cfg, "f1", "B2_1900", step_px=2)
    assert b["loss_model"]["source"] == "clutter_map"
    for va, vb in zip(a["values_dbm"], b["values_dbm"]):
        if va is None or vb is None:
            assert va == vb
        else:
            assert vb == pytest.approx(va, abs=0.05)


def test_height_offset_range_counts_squares_outside_zones(cfg):
    floor = _floor(300, 200)
    floor["zones"] = [_zone(cfg, "rack", "warehouse_racked", rect=[0, 0, 100, 100])]
    lo, hi = height_offset_range(floor, cfg)
    assert lo == 0.0   # outside squares (and the floor default) at dv = 0
    assert hi == pytest.approx(7.6 - 4.0)


# ============================================ 7. autoplace: density and heights

def _half_counts(result, split_px):
    left = sum(1 for a in result["antennas"] if a["x_px"] < split_px)
    right = len(result["antennas"]) - left
    return left, right


def test_warehouse_half_gets_more_antennas_per_area(cfg):
    # 60 m x 30 m floor split at x = 30 m: two 30 m x 30 m halves of equal area
    req = {"bands": ["B2_1900"], "threshold_dbm": THR, "source": SRC, "repeater": REP,
           "step_px": 4}
    plain = autoplace_floor(_model(_floor(600, 300)), cfg, "f1", dict(req))
    floor = _floor(600, 300)
    floor["zones"] = [_zone(cfg, "rack", "warehouse_racked", rect=[300, 0, 600, 300])]
    zoned = autoplace_floor(_model(floor), cfg, "f1", dict(req))
    p_left, p_right = _half_counts(plain, 300)
    z_left, z_right = _half_counts(zoned, 300)
    assert zoned["target_met"] and plain["target_met"]
    # measured at 60 m x 30 m / threshold -60 dBm: without the zone 2 + 1, with it 2 + 3
    assert (p_left, p_right) == (2, 1)
    assert (z_left, z_right) == (2, 3)
    assert z_right > p_right and z_right >= z_left * 1.3
    # the office half is unchanged by the rack zone next to it
    assert z_left == p_left


def test_autoplace_reports_zone_and_height_per_antenna(cfg):
    floor = _floor(600, 300)
    floor["zones"] = [_zone(cfg, "rack", "warehouse_racked", rect=[300, 0, 600, 300])]
    res = autoplace_floor(_model(floor), cfg, "f1", {
        "bands": ["B2_1900"], "threshold_dbm": THR, "source": SRC, "repeater": REP,
        "step_px": 4})
    assert res["antennas"]
    assert any(a["x_px"] >= 300 for a in res["antennas"])
    for a in res["antennas"]:
        if a["x_px"] >= 300:
            assert a["zone_name"] == "rack" and a["zone_class"] == "warehouse_racked"
            assert a["ceiling_height_m"] == 7.6 and a["height_band"] == "Over 20 ft (lift)"
            assert a["height_offset_m"] == pytest.approx(3.6)
            assert a["pathway"] == "Open ceiling / exposed"
        else:
            assert a["zone_id"] is None and a["ceiling_height_m"] == 4.0
            assert a["height_offset_m"] == 0.0


# ============================================================ 8. zone API

def test_zone_api_crud(client):
    pid = client.post("/projects", json={"name": "Zones API"}).json()["id"]
    fid = client.post("/projects/%d/floors" % pid,
                      json={"name": "L1", "w_px": 300, "h_px": 200, "ppm": 10}).json()["floor"]["id"]
    base = "/projects/%d/floors/%s/zones" % (pid, fid)
    assert client.get(base).json()["zones"] == []

    r = client.post(base, json={"name": "Racks", "class": "warehouse_racked",
                                "rect": [150, 0, 300, 200]})
    assert r.status_code == 200, r.text
    z = r.json()["zones"][0]
    assert z["id"].startswith("zone_") and z["ceiling_height_m"] == 7.6
    zid = z["id"]

    r = client.post(base, json={"zones": [
        {"name": "Desks", "class": "office_standard", "points": [[0, 0], [100, 0], [50, 80]]},
        {"name": "Lobby", "class": "retail", "rect": [0, 150, 60, 200],
         "height_band": "10 to 15 ft"}]})
    assert r.status_code == 200 and len(r.json()["zones"]) == 2
    assert client.get(base).json()["zones"][2]["ceiling_height_m"] == 3.8

    r = client.patch(base + "/" + zid, json={"height_band": "Up to 10 ft"})
    assert r.status_code == 200
    assert r.json()["zone"]["ceiling_height_m"] == 3.0
    assert r.json()["zone"]["height_source"] == "override_band"
    r = client.patch(base + "/" + zid, json={"ceiling_height_m": 5.0, "height_band": None})
    assert r.json()["zone"]["ceiling_height_m"] == 5.0
    assert r.json()["zone"]["height_source"] == "override_m"
    r = client.patch(base + "/" + zid, json={"name": "Racks B", "pathway": "Cable tray"})
    assert r.json()["zone"]["name"] == "Racks B" and r.json()["zone"]["id"] == zid

    assert client.post(base, json={"name": "bad", "class": "nope",
                                   "rect": [0, 0, 5, 5]}).status_code == 422
    assert client.patch(base + "/zone_missing", json={"name": "x"}).status_code == 404

    assert client.delete(base + "/" + zid).status_code == 200
    assert client.delete(base + "/" + zid).status_code == 404
    assert len(client.get(base).json()["zones"]) == 2


# ============================================================ 9. drawings

def test_floor_drawing_shows_zones_with_legend(cfg):
    floor = _floor(300, 200)
    floor["zones"] = [_zone(cfg, "rack", "warehouse_racked", rect=[150, 0, 300, 200],
                            name="Racks east")]
    model = _model(floor)
    svg = floor_layout_svg(model, cfg, "f1")
    assert "Racks east" in svg
    assert "Warehouse, racked storage" in svg and "7.6 m" in svg
    assert "Zone: Warehouse, racked storage" in svg
    png = floor_layout_png(model, cfg, "f1")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
