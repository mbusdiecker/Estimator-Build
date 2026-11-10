"""Plan import and automatic building perimeter (romtool/perimeter.py).

Synthetic plans: white background, walls drawn as black bars. A box with outer face
[x0, x1) x [y0, y1) and wall thickness t has its walls inside that rectangle, so the
traced perimeter should be the rectangle itself. At 10 px per metre a 30 m x 20 m
box is 300 x 200 px and 600 m2 (60000 px2).
"""

import math

import pytest

from romtool.autoplace import autoplace_floor
from romtool.clutter_map import PNG_SIG, decode_png, encode_gray_png
from romtool.config import load_config
from romtool.drawings import floor_layout_svg
from romtool.geometry import point_in_polygon
from romtool.perimeter import (PerimeterError, edit_outline, perimeter_params,
                               polygon_area, scale_from_area, trace_floor, trace_outline)

W, H = 400, 300
X0, Y0, X1, Y1 = 50, 50, 350, 250          # 300 x 200 px box, outer face
AREA_PX = (X1 - X0) * (Y1 - Y0)            # 60000
AREA_M2 = AREA_PX / 100.0                  # 600 m2 at 10 px/m
SRC = {"subtype": "direct", "injection_dbm": 15.0}
REP = {"max_dl_out_dbm": 33.0}


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def blank(w=W, h=H):
    return bytearray(b"\xff" * (w * h))


def fill(g, w, x0, y0, x1, y1, v=0):
    for y in range(y0, y1):
        base = y * w
        g[base + x0:base + x1] = bytes([v]) * (x1 - x0)


def box_walls(g, w, x0, y0, x1, y1, t=4, door=None):
    """Four walls inside [x0,x1) x [y0,y1). door=(x_start, width) cuts the bottom wall."""
    fill(g, w, x0, y0, x1, y0 + t)
    fill(g, w, x0, y1 - t, x1, y1)
    fill(g, w, x0, y0, x0 + t, y1)
    fill(g, w, x1 - t, y0, x1, y1)
    if door:
        xa, gw = door
        fill(g, w, xa, y1 - t, xa + gw, y1, 255)
    return g


def building(door=(150, 9)):
    return box_walls(blank(), W, X0, Y0, X1, Y1, door=door)


