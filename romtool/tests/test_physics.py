"""SPEC physics anchors: FSPL sanity, P.1238 shape, P.2040 wall scaling,
linear-domain compositing."""

import math

import pytest

from romtool.physics import (composite_dbm, dbm_to_mw, fspl_db, itu_p1238_pl_db,
                             mw_to_dbm, scale_wall_loss_db)


def test_fspl_anchor_1m_2ghz():
    # SPEC anchor: FSPL(1 m, 2000 MHz) ~= 38.5 dB
    assert fspl_db(1.0, 2000.0) == pytest.approx(38.5, abs=0.1)


def test_fspl_anchor_10m_1900():
    # SPEC anchor: FSPL(10 m, 1900 MHz) ~= 58 dB
    assert fspl_db(10.0, 1900.0) == pytest.approx(58.0, abs=0.1)


def test_fspl_inverse_square():
    # doubling distance adds 6.02 dB
    d = fspl_db(2.0, 2000.0) - fspl_db(1.0, 2000.0)
    assert d == pytest.approx(20 * math.log10(2), abs=1e-9)


def test_p1238_at_n20_is_fspl_minus_constant_offset():
    # P.1238's formula offset (-28) puts N=20/same-floor at FSPL - 0.45 dB
    for d, f in [(1.0, 2000.0), (5.5, 1900.0), (12.3, 751.0)]:
        p1238 = itu_p1238_pl_db(f, d, 20.0, 0, 0.0)
        assert fspl_db(d, f) - p1238 == pytest.approx(0.45, abs=1e-6)


def test_p1238_floor_term_linear():
    a = itu_p1238_pl_db(800.0, 10.0, 30.0, 0, 15.4)
    b = itu_p1238_pl_db(800.0, 10.0, 30.0, 2, 15.4)
    assert b - a == pytest.approx(2 * 15.4, abs=1e-9)


def test_p1238_office_anchor_values():
    # hand-checkable: 750 MHz, N=30, 10 m, same floor
    # 20log10(750)=57.5, +30*1, -28 -> 59.50
    assert itu_p1238_pl_db(750.0, 10.0, 30.0, 0, 15.4) == pytest.approx(59.50, abs=0.02)


def test_wall_loss_exact_at_reference():
    assert scale_wall_loss_db(15.0, 2000.0) == pytest.approx(15.0, abs=1e-12)
    assert scale_wall_loss_db(4.0, 2000.0) == pytest.approx(4.0, abs=1e-12)


def test_wall_loss_log_domain_scaling_700():
    # SPEC rule: A(f) = A(2 GHz) * log10(f)/log10(2000); concrete 15 dB @2GHz
    expected = 15.0 * math.log10(700.0) / math.log10(2000.0)
    assert scale_wall_loss_db(15.0, 700.0) == pytest.approx(expected, abs=1e-9)
    # sanity vs published P.2040 trend for concrete: ~13 dB at 700 MHz
    assert 12.8 < expected < 13.1


def test_composite_linear_sum_two_tones():
    # -90 dBm + -100 dBm -> -90 + 10log10(1.1) = -89.586 dBm
    assert composite_dbm([-90.0, -100.0]) == pytest.approx(-90.0 + 10 * math.log10(1.1), abs=1e-9)


def test_composite_doubling_adds_three_db():
    a = composite_dbm([-70.0])
    b = composite_dbm([-70.0, -70.0])
    assert b - a == pytest.approx(3.0103, abs=1e-3)


def test_composite_order_independent_and_none_safe():
    assert composite_dbm([-60, -75]) == pytest.approx(composite_dbm([-75, -60]), abs=1e-12)
    assert composite_dbm([]) is None
    assert composite_dbm([None]) is None


def test_dbm_mw_roundtrip():
    assert mw_to_dbm(dbm_to_mw(-33.7)) == pytest.approx(-33.7, abs=1e-12)
