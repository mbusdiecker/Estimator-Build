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



# ============================================================ 4. routing

def test_route_points_are_l_shaped():
    from romtool.autoplace import route_length_px, route_points
    assert route_points((10, 20), (50, 80), "x") == [[10, 20], [50, 20], [50, 80]]
    assert route_points((10, 20), (50, 80), "y") == [[10, 20], [10, 80], [50, 80]]
    assert route_points((10, 20), (50, 20), "y") == [[10, 20], [50, 20]]   # aligned
    assert route_points((10, 20), (10, 20), "x") == [[10, 20], [10, 20]]   # coincident
    # hand: 40 px across + 60 px down = 100 px in either leg order
    assert route_length_px(route_points((10, 20), (50, 80), "x")) == 100
    assert route_length_px(route_points((10, 20), (50, 80), "y")) == 100


def _check_routes(m, ppm=10.0, slack=3.0):
    pos = {e["id"]: (e["x"], e["y"]) for fl in m["floors"] for e in fl["elements"]
           if "x" in e}
    seen = 0
    for e in els(m, "cable_run"):
        if not e.get("auto") or "route" not in e:
            continue
        seen += 1
        r = e["route"]
        assert len(r) in (2, 3)
        assert tuple(r[0]) == pos[e["from_id"]] and tuple(r[-1]) == pos[e["to_id"]]
        for a, b in zip(r, r[1:]):
            assert a[0] == b[0] or a[1] == b[1]          # orthogonal segments
        seg = sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(r, r[1:]))
        assert e["length_m"] == pytest.approx(round(seg / ppm + slack, 2), abs=0.005)
    return seen


def test_every_auto_run_stores_route_and_length_agrees(cfg):
    m = make_model(900, 600)
    res = autoplace_floor(m, cfg, "f1", request(threshold_dbm=-70.0))
    runs = els(m, "cable_run")
    routed = _check_routes(m)
    # all runs except the co-located donor feeder (source -> repeater) have routes
    assert routed == len(runs) - 1
    donor = next(r for r in runs if r["cable_id"] == "lmr400")
    assert "route" not in donor
    # wide floor: trunk axis is x, so a run with a bend goes horizontal first
    assert res["routing"]["trunk_axis"] == "x"
    bent = [r for r in runs if "route" in r and len(r["route"]) == 3]
    assert bent and all(r["route"][0][1] == r["route"][1][1] for r in bent)
    # tall floor: vertical first
    m2 = make_model(300, 900)
    res2 = autoplace_floor(m2, cfg, "f1", request(threshold_dbm=-70.0))
    assert res2["routing"]["trunk_axis"] == "y"
    bent2 = [r for r in els(m2, "cable_run") if "route" in r and len(r["route"]) == 3]
    assert bent2 and all(r["route"][0][0] == r["route"][1][0] for r in bent2)
    _check_routes(m2)


def test_routes_with_remotes_taps_and_fiber(cfg):
    m = make_model(900, 600)
    res = autoplace_floor(m, cfg, "f1", request(
        threshold_dbm=-70.0, taps=True, remotes={"max_dl_out_dbm": 12.0}))
    assert res["remotes"]["count"] >= 2
    assert _check_routes(m) > 10
    fib = [r for r in els(m, "cable_run") if r["cable_id"] == "fiber_sm"]
    assert fib and all("route" in r for r in fib)
    assert sum(r["length_m"] for r in fib) == pytest.approx(res["remotes"]["fiber_m"], abs=0.02)


