"""One-line diagram and floor layout drawings (SVG and PNG) and their endpoints.

The model is autoplaced with remotes and taps on, two bands, on a small floor
(600 x 400 px at 10 px/m, step 4) so the whole file runs in a few seconds.
"""

import struct
import xml.etree.ElementTree as ET

import pytest

from romtool.autoplace import autoplace_floor
from romtool.cascade import solve_cascade
from romtool.config import load_config
from romtool.drawings import (FLOOR_PAD, FLOOR_TITLE, ONE_LINE_PNG, _antenna_power, _floor_scene,
                              _one_line_scene, _pick_k, floor_layout_png, floor_layout_svg, one_line_png,
                              one_line_svg)

SRC = {"subtype": "direct", "injection_dbm": 15.0}
REP = {"max_dl_out_dbm": 33.0}
BANDS = ["B2_1900", "B12_700"]
PNG_SIG = b"\x89PNG\r\n\x1a\n"
SVG_NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def built(cfg):
    floor = {"id": "f1", "name": "Ground", "level": 0, "ppm": 10.0, "w_px": 600,
             "h_px": 400, "walls": [{"id": "w1", "x1": 300, "y1": 0, "x2": 300, "y2": 200,
                                     "material": "drywall"}], "elements": []}
    model = {"environment": "office", "building_type": "office", "floors": [floor]}
    res = autoplace_floor(model, cfg, "f1", {
        "bands": BANDS, "threshold_dbm": -60.0, "source": SRC, "repeater": REP,
        "step_px": 4, "remotes": True, "taps": True})
    sol = solve_cascade(model, cfg)
    assert sol["errors"] == []
    return {"model": model, "result": res, "solve": sol}


def _count(elements, kind):
    return sum(1 for el in elements if el.get("type") == kind)


def _groups(svg, kind):
    root = ET.fromstring(svg.encode("utf-8"))
    return [g for g in root.iter(SVG_NS + "g") if g.get("data-kind") == kind]


def _elements(model):
    return [el for fl in model["floors"] for el in fl["elements"]]


def _png_size(png):
    assert png[:8] == PNG_SIG
    w, h = struct.unpack(">II", png[16:24])
    return w, h


# ------------------------------------------------------------ one-line

def test_one_line_svg_parses_and_has_one_group_per_node(built, cfg):
    model = built["model"]
    svg = one_line_svg(model, cfg, built["solve"], project_name="Test")
    els = _elements(model)
    assert len(_groups(svg, "antenna")) == _count(els, "antenna") > 0
    assert len(_groups(svg, "remote")) == _count(els, "remote") >= 1
    assert len(_groups(svg, "splitter")) == _count(els, "splitter") >= 1
    assert len(_groups(svg, "tapper")) == _count(els, "tapper")
    assert len(_groups(svg, "coupler")) == _count(els, "coupler")
    # fiber run to the remote is dashed; every run is a polyline with a title
    assert "stroke-dasharray" in svg
    assert svg.count("<polyline") == sum(1 for el in els if el["type"] == "cable_run")


def test_one_line_svg_power_and_red_flag(built, cfg):
    model = built["model"]
    svg = one_line_svg(model, cfg, built["solve"])
    assert "port " in svg and " dBm" in svg
    # a synthetic solve with one antenna below 0 dBm must be drawn in red
    ant = next(el for el in _elements(model) if el["type"] == "antenna")
    fake = {"errors": [], "bands": {"B2_1900": {"label": "B2", "antennas": [
        {"element_id": ant["id"], "port_dbm": -3.5}]}}}
    assert _antenna_power(fake, "worst")[ant["id"]] == (-3.5, "B2")
    svg_fake = one_line_svg(model, cfg, fake)
    root = ET.fromstring(svg_fake.encode("utf-8"))
    red = [e for e in root.iter(SVG_NS + "text") if e.get("fill") == "#b00020"]
    assert red and any("-3.5" in (e.text or "") for e in red)


def test_one_line_png_signature_and_size(built, cfg):
    model = built["model"]
    png = one_line_png(model, cfg, built["solve"], project_name="Test")
    scene = _one_line_scene(model, cfg, built["solve"], ONE_LINE_PNG, "Test", None,
                            "worst", ONE_LINE_PNG["chars"])
    assert _png_size(png) == (scene["w"], scene["h"])
    assert scene["w"] > 400 and scene["h"] > 100


def test_one_line_without_solve_still_draws(built, cfg):
    svg = one_line_svg(built["model"], cfg, None)
    ET.fromstring(svg.encode("utf-8"))
    assert "No cascade solve" in svg


# ------------------------------------------------------------ floor layout

def test_floor_layout_svg_shapes_fiber_and_route_points(built, cfg):
    model = built["model"]
    fl = model["floors"][0]
    svg = floor_layout_svg(model, cfg, "f1", project_name="Test")
    ET.fromstring(svg.encode("utf-8"))
    assert len(_groups(svg, "antenna")) == _count(fl["elements"], "antenna")
    assert len(_groups(svg, "remote")) == _count(fl["elements"], "remote")
    assert len(_groups(svg, "splitter")) == _count(fl["elements"], "splitter")
    assert "stroke-dasharray" in svg  # fiber run
    routed = [r for r in fl["elements"] if r["type"] == "cable_run" and r.get("route")]
    assert routed
    for run in routed:
        bend = run["route"][1]
        assert "%.2f,%.2f" % (bend[0] + FLOOR_PAD, bend[1] + FLOOR_TITLE) in svg
    assert 'href="data:' not in svg  # no background image on this floor


def test_floor_layout_png_matches_scene(built, cfg):
    model = built["model"]
    png = floor_layout_png(model, cfg, "f1")
    k = _pick_k(600, 400)
    scene = _floor_scene(model, cfg, "f1", k, True, None)
    assert _png_size(png) == (scene["w"], scene["h"])
    assert scene["k"] == k == 2  # 600 x 400 plate doubles to 1200 x 800 (1600 px cap)


def test_floor_layout_unknown_floor_raises(built, cfg):
    from romtool.drawings import DrawingError
    with pytest.raises(DrawingError):
        floor_layout_svg(built["model"], cfg, "nope")


# ------------------------------------------------------------ endpoints

def _project_with_autoplace(client):
    pid = client.post("/projects", json={"name": "Drawings test"}).json()["id"]
    fl = client.post("/projects/%d/floors" % pid, json={
        "name": "Ground", "ppm": 10.0, "w_px": 600, "h_px": 400, "level": 0}).json()["floor"]
    body = {"bands": BANDS, "threshold_dbm": -60.0, "source": SRC, "repeater": REP,
            "step_px": 4, "remotes": True, "taps": True}
    r = client.post("/projects/%d/floors/%s/autoplace" % (pid, fl["id"]), json=body)
    assert r.status_code == 200, r.text
    return pid, fl["id"]


def test_drawing_endpoints(client):
    pid, fid = _project_with_autoplace(client)
    r = client.get("/projects/%d/one_line.svg" % pid)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    ET.fromstring(r.content)
    r = client.get("/projects/%d/one_line.png" % pid)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == PNG_SIG
    r = client.get("/projects/%d/floors/%s/layout.svg" % (pid, fid))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    ET.fromstring(r.content)
    r = client.get("/projects/%d/floors/%s/layout.png" % (pid, fid))
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == PNG_SIG
    r = client.get("/projects/%d/floors/nope/layout.svg" % pid)
    assert r.status_code == 404
    r = client.get("/projects/999999/one_line.svg")
    assert r.status_code == 404
