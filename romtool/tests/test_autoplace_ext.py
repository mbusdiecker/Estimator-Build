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


# ====================================================== 2. taps and couplers

def test_tap_table_picks_coupler_only_when_it_balances_better(cfg):
    from romtool.autoplace import _tap_table
    tbl = _tap_table(cfg, 4, 40, 0.5)
    eq = _tap_table(cfg, 4, 40, 0.5, use_taps=False)
    # small trees stay on equal splitters (hand: 4 antennas = one 4-way, 7.5 dB)
    for n in (2, 3, 4, 5, 8, 12):
        assert tbl[n][2] == ("equal",), n
    assert tbl[4][0] == pytest.approx(7.5)
    # 10 antennas: the 10 dB coupler feeds one antenna, its 0.6 dB through port
    # feeds a 9-antenna tree (3-way into 3-way = 11.0 dB): worst 11.6 dB vs 13.0
    assert tbl[10][2] == ("tap", "coupler", "default", 1)
    assert tbl[10][0] == pytest.approx(0.6 + 11.0)
    assert tbl[10][1] == pytest.approx(10.0)
    assert eq[10][0] == pytest.approx(13.0)
    # the equal-only table is exactly the equal tree for every n
    assert all(e[2] in (("equal",), ("leaf",)) for e in eq[1:])
    # taps never raise the worst device loss over equal splitters
    for n in range(2, 41):
        assert tbl[n][0] <= eq[n][0] + 1e-9, n
    # a hysteresis larger than any gain turns taps off
    huge = _tap_table(cfg, 4, 40, 99.0)
    assert all(t[2][0] != "tap" for t in huge[2:])


def test_cascade_picks_tap_part_by_part_id(cfg):
    c = copy.deepcopy(cfg)
    c["parts"]["tappers"]["t6"] = {"label": "Tap 6 dB", "ways": 2,
                                   "through_il_db": 1.2, "tap_loss_db": 6.0}
    m = {"environment": "office", "floors": [{
        "id": "f1", "level": 0, "ppm": 10.0, "w_px": 100, "h_px": 100, "walls": [],
        "elements": [
            {"id": "s", "type": "source", "subtype": "direct", "injection_dbm": 10.0},
            {"id": "t", "type": "tapper", "part_id": "t6"},
            {"id": "a1", "type": "antenna", "antenna_id": "omni_das", "x": 1, "y": 1},
            {"id": "a2", "type": "antenna", "antenna_id": "omni_das", "x": 2, "y": 2},
            {"id": "r0", "type": "cable_run", "from_id": "s", "to_id": "t",
             "cable_id": "coax_1_2in", "length_m": 0.0},
            {"id": "r1", "type": "cable_run", "from_id": "t", "to_id": "a1",
             "cable_id": "coax_1_2in", "length_m": 0.0, "port": "tap"},
            {"id": "r2", "type": "cable_run", "from_id": "t", "to_id": "a2",
             "cable_id": "coax_1_2in", "length_m": 0.0, "port": "through"}]}]}
    sol = solve_cascade(m, c)
    assert sol["errors"] == []
    ant = {a["element_id"]: a for a in sol["bands"]["B2_1900"]["antennas"]}
    assert ant["a1"]["in_dbm"] == pytest.approx(10.0 - 6.0)
    assert ant["a2"]["in_dbm"] == pytest.approx(10.0 - 1.2)
    m["floors"][0]["elements"][1]["part_id"] = "nope"
    assert any("part_id" in e for e in solve_cascade(m, c)["errors"])


def test_taps_off_by_default(cfg):
    m = make_model(900, 600)
    res = autoplace_floor(m, cfg, "f1", request(threshold_dbm=-70.0))
    assert res["taps"] is None
    assert not els(m, "coupler") and not els(m, "tapper")
    assert "couplers" not in res["elements_added"]