def test_feed_run_length_is_route_plus_slack_plus_riser(cfg):
    m = {"environment": "office", "building_type": "office", "floors": [
        {"id": "f0", "name": "F0", "level": 0, "ppm": 10.0, "w_px": 600, "h_px": 400,
         "walls": [], "elements": [
             {"id": "src", "type": "source", "subtype": "direct", "injection_dbm": 15.0,
              "x": 100, "y": 300},
             {"id": "rp", "type": "repeater", "x": 100, "y": 300},
             {"id": "rr", "type": "cable_run", "from_id": "src", "to_id": "rp",
              "cable_id": "lmr400", "length_m": 2.0}]},
        {"id": "f1", "name": "F1", "level": 2, "ppm": 10.0, "w_px": 600, "h_px": 400,
         "walls": [], "elements": []}]}
    autoplace_floor(m, cfg, "f1", request(feed_element_id="rp"))
    f1 = m["floors"][1]["elements"]
    feed = next(e for e in f1 if e["type"] == "cable_run" and e["from_id"] == "rp")
    seg = sum(abs(b[0] - a[0]) + abs(b[1] - a[1])
              for a, b in zip(feed["route"], feed["route"][1:]))
    assert feed["route"][0] == [100, 300]
    # two levels up at floor_height_m = 4 m each
    assert feed["length_m"] == pytest.approx(round(seg / 10.0 + 3.0 + 8.0, 2), abs=0.005)


# ================================================== 5. clutter map from a plan

import struct
import zlib

from romtool.clutter_map import (ClutterField, ClutterMapError, apply_overrides,
                                 build_clutter_map, decode_image, decode_png,
                                 encode_gray_png)
from romtool.predict import predict_floor


def _png(w, h, ctype, rows, depth=8, filters=None, plte=None):
    """PNG encoder with selectable per-row filters (0 none, 1 sub, 2 up, 3 avg,
    4 paeth) so the decoder's unfilter paths are exercised."""
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    bpp = max(1, ch * depth // 8)
    out, prev = bytearray(), bytearray(len(rows[0]))
    for y, row in enumerate(rows):
        ft = (filters or [0] * h)[y]
        line = bytearray()
        for i, v in enumerate(row):
            a = row[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ft == 0:
                pr = 0
            elif ft == 1:
                pr = a
            elif ft == 2:
                pr = b
            elif ft == 3:
                pr = (a + b) >> 1
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line.append((v - pr) & 255)
        out += bytes([ft]) + line
        prev = bytearray(row)

    def chunk(t, body):
        return (struct.pack(">I", len(body)) + t + body
                + struct.pack(">I", zlib.crc32(t + body) & 0xFFFFFFFF))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, ctype, 0, 0, 0))
    if plte:
        png += chunk(b"PLTE", plte)
    return png + chunk(b"IDAT", zlib.compress(bytes(out))) + chunk(b"IEND", b"")


def test_png_decoder_handles_filters_and_color_types():
    w, h = 7, 5
    # gray, every filter type
    rows = [bytes((x * 31 + y * 17) % 256 for x in range(w)) for y in range(h)]
    png = _png(w, h, 0, rows, filters=[0, 1, 2, 3, 4])
    assert decode_png(png) == (w, h, b"".join(rows))
    # RGB -> luma (299 R + 587 G + 114 B) / 1000
    rgb = [bytes(((x * 40 + c * 70 + y * 9) % 256) for x in range(w) for c in range(3))
           for y in range(h)]
    gw, gh, gray = decode_png(_png(w, h, 2, rgb, filters=[4, 3, 2, 1, 0]))
    want = bytes((299 * r[3 * x] + 587 * r[3 * x + 1] + 114 * r[3 * x + 2]) // 1000
                 for r in rgb for x in range(w))
    assert gray == want
    # RGBA: fully transparent pixels read as white, opaque black stays black
    px = []
    for x in range(w):
        px += [0, 0, 0, 0 if x % 2 else 255]
    rgba = [bytes(px) for _ in range(h)]
    _, _, g2 = decode_png(_png(w, h, 6, rgba))
    assert g2[:4] == bytes([0, 255, 0, 255])
    # palette
    pal = [bytes([0, 1, 1, 0, 1, 0, 1])] * h
    _, _, g3 = decode_png(_png(w, h, 3, pal, plte=bytes([0, 0, 0, 255, 255, 255])))
    assert g3[:7] == bytes([0, 255, 255, 0, 255, 0, 255])
    # gray encoder round trip
    gpx = bytes((i * 7) % 256 for i in range(12 * 9))
    assert decode_png(encode_gray_png(12, 9, gpx)) == (12, 9, gpx)
    with pytest.raises(ClutterMapError):
        decode_png(b"not a png")


