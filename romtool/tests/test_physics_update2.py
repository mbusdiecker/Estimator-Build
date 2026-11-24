"""Physics update 2: walls from DXF / vector-PDF floor plans, the trunk2
trunk topology and "best" (trunk2 vs trunk4), the per-remote antenna cap
(remotes.max_antennas_per_unit) and the 47 dBm antenna port ceiling.

Hand anchors are noted at each test. The helpers of the first physics update
(test_physics_update.py) are reused: make_model, request (direct source
15 dBm, repeater cap 33 dBm, threshold -60 dBm) and the element-walk checks.
"""

import copy
import json
import math

import pytest

from romtool.autoplace import AutoplaceError, autoplace_floor
from romtool.cascade import cable_loss_db, solve_cascade
from romtool.config import load_config
from romtool.plan_walls import (PlanWallsError, floor_from_plan, main,
                                merge_segments, scale_to_area, wall_density,
                                walls_from_dxf, walls_from_pdf)

from test_physics_update import (_antenna_check, _every_antenna_fed, _runmap,
                                 els, make_model, request)

SQFT = 1000.0 / 0.09290304        # a 40 x 25 m floor, 1000 m2


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _dxf_path(tmp_path):
    """40 x 25 m box (LWPOLYLINE, closed) + 3 interior walls + one wall drawn
    as two parallel faces 0.2 m apart, in metres as drawing units."""
    ezdxf = pytest.importorskip("ezdxf")
    doc = ezdxf.new("R2013")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (40, 0), (40, 25), (0, 25)], close=True,
                       dxfattribs={"layer": "A-WALL"})
    msp.add_line((12, 0), (12, 10), dxfattribs={"layer": "A-WALL"})
    msp.add_line((12, 15), (12, 25), dxfattribs={"layer": "A-WALL"})   # door gap
    msp.add_line((12, 17), (40, 17), dxfattribs={"layer": "A-WALL"})
    msp.add_line((30, 0), (30, 12), dxfattribs={"layer": "PARTITION"})
    msp.add_line((30.2, 0), (30.2, 12), dxfattribs={"layer": "PARTITION"})
    path = tmp_path / "plan2.dxf"
    doc.saveas(str(path))
    return path


# ================================================== 1. walls from floor plans

def test_dxf_import_layers_and_merge(tmp_path):
    raw = walls_from_dxf(str(_dxf_path(tmp_path)))
    assert raw["source"] == "dxf"
    assert raw["n_raw"] == 9                     # 4 box + 3 interior + 2 faces
    assert raw["wall_layers"] == ["A-WALL", "PARTITION"]
    assert raw["layer_filtered"] is True
    assert tuple(round(v, 6) for v in raw["bbox"]) == (0.0, 0.0, 40.0, 25.0)


def test_dxf_scale_bbox_within_two_percent(tmp_path):
    raw = walls_from_dxf(str(_dxf_path(tmp_path)))
    scaled = scale_to_area(raw, SQFT, use_perimeter=False)
    assert scaled["scale_source"] == "bbox"
    assert scaled["m_per_unit"] == pytest.approx(1.0, abs=0.02)
    # 40 x 25 drawing units scaled back to 1000 m2, within 2%
    assert scaled["bbox_units_area"] * scaled["m_per_unit"] ** 2 == \
        pytest.approx(1000.0, rel=0.02)
    assert scaled["n_walls"] == 8                # the two faces are one wall
    counts = scaled["merge_counts"]
    assert counts == {"raw": 9, "duplicates": 0, "collinear": 0,
                      "double_line": 1, "short": 0, "walls": 8}
    # the collapsed double-line wall sits on the midline of the two faces
    mid = [s for s in scaled["segments"]
           if abs(s[0] - 30.1) < 0.02 and abs(s[2] - 30.1) < 0.02]
    assert len(mid) == 1 and pytest.approx(mid[0][3] - mid[0][1], abs=0.01) == 12.0


def test_dxf_scale_reports_the_perimeter_source_when_it_traces(tmp_path):
    raw = walls_from_dxf(str(_dxf_path(tmp_path)))
    scaled = scale_to_area(raw, SQFT)            # perimeter attempt on
    if scaled["scale_source"] == "perimeter":
        assert scaled["n_walls"] == 8
        assert scaled["bbox_units_area"] * scaled["m_per_unit"] ** 2 == \
            pytest.approx(1000.0, rel=0.03)
        assert scaled["perimeter"]["succeeded"] is True
    else:
        # the trace failed: the fallback is reported, not silent
        assert scaled["perimeter"]["attempted"] is True
        assert scaled["warnings"], "the fallback should carry its reason"