def test_autoplace_taps_use_couplers_and_report_counts(cfg):
    body = request(threshold_dbm=-70.0)
    m0 = make_model(900, 600)
    off = autoplace_floor(m0, cfg, "f1", copy.deepcopy(body))
    m = make_model(900, 600)
    on = autoplace_floor(m, cfg, "f1", dict(body, taps=True))
    assert on["target_met"] and off["target_met"]
    ea = on["elements_added"]
    assert ea["couplers"] + ea["tappers"] >= 1
    assert ea["splitters"] == len(els(m, "splitter"))
    assert ea["couplers"] == len(els(m, "coupler"))
    assert ea["tappers"] == len(els(m, "tapper"))
    t = on["taps"]
    assert t["device_worst_loss_db"]["tapped"] <= t["device_worst_loss_db"]["equal_splitters"] - 0.5
    assert sum(t["parts_used"].values()) == ea["couplers"] + ea["tappers"]
    # structure: each tap device has exactly one 'through' and one tap/coupled run
    runs = els(m, "cable_run")
    for dev in els(m, "coupler") + els(m, "tapper"):
        out = [r for r in runs if r["from_id"] == dev["id"]]
        assert sorted(r["port"] for r in out) == sorted(
            ["through", "coupled" if dev["type"] == "coupler" else "tap"])
        assert dev["part_id"] in cfg["parts"]["couplers" if dev["type"] == "coupler" else "tappers"]
    sol = solve_cascade(m, cfg)
    assert sol["errors"] == []
    bom = generate_bom(m, cfg)["items"]
    assert sum(i["qty"] for i in bom if i["category"] == "coupler") == ea["couplers"]
    assert sum(i["qty"] for i in bom if i["category"] == "splitter") == ea["splitters"]
    # deterministic
    m2 = make_model(900, 600)
    again = autoplace_floor(m2, cfg, "f1", dict(body, taps=True))
    assert again["elements_added"] == ea and again["antennas"] == on["antennas"]
    # invalid option
    with pytest.raises(AutoplaceError):
        autoplace_floor(make_model(600, 400), cfg, "f1", request(taps={"x": 1}))


# ============================================ 3. fixed losses per antenna branch

def _branch_chain(branch_db):
    ant = {"id": "a", "type": "antenna", "antenna_id": "omni_das", "x": 1, "y": 1}
    if branch_db is not None:
        ant["branch_loss_db"] = branch_db
    return {"environment": "office", "floors": [{
        "id": "f1", "level": 0, "ppm": 10.0, "w_px": 100, "h_px": 100, "walls": [],
        "elements": [
            {"id": "s", "type": "source", "subtype": "direct", "injection_dbm": 10.0},
            ant,
            {"id": "r", "type": "cable_run", "from_id": "s", "to_id": "a",
             "cable_id": "coax_1_2in", "length_m": 0.0}]}]}


def test_cascade_applies_and_shows_branch_loss(cfg):
    # hand math: 10 dBm in, 0 m cable, 1.0 dB branch hardware -> 9.0 dBm at the
    # port, EIRP 9.0 + 2 dBi (omni_das) = 11.0 dBm
    sol = solve_cascade(_branch_chain(1.0), cfg)
    b = sol["bands"]["B2_1900"]
    row = next(n for n in b["nodes"] if n["element_id"] == "a")
    ant = b["antennas"][0]
    assert ant["in_dbm"] == pytest.approx(10.0)
    assert ant["port_dbm"] == pytest.approx(9.0)
    assert ant["eirp_dbm"] == pytest.approx(11.0)
    assert ant["branch_loss_db"] == pytest.approx(1.0)
    assert row["branch_loss_db"] == pytest.approx(1.0)
    assert "branch hardware" in row["detail"]
    # without the field the output is the old shape and values
    base = solve_cascade(_branch_chain(None), cfg)["bands"]["B2_1900"]["antennas"][0]
    assert base["eirp_dbm"] == pytest.approx(12.0) and "branch_loss_db" not in base
    # the below-0-dBm warning is judged at the port, after the allowance
    low = _branch_chain(10.5)
    w = solve_cascade(low, cfg)["bands"]["B2_1900"]["warnings"]
    assert any("below the 0 dBm" in x for x in w)