def test_optional_decoders_fail_clearly_or_work():
    import importlib.util
    jpeg_head = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    if importlib.util.find_spec("PIL") is None:
        with pytest.raises(ClutterMapError, match="Pillow"):
            decode_image(jpeg_head)
    else:
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("L", (8, 6), 200).save(buf, "JPEG")
        w, h, g = decode_image(buf.getvalue())
        assert (w, h) == (8, 6) and abs(g[0] - 200) <= 3
    if importlib.util.find_spec("fitz") is None:
        with pytest.raises(ClutterMapError, match="PyMuPDF"):
            decode_image(b"%PDF-1.4\n")
    with pytest.raises(ClutterMapError):
        decode_image(b"GIF89a....")


def _rect(g, w, x0, y0, x1, y1):
    for y in range(y0, y1):
        g[y * w + x0:y * w + x1] = b"\x00" * (x1 - x0)


def plan_gray(w=1200, h=400, dense_to=None, t=2, outer=True):
    """Plan with a dense-walled left half (3.5 m x 5 m rooms) and an open
    right half, at 10 px/m."""
    g = bytearray(b"\xff" * (w * h))
    dense_to = w // 2 if dense_to is None else dense_to
    if outer:
        _rect(g, w, 0, 0, w, t)
        _rect(g, w, 0, h - t, w, h)
        _rect(g, w, 0, 0, t, h)
        _rect(g, w, w - t, 0, w, h)
    for x in range(35, dense_to, 35):
        _rect(g, w, x, 0, x + t, h)
    for y in range(50, h, 50):
        _rect(g, w, 0, y, dense_to, y + t)
    return g


def make_floor(w=1200, h=400):
    return {"id": "f1", "name": "F1", "level": 0, "ppm": 10.0, "w_px": w, "h_px": h,
            "walls": [], "elements": []}


def _classes(cm):
    codes = cm["codes"]
    return [[codes[ch] for ch in row] for row in cm["grid"]]


def test_scoring_dense_half_open_half(cfg):
    w, h = 1200, 400
    cm = build_clutter_map(bytes(plan_gray(w, h)), w, h, make_floor(w, h), cfg)
    cls = _classes(cm)
    assert (cm["rows"], cm["cols"], cm["cell_m"]) == (14, 40, 3.0)
    # a window well inside each half
    dense = [c for row in cls[2:12] for c in row[2:17]]
    openh = [c for row in cls[2:12] for c in row[24:38]]
    assert dense.count("dense_rooms") >= 0.9 * len(dense)
    assert openh.count("open") >= 0.9 * len(openh)
    assert sum(cm["stats"].values()) == 14 * 40
    assert cm["auto_grid"] == cm["grid"] and cm["overrides"] == {}


def test_scoring_is_independent_of_wall_thickness(cfg):
    w, h = 1200, 400
    a = build_clutter_map(bytes(plan_gray(w, h, t=2)), w, h, make_floor(w, h), cfg)
    b = build_clutter_map(bytes(plan_gray(w, h, t=4)), w, h, make_floor(w, h), cfg)
    same = sum(x == y for ra, rb in zip(a["grid"], b["grid"]) for x, y in zip(ra, rb))
    assert same >= 0.97 * 14 * 40


def test_text_and_hatching_are_suppressed(cfg):
    w, h = 600, 300
    g = bytearray(b"\xff" * (w * h))
    # "text": dense short strokes (2 px wide, 8 px tall) across a block of the plan
    for y in range(40, 200, 12):
        for x in range(40, 400, 6):
            _rect(g, w, x, y, x + 2, y + 8)
    # diagonal hatching
    for k in range(0, 200, 5):
        for s in range(60):
            x, y = 420 + k // 2 + s, 40 + s
            if 0 <= x < w and 0 <= y < h:
                g[y * w + x] = 0
    cm = build_clutter_map(bytes(g), w, h, make_floor(w, h), cfg)
    cls = _classes(cm)
    inner = [c for row in cls[2:6] for c in row[2:12]]
    assert inner.count("open") == len(inner)
    # the same ink as straight long strokes would score as walls
    g2 = bytearray(b"\xff" * (w * h))
    for x in range(40, 400, 6):
        _rect(g2, w, x, 40, x + 2, 200)
    cm2 = build_clutter_map(bytes(g2), w, h, make_floor(w, h), cfg)
    assert "open" not in [c for row in _classes(cm2)[2:6] for c in row[4:10]]


