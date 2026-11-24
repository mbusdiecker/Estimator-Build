"""Physics update 5: find the building on the sheet before tracing the
outline.

isolate_building (run by scale_to_area before the perimeter trace): the
sheet frame and inner border, the title-block strip and small margin items
drop, and the trace, the scale and the floor walls come from the kept
building line work only. Every synthetic sheet is drawn in-test (pymupdf
vector PDFs and ezdxf), so every expected count is hand-known. The QA cases
behind the update: 6 of 16 real carrier sheets traced the sheet frame, a
title block or a hull over margin boxes instead of the building.
"""

import math

import pytest

from romtool.plan_walls import (PlanWallsError, floor_from_plan,
                                isolate_building, plan_walls_params,
                                scale_to_area, walls_from_dxf, walls_from_pdf)

SQFT_PER_M2 = 1.0 / 0.09290304


@pytest.fixture(scope="module")
def p():
    return plan_walls_params()


def rect(x0, y0, x1, y1):
    return [[x0, y0, x1, y0], [x1, y0, x1, y1], [x1, y1, x0, y1],
            [x0, y1, x0, y0]]


def l_shape(x0, y0, x1, y1, nx, ny):
    """L building = full rectangle minus the lower-left notch (notch corner
    at 2/3 of the width and 2/3 of the height), with a hand-known interior
    partition grid: nx verticals / ny horizontals per lobe. Returns the
    segments and the L's exact area."""
    xm, ym = x0 + 2.0 / 3.0 * (x1 - x0), y0 + 2.0 / 3.0 * (y1 - y0)
    segs = [[x0, y0, x1, y0], [x1, y0, x1, y1], [x1, y1, xm, y1],
            [xm, y1, xm, ym], [xm, ym, x0, ym], [x0, ym, x0, y0]]
    for i in range(1, nx + 1):
        x = x0 + (xm - x0) * i / (nx + 1.0)
        segs.append([x, y0, x, ym])
    for j in range(1, ny + 1):
        y = y0 + (ym - y0) * j / (ny + 1.0)
        segs.append([x0, y, xm, y])
    for i in range(1, nx + 1):
        x = xm + (x1 - xm) * i / (nx + 1.0)
        segs.append([x, y0, x, y1])
    for j in range(1, ny + 2):
        y = y0 + (y1 - y0) * j / (ny + 2.0)
        segs.append([xm, y, x1, y])
    area = (x1 - x0) * (y1 - y0) - (xm - x0) * (y1 - ym)
    return segs, area


def main_sheet_segments():
    """The QA sheet layout: outer frame, inner border, a title block of long
    parallel verticals plus box dividers along the right edge, two legend
    boxes and a north arrow in the margins, and an L-shaped building with
    interior walls, off centre, covering roughly a third of the frame.

    Hand anchors: outer frame rect(10,10,450,360) = 1580 drawing units; the
    L spans (150,25)..(372,348) with nx=6, ny=7 -> area 55774.25 units2."""
    s = []
    s += rect(10, 10, 450, 360)                    # outer frame: 4 segments
    s += rect(16, 16, 444, 354)                    # inner border: 4 segments
    for x in (384.0, 404.0, 424.0):                # title block verticals
        s.append([x, 16, x, 354])
    y = 61.0                                       # title block box dividers
    while y < 354:
        s.append([384.0, y, 444, y])
        y += 45.0
    s += rect(30, 316, 85, 346)                    # legend box 1: 4 segments
    s += rect(95, 316, 140, 346)                   # legend box 2: 4 segments
    s += [[30, 280, 44, 296], [44, 296, 44, 280],
          [44, 280, 30, 280]]                      # north arrow: 3 segments
    bsegs, area = l_shape(150, 25, 372, 348, 6, 7)  # the building: 33 segments
    s += bsegs
    return s, area


