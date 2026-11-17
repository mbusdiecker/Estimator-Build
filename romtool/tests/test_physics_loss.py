"""Physics loss mode: free-space exponent plus measured wall crossings x material loss."""

import math

import pytest

from romtool.clutter_map import ClutterField, ClutterMapError
from romtool.config import load_config
from romtool.lossmodel import LossModelError, active_loss_mode, resolve_loss_model
from romtool.physics import scale_wall_loss_db


def _cmap(cpm=0.25, exterior_col=None, with_cpm=True):
    rows = cols = 4
    grid = []
    for _r in range(rows):
        grid.append("".join("x" if c == exterior_col else "o" for c in range(cols)))
    m = {"cols": cols, "rows": rows, "cell_px": 30.0, "ppm": 10.0,
         "codes": {"o": "open", "x": "exterior"}, "grid": grid}
    if with_cpm:
        m["cpm"] = [[cpm] * cols for _ in range(rows)]
    return m


def _band(cfg, bid="B2_1900"):
    return [b for b in cfg["bands"] if b["id"] == bid][0]


def test_loss_mode_defaults_and_validates():
    assert active_loss_mode({}, {}) == "calibrated"
    assert active_loss_mode({"loss_mode": "physics"}, {}) == "physics"
    assert active_loss_mode({"loss_mode": "physics"}, {"loss_mode": "calibrated"}) == "calibrated"
    with pytest.raises(LossModelError):
        active_loss_mode({"loss_mode": "magic"}, {})


def test_physics_uses_free_space_n_and_frequency_scaled_wall_loss():
    cfg = load_config()
    band = _band(cfg)
    floor = {"walls": [], "building_type": "hospital", "clutter_map": _cmap()}
    res = resolve_loss_model({"loss_mode": "physics"}, cfg, floor, band, "office")
    assert res["source"] == "physics" and res["n"] == 20.0
    want = scale_wall_loss_db(cfg["wall_materials"]["drywall"]["loss_db"], band["freq_mhz"],
                              cfg["wall_loss_ref_mhz"])
    assert res["physics_wall_db"] == pytest.approx(want, abs=1e-3)
    # calibrated mode on the same floor still uses the building type N
    cal = resolve_loss_model({}, cfg, floor, band, "office")
    assert cal["n"] == cfg["building_types"]["hospital"]["n"] and cal["source"] == "clutter_map"


def test_physics_without_measured_map_raises():
    cfg = load_config()
    with pytest.raises(LossModelError):
        resolve_loss_model({"loss_mode": "physics"}, cfg, {"walls": []}, _band(cfg), "office")
    floor = {"walls": [], "clutter_map": _cmap(with_cpm=False)}
    with pytest.raises(LossModelError):
        resolve_loss_model({"loss_mode": "physics"}, cfg, floor, _band(cfg), "office")


def test_field_is_crossings_times_wall_db_and_exterior_is_free():
    cfg = load_config()
    f = ClutterField(_cmap(cpm=0.25, exterior_col=3), cfg, 10.0, wall_db=4.0)
    assert f.db_at(5, 5) == pytest.approx(1.0)      # 0.25 walls/m x 4 dB
    assert f.db_at(100, 5) == 0.0                    # exterior column
    # a straight 6 m line through uniform interior squares loses 6 dB
    assert f.trace(5, 15, 65, 15) == pytest.approx(6.0, abs=1e-6)
    with pytest.raises(ClutterMapError):
        ClutterField(_cmap(with_cpm=False), cfg, 10.0, wall_db=4.0)


def test_built_clutter_map_stores_measured_cpm():
    from romtool.clutter_map import build_clutter_map
    cfg = load_config()
    w = h = 120
    gray = bytearray([255]) * (w * h)
    for y in range(h):                               # vertical walls every 20 px (2 m at 10 px/m)
        for x in range(0, w, 20):
            gray[y * w + x] = 0
    cm = build_clutter_map(bytes(gray), w, h, {"w_px": w, "h_px": h, "ppm": 10.0}, cfg)
    assert len(cm["cpm"]) == cm["rows"] and len(cm["cpm"][0]) == cm["cols"]
    mid = cm["cpm"][cm["rows"] // 2][cm["cols"] // 2]
    assert 0.2 <= mid <= 0.3                         # one wall per 2 m on one axis, averaged over both


def test_double_line_walls_count_once():
    from romtool.clutter_map import build_clutter_map
    cfg = load_config()
    w = h = 120
    gray = bytearray([255]) * (w * h)
    for y in range(h):                               # each wall drawn as two faces 0.2 m apart, every 3 m
        for x in range(0, w - 2, 30):
            gray[y * w + x] = 0
            gray[y * w + x + 2] = 0
    cm = build_clutter_map(bytes(gray), w, h, {"w_px": w, "h_px": h, "ppm": 10.0}, cfg)
    mid = cm["cpm"][cm["rows"] // 2][cm["cols"] // 2]
    assert 0.13 <= mid <= 0.2                        # 1/3 wall per m on one axis -> about 0.17, not 0.33


def test_api_loss_mode(client):
    pid = [p for p in client.get("/projects").json()["projects"] if p["example"]][0]["id"]
    assert client.patch("/projects/%d" % pid, json={"loss_mode": "nope"}).status_code == 422
    assert client.patch("/projects/%d" % pid, json={"loss_mode": "physics"}).status_code == 200
    assert client.patch("/projects/%d" % pid, json={"loss_mode": None}).status_code == 200