def test_exterior_and_core_classes(cfg):
    w, h = 600, 300
    g = bytearray(b"\xff" * (w * h))
    # building shell with no ink outside: x 150..450, y 60..240, plus a solid core
    _rect(g, w, 150, 60, 450, 62)
    _rect(g, w, 150, 238, 450, 240)
    _rect(g, w, 150, 60, 152, 240)
    _rect(g, w, 448, 60, 450, 240)
    _rect(g, w, 270, 120, 330, 180)
    cm = build_clutter_map(bytes(g), w, h, make_floor(w, h), cfg)
    cls = _classes(cm)
    assert cls[0][0] == "exterior" and cls[-1][-1] == "exterior" and cls[5][1] == "exterior"
    assert cls[5][10] == "core"            # centre of the solid block (x 300, y 150)
    assert cls[5][7] in ("open", "light_office")   # inside the shell, not exterior
    # detect_exterior off leaves everything empty as open
    cm2 = build_clutter_map(bytes(g), w, h, make_floor(w, h), cfg, detect_exterior=False)
    assert cm2["stats"]["exterior"] == 0
    # an outline polygon marks everything outside it exterior
    outline = [[150, 60], [450, 60], [450, 240], [150, 240]]
    cm3 = build_clutter_map(bytes(g), w, h, make_floor(w, h), cfg, outline=outline,
                            detect_exterior=False)
    assert _classes(cm3)[0][0] == "exterior" and _classes(cm3)[5][10] != "exterior"


def test_manual_overrides_per_square(cfg):
    w, h = 600, 300
    cm = build_clutter_map(bytes(plan_gray(w, h)), w, h, make_floor(w, h), cfg)
    auto = cm["grid"][4][10]
    apply_overrides(cm, cfg, 10.0, set_cells=[{"row": 4, "col": 10, "class": "core"}])
    assert _classes(cm)[4][10] == "core" and cm["overrides"] == {"4,10": "core"}
    assert cm["auto_grid"][4][10] == auto and cm["stats"]["core"] >= 1
    # a point in metres: x 31 m, y 13 m falls in column 10, row 4
    apply_overrides(cm, cfg, 10.0, set_cells=[{"x_m": 31.0, "y_m": 13.0, "class": "exterior"}])
    assert _classes(cm)[4][10] == "exterior"
    # a rectangle in metres (x 0-6 m, y 0-3 m = columns 0-2, rows 0-1)
    apply_overrides(cm, cfg, 10.0, set_cells=[{"rect_m": [0, 0, 6, 3], "class": "open"}])
    assert all(_classes(cm)[r][c] == "open" for r in (0, 1) for c in (0, 1, 2))
    apply_overrides(cm, cfg, 10.0, clear_cells=[{"row": 4, "col": 10}])
    assert cm["grid"][4][10] == auto
    apply_overrides(cm, cfg, 10.0, clear_all=True)
    assert cm["grid"] == cm["auto_grid"]
    with pytest.raises(ClutterMapError):
        apply_overrides(cm, cfg, 10.0, set_cells=[{"row": 0, "col": 0, "class": "lava"}])
    with pytest.raises(ClutterMapError):
        apply_overrides(cm, cfg, 10.0, set_cells=[{"row": 99, "col": 0, "class": "core"}])


def test_clutter_config_values_are_graded_and_derived(cfg):
    cm = cfg["clutter_map"]
    assert set(cm["classes"]) == {"open", "light_office", "dense_rooms", "core", "exterior"}
    for cid, c in cm["classes"].items():
        assert c["grade"] == "E" and c["ref"] and c["code"], cid
    bt = cfg["building_types"]
    # starting values come from the calibrated building types
    assert cm["classes"]["light_office"]["db_per_m"] == bt["office"]["clutter_db_per_m"]
    assert cm["classes"]["dense_rooms"]["db_per_m"] == bt["hospital"]["clutter_db_per_m"]
    assert cm["classes"]["core"]["db_per_m"] == bt["warehouse_open"]["clutter_db_per_m"]
    assert cm["param_grade"] == "E" and cm["param_ref"]