def test_branch_allowance_from_config_parts(cfg):
    from romtool.autoplace import _parse_branch_losses
    assert _parse_branch_losses(cfg, None, 1) == {"enabled": False}
    one = _parse_branch_losses(cfg, True, 1)
    hw = cfg["branch_hardware"]
    # one band: 2 connectors + 1 jumper, no diplexer
    assert one["parts"] == {"connectors": 2, "jumpers": 1, "diplexers": 0}
    assert one["total_db"] == pytest.approx(2 * hw["connector"]["loss_db"]
                                            + hw["jumper"]["loss_db"])
    two = _parse_branch_losses(cfg, True, 2)  # bands share an antenna: + diplexer
    assert two["parts"]["diplexers"] == 1
    assert two["total_db"] == pytest.approx(one["total_db"] + hw["diplexer"]["loss_db"])
    ov = _parse_branch_losses(cfg, {"connectors": 4, "jumper_loss_db": 0.5,
                                    "diplexers": 0}, 3)
    assert ov["total_db"] == pytest.approx(4 * hw["connector"]["loss_db"] + 0.5)
    for key in ("connector", "jumper", "diplexer"):
        assert hw[key]["grade"] in ("P", "G", "C", "E") and hw[key]["ref"]
    with pytest.raises(AutoplaceError):
        _parse_branch_losses(cfg, {"nope": 1}, 1)
    with pytest.raises(AutoplaceError):
        _parse_branch_losses(cfg, {"jumpers": -1}, 1)


def test_autoplace_branch_losses_reach_every_antenna_and_the_bom(cfg):
    m = make_model(900, 600)
    res = autoplace_floor(m, cfg, "f1", request(
        bands=["B2_1900", "B5_850"], threshold_dbm=-70.0, branch_losses=True))
    bl = res["branch_losses"]
    assert bl["parts_per_antenna"] == {"connectors": 2, "jumpers": 1, "diplexers": 1}
    ants = els(m, "antenna")
    assert all(a["branch_loss_db"] == pytest.approx(bl["per_antenna_db"]) for a in ants)
    assert res["target_met"]
    # the cascade port is exactly the allowance below the run output
    with_bl = solve_cascade(m, cfg)["bands"]["B2_1900"]["antennas"]
    for a in ants:
        a.pop("branch_loss_db")
    without = {x["element_id"]: x for x in solve_cascade(m, cfg)["bands"]["B2_1900"]["antennas"]}
    for x in with_bl:
        assert without[x["element_id"]]["in_dbm"] == pytest.approx(x["in_dbm"])
        assert (without[x["element_id"]]["port_dbm"] - x["port_dbm"]
                in (pytest.approx(bl["per_antenna_db"], abs=0.01), pytest.approx(0.0, abs=0.01)))
    # BOM lists the hardware
    for a in ants:
        a["branch_loss_db"] = bl["per_antenna_db"]
    bom = generate_bom(m, cfg)["items"]
    hw = {i["item"]: i["qty"] for i in bom if i["category"] == "branch hardware"}
    n = res["antenna_count"]
    assert hw[cfg["branch_hardware"]["connector"]["label"]] == 2 * n
    assert hw[cfg["branch_hardware"]["jumper"]["label"]] == n
    assert hw[cfg["branch_hardware"]["diplexer"]["label"]] == n


def test_branch_losses_off_by_default(cfg):
    m = make_model(900, 600)
    res = autoplace_floor(m, cfg, "f1", request(threshold_dbm=-70.0))
    assert res["branch_losses"] is None
    assert all("branch_loss_db" not in a for a in els(m, "antenna"))
    assert not [i for i in generate_bom(m, cfg)["items"] if i["category"] == "branch hardware"]

