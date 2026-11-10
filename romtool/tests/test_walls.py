"""Wall-crossing sum tests: strict ray/segment crossings summed per material
with the P.2040 log-domain frequency scaling rule."""

import math

import pytest

from romtool.geometry import build_wall_index, ray_wall_crossings, seg_cross
from romtool.physics import scale_wall_loss_db

MATS = {"concrete": 15.0, "cmu": 10.0, "drywall": 4.0, "glass": 5.0,
        "low_e_glass": 8.0, "wood": 4.0, "metal": 30.0}


def _walls(*specs):
    # specs: (x1,y1,x2,y2,material)
    return [{"id": "w%d" % i, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "material": m}
            for i, (x1, y1, x2, y2, m) in enumerate(specs)]


def loss(walls, p1, p2, f_mhz=2000.0, ref_mhz=2000.0):
    idx = build_wall_index(walls)
    hits = ray_wall_crossings(idx, p1, p2)
    return sum(scale_wall_loss_db(MATS[w[8]["material"]], f_mhz, ref_mhz) for w in hits), len(hits)


def test_seg_cross_basics():
    assert seg_cross((0, 0), (10, 10), (5, -1), (5, 11)) is True
    assert seg_cross((0, 0), (10, 10), (15, -1), (15, 11)) is False
    # parallel lines
    assert seg_cross((0, 0), (10, 0), (0, 5), (10, 5)) is False


def test_wall_sum_at_reference_frequency():
    # ray crosses one drywall (4 dB) and one glass (5 dB): total 9 dB at 2 GHz
    walls = _walls((40, 0, 40, 40, "drywall"), (60, 0, 60, 40, "glass"))
    total, n = loss(walls, (10, 20), (90, 20))
    assert n == 2
    assert total == pytest.approx(9.0, abs=1e-12)


def test_wall_sum_scaled_at_900mhz():
    walls = _walls((40, 0, 40, 40, "drywall"), (60, 0, 60, 40, "glass"))
    total, n = loss(walls, (10, 20), (90, 20), f_mhz=900.0)
    assert n == 2
    expected = 9.0 * math.log10(900.0) / math.log10(2000.0)
    assert total == pytest.approx(expected, abs=1e-9)


def test_two_parallel_concrete_walls():
    walls = _walls((40, 0, 40, 100, "concrete"), (55, 0, 55, 100, "concrete"))
    total, n = loss(walls, (10, 50), (90, 50))
    assert n == 2
    assert total == pytest.approx(30.0, abs=1e-12)


def test_wall_off_path_not_counted():
    walls = _walls((40, 0, 40, 10, "metal"))  # ray runs at y=20, wall ends at y=10
    total, n = loss(walls, (10, 20), (90, 20))
    assert n == 0 and total == 0.0


def test_ray_endpoint_on_wall_not_double_counted():
    # grid point exactly on a wall line: no strict crossing, no double count
    walls = _walls((40, 0, 40, 100, "concrete"))
    total, n = loss(walls, (40, 50), (90, 50))
    assert n == 0 and total == 0.0


def test_diagonal_ray_crossings():
    walls = _walls((50, 0, 50, 100, "cmu"), (20, 60, 80, 60, "metal"))
    total, n = loss(walls, (10, 10), (90, 90))  # crosses x=50 at y=50, y=60 at x=60
    assert n == 2
    assert total == pytest.approx(10.0 + 30.0, abs=1e-12)