def _hand_map(codes_row, cell_px=30.0):
    return {"version": 1, "cell_m": cell_px / 10.0, "cell_px": cell_px, "cols": len(codes_row),
            "rows": 1, "ppm": 10.0,
            "codes": {"o": "open", "l": "light_office", "d": "dense_rooms",
                      "c": "core", "x": "exterior"},
            "grid": [codes_row], "auto_grid": [codes_row], "overrides": {}}


def test_field_trace_is_the_per_square_length_sum(cfg):
    # four 3 m squares: open (0.1), open, core (1.0), open at 10 px/m
    f = ClutterField(_hand_map("ooco"), cfg, 10.0)
    # straight along the middle: 6 m open + 3 m core + 3 m open
    assert f.trace(0, 15, 120, 15) == pytest.approx(6 * 0.1 + 3 * 1.0 + 3 * 0.1)
    # ends inside squares: from x=15 (mid square 0) to x=105 (mid square 3)
    assert f.trace(15, 15, 105, 15) == pytest.approx(4.5 * 0.1 + 3.0 + 1.5 * 0.1)
    # reversed direction gives the same value
    assert f.trace(105, 15, 15, 15) == pytest.approx(f.trace(15, 15, 105, 15))
    # a diagonal inside one square: 0.1 dB/m x length
    assert f.trace(2, 2, 14, 11) == pytest.approx(0.1 * math.hypot(12, 9) / 10.0)
    # same point: zero
    assert f.trace(40, 5, 40, 5) == 0.0
    # exterior squares do not enter the mean / min
    g = ClutterField(_hand_map("oxxc"), cfg, 10.0)
    assert g.mean_db == pytest.approx(0.55) and g.min_db == 0.1 and g.max_db == 1.0


def test_field_uniform_map_is_exact_everywhere(cfg):
    f = ClutterField(_hand_map("llllllll"), cfg, 10.0)
    ent = f.entry(37.0, 11.0, cache=False)
    xs = [3.0, 100.0, 239.0, 120.5]
    ys = [29.0, 5.0, 14.0, 0.0]
    got = f.loss_list(ent, xs, ys)
    for x, y, v in zip(xs, ys, got):
        assert v == pytest.approx(0.3 * math.hypot(x - 37.0, y - 11.0) / 10.0, abs=1e-9)


def _interp_errors(f, ax, ay, xs, ys):
    ent = f.entry(ax, ay, cache=False)
    out = []
    for x in xs:
        for y in ys:
            d = math.hypot(x - ax, y - ay) / 10.0
            out.append((d, abs(f.loss_at(ent, x, y, d) - f.trace(ax, ay, x, y))))
    return out


def test_field_interpolation_error_is_bounded(cfg):
    # realistic plan: interpolated loss (lattice nodes + bilinear residual) vs the
    # exact traversal. Measured: mean about 0.1 dB, max about 1.7 dB within 30 m of
    # the antenna (a few dB at 100 m, where the signal is far below threshold).
    w, h = 1200, 400
    cm = build_clutter_map(bytes(plan_gray(w, h)), w, h, make_floor(w, h), cfg)
    f = ClutterField(cm, cfg, 10.0)
    for ax, ay in ((150.0, 100.0), (700.0, 200.0), (500.0, 60.0)):
        errs = _interp_errors(f, ax, ay, range(1, 1200, 37), range(1, 400, 23))
        assert sum(e for _, e in errs) / len(errs) < 0.3
        assert max(e for d, e in errs if d <= 30.0) < 2.5
    # adversarial: a random class per square (0.1 to 1.0 dB/m, no structure)
    import random
    rnd = random.Random(7)
    rows, cols = 8, 12
    grid = ["".join(rnd.choice("oolldc") for _ in range(cols)) for _ in range(rows)]
    cm2 = _hand_map("o")
    cm2.update({"cols": cols, "rows": rows, "grid": grid, "auto_grid": grid})
    errs = _interp_errors(ClutterField(cm2, cfg, 10.0), 150.0, 100.0,
                          range(1, 360, 13), range(1, 240, 11))
    assert len(errs) > 300
    assert sum(e for _, e in errs) / len(errs) < 0.6 and max(e for _, e in errs) < 4.0


