"""Physics update 6: the estimator's building box.

crop_to_box / scale_to_area(box=...) / floor_from_plan(box=...) and the CLI
--box / --box-frac / --preview: the estimator draws a box around the building
on the plan page and only the line work whose midpoint is inside it is kept,
before the trace, the scale and the walls. This replaced brief 5's heuristic
isolate_building as the default way to isolate the building after the
2026-11-18 real-plan QA (isolate kept the title block instead of the
building on 5 of 16 sheets; isolate stays available with isolate=True, off
by default). The sheet helpers come from test_physics_update5, where the
synthetic sheet layout (frame, title block, legend boxes, north arrow, an
L-shaped building of hand-known area) is defined and every expected count is
hand-known.
"""

import json
import math

import pytest

from romtool.plan_walls import (PlanWallsError, crop_to_box,
                                floor_from_plan, main, scale_to_area)

from test_physics_update5 import SQFT_PER_M2, main_sheet_segments

BBOX_UNITS = (10.0, 10.0, 450.0, 360.0)     # bbox of the whole sheet's line work
BUILDING = (150.0, 25.0, 372.0, 348.0)      # the L building, hand anchors
# Box around the building only, drawn clear of the sheet furniture: the
# inner border's bottom edge runs at y = 354, legend 2's east wall at
# x = 140 (midpoints on a box edge count as inside), so both edges of the
# box sit strictly between the furniture and the building (x0 in (140, 150),
# y1 in (348, 354)).
BOX = [145.0, 20.0, 380.0, 352.0]


def box_frac(x0, y0, x1, y1, bb=BBOX_UNITS):
    """The same box as fractions of the sheet's line-work bounding box."""
    return {
        "frac": [(x0 - bb[0]) / (bb[2] - bb[0]), (y0 - bb[1]) / (bb[3] - bb[1]),
                 (x1 - bb[0]) / (bb[2] - bb[0]), (y1 - bb[1]) / (bb[3] - bb[1])],
    }


def _len(s):
    return math.hypot(s[2] - s[0], s[3] - s[1])


def test_crop_to_box_keeps_the_building_only():
    segs, _area = main_sheet_segments()
    kept, info = crop_to_box(segs, BOX)
    assert info["n_in"] == len(segs)
    assert info["n_kept"] == 33                # exactly the building segments
    assert info["kept_share"] > 0.5
    for s in kept:
        for x, y in ((s[0], s[1]), (s[2], s[3])):
            assert BUILDING[0] - 1 <= x <= BUILDING[2] + 1 and \
                BUILDING[1] - 1 <= y <= BUILDING[3] + 1


def test_box_scales_the_building_outline_and_walls():
    """With a box around the building, the outline area is within 5 % of the
    building's area and a known wall scales within 3 %."""
    segs, area = main_sheet_segments()
    scaled = scale_to_area(segs, area * SQFT_PER_M2, box=BOX)
    assert scaled["scale_source"] == "perimeter"
    assert scaled["flags"] == []
    # the box reports what it kept, in drawing units
    assert scaled["box"]["n_kept"] == 33 and scaled["box"]["n_in"] == len(segs)
    assert [round(v, 6) for v in scaled["box"]["box_units"]] == \
        [round(v, 6) for v in BOX]
    # the traced outline is the L, within 5 % of its true area, at ~1 m/unit
    assert scaled["perimeter"]["area_units_sq"] == pytest.approx(area, rel=0.05)
    assert scaled["m_per_unit"] == pytest.approx(1.0, rel=0.05)
    # a known wall scales within 3 %: everything kept belongs to the
    # building, so the walls must carry the building's line length (~1 m/unit)
    wall_len = sum(_len(s) for s in scaled["segments"])
    true_len = sum(
        _len(s) for s in segs
        if BUILDING[0] - 1 <= (s[0] + s[2]) / 2 <= BUILDING[2] + 1 and
        BUILDING[1] - 1 <= (s[1] + s[3]) / 2 <= BUILDING[3] + 1)
    assert true_len < sum(_len(s) for s in segs)   # the box dropped the frame
    assert wall_len == pytest.approx(true_len, rel=0.03)


def test_frac_box_and_units_box_match_exactly():
    """{"frac": ...} and the same box in drawing units give identical
    results."""
    segs, area = main_sheet_segments()
    a = scale_to_area(segs, area * SQFT_PER_M2, box=BOX)
    b = scale_to_area(segs, area * SQFT_PER_M2, box=box_frac(*BOX))
    assert b["m_per_unit"] == a["m_per_unit"]   # same trace, same scale
    assert b["segments"] == a["segments"]
    assert b["scale_source"] == a["scale_source"] == "perimeter"
    assert b["box"]["n_kept"] == a["box"]["n_kept"] == 33
    # floor_from_plan agrees too
    fa = floor_from_plan(segs, area * SQFT_PER_M2, floor_id="f1", box=BOX)
    fb = floor_from_plan(segs, area * SQFT_PER_M2, floor_id="f1",
                         box=box_frac(*BOX))
    assert fa["walls"] == fb["walls"]
    assert fb["plan"]["box"]["box_units"] == \
        pytest.approx(fa["plan"]["box"]["box_units"], abs=1e-6)