def rect_outline(x0=X0, y0=Y0, x1=X1, y1=Y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def close(a, b, rel):
    return abs(a - b) <= rel * abs(b)


# -------------------------------------------------------------- tracing

def test_rectangle_with_door_gap_traces_to_the_rectangle(cfg):
    res = trace_outline(bytes(building()), W, H, 10.0, cfg)
    assert close(res["area_px"], AREA_PX, 0.03)
    assert len(res["outline"]) == 4
    xs = sorted({p[0] for p in res["outline"]})
    ys = sorted({p[1] for p in res["outline"]})
    assert xs == [X0, X1] and ys == [Y0, Y1]
    assert close(res["area_m2"], AREA_M2, 0.03)
    assert res["warnings"] == []


def test_l_shaped_building_traces_to_an_l(cfg):
    g = blank()
    box_walls(g, W, 50, 50, 350, 150)
    box_walls(g, W, 50, 50, 150, 250, door=(100, 9))
    res = trace_outline(bytes(g), W, H, 10.0, cfg)
    want = 300 * 100 + 100 * 100                    # the two arms, 40000 px2
    assert close(res["area_px"], want, 0.03)
    assert len(res["outline"]) == 6
    assert sorted(p for p in map(tuple, res["outline"])) == sorted(
        [(50, 50), (350, 50), (350, 150), (150, 150), (150, 250), (50, 250)])


def test_interior_walls_do_not_change_the_perimeter(cfg):
    base = trace_outline(bytes(building()), W, H, 10.0, cfg)
    g = building()
    fill(g, W, 150, Y0, 154, Y1)                    # partition across
    fill(g, W, X0, 150, X1, 154)                    # cross partition
    fill(g, W, 200, 100, 204, 180)                  # stub wall
    inner = trace_outline(bytes(g), W, H, 10.0, cfg)
    assert inner["outline"] == base["outline"]
    assert inner["area_px"] == base["area_px"]


def test_text_near_the_border_does_not_leak_in(cfg):
    base = trace_outline(bytes(building()), W, H, 10.0, cfg)
    g = building()
    # 3 x 5 px glyphs, 1 px apart, 4 px outside the east wall (inside the gap-close radius)
    for row, y in enumerate((100, 112, 124)):
        for i in range(6):
            fill(g, W, X1 + 4 + i * 4, y, X1 + 7 + i * 4, y + 5)
    # and glyphs just inside the north wall
    for i in range(6):
        fill(g, W, 120 + i * 4, Y0 + 5, 123 + i * 4, Y0 + 10)
    res = trace_outline(bytes(g), W, H, 10.0, cfg)
    assert res["outline"] == base["outline"]
    assert res["work"]["specks_dropped"] >= 1


def test_sheet_border_frame_is_ignored(cfg):
    g = building()
    fill(g, W, 5, 5, W - 5, 7)
    fill(g, W, 5, H - 7, W - 5, H - 5)
    fill(g, W, 5, 5, 7, H - 5)
    fill(g, W, W - 7, 5, W - 5, H - 5)
    res = trace_outline(bytes(g), W, H, 10.0, cfg)
    assert close(res["area_px"], AREA_PX, 0.03)
    assert any("sheet-border" in w for w in res["warnings"])


def test_unknown_scale_uses_pixel_fallbacks_and_warns(cfg):
    res = trace_outline(bytes(building()), W, H, None, cfg)
    assert res["area_m2"] is None and res["ppm"] is None
    assert any("scale not set" in w for w in res["warnings"])
    assert close(res["area_px"], AREA_PX, 0.03)


def test_no_building_raises(cfg):
    with pytest.raises(PerimeterError):
        trace_outline(bytes(blank()), W, H, 10.0, cfg)


def test_open_exterior_does_not_trace_the_building(cfg):
    # south and east walls missing: the gap is too wide to close, so the traced
    # footprint is only the walls that remain. The trace is then far smaller than
    # the building; the overlay check and a manual outline are the fix (README).
    g = box_walls(blank(), W, X0, Y0, X1, Y1)
    fill(g, W, X0, Y1 - 4, X1, Y1, 255)
    fill(g, W, X1 - 4, Y0, X1, Y1, 255)
    res = trace_outline(bytes(g), W, H, 10.0, cfg)
    assert res["area_px"] < 0.5 * AREA_PX


def frame_rect(g, w, x0, y0, x1, y1, t=2):
    """A thin rectangular ring with its outer face at [x0, x1) x [y0, y1)."""
    fill(g, w, x0, y0, x1, y0 + t)
    fill(g, w, x0, y1 - t, x1, y1)
    fill(g, w, x0, y0, x0 + t, y1)
    fill(g, w, x1 - t, y0, x1, y1)


def test_double_sheet_frame_with_title_text_and_notch_is_peeled(cfg):
    """Building (L-shaped, notch 8 m x 6 m in the south-east corner) inside a double
    sheet frame, with title-block text outside the frame. Span rule off so the
    peel rule does the work."""
    CW, CH = 500, 380
    g = blank(CW, CH)
    frame_rect(g, CW, 10, 10, 490, 370)            # outer border
    frame_rect(g, CW, 17, 17, 483, 363)            # inner border
    box_walls(g, CW, 60, 60, 380, 240)             # building, east arm
    box_walls(g, CW, 60, 60, 300, 300)             # building, south arm (notch at 300..380 x 240..300)
    for row in range(3):                           # title text below the frame
        for i in range(10):
            fill(g, CW, 300 + i * 4, 374 + row * 2, 303 + i * 4, 379)
    res = trace_outline(bytes(g), CW, CH, 10.0, cfg, frame_frac=0.99)
    assert res["work"]["frames_peeled"] == 2
    assert any("peeled 2" in w for w in res["warnings"])
    want = 320 * 180 + 240 * 60                    # L with the notch, 72000 px2
    assert close(res["area_px"], want, 0.03)
    assert len(res["outline"]) == 6
    assert (300.0, 240.0) in [tuple(p) for p in res["outline"]]   # notch corner kept


def test_building_with_furniture_against_its_walls_is_not_peeled(cfg):
    """Furniture right against all four walls (blocks 6 px inside each wall, every
    14 px), with and without a continuous ring 2 px inside the walls. Nothing may
    peel, and the trace is the building itself."""
    for ring in (False, True):
        g = building()
        if ring:
            fill(g, W, X0 + 2, Y0 + 2, X1 - 2, Y0 + 4, 0)
            fill(g, W, X0 + 2, Y1 - 4, X1 - 2, Y1 - 2, 0)
            fill(g, W, X0 + 2, Y0 + 2, X0 + 4, Y1 - 2, 0)
            fill(g, W, X1 - 4, Y0 + 2, X1 - 2, Y1 - 2, 0)
        for x in range(X0 + 10, X1 - 10, 14):
            fill(g, W, x, Y0 + 6, x + 4, Y0 + 10, 0)
            fill(g, W, x, Y1 - 10, x + 4, Y1 - 6, 0)
        for y in range(Y0 + 10, Y1 - 10, 14):
            fill(g, W, X0 + 6, y, X0 + 10, y + 4, 0)
            fill(g, W, X1 - 10, y, X1 - 6, y + 4, 0)
        res = trace_outline(bytes(g), W, H, 10.0, cfg, frame_frac=0.99)
        assert res["work"]["frames_peeled"] == 0, ring
        assert close(res["area_px"], AREA_PX, 0.03), ring


def test_span_rule_and_peel_rule_are_separate(cfg):
    """One frame, rectangular building. The span rule drops the frame when frame_frac
    allows it; with peeling off (frame_peel_max 0) the frame stays unless the span
    rule drops it; with the span rule off the peel rule removes it."""
    CW, CH = 400, 300
    g = box_walls(blank(CW, CH), CW, 50, 50, 350, 250)
    frame_rect(g, CW, 6, 6, 394, 294)
    span = trace_outline(bytes(g), CW, CH, 10.0, cfg, frame_peel_max=0)
    assert span["work"]["frames_dropped"] == 1 and span["work"]["frames_peeled"] == 0
    assert close(span["area_px"], AREA_PX, 0.03)
    with pytest.raises(PerimeterError):             # frame kept: footprint is the whole page
        trace_outline(bytes(g), CW, CH, 10.0, cfg, frame_frac=0.99, frame_peel_max=0)
    peel = trace_outline(bytes(g), CW, CH, 10.0, cfg, frame_frac=0.99)
    assert peel["work"]["frames_peeled"] == 1 and peel["work"]["frames_dropped"] == 0
    assert close(peel["area_px"], AREA_PX, 0.03)


def test_perimeter_params_are_validated(cfg):
    assert perimeter_params(cfg)["gap_m"] == 1.0
    with pytest.raises(PerimeterError):
        perimeter_params(cfg, dark_threshold=0)
    with pytest.raises(PerimeterError):
        perimeter_params(cfg, nonsense=1)


def test_perimeter_config_is_graded_with_a_reference(cfg):
    block = cfg["perimeter"]
    assert block["grade"] == "E" and "tests/test_perimeter.py" in block["ref"]
    assert set(perimeter_params(cfg)) >= {"gap_m", "simplify_m", "snap_deg"}


def test_polygon_area_and_scale_from_area():
    assert polygon_area(rect_outline()) == AREA_PX
    assert scale_from_area(AREA_PX, 600.0) == pytest.approx(10.0)
    with pytest.raises(PerimeterError):
        scale_from_area(AREA_PX, 0)


def test_scale_from_area_sets_ppm_and_area(cfg):
    res = trace_floor(bytes(building()), W, H, None, cfg, area_m2=300.0)
    assert res["scale_source"] == "from_area"
    assert res["ppm"] == pytest.approx(math.sqrt(AREA_PX / 300.0), rel=0.01)
    assert res["area_m2"] == pytest.approx(300.0, rel=0.001)


def test_known_scale_is_kept_when_no_area_given(cfg):
    res = trace_floor(bytes(building()), W, H, 10.0, cfg)
    assert res["scale_source"] == "known" and res["ppm"] == 10.0
    assert close(res["area_m2"], AREA_M2, 0.03)


def test_edit_outline_replaces_and_nudges():
    ol = rect_outline()
    assert edit_outline(ol, points=[[0, 0], [1, 0], [0, 1]]) == [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]
    moved = edit_outline(ol, ppm=10.0, nudges=[{"index": 1, "dx_m": 1.0, "dy_m": -0.5}])
    assert moved[1] == [X1 + 10.0, Y0 - 5.0] and moved[0] == ol[0]
    assert edit_outline(ol, nudges=[{"index": 0, "dx_px": 3, "dy_px": 2}])[0] == [X0 + 3, Y0 + 2]
    with pytest.raises(PerimeterError):
        edit_outline(ol, nudges=[{"index": 9, "dx_px": 1}])
    with pytest.raises(PerimeterError):
        edit_outline(ol, nudges=[{"index": 0, "dx_m": 1}])     # metres need a scale
    with pytest.raises(PerimeterError):
        edit_outline(ol, points=[[0, 0], [1, 1]])


def test_autoplace_keeps_every_antenna_inside_the_traced_outline(cfg):
    res = trace_outline(bytes(building()), W, H, 10.0, cfg)
    floor = {"id": "f1", "name": "F1", "level": 0, "ppm": 10.0, "w_px": W, "h_px": H,
             "walls": [], "elements": [], "outline": res["outline"]}
    model = {"environment": "office", "building_type": "office", "floors": [floor]}
    req = {"bands": ["B2_1900"], "threshold_dbm": -60.0, "source": SRC, "repeater": REP,
           "step_px": 4}
    autoplace_floor(model, cfg, "f1", req)
    ants = [e for e in floor["elements"] if e["type"] == "antenna"]
    assert ants
    for a in ants:
        assert point_in_polygon(a["x"], a["y"], res["outline"]), (a["x"], a["y"])


# ------------------------------------------------------------ PDF and JPEG

def test_pdf_page_renders_and_traces_with_scale_from_area(cfg):
    fitz = pytest.importorskip("fitz")
    from romtool.clutter_map import render_pdf_page
    # 1:100 on paper: 1 m = 28.35 pt, so the 30 x 20 m box is 850.4 x 566.9 pt
    pt_per_m = 28.35
    doc = fitz.open()
    page = doc.new_page(width=1000, height=700)
    x0, y0 = 60.0, 60.0
    bw, bh, t = 30 * pt_per_m, 20 * pt_per_m, 0.4 * pt_per_m
    for rect in ((x0, y0, x0 + bw, y0 + t), (x0, y0 + bh - t, x0 + bw, y0 + bh),
                 (x0, y0, x0 + t, y0 + bh), (x0 + bw - t, y0, x0 + bw, y0 + bh)):
        page.draw_rect(fitz.Rect(*rect), color=None, fill=(0, 0, 0))
    data = doc.tobytes()
    w, h, gray, dpi = render_pdf_page(data, page=1, dpi=72)
    assert (w, h) == (1000, 700) and dpi == 72
    res = trace_floor(gray, w, h, None, cfg, area_m2=600.0)
    # at 72 dpi one point is one pixel, so the true scale is pt_per_m px per m
    assert res["ppm"] == pytest.approx(pt_per_m, rel=0.03)
    assert res["area_m2"] == pytest.approx(600.0, rel=0.001)


def test_pdf_render_caps_pixels_and_checks_page(cfg):
    fitz = pytest.importorskip("fitz")
    from romtool.clutter_map import ClutterMapError, render_pdf_page
    doc = fitz.open()
    doc.new_page(width=2592, height=1728)
    data = doc.tobytes()
    w, h, _gray, dpi = render_pdf_page(data, page=1, dpi=300, max_pixels=8_000_000)
    assert w * h <= 8_000_000 and dpi < 300
    with pytest.raises(ClutterMapError):
        render_pdf_page(data, page=2, dpi=72)


def test_jpeg_plan_traces_with_pillow():
    pil = pytest.importorskip("PIL.Image")
    import io
    from romtool.clutter_map import decode_image
    img = pil.frombytes("L", (W, H), bytes(building()))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    iw, ih, gray = decode_image(buf.getvalue())
    assert (iw, ih) == (W, H)
    from romtool.config import load_config as _lc
    res = trace_outline(gray, iw, ih, 10.0, _lc())
    assert close(res["area_px"], AREA_PX, 0.05)


# ------------------------------------------------------------ drawings

def test_layout_draws_bold_perimeter_and_the_plan(cfg):
    png = encode_gray_png(W, H, bytes(building()))
    assert png[:8] == PNG_SIG and decode_png(png)[2] == bytes(building())
    res = trace_outline(bytes(building()), W, H, 10.0, cfg)
    import base64
    model = {"environment": "office", "floors": [{
        "id": "f1", "name": "F1", "level": 0, "ppm": 10.0, "w_px": W, "h_px": H,
        "walls": [], "elements": [], "outline": res["outline"],
        "image": {"mime": "image/png", "w": W, "h": H,
                  "data_b64": base64.b64encode(png).decode("ascii")}}]}
    svg = floor_layout_svg(model, cfg, "f1")
    assert "data:image/png;base64" in svg
    assert 'stroke="#112732"' in svg and 'stroke-width="3"' in svg
    assert "Building perimeter" in svg
    model["floors"][0]["outline"] = None
    model["floors"][0].pop("outline")
    assert "Building perimeter" not in floor_layout_svg(model, cfg, "f1")


# ------------------------------------------------------------ API

def _project(client, name="Perimeter test"):
    return client.post("/projects", json={"name": name}).json()["id"]


def test_api_plan_import_stores_image_and_outline(client):
    pid = _project(client)
    fid = client.post("/projects/%d/floors" % pid,
                      json={"name": "L1", "ppm": 10}).json()["floor"]["id"]
    base = "/projects/%d/floors/%s" % (pid, fid)
    png = encode_gray_png(W, H, bytes(building()))
    r = client.post(base + "/plan", files={"file": ("plan.png", png, "image/png")},
                    params={"area_m2": 600})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["floor"]["w_px"] == W and body["floor"]["h_px"] == H
    assert body["floor"]["image"]["mime"] == "image/png"
    per = body["perimeter"]
    assert per["scale"]["source"] == "from_area" and per["scale"]["ppm"] == pytest.approx(
        10.0, rel=0.01)
    assert per["area_m2"] == pytest.approx(600.0, rel=0.001) and len(per["outline"]) == 4
    img = client.get(base + "/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    got = client.get(base + "/perimeter")
    assert got.status_code == 200 and got.json()["outline"] == per["outline"]
    svg = client.get(base + "/layout.svg")
    assert svg.status_code == 200 and "Building perimeter" in svg.text


def test_api_manual_override_retrace_and_scale(client):
    pid = _project(client, "Perimeter edits")
    fid = client.post("/projects/%d/floors" % pid,
                      json={"name": "L1", "ppm": 10}).json()["floor"]["id"]
    base = "/projects/%d/floors/%s" % (pid, fid)
    png = encode_gray_png(W, H, bytes(building()))
    assert client.post(base + "/plan", files={"file": ("p.png", png, "image/png")},
                       params={"trace": "false"}).json()["perimeter"]["outline"] is None
    assert client.get(base + "/perimeter").status_code == 404
    assert client.post(base + "/perimeter/trace", json={}).status_code == 200
    first = client.get(base + "/perimeter").json()
    assert first["method"] == "traced" and first["scale"]["source"] == "calibrated"
    moved = client.patch(base + "/perimeter", json={"nudges": [
        {"index": 0, "dx_px": 2, "dy_px": 0}]})
    assert moved.status_code == 200 and moved.json()["method"] == "manual"
    assert moved.json()["outline"][0][0] == first["outline"][0][0] + 2
    pts = client.patch(base + "/perimeter", json={"points": rect_outline(40, 40, 360, 260)})
    assert pts.json()["outline"][2] == [360, 260]
    area = client.patch(base + "/perimeter", json={"area_sqft": 6458.0})
    assert area.json()["scale"]["source"] == "from_area"
    assert area.json()["area_m2"] == pytest.approx(6458.0 * 0.09290304, rel=0.001)
    assert client.patch(base + "/perimeter", json={"points": [[0, 0]]}).status_code == 422
    retr = client.post(base + "/perimeter/trace", json={"gap_m": 0.5})
    assert retr.status_code == 200 and retr.json()["method"] == "traced"
    assert client.patch(base + "/perimeter", json={"clear": True}).status_code == 200
    assert client.get(base + "/perimeter").status_code == 404


def test_api_ppm_source_on_floor_and_patch(client):
    pid = _project(client, "Scale source")
    f = client.post("/projects/%d/floors" % pid, json={"name": "L1"}).json()["floor"]
    assert f["ppm_source"] == "default"
    fid = f["id"]
    f2 = client.patch("/projects/%d/floors/%s" % (pid, fid), json={"ppm": 12.5}).json()["floor"]
    assert (f2["ppm"], f2["ppm_source"]) == (12.5, "calibrated")


def test_api_plan_rejects_unknown_formats_and_missing_image(client):
    pid = _project(client, "Bad plan")
    fid = client.post("/projects/%d/floors" % pid, json={"name": "L1"}).json()["floor"]["id"]
    base = "/projects/%d/floors/%s" % (pid, fid)
    bad = client.post(base + "/plan", files={"file": ("x.gif", b"GIF89a....", "image/gif")})
    assert bad.status_code == 422
    assert client.post(base + "/perimeter/trace", json={}).status_code == 422