def _predict_pct(m, cfg, thr, step=4):
    vals = predict_floor(m, cfg, "f1", "B2_1900", step_px=step)["values_dbm"]
    return 100.0 * sum(1 for v in vals if v is not None and v >= thr - 1e-9) / len(vals)


def test_predict_uniform_map_equals_uniform_building_type_clutter(cfg):
    # a map of light_office everywhere (0.3 dB/m) must give the same prediction
    # as the building type office (clutter 0.3 dB/m, N 30)
    def model(with_map):
        fl = make_floor(400, 300)
        fl["elements"] = [
            {"id": "s", "type": "source", "subtype": "direct", "injection_dbm": 10.0},
            {"id": "a", "type": "antenna", "antenna_id": "omni_das", "x": 120.0, "y": 90.0},
            {"id": "r", "type": "cable_run", "from_id": "s", "to_id": "a",
             "cable_id": "coax_1_2in", "length_m": 5.0}]
        if with_map:
            fl["clutter_map"] = {**_hand_map("l" * 14, 30.0), "rows": 10,
                                 "grid": ["l" * 14] * 10, "auto_grid": ["l" * 14] * 10}
        return {"environment": "office", "building_type": "office", "floors": [fl]}
    a = predict_floor(model(False), cfg, "f1", "B2_1900", step_px=5)
    b = predict_floor(model(True), cfg, "f1", "B2_1900", step_px=5)
    assert b["loss_model"]["source"] == "clutter_map" and a["loss_model"]["source"] == "building_type"
    assert b["loss_model"]["clutter_map"]["mean_db_per_m"] == pytest.approx(0.3)
    assert b["values_dbm"] == a["values_dbm"]
    # without a map nothing changed: the building type path is untouched
    assert a["loss_model"]["clutter_db_per_m"] == 0.3


def test_predict_with_core_strip_matches_hand_math(cfg):
    # antenna at x=15 px (mid first square) in a 1-row map: open, open, core, open
    # at 10 px/m; point at x=105 px is 90 px = 9 m away: 4.5 m open + 3 m core + 1.5 m open
    fl = make_floor(120, 30)
    fl["elements"] = [
        {"id": "s", "type": "source", "subtype": "direct", "injection_dbm": 10.0},
        {"id": "a", "type": "antenna", "antenna_id": "omni_das", "x": 15.0, "y": 15.0},
        {"id": "r", "type": "cable_run", "from_id": "s", "to_id": "a",
         "cable_id": "coax_1_2in", "length_m": 0.0}]
    fl["clutter_map"] = _hand_map("ooco")
    m = {"environment": "office", "floors": [fl]}
    pr = predict_floor(m, cfg, "f1", "B2_1900", step_px=15)
    eirp = pr["antennas"][0]["eirp_dbm"]
    pl = (20 * math.log10(1960.0) + 30.0 * math.log10(9.0) - 28.0
          + (4.5 * 0.1 + 3.0 * 1.0 + 1.5 * 0.1))
    cols = pr["grid"]["cols"]
    idx = 1 * cols + 7          # row y=15, column x=105
    assert pr["values_dbm"][idx] == pytest.approx(eirp - pl, abs=0.06)


def test_walls_drawn_take_precedence_over_the_map(cfg):
    fl = make_floor(400, 300)
    fl["walls"] = [{"id": "w1", "x1": 200, "y1": 0, "x2": 200, "y2": 300, "material": "drywall"}]
    fl["clutter_map"] = {**_hand_map("c" * 14, 30.0), "rows": 10,
                         "grid": ["c" * 14] * 10, "auto_grid": ["c" * 14] * 10}
    fl["elements"] = [
        {"id": "s", "type": "source", "subtype": "direct", "injection_dbm": 10.0},
        {"id": "a", "type": "antenna", "antenna_id": "omni_das", "x": 100.0, "y": 100.0},
        {"id": "r", "type": "cable_run", "from_id": "s", "to_id": "a",
         "cable_id": "coax_1_2in", "length_m": 5.0}]
    m = {"environment": "office", "building_type": "office", "floors": [fl]}
    with_map = predict_floor(m, cfg, "f1", "B2_1900", step_px=10)
    del fl["clutter_map"]
    without = predict_floor(m, cfg, "f1", "B2_1900", step_px=10)
    assert with_map["loss_model"]["source"] == "walls_drawn"
    assert "clutter_map" not in with_map["loss_model"]
    assert with_map["values_dbm"] == without["values_dbm"]


