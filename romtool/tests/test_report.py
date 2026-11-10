"""PNG report renderer: structural PNG validity + KPI badge + legend."""

import struct
import zlib

import pytest

from romtool.predict import predict_floor
from romtool.report import (encode_png, color_for_dbm, render_heatmap_png)

from test_cascade import chain_model, test_cfg
from test_kpi import EIRP, room_cfg, room_with_antenna_at_exact_port


def _solve_predict(cfg=None):
    cfg = cfg or test_cfg()
    model = chain_model()
    pr = predict_floor(model, cfg, "f1", "T2G", step_px=4)
    return model, cfg, pr


def test_encode_png_valid_structure():
    w, h = 5, 4
    rows = [bytes(b"".join([b"\xff\x00\x00\xff"] * w)) for _ in range(h)]
    png = encode_png(rows, w, h)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert png[-8:-4] == b"IEND"
    length, tag = struct.unpack(">IB", png[8:13])
    assert tag == 0x49  # 'I'
    ihdr = png[16:16 + 13]
    ww, hh, depth, ctype = struct.unpack(">IIBB", ihdr[:10])
    assert (ww, hh, depth, ctype) == (5, 4, 8, 6)
    # IDAT inflates to h * (1 + w*4)
    idat_start = png.find(b"IDAT") + 4
    idat_len = struct.unpack(">I", png[png.find(b"IDAT") - 4:png.find(b"IDAT")])[0]
    raw = zlib.decompress(png[idat_start:idat_start + idat_len])
    assert len(raw) == h * (1 + w * 4)


def test_color_for_dbm_piecewise_and_clamped():
    scale = [{"dbm": -95, "rgb": [0, 0, 255]}, {"dbm": -55, "rgb": [0, 255, 0]}]
    assert color_for_dbm(-100, scale) == (0, 0, 255)   # below -> clamp first
    assert color_for_dbm(-50, scale) == (0, 255, 0)    # above -> clamp last
    assert color_for_dbm(-75, scale) == (0, 127, 127)  # midpoint blended (int)
    assert color_for_dbm(-75, []) == (128, 128, 128)   # no scale -> grey


def test_color_for_dbm_handles_descending_config_scale(cfg):
    """Config lists stops hot->cold; hottest values must map to the hot color
    (regression: dark-grey hotspots around antennas in the report PNG)."""
    hot = color_for_dbm(-50, cfg["report"]["color_scale"])
    cold = color_for_dbm(-130, cfg["report"]["color_scale"])
    assert hot == tuple(cfg["report"]["color_scale"][0]["rgb"])
    assert cold == tuple(cfg["report"]["color_scale"][-1]["rgb"])
    assert sum(hot) > sum(cold)


def test_heatmap_png_reports_badge_and_legend():
    model, cfg, pr = _solve_predict()
    png = render_heatmap_png(pr, cfg, title_extra="P1 F1")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # width: 100 px floor at scale 4 -> 400 + 96 legend
    ihdr = png[16:29]
    w, h = struct.unpack(">II", ihdr[:8])
    assert w == 100 * 4 + 96
    assert h == 100 * 4 + 26


def test_heatmap_png_kpi_badge_reflects_pass_state(room_cfg):
    # hot chain -> PASS badge state, valid PNG
    hot = room_with_antenna_at_exact_port(EIRP + 40.0)
    pr_hot = predict_floor(hot, room_cfg, "L0", "T2G", step_px=1)
    assert pr_hot["kpi"]["presets"][0]["pass"] is True
    png_hot = render_heatmap_png(pr_hot, room_cfg)
    assert png_hot[:4] == b"\x89PNG"
