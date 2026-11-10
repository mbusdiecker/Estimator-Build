"""Autoplace extensions: remote units, taps/couplers, branch losses, routing,
clutter map. Hand anchors are noted at each test.
"""

import copy
import math

import pytest

from romtool.autoplace import AutoplaceError, autoplace_floor
from romtool.bom import generate_bom
from romtool.cascade import cable_loss_db, solve_cascade
from romtool.config import load_config

SRC = {"subtype": "direct", "injection_dbm": 15.0}
REP = {"max_dl_out_dbm": 33.0}
THR = -60.0


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def make_model(w_px, h_px, building_type="office", **floor_extra):
    floor = {"id": "f1", "name": "F1", "level": 0, "ppm": 10.0, "w_px": w_px,
             "h_px": h_px, "walls": [], "elements": []}
    floor.update(floor_extra)
    model = {"environment": "office", "floors": [floor]}
    if building_type:
        model["building_type"] = building_type
    return model


def request(**kw):
    req = {"bands": ["B2_1900"], "threshold_dbm": THR, "source": SRC,
           "repeater": REP, "step_px": 4}
    req.update(kw)
    return req


def els(model, typ):
    return [e for fl in model["floors"] for e in fl["elements"] if e["type"] == typ]


# ======================================================== 1. remote units

def _remote_chain(cfg, remote_extra=None, band_cap=None):
    """source (direct 15 dBm) -> repeater (+65, cap 33) -> fiber 100 m ->
    remote -> 1/2 in 10 m -> antenna."""
    remote = {"id": "ru", "type": "remote", "label": "RU", "x": 0, "y": 0}
    remote.update(remote_extra or {})
    return {"environment": "office", "floors": [{
        "id": "f1", "level": 0, "ppm": 10.0, "w_px": 100, "h_px": 100, "walls": [],
        "elements": [
            {"id": "s", "type": "source", "subtype": "direct", "injection_dbm": 15.0},
            {"id": "rp", "type": "repeater", "ports": 2},
            remote,
            {"id": "a", "type": "antenna", "antenna_id": "omni_das", "x": 5, "y": 5},
            {"id": "r1", "type": "cable_run", "from_id": "s", "to_id": "rp",
             "cable_id": "lmr400", "length_m": 2.0},
            {"id": "r2", "type": "cable_run", "from_id": "rp", "to_id": "ru",
             "cable_id": "fiber_sm", "length_m": 100.0},
            {"id": "r3", "type": "cable_run", "from_id": "ru", "to_id": "a",
             "cable_id": "coax_1_2in", "length_m": 10.0},
        ]}]}


def test_remote_cascade_caps_output_per_band(cfg):
    # hand math: 15 dBm injected, +65 dB gain capped at 33 dBm; fiber adds no RF
    # loss; the remote passes 33 and caps at its own max_dl_out_dbm (30 default)
    m = _remote_chain(cfg)
    sol = solve_cascade(m, cfg)
    assert sol["errors"] == []
    b = sol["bands"]["B2_1900"]
    ru = next(n for n in b["nodes"] if n["element_id"] == "ru")
    assert ru["in_dbm"] == pytest.approx(33.0)
    assert ru["out_dbm"] == pytest.approx(30.0)
    loss = cable_loss_db(cfg["cables"]["coax_1_2in"], 10.0, 1960.0)
    assert b["antennas"][0]["in_dbm"] == pytest.approx(30.0 - loss, abs=0.01)
    assert any("Remote" in w for w in b["warnings"])  # clamp is reported
    # per-band cap on the element wins for that band only
    m2 = _remote_chain(cfg, {"max_dl_out_dbm_per_band": {"B2_1900": 24.0}})
    s2 = solve_cascade(m2, cfg)
    n2 = next(n for n in s2["bands"]["B2_1900"]["nodes"] if n["element_id"] == "ru")
    assert n2["out_dbm"] == pytest.approx(24.0)
    n3 = next(n for n in s2["bands"]["B5_850"]["nodes"] if n["element_id"] == "ru")
    assert n3["out_dbm"] == pytest.approx(30.0)
    # a gain adds before the cap: -50 dBm injected - 2 m LMR-400 + 65 dB gain
    # reaches the remote at about 14.6 dBm; +6 dB gain leaves it below the cap
    m3 = _remote_chain(cfg, {"dl_gain_db": 6.0, "max_dl_out_dbm": 40.0})
    m3["floors"][0]["elements"][0]["injection_dbm"] = -50.0
    s3 = solve_cascade(m3, cfg)
    n4 = next(n for n in s3["bands"]["B2_1900"]["nodes"] if n["element_id"] == "ru")
    donor = cable_loss_db(cfg["cables"]["lmr400"], 2.0, 1960.0)
    assert n4["out_dbm"] == pytest.approx(-50.0 - donor + 65.0 + 6.0, abs=0.01)


def test_remote_port_limit_is_checked(cfg):
    m = _remote_chain(cfg)
    m["floors"][0]["elements"].append(
        {"id": "a2", "type": "antenna", "antenna_id": "omni_das", "x": 8, "y": 8})
    m["floors"][0]["elements"].append(
        {"id": "r4", "type": "cable_run", "from_id": "ru", "to_id": "a2",
         "cable_id": "coax_1_2in", "length_m": 4.0})
    sol = solve_cascade(m, cfg)
    assert any("remote" in e and "port" in e for e in sol["errors"])