def _plan_model(cfg, with_map):
    w, h = 1200, 400
    fl = make_floor(w, h)
    if with_map:
        fl["clutter_map"] = build_clutter_map(bytes(plan_gray(w, h)), w, h, fl, cfg)
    return {"environment": "office", "building_type": "office", "floors": [fl]}


def test_dense_half_gets_more_antennas_with_a_clutter_map(cfg):
    body = request(threshold_dbm=-66.0)

    def halves(res, w=1200):
        left = sum(1 for a in res["antennas"] if a["x_px"] < w / 2)
        return left, res["antenna_count"] - left

    plain = autoplace_floor(_plan_model(cfg, False), cfg, "f1", copy.deepcopy(body))
    mapped_model = _plan_model(cfg, True)
    mapped = autoplace_floor(mapped_model, cfg, "f1", copy.deepcopy(body))
    pl_left, pl_right = halves(plain)
    mp_left, mp_right = halves(mapped)
    assert plain["target_met"] and mapped["target_met"]
    assert abs(pl_left - pl_right) <= 2          # no map: the two halves are alike
    assert mp_left >= mp_right + 3               # map: the dense half needs more
    assert mapped["loss_model"]["B2_1900"]["source"] == "clutter_map"
    assert plain["loss_model"]["B2_1900"]["source"] == "building_type"
    # the placement is judged by the same prediction predict_floor makes
    row = mapped["achieved"]["B2_1900"][0]
    assert _predict_pct(mapped_model, cfg, -66.0) == pytest.approx(row["covered_pct"], abs=0.01)
    # deterministic
    again = autoplace_floor(_plan_model(cfg, True), cfg, "f1", copy.deepcopy(body))
    assert again["antennas"] == mapped["antennas"]


def test_autoplace_ignores_the_map_when_walls_are_drawn(cfg):
    m = _plan_model(cfg, True)
    m["floors"][0]["walls"] = [{"id": "w1", "x1": 600, "y1": 0, "x2": 600, "y2": 400,
                                "material": "drywall"}]
    res = autoplace_floor(m, cfg, "f1", request(threshold_dbm=-70.0))
    assert res["loss_model"]["B2_1900"]["source"] == "walls_drawn"


# -------------------------------------------------------------- clutter API

