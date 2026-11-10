"""SPEC acceptance benchmark: example project solves + predicts headless well
under 5 s for a full 1-px-step grid (the example floor alone is ~11k px^2,
i.e. ~11,000 grid points at 1 px step -- larger than the SPEC's 3000 px^2
acceptance grid)."""

import time

import pytest

from romtool.cascade import solve_cascade
from romtool.config import load_config
from romtool.example_project import build_example_model
from romtool.predict import predict_floor


def test_example_solve_and_predict_under_5s():
    cfg = load_config()
    model = build_example_model()
    t0 = time.perf_counter()
    solve = solve_cascade(model, cfg)
    assert solve["errors"] == []
    bands = solve["bands"]
    assert len(bands) == 12  # phase-3 default band set: 4 LTE + B14 + 4 LMR + 3 NR
    for bid, res in bands.items():
        assert len(res["antennas"]) == 4
    t_solve = time.perf_counter() - t0

    t0 = time.perf_counter()
    for fl in model["floors"]:
        pr = predict_floor(model, cfg, fl["id"], "B12_700", step_px=1)
        # 1-px-step grid over the whole floor
        assert pr["grid"]["cols"] * pr["grid"]["step_px"] >= 120
        assert pr["grid"]["rows"] * pr["grid"]["step_px"] >= 92
        assert all(v is not None for v in pr["values_dbm"])
    t_predict = time.perf_counter() - t0

    total = t_solve + t_predict
    print("\nbench: solve %.3fs, predict (2 floors) %.3fs, total %.3fs"
          % (t_solve, t_predict, total))
    assert total < 5.0


def test_3000px2_grid_at_1px_step_under_5s():
    """Literal SPEC acceptance: a 3000 px^2 grid at 1 px step (60x50 px floor)."""
    cfg = load_config()
    model = build_example_model()
    for fl in model["floors"]:
        fl["w_px"], fl["h_px"] = 60, 50
    solve = solve_cascade(model, cfg)
    assert solve["errors"] == []
    t0 = time.perf_counter()
    pr = predict_floor(model, cfg, "exf1", "B12_700", step_px=1)
    dt = time.perf_counter() - t0
    n = pr["grid"]["n_points"]
    assert n == 60 * 50  # 3000 px^2 at 1 px step
    print("\nbench 3000 px^2 @1px: %.3fs (%d pts)" % (dt, n))
    assert dt < 5.0


def test_multi_band_predict_under_5s():
    cfg = load_config()
    model = build_example_model()
    solve = solve_cascade(model, cfg)
    t0 = time.perf_counter()
    for bid in [b["id"] for b in cfg["bands"]]:
        pr = predict_floor(model, cfg, "exf1", bid, step_px=1,
                           solve_result=solve)
        assert pr["kpi"]["covered_pct"] >= 0.0
    dt = time.perf_counter() - t0
    print("\nbench %d bands @1px: %.3fs" % (len(cfg["bands"]), dt))
    assert dt < 5.0


def test_nr_composite_view_under_5s():
    """Phase-3 acceptance: composite/best-band view across the 3 NR bands on
    the example ground floor (~11k grid points) in well under 5 s."""
    from romtool.composite import predict_floor_multiband

    cfg = load_config()
    model = build_example_model()
    solve = solve_cascade(model, cfg)
    t0 = time.perf_counter()
    multi = predict_floor_multiband(model, cfg, "exf1",
                                    ["N71_600", "N41_2500", "N77_CBAND"],
                                    step_px=1, solve_result=solve)
    dt = time.perf_counter() - t0
    n = multi["grid"]["n_points"]
    assert n >= 11000
    assert len(multi["per_band"]) == 3
    assert len(multi["values_dbm"]) == n
    print("\nbench 3-band NR composite @1px: %.3fs (%d pts)" % (dt, n))
    assert dt < 5.0