def test_dxf_floor_from_plan_autoplaces_in_physics_mode(tmp_path, cfg):
    raw = walls_from_dxf(str(_dxf_path(tmp_path)))
    floor = floor_from_plan(raw, SQFT, ppm=2.0, floor_id="f1")
    assert len(floor["walls"]) == 8
    assert all(w["material"] == "drywall" for w in floor["walls"])
    assert floor["loss_mode"] == "physics"
    assert floor["elements"] == []
    cmap = floor["clutter_map"]
    assert cmap["cpm"] and any(v > 0 for row in cmap["cpm"] for v in row)
    model = {"environment": "office", "floors": [floor]}
    res = autoplace_floor(model, cfg, "f1", request())
    assert res["target_met"] and res["antenna_count"] >= 1
    # drawn walls are counted per ray; the floor is accepted unchanged
    assert res["loss_model"]["B2_1900"]["source"] == "walls_drawn"
    sol = solve_cascade(model, cfg)
    assert sol["errors"] == []


def test_merge_segments_duplicates_collinear_faces_and_short():
    # exact duplicate (either endpoint order) collapses
    r = merge_segments([[0, 0, 10, 0], [10, 0, 0, 0]])
    assert r["counts"]["duplicates"] == 1 and len(r["segments"]) == 1
    # collinear overlapping pieces merge into one long wall...
    r = merge_segments([[0, 0, 10, 0], [5, 0, 20, 0]])
    assert r["counts"]["collinear"] == 1 and len(r["segments"]) == 1
    assert r["segments"][0][2] == pytest.approx(20.0)
    # ...but a door gap keeps them separate
    r = merge_segments([[0, 0, 9, 0], [11, 0, 20, 0]])
    assert len(r["segments"]) == 2
    # double-line faces (0.2 m apart) collapse to the midline
    r = merge_segments([[30, 0, 30, 12], [30.2, 0, 30.2, 12]])
    assert r["counts"]["double_line"] == 1 and len(r["segments"]) == 1
    assert r["segments"][0][0] == pytest.approx(30.1, abs=1e-9)
    # distinct walls stay: no overlap, or more than 0.4 m apart
    r = merge_segments([[0, 0, 10, 0], [20, 0.1, 30, 0.1]])
    assert len(r["segments"]) == 2
    r = merge_segments([[0, 0, 10, 0], [0, 0.6, 8, 0.6]])
    assert len(r["segments"]) == 2
    # pieces under 0.3 m drop (metre units here: post-scale text/tick rule)
    r = merge_segments([[0, 0, 10, 0], [12, 5, 12.2, 5]])
    assert r["counts"]["short"] == 1 and len(r["segments"]) == 1


def test_pdf_import_and_text_only_page(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page(width=560, height=400)
    page.draw_line((20, 20), (540, 20))          # outer box, 90 m x 60 m paper pts
    page.draw_line((540, 20), (540, 380))
    page.draw_line((540, 380), (20, 380))
    page.draw_line((20, 380), (20, 20))
    page.draw_line((280, 20), (280, 200))        # interior wall with a door gap
    page.draw_line((280, 220), (280, 380))
    pth = str(tmp_path / "plan.pdf")
    doc.save(pth)
    raw = walls_from_pdf(pth)
    assert raw["source"] == "pdf"
    assert raw["n_raw"] == 6 and raw["reason"] is None
    assert raw["bbox"][0] == pytest.approx(20.0)
    # the PDF imports the same way: scale, walls, physics floor
    sq_ft = 2700.0 / 0.09290304                  # ~90 x 60 pt plan = 2700 m2
    scaled = scale_to_area(raw, sq_ft, use_perimeter=False)
    assert scaled["n_walls"] == 6
    floor = floor_from_plan(scaled, sq_ft, floor_id="f1")
    assert len(floor["walls"]) == 6 and floor["loss_mode"] == "physics"
    # a page with only text: no walls and a clear reason, no OCR
    doc2 = pymupdf.open()
    pg = doc2.new_page(width=560, height=400)
    pg.insert_text((72, 72), "PLAN A-101 SCALE 1:100")
    pth2 = str(tmp_path / "text.pdf")
    doc2.save(pth2)
    r2 = walls_from_pdf(pth2)
    assert r2["segments"] == []
    assert "raster" in r2["reason"] or "vector" in r2["reason"]
    with pytest.raises(PlanWallsError):
        scale_to_area([], SQFT)


def test_wall_density_report(tmp_path):
    raw = walls_from_dxf(str(_dxf_path(tmp_path)))
    floor = floor_from_plan(raw, SQFT, ppm=2.0)
    d = wall_density(floor, n_rays=90)
    assert d["n_walls"] == 8
    # hand anchor: 130 m box + 10 + 10 + 28 m interiors + the 12 m midline = 190
    assert d["wall_m_total"] == pytest.approx(190.0, abs=0.5)
    assert d["floor_area_m2"] > 1000.0           # canvas includes the margin
    assert d["wall_m_per_m2"] > 0 and d["crossings_per_m_mean"] > 0
    assert d["crossings_per_m_mean"] < 1.0       # sparse plan, far below core


def test_plan_walls_cli_writes_floor_json(tmp_path):
    out = tmp_path / "floor.json"
    rc = main([str(_dxf_path(tmp_path)), "--sq-ft", str(SQFT),
               "--out", str(out), "--no-perimeter"])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["walls"]) == 8 and data["loss_mode"] == "physics"
    assert data["plan"]["scale_source"] in ("bbox", "perimeter")


