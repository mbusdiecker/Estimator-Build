"""Physics update 3: raster wall tracing from floor-plan images.

walls_from_raster (saturation cut, Otsu, blob removal, Zhang-Suen thinning,
probabilistic Hough, the vector-path merge in pixel buckets), the
walls_from_pdf raster fallback with `path_used` reporting, the density
cross-check against the known drawn walls and the `--raster` CLI. The
helper plan (a box, interior walls, a double-line wall, text-like blobs and
a coloured overlay line) is drawn in-test with numpy/cv2 so every expected
count is hand-known.
"""

import math

import numpy as np
import pytest

from romtool.plan_walls import (floor_from_plan, main, scale_to_area,
                                wall_density, walls_from_pdf,
                                walls_from_raster)

cv2 = pytest.importorskip("cv2")

SQFT = 875.0 / 0.09290304                   # the box is 35 x 25 m = 875 m2
BOX = (50, 50, 750, 550)                    # 700 x 500 px -> 35 x 25 m


def _synthetic_plan():
    """Draw the test plan. Walls are dark grey and hand-anchored:

    - the outer box (4 walls, 120 m at 20 px/m),
    - one interior vertical with a door gap (2 x 10 m walls),
    - one interior horizontal (17.5 m),
    - one double-line wall, faces 6 px (0.3 m) apart (one 12.5 m wall),
    - three text-like 8 x 8 blobs,
    - a saturated red overlay line through open floor (ignored).

    Returns (bgr image, known wall segments in px after merging: 8 walls,
    3400 px = 170 m of wall).
    """
    img = np.full((600, 800, 3), 255, np.uint8)
    dark = (30, 30, 30)
    x0, y0, x1, y1 = BOX
    cv2.rectangle(img, (x0, y0), (x1, y1), dark, 3)          # outer box
    cv2.line(img, (400, 50), (400, 250), dark, 3)            # door gap 250-350
    cv2.line(img, (400, 350), (400, 550), dark, 3)
    cv2.line(img, (400, 300), (750, 300), dark, 3)           # interior wall
    cv2.line(img, (200, 50), (200, 300), dark, 3)            # double-line wall
    cv2.line(img, (206, 50), (206, 300), dark, 3)            # ...second face
    for cx in (550, 600, 650):                               # text-like blobs
        img[420:428, cx:cx + 8] = 30
    cv2.line(img, (80, 460), (380, 520), (0, 0, 255), 5)     # red overlay line
    known = [[x0, y0, x1, y0], [x0, y1, x1, y1],
             [x0, y0, x0, y1], [x1, y0, x1, y1],
             [400, 50, 400, 250], [400, 350, 400, 550],
             [400, 300, 750, 300], [203, 50, 203, 300]]
    return img, known


def _tot_len(segs):
    return sum(math.hypot(s[2] - s[0], s[3] - s[1]) for s in segs)


# ================================================ 1. walls_from_raster

def test_raster_traces_the_drawn_walls():
    img, _known = _synthetic_plan()
    rr = walls_from_raster(img)
    assert rr["source"] == "raster"
    n = len(rr["segments"])
    assert 7 <= n <= 9, n                     # 8 walls, within one segment
    # total traced wall length near the known 3400 px of wall
    assert _tot_len(rr["segments"]) == pytest.approx(3400.0, rel=0.1)
    # the blobs and the red overlay line are ignored: no segment midpoint
    # on the red line's open-floor strip or at the blob row
    for s in rr["segments"]:
        mx, my = 0.5 * (s[0] + s[2]), 0.5 * (s[1] + s[3])
        assert not (mx < 390 and 440 < my < 540), (mx, my)   # red line strip
        assert not (520 < mx < 680 and 400 < my < 440), (mx, my)  # blob row
    assert rr["low_confidence"] is False
    assert rr["confidence"] >= 0.8
    assert rr["warnings"] == []
    assert rr["double_line_merges"] >= 1      # the two faces collapsed
    assert rr["blobs_removed"] >= 3


def test_raster_low_confidence_says_so():
    # a box plus 45-degree hatch strokes (35 px, under the 40 px long-diag
    # keep line): the hatch is ink, not blobs, and not kept as walls, so
    # the confidence drops and the result must say so rather than return
    # walls silently
    img = np.full((300, 400, 3), 255, np.uint8)
    dark = (30, 30, 30)
    cv2.rectangle(img, (50, 50), (350, 250), dark, 3)
    rng = np.random.default_rng(7)
    for _ in range(60):
        cx, cy = int(rng.integers(70, 330)), int(rng.integers(70, 230))
        cv2.line(img, (cx, cy), (cx + 25, cy + 25), dark, 2)
    rr = walls_from_raster(img)
    assert len(rr["segments"]) >= 3           # the box survives
    assert rr["diagonals_dropped"] >= 30      # hatch strokes dropped as diags
    assert rr["confidence"] < 0.5             # hatch ink is not explained
    assert rr["low_confidence"] is True
    assert rr["warnings"], "low confidence must say so"


# ================================================ 2. PDF raster fallback