def _run_sheet_body(raw, area, p):
    """Assertions shared by the pymupdf and ezdxf main-sheet variants."""
    r = isolate_building(raw, params=p)
    assert len(r["dropped"]["frame"]) == 8        # outer frame + inner border
    assert len(r["dropped"]["title_block"]) == 10  # 3 verticals + 7 dividers
    assert len(r["dropped"]["small_cluster"]) == 11  # 2 legends + arrow
    assert len(r["kept"]) == 33
    assert r["kept_share"] > 0.5
    # everything kept belongs to the building box
    bb = (150, 25, 372, 348)
    for s in r["kept"]:
        for x, y in ((s[0], s[1]), (s[2], s[3])):
            assert bb[0] - 1 <= x <= bb[2] + 1 and bb[1] - 1 <= y <= bb[3] + 1

    scaled = scale_to_area(raw, area * SQFT_PER_M2, isolate=True)
    assert scaled["scale_source"] == "perimeter"
    assert scaled["flags"] == []
    assert scaled["isolate"]["dropped"] == {"frame": 8, "title_block": 10,
                                            "small_cluster": 11}
    assert scaled["isolate"]["kept_share"] > 0.5
    assert scaled["m_per_unit"] == pytest.approx(1.0, rel=0.05)
    # the traced outline is the L, within 5 % of its true area
    o_area = scaled["perimeter"]["area_units_sq"]
    assert o_area == pytest.approx(area, rel=0.05)
    # a known wall length scales correctly: the whole kept line work is the
    # building's, at ~1 m per unit
    kept_len = sum(math.hypot(s[2] - s[0], s[3] - s[1]) for s in r["kept"])
    wall_len = sum(math.hypot(s[2] - s[0], s[3] - s[1])
                   for s in scaled["segments"])
    assert wall_len == pytest.approx(kept_len, rel=0.03)
    return scaled