# ============================================== 2. trunk2 and best topologies

def test_trunk2_topology_on_a_long_thin_floor(cfg):
    body = request(threshold_dbm=-55.0, remotes={"count": 1}, topology="trunk2")
    m = make_model(900, 300)
    res = autoplace_floor(m, cfg, "f1", body)
    assert res["topology"] == "trunk2" and res["target_met"]
    n = res["antenna_count"]
    assert n >= 4, n
    ru = els(m, "remote")[0]
    splitters = els(m, "splitter")
    assert len(splitters) == 1 and splitters[0]["ways"] == 2
    assert (splitters[0]["x"], splitters[0]["y"]) == (ru["x"], ru["y"])
    assert _every_antenna_fed(m) == n
    ants = els(m, "antenna")
    devices = els(m, "tapper") + els(m, "coupler")
    assert len(devices) == n - 2                 # two trunk ends, no tap device
    runs = _runmap(m)
    ends = 0
    sides = {"lo": 0, "hi": 0}
    for a in ants:
        feeds = [r for r in runs.values() if r["to_id"] == a["id"]]
        assert len(feeds) == 1, a["id"]
        side = "lo" if a["x"] <= ru["x"] else "hi"
        sides[side] += 1
        if feeds[0].get("port") in ("tap", "coupled"):
            dev = next(e for e in devices if e["id"] == feeds[0]["from_id"])
            assert (dev["x"], dev["y"]) == (a["x"], a["y"])
        else:
            ends += 1
    assert ends == 2
    assert sides["lo"] >= 1 and sides["hi"] >= 1
    # the cascade and the trunk path (splitter, taps, through losses, cable) agree
    _antenna_check(cfg, m)
    # taps decrease outward on each trunk: the trunk end couples 0 dB
    parts = cfg["parts"]
    ratio_of = {}
    for dev in devices:
        key = parts["tappers" if dev["type"] == "tapper" else "couplers"][
            dev.get("part_id", "default")]
        field = "tap_loss_db" if dev["type"] == "tapper" else "coupled_loss_db"
        ratio_of[dev["id"]] = float(dev.get(field, key[field]))
    for side in ("lo", "hi"):
        grp = sorted((a for a in ants
                      if ("lo" if a["x"] <= ru["x"] else "hi") == side),
                     key=lambda a: (a["x"] - ru["x"]) ** 2
                     + (a["y"] - ru["y"]) ** 2)
        vals = [ratio_of.get(next(r["from_id"] for r in runs.values()
                                  if r["to_id"] == a["id"]), 0.0)
                for a in grp]
        assert vals == sorted(vals, reverse=True), (side, vals)
    # deterministic
    again = autoplace_floor(make_model(900, 300), cfg, "f1", copy.deepcopy(body))
    assert again["antennas"] == res["antennas"]
    assert again["elements_added"] == res["elements_added"]


def test_best_builds_both_trunks_and_reports_the_choice(cfg):
    body = request(threshold_dbm=-55.0, remotes={"count": 1}, topology="best")
    m = make_model(900, 300)
    res = autoplace_floor(m, cfg, "f1", body)
    assert res["topology"] in ("trunk2", "trunk4")
    assert res["target_met"]
    choice = res["topology_choice"]
    assert choice["requested"] == "best" and choice["chosen"] == res["topology"]
    cands = choice["candidates"]
    assert set(cands) == {"trunk2", "trunk4"}
    got, other = cands[res["topology"]], cands[
        "trunk4" if res["topology"] == "trunk2" else "trunk2"]
    # the choice rule: higher worst port; a tie takes the fewer elements
    if got["worst_antenna_port_dbm"] == other["worst_antenna_port_dbm"]:
        assert got["elements"] <= other["elements"]
    else:
        assert got["worst_antenna_port_dbm"] > other["worst_antenna_port_dbm"]
    assert sol_ok(m, cfg)
    # taps: true does not combine with an explicit topology, "best" included
    with pytest.raises(AutoplaceError):
        autoplace_floor(make_model(600, 400), cfg, "f1",
                        dict(request(remotes={"count": 1}), topology="best",
                             taps=True))