def test_api_clutter_map_roundtrip(client, cfg):
    png = encode_gray_png(1200, 400, bytes(plan_gray()))
    pid = client.post("/projects", json={"name": "CM", "building_type": "office"}).json()["id"]
    fid = client.post("/projects/%d/floors" % pid,
                      json={"name": "L1", "ppm": 10, "w_px": 1200, "h_px": 400}).json()["floor"]["id"]
    base = "/projects/%d/floors/%s/clutter-map" % (pid, fid)
    assert client.get(base).status_code == 404
    assert client.post(base).status_code == 422             # no file, no floor image
    r = client.post(base, files={"file": ("plan.png", png, "image/png")}, params={"cell_m": 3})
    assert r.status_code == 200, r.text
    cm = r.json()["clutter_map"]
    assert (cm["rows"], cm["cols"]) == (14, 40) and "auto_grid" not in cm
    assert client.get(base).json()["clutter_map"]["stats"] == cm["stats"]
    # overrides
    p = client.patch(base, json={"set": [{"row": 5, "col": 5, "class": "core"}]})
    assert p.status_code == 200 and p.json()["clutter_map"]["overrides"] == {"5,5": "core"}
    assert p.json()["clutter_map"]["grid"][5][5] == "c"
    assert client.patch(base, json={"set": [{"row": 5, "col": 5, "class": "lava"}]}).status_code == 422
    # rebuild keeps the override when the grid is unchanged; cell_m change drops it
    r2 = client.post(base, files={"file": ("plan.png", png, "image/png")})
    assert r2.json()["clutter_map"]["overrides"] == {"5,5": "core"}
    r3 = client.post(base, files={"file": ("plan.png", png, "image/png")}, params={"cell_m": 6})
    assert r3.json()["clutter_map"]["overrides"] == {} and r3.json()["clutter_map"]["cols"] == 20
    # bad inputs
    assert client.post(base, files={"file": ("x.png", b"nope", "image/png")}).status_code == 422
    assert client.post(base, files={"file": ("plan.png", png, "image/png")},
                       params={"cell_m": 0.1}).status_code == 422
    # autoplace and predict use it
    client.post(base, files={"file": ("plan.png", png, "image/png")}, params={"cell_m": 3})
    body = {"bands": ["B2_1900"], "threshold_dbm": -66.0, "source": SRC, "repeater": REP,
            "step_px": 4}
    ap = client.post("/projects/%d/floors/%s/autoplace" % (pid, fid), json=body)
    assert ap.status_code == 200, ap.text
    assert ap.json()["loss_model"]["B2_1900"]["source"] == "clutter_map"
    pr = client.post("/projects/%d/floors/%s/predict?band=B2_1900&step=4" % (pid, fid)).json()
    assert pr["loss_model"]["source"] == "clutter_map"
    # delete
    assert client.delete(base).status_code == 200
    assert client.get(base).status_code == 404
    assert client.delete(base).status_code == 404


def test_api_clutter_map_from_the_floor_image(client):
    png = encode_gray_png(300, 200, bytes(plan_gray(300, 200, dense_to=150)))
    pid = client.post("/projects", json={"name": "CM2"}).json()["id"]
    fid = client.post("/projects/%d/floors" % pid,
                      json={"name": "L1", "ppm": 10}).json()["floor"]["id"]
    up = client.post("/projects/%d/floors/%s/image" % (pid, fid),
                     files={"file": ("plan.png", png, "image/png")})
    assert up.status_code == 200
    r = client.post("/projects/%d/floors/%s/clutter-map" % (pid, fid))
    assert r.status_code == 200, r.text
    assert (r.json()["clutter_map"]["rows"], r.json()["clutter_map"]["cols"]) == (7, 10)


def test_pdf_plan_renders_at_the_floor_width():
    fitz = pytest.importorskip("fitz")     # PyMuPDF is optional (uv run --with pymupdf)
    doc = fitz.open()
    page = doc.new_page(width=300, height=150)
    page.draw_line((10, 10), (290, 10), width=2)
    page.draw_line((10, 10), (10, 140), width=2)
    w, h, gray = decode_image(doc.tobytes(), floor_w_px=600)
    assert (w, h) == (600, 300)            # zoomed so the image width equals the floor width
    assert gray[20 * w + 200] < 128 and gray[100 * w + 300] > 200   # on the top line (y 10 pt = 20 px) / blank


# ======================================================== walls bound placement

def test_walls_without_outline_bound_auto_antennas(cfg):
    """A floor with walls and no outline places antennas inside the walls'
    bounding box (0.5 m inset), not on the canvas rectangle."""
    x0, y0, x1, y1 = 100, 100, 420, 300
    walls = [
        {"id": "w1", "x1": x0, "y1": y0, "x2": x1, "y2": y0, "material": "drywall"},
        {"id": "w2", "x1": x1, "y1": y0, "x2": x1, "y2": y1, "material": "drywall"},
        {"id": "w3", "x1": x1, "y1": y1, "x2": x0, "y2": y1, "material": "drywall"},
        {"id": "w4", "x1": x0, "y1": y1, "x2": x0, "y2": y0, "material": "drywall"},
    ]
    m = make_model(600, 400, walls=walls)
    res = autoplace_floor(m, cfg, "f1", request())
    ants = els(m, "antenna")
    assert len(ants) >= 1 and len(ants) == len(res["antennas"])
    for a in ants:
        assert x0 <= a["x"] <= x1 and y0 <= a["y"] <= y1, (a["x"], a["y"])