def test_pdf_sheet_cleanup_traces_the_building(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    segs, area = main_sheet_segments()
    doc = pymupdf.open()
    page = doc.new_page(width=460, height=370)
    for s in segs:
        page.draw_line(tuple(s[:2]), tuple(s[2:]))
    path = str(tmp_path / "sheet.pdf")
    doc.save(path)
    raw = walls_from_pdf(path)["segments"]
    assert len(raw) >= 40
    scaled = _run_sheet_body(raw, area, plan_walls_params())
    floor = floor_from_plan(scaled, area * SQFT_PER_M2, floor_id="f1")
    assert floor["plan"]["flags"] == []
    assert floor["plan"]["isolate"]["dropped"]["frame"] == 8
    assert "outline_density" in floor["plan"]
    od = floor["plan"]["outline_density"]
    assert od["wall_m_per_m2"] > 0 and od["crossings_per_m_mean"] is not None
    assert od["area_m2"] == pytest.approx(area, rel=0.05)


def test_dxf_sheet_cleanup_traces_the_building(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    segs, area = main_sheet_segments()
    doc = ezdxf.new("R2013")
    msp = doc.modelspace()
    for s in segs:
        msp.add_line(tuple(s[:2]), tuple(s[2:]), dxfattribs={"layer": "PLAN"})
    path = tmp_path / "sheet.dxf"
    doc.saveas(str(path))
    raw = walls_from_dxf(str(path))["segments"]
    assert len(raw) >= 40
    _run_sheet_body(raw, area, plan_walls_params())


def test_building_nearly_fills_the_frame(p):
    """About 85 % of the frame: the building is kept whole, nothing of it
    is dropped."""
    s = rect(5, 5, 655, 515)
    s += rect(25, 25, 635, 465)                # the building, 84 % of the frame
    for x in (150, 300, 450):                  # interior partitions
        s.append([x, 25, x, 465])
    for y in (150, 300):
        s.append([25, y, 635, y])
    r = isolate_building(s, params=p)
    assert len(r["dropped"]["frame"]) + len(r["dropped"]["title_block"]) == 4
    assert r["dropped"]["small_cluster"] == []
    assert len(r["kept"]) == 9
    assert r["kept_share"] > 0.6
    bb = bbox2(r["kept"])
    assert bb == pytest.approx((25, 25, 635, 465), abs=1e-6)
    scaled = scale_to_area(s, 268400.0 * SQFT_PER_M2, isolate=True)
    assert scaled["flags"] == []
    assert scaled["scale_source"] == "perimeter"
    assert scaled["m_per_unit"] == pytest.approx(1.0, rel=0.05)


def bbox2(segs):
    xs = [c for s in segs for c in (s[0], s[2])]
    ys = [c for s in segs for c in (s[1], s[3])]
    return (min(xs), min(ys), max(xs), max(ys))


def test_two_wings_and_a_detached_third_are_kept(p):
    """Two wings joined by a corridor plus a detached third wing a short gap
    away (3.4 % of the drawing, inside the satellite gap of 5 %): all three
    wings are kept."""
    s = rect(20, 150, 120, 250)                          # wing A
    s += [[120, 190, 200, 190], [120, 210, 200, 210]]    # corridor walls
    s += rect(200, 150, 300, 250)                        # wing B
    s += rect(314, 150, 414, 250)                        # wing C, 14-unit gap
    r = isolate_building(s, params=p)
    assert r["kept_share"] == pytest.approx(1.0)
    assert r["dropped"] == {"frame": [], "title_block": [], "small_cluster": []}
    assert len(r["kept"]) == 14
    # the wings were recognized as one connected cluster + one satellite, not
    # silently merged: the third wing's east wall is beyond the close gap.
    # The trace encloses the whole joined mass, so the scale follows the
    # enclosed area rather than the union of wall lengths: the scale is
    # reported from the perimeter either way.
    scaled = scale_to_area(s, 21600.0 * SQFT_PER_M2)
    assert scaled["flags"] == []
    assert scaled["scale_source"] == "perimeter"
    assert scaled["isolate"]["kept_share"] == pytest.approx(1.0)
    assert scaled["n_kept"] == 14


def test_frame_and_title_block_only_is_a_clear_error(p):
    """No building on the sheet: a clear error, not a frame outline."""
    s = rect(5, 5, 655, 515)
    s += rect(15, 15, 645, 505)
    for x in (545, 555, 565):
        s.append([x, 15, x, 505])
    for y in (75, 135, 195, 255, 315, 375, 435, 495):
        s.append([545, y, 645, y])
    r = isolate_building(s, params=p)
    assert r["kept"] == []
    with pytest.raises(PlanWallsError):
        scale_to_area(s, 1000.0, isolate=True)


def _margin_notes(seed=7, blocks=90):
    """Small isolated note-line clusters in the left margin: 8 short
    segments each, well past the satellite gap from anything."""
    import random
    rng = random.Random(seed)
    out = []
    for _ in range(blocks):
        x, y = rng.uniform(20, 135), rng.uniform(20, 300)
        for _ in range(4):
            out.append([x, y, x + rng.uniform(6, 14), y + rng.uniform(-2, 2)])
            out.append([x + rng.uniform(0, 4), y + rng.uniform(3, 9),
                        x + rng.uniform(8, 16), y + rng.uniform(3, 9)])
    return out


def test_flag_low_keep_share(p):
    segs, _area = main_sheet_segments()
    junk = _margin_notes()
    r = isolate_building(segs + junk, params=p)
    assert r["kept_share"] < 0.5
    scaled = scale_to_area(segs + junk, 55774.25 * SQFT_PER_M2, isolate=True)
    assert "low_keep_share" in scaled["flags"]
    assert scaled["warnings"]


def test_flag_outline_touches_raw_bbox(p):
    """A ring building sharing three frame sides: the outline follows the
    outermost line work (the QA frame-leak symptom)."""
    s = rect(5, 5, 655, 515)
    s += rect(5, 30, 655, 515)      # building box on the left/right/bottom
    for x in (100, 200, 300, 400, 500, 600):
        s.append([x, 30, x, 515])
    for y in (100, 170, 240, 310, 400, 470):
        s.append([5, y, 655, y])
    r = isolate_building(s, params=p)
    assert r["dropped"]["frame"] == [] and r["dropped"]["title_block"] == []
    assert r["kept_share"] == pytest.approx(1.0)
    scaled = scale_to_area(s, 300000.0 * SQFT_PER_M2)
    assert "outline_touches_raw_bbox" in scaled["flags"]
    assert scaled["warnings"]


def test_flag_outline_mostly_empty(p):
    """A ring building with dense wall work concentrated in one quadrant:
    the outline covers the whole bounding box but most of it is empty."""
    s = rect(20, 20, 460, 350)
    for x in range(240, 460, 20):                 # partition grid, top right
        s.append([x, 20, x, 185])
    for y in range(40, 185, 20):
        s.append([240, y, 460, y])
    r = isolate_building(s, params=p)
    assert r["dropped"] == {"frame": [], "title_block": [], "small_cluster": []}
    assert r["kept_share"] == pytest.approx(1.0)
    scaled = scale_to_area(s, 145200.0 * SQFT_PER_M2)
    assert "outline_mostly_empty" in scaled["flags"]
    assert "outline_touches_raw_bbox" in scaled["flags"]


def test_flags_stay_quiet_on_the_clean_cases(p):
    for sheet, area in (
            (main_sheet_segments()[0], 55774.25),
            # the clean two-wing sheet
            (rect(20, 150, 120, 250) + [[120, 190, 200, 190],
                                        [120, 210, 200, 210]]
             + rect(200, 150, 300, 250) + rect(314, 150, 414, 250), 30000.0),
    ):
        scaled = scale_to_area(sheet, area * SQFT_PER_M2, isolate=True)
        assert scaled["flags"] == []
        warn = [w for w in scaled["warnings"] if "cleanup" in w.lower()
                and "kept only" in w.lower()]
        assert warn == []


def test_isolate_off_keeps_raw_behaviour():
    """isolate=False preserves the old scale_to_area inputs (walls from a
    frame-less building import unchanged)."""
    segs = [[0, 0, 400, 0], [400, 0, 400, 250], [400, 250, 0, 250],
            [0, 250, 0, 0]]
    scaled = scale_to_area(segs, 1000.0 / 0.09290304, use_perimeter=False,
                           isolate=False)
    assert scaled["n_raw"] == 4 and scaled["n_kept"] == 4
    assert scaled["isolate"]["attempted"] is False
    assert scaled["flags"] == []


def test_outline_density_metric_shapes():
    """outline_density: the ray fan counts only what is inside the outline."""
    from romtool.plan_walls import outline_density
    # a 4 x 2 m floor at 2 px/m: outline 8 x 4 px
    floor = {
        "ppm": 2.0, "w_px": 9, "h_px": 5,
        "outline": [[0, 0], [8, 0], [8, 4], [0, 4]],
        "walls": [{"x1": 0, "y1": 0, "x2": 8, "y2": 0, "material": "drywall"},
                  {"x1": 0, "y1": 4, "x2": 8, "y2": 4, "material": "drywall"},
                  {"x1": 4, "y1": 0, "x2": 4, "y2": 4, "material": "drywall"}],
    }
    od = outline_density(floor, n_rays=180)
    assert od is not None
    assert od["area_m2"] == pytest.approx(8.0, rel=0.01)
    assert od["wall_m_per_m2"] == pytest.approx(10.0 / 8.0, rel=0.05)
    assert od["crossings_per_m_mean"] > 0
    # without an outline there is nothing to report
    assert outline_density(dict(floor, outline=[])) is None


def test_plan_walls_params_validates():
    from romtool.plan_walls import PlanWallsError as E
    assert plan_walls_params()["frame_edge_tol_frac"] == 0.015
    with pytest.raises(E):
        plan_walls_params(unknown_thing=1.0)
    with pytest.raises(E):
        plan_walls_params(flag_keep_share=0.0)