def test_remotes_off_by_default_and_unchanged(cfg):
    m = make_model(600, 400)
    res = autoplace_floor(m, cfg, "f1", request())
    assert res["remotes"] is None
    assert not els(m, "remote")
    assert "remotes" not in res["elements_added"]


def test_remotes_sized_so_zone_budgets_close(cfg):
    # a head-end level of 12 dBm per remote forces zones; default 30 dBm does not
    body = request(threshold_dbm=-60.0)
    m1 = make_model(900, 600)
    r1 = autoplace_floor(m1, cfg, "f1", dict(body, remotes=True))
    assert r1["remotes"]["count"] == 1 and r1["remotes"]["budget_closed"]
    m2 = make_model(900, 600)
    r2 = autoplace_floor(m2, cfg, "f1", dict(body, remotes={"max_dl_out_dbm": 12.0}))
    rem = r2["remotes"]
    assert rem["count"] >= 2 and rem["budget_closed"] is True
    assert rem["worst_antenna_port_dbm"] >= 0.0
    # one fewer remote would not have closed: size the same floor at count-1
    m3 = make_model(900, 600)
    r3 = autoplace_floor(m3, cfg, "f1", dict(
        body, remotes={"max_dl_out_dbm": 12.0, "count": rem["count"] - 1}))
    assert r3["remotes"]["budget_closed"] is False
    assert any("does not close" in w for w in r3["warnings"])
    assert r2["target_met"]
    # elements: remotes + fiber runs, cascade solves, BOM lists them
    assert len(els(m2, "remote")) == rem["count"]
    sol = solve_cascade(m2, cfg)
    assert sol["errors"] == []
    bom = generate_bom(m2, cfg)["items"]
    assert sum(i["qty"] for i in bom if i["category"] == "remote") == rem["count"]
    fiber = [i for i in bom if i["category"] == "cable" and "Fiber" in i["item"]]
    assert {i["unit"] for i in fiber} == {"m", "ft"}
    fiber_m = next(i["qty"] for i in fiber if i["unit"] == "m")
    assert fiber_m == pytest.approx(rem["fiber_m"], abs=0.02)
    assert any(i["category"] == "connectors" and "Fiber" in i["item"] for i in bom)
    # every antenna belongs to exactly one remote zone
    assert sum(u["antennas"] for u in rem["units"]) == r2["antenna_count"]


def test_remote_placement_is_zone_load_centre_and_fiber_is_manhattan(cfg):
    m = make_model(900, 600)
    res = autoplace_floor(m, cfg, "f1", request(remotes={"max_dl_out_dbm": 12.0}))
    hx, hy = res["headend"]["x_px"], res["headend"]["y_px"]
    ants = {e["id"]: e for e in els(m, "antenna")}
    runs = {e["id"]: e for e in els(m, "cable_run")}
    for ru in els(m, "remote"):
        feeds = [r for r in runs.values() if r["from_id"] == ru["id"]]
        zone = []
        stack = [r["to_id"] for r in feeds]
        kids = {}
        for r in runs.values():
            kids.setdefault(r["from_id"], []).append(r["to_id"])
        while stack:
            n = stack.pop()
            if n in ants:
                zone.append(ants[n])
            stack.extend(kids.get(n, []))
        assert zone
        cx = sum(a["x"] for a in zone) / len(zone)
        cy = sum(a["y"] for a in zone) / len(zone)
        assert ru["x"] == pytest.approx(cx, abs=0.06)
        assert ru["y"] == pytest.approx(cy, abs=0.06)
        fib = next(r for r in runs.values() if r["to_id"] == ru["id"])
        assert fib["cable_id"] == "fiber_sm"
        want = round((abs(hx - ru["x"]) + abs(hy - ru["y"])) / 10.0 + 3.0, 2)
        assert fib["length_m"] == pytest.approx(want, abs=0.011)
    # the zones are disjoint spatial halves: bounding boxes of zone antennas
    # do not nest (recursive bisection)
    assert len({(u["x_px"], u["y_px"]) for u in res["remotes"]["units"]}) == res["remotes"]["count"]


def test_remotes_deterministic_and_validated(cfg):
    body = request(remotes={"max_dl_out_dbm": 12.0})
    a = autoplace_floor(make_model(900, 600), cfg, "f1", copy.deepcopy(body))
    b = autoplace_floor(make_model(900, 600), cfg, "f1", copy.deepcopy(body))
    assert a["remotes"] == b["remotes"] and a["antennas"] == b["antennas"]
    with pytest.raises(AutoplaceError):
        autoplace_floor(make_model(600, 400), cfg, "f1", request(remotes={"bogus": 1}))
    with pytest.raises(AutoplaceError):
        autoplace_floor(make_model(600, 400), cfg, "f1", request(remotes={"count": 0}))
    with pytest.raises(AutoplaceError):
        autoplace_floor(make_model(600, 400), cfg, "f1",
                        request(remotes=True, feed_element_id="x"))


def test_remote_config_values_are_graded(cfg):
    r = cfg["remote"]
    for key in ("dl_gain", "max_dl_out", "ports"):
        assert r[key + "_grade"] in ("P", "G", "C", "E"), key
        assert r[key + "_ref"], key
    assert r["max_dl_out_grade"] == "E"
    assert cfg["autoplace"]["max_remotes_grade"] == "E"
    assert cfg["cables"]["fiber_sm"]["medium"] == "fiber"