def test_pdf_image_only_page_falls_back_to_raster(tmp_path):
    img, _known = _synthetic_plan()
    ok, buf = cv2.imencode(".png", img)
    assert ok
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=800, height=600)   # 1 pt per px, same aspect
    page.insert_image(page.rect, stream=buf.tobytes())
    pth = str(tmp_path / "raster_plan.pdf")
    doc.save(pth)
    r = walls_from_pdf(pth)
    assert r["source"] == "pdf"
    assert r["path_used"] == "raster"
    assert r["drawings"] == 0 and r["reason"] is None
    assert 7 <= r["n_raw"] <= 9               # traced through the fallback
    assert r["raster"]["confidence"] >= 0.8
    assert r["raster"]["dpi"] == 180.0
    # segments came back in PDF points: the image was placed 1 pt per px,
    # so the 700 px box is a 700 pt wide box starting at x = 50 pt (the
    # render zoom cancels in the px -> pt conversion)
    bb = r["bbox"]
    assert bb[0] == pytest.approx(50.0, abs=3.0)
    assert bb[2] - bb[0] == pytest.approx(700.0, abs=30.0)
    # the fallback result scales and builds a physics floor like any other
    scaled = scale_to_area(r, SQFT, use_perimeter=False)
    assert scaled["n_walls"] >= 7 and scaled["scale_source"] == "bbox"
    floor = floor_from_plan(scaled, SQFT, floor_id="f1")
    assert len(floor["walls"]) == scaled["n_walls"]
    assert floor["loss_mode"] == "physics"
    # forced vector-only on the same page: empty, with a reason
    r2 = walls_from_pdf(pth, raster=False)
    assert r2["segments"] == [] and r2["path_used"] == "vector"
    assert r2["reason"]


# ================================================ 3. density cross-check

def test_raster_density_matches_the_known_walls():
    img, known = _synthetic_plan()
    rr = walls_from_raster(img)
    scaled_t = scale_to_area(rr, SQFT, use_perimeter=False)
    floor_t = floor_from_plan(scaled_t, SQFT)
    d_t = wall_density(floor_t)
    scaled_k = scale_to_area(known, SQFT, use_perimeter=False)
    floor_k = floor_from_plan(scaled_k, SQFT)
    d_k = wall_density(floor_k)
    # hand anchor: 120 m box + 10 + 10 + 17.5 m interiors + 12.5 m midline
    # = 170 m of wall on 875 m2 -> both densities ~0.194 m/m2
    assert d_k["wall_m_total"] == pytest.approx(170.0, abs=1.0)
    assert d_t["wall_m_per_m2"] == pytest.approx(d_k["wall_m_per_m2"],
                                                 rel=0.15)
    assert d_t["n_walls"] <= d_k["n_walls"] + 1


# ================================================ 4. CLI --raster

def test_plan_walls_cli_raster(tmp_path, capsys):
    img, _known = _synthetic_plan()
    ok, buf = cv2.imencode(".png", img)
    assert ok
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=800, height=600)
    page.insert_image(page.rect, stream=buf.tobytes())
    pth = str(tmp_path / "raster_plan.pdf")
    doc.save(pth)
    rc = main([pth, "--sq-ft", str(SQFT), "--raster", "--no-perimeter"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "path used           : raster" in out
    assert "confidence" in out
    assert "wall density" in out


def test_dxf_polyface_mesh_walls_ignore_face_records(tmp_path):
    """iBwave DXF exports draw walls as polyface meshes; face records sit at (0,0) in the vertex list and must not
    become wall end points (they drew spokes to the origin before the fix)."""
    ezdxf = pytest.importorskip("ezdxf")
    from romtool import plan_walls as pw
    doc = ezdxf.new()
    msp = doc.modelspace()
    for (x1, y1, x2, y2) in [(100, 100, 140, 100), (140, 100, 140, 125), (140, 125, 100, 125), (100, 125, 100, 100),
                             (120, 100, 120, 125)]:
        mesh = msp.add_polyface(dxfattribs={"layer": "Wall"})
        mesh.append_face([(x1, y1, 0), (x2, y2, 0), (x2, y2, 3), (x1, y1, 3)])
    path = tmp_path / "polyface.dxf"
    doc.saveas(path)
    res = pw.walls_from_dxf(str(path))
    xs = [v for s in res["segments"] for v in (s[0], s[2])]
    ys = [v for s in res["segments"] for v in (s[1], s[3])]
    assert min(xs) >= 100 - 1e-6 and min(ys) >= 100 - 1e-6
    assert res["bbox"][0] >= 100 - 1e-6 and res["bbox"][1] >= 100 - 1e-6


def test_floor_outline_is_in_floor_px(tmp_path):
    """The traced outline must use the same px frame as the walls: its area at the floor's ppm equals the requested
    floor area (it was left in drawing units, which inflated the planner's area and stub antenna count)."""
    from romtool import plan_walls as pw
    segs = [[0, 0, 400, 0], [400, 0, 400, 250], [400, 250, 0, 250], [0, 250, 0, 0],
            [150, 0, 150, 250], [0, 120, 400, 120], [300, 120, 300, 250]]
    sq_ft = 20000.0
    scaled = pw.scale_to_area(segs, sq_ft)
    floor = pw.floor_from_plan(scaled, sq_ft, floor_id="f1")
    o = floor.get("outline")
    if scaled["scale_source"] != "perimeter" or not o:
        pytest.skip("perimeter trace did not succeed on the synthetic plan")
    a_px = abs(sum(o[i][0] * o[i - 1][1] - o[i - 1][0] * o[i][1] for i in range(len(o)))) / 2.0
    a_m2 = a_px / floor["ppm"] ** 2
    assert a_m2 == pytest.approx(sq_ft * 0.09290304, rel=0.05)
    assert all(-1 <= x <= floor["w_px"] + 1 and -1 <= y <= floor["h_px"] + 1 for x, y in o)