def sol_ok(model, cfg):
    return solve_cascade(model, cfg)["errors"] == []


# =================================================== 3. antennas per unit cap

def test_max_antennas_per_unit_caps_units(cfg):
    body = request()
    base = autoplace_floor(make_model(900, 600), cfg, "f1",
                           dict(body, remotes={"count": 1}))
    assert base["antenna_count"] >= 6, base["antenna_count"]
    capped = autoplace_floor(make_model(900, 600), cfg, "f1",
                             dict(body, remotes={"max_antennas_per_unit": 3}))
    rem = capped["remotes"]
    assert rem["max_antennas_per_unit"] == 3
    assert rem["count"] == len(rem["units"]) >= 1
    assert all(u["antennas"] <= 3 for u in rem["units"])
    n = capped["antenna_count"]
    assert rem["count"] >= math.ceil(n / 3.0)
    # a fixed count that cannot hold all antennas under the cap is refused
    with pytest.raises(AutoplaceError):
        autoplace_floor(make_model(900, 600), cfg, "f1",
                        dict(body, remotes={"count": 2,
                                            "max_antennas_per_unit": 3}))
    with pytest.raises(AutoplaceError):
        autoplace_floor(make_model(600, 400), cfg, "f1",
                        dict(body, remotes={"max_antennas_per_unit": 0}))
    # cap=1: every remote carries exactly one antenna
    single = autoplace_floor(make_model(600, 400), cfg, "f1",
                             dict(body, remotes={"max_antennas_per_unit": 1}))
    assert all(u["antennas"] == 1 for u in single["remotes"]["units"])
    assert single["remotes"]["count"] == single["antenna_count"]


# ========================================================== 4. port ceiling

def test_config_ceiling_is_47_dbm_grade_d(cfg):
    for aid in ("omni_das", "panel_das"):
        a = cfg["antennas"][aid]
        assert a["max_port_dbm"] == 47.0
        assert a["max_port_dbm_grade"] == "D"
        assert "Matt 2026-11-17" in a["max_port_dbm_ref"]


def test_high_power_source_no_longer_clamped_at_20_dbm(cfg):
    """The 47 dBm ceiling leaves calculated port powers above the old 20 dBm
    stand-in in place; only ports above 47 dBm are clamped."""
    def chain(injection):
        return {"environment": "office", "floors": [{
            "id": "f1", "level": 0, "ppm": 10.0, "w_px": 100, "h_px": 100,
            "walls": [], "elements": [
                {"id": "s", "type": "source", "subtype": "direct",
                 "injection_dbm": injection},
                {"id": "a", "type": "antenna", "antenna_id": "omni_das",
                 "x": 5, "y": 5},
                {"id": "r1", "type": "cable_run", "from_id": "s", "to_id": "a",
                 "cable_id": "coax_1_2in", "length_m": 5.0}]}]}
    loss = cable_loss_db(cfg["cables"]["coax_1_2in"], 5.0, 1960.0)
    warm = solve_cascade(chain(40.0), cfg)["bands"]["B2_1900"]["antennas"][0]
    assert warm["capped"] is False
    assert warm["port_dbm"] == pytest.approx(40.0 - loss, abs=0.01)
    assert warm["port_dbm"] > 20.0
    hot = solve_cascade(chain(60.0), cfg)["bands"]["B2_1900"]["antennas"][0]
    assert hot["capped"] is True
    assert hot["port_dbm"] == pytest.approx(47.0)
    # the clamp still governs in autoplace: a 47 dBm head end is never capped
    body = request(threshold_dbm=-70.0, repeater={"max_dl_out_dbm": 47.0},
                   source={"subtype": "direct", "injection_dbm": 47.0})
    res = autoplace_floor(make_model(600, 400), cfg, "f1", body)
    assert res["cascade"]["capped_antennas"]["B2_1900"] == 0
    assert max(a["eirp_dbm"]["B2_1900"] for a in res["antennas"]) > 40.0