def test_floor_from_plan_reports_the_box():
    segs, area = main_sheet_segments()
    floor = floor_from_plan(segs, area * SQFT_PER_M2, floor_id="f1", box=BOX)
    bx = floor["plan"]["box"]
    assert bx["n_kept"] == 33 and bx["n_in"] == len(segs)
    assert bx["box_units"] == [145.0, 20.0, 380.0, 352.0]
    assert bx["kept_share"] > 0.5
    assert floor["plan"]["isolate"]["attempted"] is False
    # without a box nothing is reported and nothing is dropped
    floor_no = floor_from_plan(segs, area * SQFT_PER_M2, floor_id="f2")
    assert "box" in floor_no["plan"] and floor_no["plan"]["box"] is None


def test_empty_box_and_no_area_box_are_clear_errors():
    segs, area = main_sheet_segments()
    with pytest.raises(PlanWallsError):
        scale_to_area(segs, area * SQFT_PER_M2,
                      box=[20.0, 40.0, 145.0, 270.0])      # margin, no line work
    with pytest.raises(PlanWallsError):
        scale_to_area(segs, area * SQFT_PER_M2,
                      box=[140.0, 20.0, 140.0, 355.0])     # zero width
    with pytest.raises(PlanWallsError):
        crop_to_box(segs, {"frac": [0.3, 0.0, 0.3, 1.0]})


def test_isolate_defaults_to_off():
    """scale_to_area without arguments keeps every segment: the frame is not
    dropped (the estimator's box, not the heuristic cleanup, is the
    default)."""
    segs, area = main_sheet_segments()
    scaled = scale_to_area(segs, area * SQFT_PER_M2)
    assert scaled["isolate"]["attempted"] is False
    assert scaled["n_kept"] == scaled["n_raw"] == len(segs)


def _sheet_pdf(tmp_path, name="sheet.pdf"):
    pymupdf = pytest.importorskip("pymupdf")
    segs, area = main_sheet_segments()
    doc = pymupdf.open()
    page = doc.new_page(width=460, height=370)
    for s in segs:
        page.draw_line(tuple(s[:2]), tuple(s[2:]))
    path = tmp_path / name
    doc.save(str(path))
    return str(path), area


def test_cli_writes_the_preview_and_accepts_both_box_forms(tmp_path, capsys):
    path, area = _sheet_pdf(tmp_path)
    sqft = area * SQFT_PER_M2
    png = tmp_path / "preview.png"
    out = tmp_path / "floor.json"
    rc = main([path, "--sq-ft", str(sqft), "--box", "145,20,380,352",
               "--preview", str(png), "--out", str(out)])
    assert rc == 0
    assert png.stat().st_size > 0
    floor = json.loads(out.read_text(encoding="utf-8"))
    assert floor["plan"]["box"]["n_kept"] == 33
    assert floor["plan"]["scale_source"] == "perimeter"
    assert floor["plan"]["isolate"]["attempted"] is False
    # the fraction form gives the same floor
    png2 = tmp_path / "preview_frac.png"
    out2 = tmp_path / "floor_frac.json"
    frac = box_frac(*BOX)
    rc2 = main([path, "--sq-ft", str(sqft),
                "--box-frac", ",".join("%.6f" % v for v in frac["frac"]),
                "--preview", str(png2), "--out", str(out2)])
    assert rc2 == 0
    assert json.loads(out2.read_text(encoding="utf-8"))["walls"] == \
        floor["walls"]
    # the preview exists in the frac form too
    assert png2.stat().st_size > 0
    # --preview works without a box (grid only) ...
    png3 = tmp_path / "preview_nobox.png"
    rc3 = main([path, "--sq-ft", str(sqft), "--preview", str(png3),
                "--no-perimeter"])
    assert rc3 == 0 and png3.stat().st_size > 0
    # ... and both box forms together are refused
    rc4 = main([path, "--sq-ft", str(sqft), "--box", "1,2,3,4",
                "--box-frac", "0,0,1,1"])
    assert rc4 == 2
    assert "not both" in capsys.readouterr().err


def test_cli_isolate_flag_still_runs_the_cleanup(tmp_path, capsys):
    path, area = _sheet_pdf(tmp_path)
    out = tmp_path / "floor_iso.json"
    rc = main([path, "--sq-ft", str(area * SQFT_PER_M2), "--isolate",
               "--out", str(out)])
    assert rc == 0
    assert "sheet cleanup" in capsys.readouterr().out
    floor = json.loads(out.read_text(encoding="utf-8"))
    assert floor["plan"]["isolate"]["attempted"] is True
    assert floor["plan"]["isolate"]["dropped"]["frame"] == 8
    assert floor["plan"]["isolate"]["dropped"]["title_block"] == 10
    assert floor["plan"]["isolate"]["dropped"]["small_cluster"] == 11


def test_cli_bad_box_is_a_clean_error(tmp_path, capsys):
    path, area = _sheet_pdf(tmp_path)
    rc = main([path, "--sq-ft", str(area * SQFT_PER_M2), "--box", "1,2,3"])
    assert rc == 2
    assert "x0,y0,x1,y1" in capsys.readouterr().err
    rc = main([path, "--sq-ft", str(area * SQFT_PER_M2),
               "--box", "20,40,145,270"])
    assert rc == 2
    assert "no line work inside the building box" in capsys.readouterr().err
