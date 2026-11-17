"""Physics update: per-remote operator power, uplink limit, limiting band
report, distribution topologies (star / trunk4), remote count sizing and the
covered-area / floor-count project inputs.

Hand anchors are noted at each test. The legacy-combination test pins the
numbers of the pre-update engine measured on HEAD (see PHYSICS-REPORT.md).
"""

import copy
import json
import math

import pytest

from romtool.autoplace import AutoplaceError, _parse_uplink, autoplace_floor
from romtool.bom import generate_bom
from romtool.cascade import (cable_loss_db, remote_max_out_dbm,
                             remote_per_operator_split,
                             remote_operators_sharing, solve_cascade)
from romtool.config import load_config
from romtool.grades import settings_for
from romtool.physics import uplink_max_path_loss_db

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


# ===================================================== 1. operator power split

def _remote_chain(remote_extra=None, ops=None):
    """source (direct 15 dBm) -> repeater (+65 dB, cap 33) -> fiber ->
    remote (config cap) -> 1/2 in 10 m -> antenna."""
    remote = {"id": "ru", "type": "remote", "label": "RU", "x": 0, "y": 0}
    remote.update(remote_extra or {})
    if ops is not None:
        remote["operators_sharing"] = ops
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
             "cable_id": "coax_1_2in", "length_m": 10.0}]}]}


def _remote_node(sol, eid="ru"):
    return next(n for n in sol["bands"]["B2_1900"]["nodes"]
                if n["element_id"] == eid)


def test_operator_split_lowers_the_cap_by_10log10_operators(cfg):
    # hand math: 10*log10(3) = 4.771 dB; 33 dBm composite -> 28.229 dBm per operator
    assert remote_per_operator_split(33.0, 3) == pytest.approx(33.0 - 10 * math.log10(3.0))
    assert remote_per_operator_split(46.0, 3) == pytest.approx(41.229, abs=0.01)
    assert remote_per_operator_split(16.0, 8) == pytest.approx(6.969, abs=0.01)
    rated = remote_max_out_dbm({}, cfg["remote"], "B2_1900")
    one = _remote_node(solve_cascade(_remote_chain(ops=1), cfg))
    auto = _remote_node(solve_cascade(_remote_chain(), cfg))
    three = _remote_node(solve_cascade(_remote_chain(ops=3), cfg))
    assert one["out_dbm"] == pytest.approx(rated)        # 33 dBm composite as before
    assert auto["out_dbm"] == pytest.approx(rated)       # unkeyed element unchanged
    assert three["out_dbm"] == pytest.approx(33.0 - 10 * math.log10(3.0), abs=0.01)
    assert three["out_dbm"] - one["out_dbm"] == pytest.approx(-4.771, abs=0.01)
    assert "per operator" in three["detail"] and "rated" in three["detail"]
    # the antenna level follows the per-operator cap through the same branch cable
    loss = cable_loss_db(cfg["cables"]["coax_1_2in"], 10.0, 1960.0)
    ant = solve_cascade(_remote_chain(ops=3), cfg)["bands"]["B2_1900"]["antennas"][0]
    assert ant["in_dbm"] == pytest.approx(three["out_dbm"] - loss, abs=0.01)


def test_no_operators_key_on_the_element_is_untouched(cfg):
    el = _remote_chain()["floors"][0]["elements"][2]
    assert remote_operators_sharing(el) == 1.0
    assert "operators_sharing" not in el


def test_remotes_report_rated_and_per_operator(cfg):
    bid = "B2_1900"
    res = autoplace_floor(make_model(900, 600), cfg, "f1", request(remotes=True))
    rem = res["remotes"]
    assert rem["operators_sharing"] == 1.0         # off until the request asks
    rated = rem["units"][0]["max_dl_out_dbm"][bid]
    assert rem["units"][0]["per_operator_out_dbm"][bid] == pytest.approx(rated, abs=0.02)

    m2 = make_model(900, 600)
    res2 = autoplace_floor(m2, cfg, "f1", request(remotes={"operators": 3}))
    rem2 = res2["remotes"]
    assert rem2["operators_sharing"] == 3.0
    rated2 = rem2["units"][0]["max_dl_out_dbm"][bid]
    assert rem2["units"][0]["per_operator_out_dbm"][bid] == \
        pytest.approx(rated2 - 10 * math.log10(3.0), abs=0.02)
    # the remote element carries the split, and the cascade reproduces it
    ruel = els(m2, "remote")[0]
    assert ruel["operators_sharing"] == 3
    node = _remote_node(solve_cascade(m2, cfg), ruel["id"])
    cap = remote_per_operator_split(rated2, 3)
    assert node["out_dbm"] == pytest.approx(min(node["in_dbm"]
                                                + cfg["remote"]["dl_gain_db"], cap),
                                            abs=0.05)


def test_remotes_request_validates_operators(cfg):
    with pytest.raises(AutoplaceError):
        autoplace_floor(make_model(600, 400), cfg, "f1",
                        request(remotes={"operators": 0}))
    with pytest.raises(AutoplaceError):
        autoplace_floor(make_model(600, 400), cfg, "f1", request(remotes={"x": 1}))


def test_remotes_operators_true_takes_the_config_assumption(cfg):
    res = autoplace_floor(make_model(600, 400), cfg, "f1",
                          request(remotes={"operators": True, "count": 1}))
    assert res["remotes"]["operators_sharing"] == 3.0
    assert res["remotes"]["units"][0]["per_operator_out_dbm"]["B2_1900"] == \
        pytest.approx(33.0 - 10 * math.log10(3.0), abs=0.02)


def test_config_carries_the_operator_assumption(cfg):
    r = cfg["remote"]
    assert r["operators_sharing"] == 3
    assert r["operators_sharing_grade"] == "D"
    assert "Matt 2026-11-16" in r["operators_sharing_ref"]


# ============================================================ 2. uplink limit

def test_uplink_path_loss_formula():
    u = {"handset_tx_dbm": 23.0, "edge_rbs": 6, "rb_bw_hz": 180000.0,
         "ul_sinr_db": 0.0, "system_noise_figure_db": 20.0, "margin_db": 0.0}
    # hand math: 23 - 10log10(6) + 2 - 5 - (-174 + 10log10(180000) + 20 + 0) - 0
    want = (23.0 - 10.0 * math.log10(6.0) + 2.0 - 5.0
            - (-174.0 + 10.0 * math.log10(180000.0) + 20.0 + 0.0))
    assert uplink_max_path_loss_db(u, 2.0, 5.0) == pytest.approx(want)
    assert want == pytest.approx(113.667, abs=0.01)
    # zero loss and zero gain: handset power per RB against the received floor
    assert uplink_max_path_loss_db(u, 0.0, 0.0) == pytest.approx(
        23.0 - 10 * math.log10(6) + 174.0 - 10 * math.log10(180000.0) - 20.0)
    # each control moves it exactly as the formula says
    assert uplink_max_path_loss_db(dict(u, margin_db=3.0), 2, 5) == pytest.approx(want - 3)
    assert uplink_max_path_loss_db(dict(u, ul_sinr_db=7.0), 2, 5) == pytest.approx(want - 7)
    assert uplink_max_path_loss_db(dict(u, system_noise_figure_db=15.0), 2, 5) == \
        pytest.approx(want + 5)
    assert uplink_max_path_loss_db(dict(u, handset_tx_dbm=21.0), 2, 5) == \
        pytest.approx(want - 2)
    assert uplink_max_path_loss_db(dict(u, edge_rbs=60), 2, 5) == pytest.approx(want - 10)
    with pytest.raises(ValueError):
        uplink_max_path_loss_db(dict(u, edge_rbs=0))


def test_uplink_config_block_is_graded(cfg):
    u = cfg["uplink"]
    for key in ("handset_tx_dbm", "edge_rbs", "rb_bw_hz", "ul_sinr_db",
                "system_noise_figure_db", "margin_db"):
        assert u[key + "_grade"] in ("P", "G", "B", "C", "D", "E"), key
        assert u[key + "_ref"], key
    assert u["handset_tx_dbm_grade"] == "B" and "23" in u["handset_tx_dbm_ref"]
    assert u["rb_bw_hz"] == 180000.0


def test_uplink_request_parsing(cfg):
    on = _parse_uplink(cfg, None)
    assert on["enabled"] is True and on["params"]["handset_tx_dbm"] == 23.0
    assert _parse_uplink(cfg, False)["enabled"] is False
    assert _parse_uplink(cfg, True)["enabled"] is True
    assert _parse_uplink(cfg, {"margin_db": 4.0})["params"]["margin_db"] == 4.0
    with pytest.raises(AutoplaceError):
        _parse_uplink(cfg, {"nope": 1})
    with pytest.raises(AutoplaceError):
        _parse_uplink(cfg, {"rb_bw_hz": 0})
    with pytest.raises(AutoplaceError):
        _parse_uplink({}, None)     # absent config block must come from the request


def test_uplink_limits_planning_when_it_binds(cfg):
    # a +30 dB margin drags the uplink budget below the downlink budget, so the
    # uplink sets spacing: the radius shrinks and the limiting radio moves to it
    body = request()
    plain = autoplace_floor(make_model(600, 400), cfg, "f1", body)
    assert plain["uplink"]["enabled"] is True
    pl = plain["limiting"]["bands"]["B2_1900"]
    assert pl["limiting"] == "downlink"          # the uplink is far looser here
    assert pl["uplink_max_path_loss_db"] > pl["downlink_max_path_loss_db"]
    ul_pl = pl["uplink_max_path_loss_db"]
    tight = autoplace_floor(make_model(600, 400), cfg, "f1",
                            dict(body, uplink={"margin_db": 30.0}))
    tl = tight["limiting"]["bands"]["B2_1900"]
    assert tl["limiting"] == "uplink"
    # the layout re-plans with a different split, so the exact figure shifts a
    # little; the direction flip and the margin drop are what the test pins
    assert tl["uplink_max_path_loss_db"] == pytest.approx(ul_pl - 30.0, abs=3.0)
    assert tl["uplink_radius_m"] < tl["downlink_radius_m"]
    assert tight["limiting"]["limiting_direction"] == "uplink"
    assert tight["antenna_count"] >= 1
    # and uplink: false is exactly the plain run
    off = autoplace_floor(make_model(600, 400), cfg, "f1", dict(body, uplink=False))
    offrow = off["limiting"]["bands"]["B2_1900"]
    assert offrow["uplink_max_path_loss_db"] is None
    assert offrow["limiting"] == "downlink"
    assert [a["x_px"] for a in plain["antennas"]] == [a["x_px"] for a in off["antennas"]]


# ======================================================= 3. limiting band report

def test_limiting_report_structure_and_values(cfg):
    res = autoplace_floor(make_model(600, 400), cfg, "f1", request())
    lim = res["limiting"]
    assert lim["uplink_enabled"] is True
    assert set(lim["bands"]) == {"B2_1900"}
    row = lim["bands"]["B2_1900"]
    assert set(row) == {"downlink_max_path_loss_db", "uplink_max_path_loss_db",
                        "downlink_radius_m", "uplink_radius_m", "limiting",
                        "radius_m"}
    # downlink basis: the weakest antenna's EIRP minus the RSRP threshold
    eirps = [a["eirp_dbm"]["B2_1900"] for a in res["antennas"]]
    assert row["downlink_max_path_loss_db"] == pytest.approx(
        min(eirps) - res["targets"]["threshold_dbm"], abs=0.05)
    assert lim["limiting_band"] == "B2_1900"
    assert lim["limiting_direction"] in ("downlink", "uplink")
    assert row["radius_m"] == pytest.approx(min(row["downlink_radius_m"],
                                                row["uplink_radius_m"]), abs=0.05)
    # the report says which model turned each limit into a radius
    assert "EIRP" in lim["downlink_basis"]
    assert lim["uplink_basis"]
    # the summary text carries the limiting radio for the reader
    assert res["limiting"]["limiting_direction"] in res["summary"]
    assert res["limiting"]["limiting_band"] in res["summary"]


def test_limiting_report_two_bands(cfg):
    res = autoplace_floor(make_model(600, 400), cfg, "f1",
                          request(bands=["B2_1900", "B5_850"]))
    bands = res["limiting"]["bands"]
    assert set(bands) == {"B2_1900", "B5_850"}
    best = min(bands.items(), key=lambda kv: kv[1]["radius_m"])
    assert res["limiting"]["limiting_band"] == best[0]
    assert res["limiting"]["radius_m"] == pytest.approx(best[1]["radius_m"], abs=0.02)


# =================================================== 4. star / trunk4 topologies

def _runmap(model):
    return {e["id"]: e for fl in model["floors"] for e in fl["elements"]
            if e["type"] == "cable_run"}


def _every_antenna_fed(model):
    """Every antenna reaches the feeding active unit through real elements."""
    runs = list(_runmap(model).values())
    fiber_to = [r["to_id"] for r in runs if r["cable_id"] == "fiber_sm"]
    for a in els(model, "antenna"):
        seen, cur = set(), a["id"]
        while cur not in fiber_to:
            run = next((r for r in runs if r["to_id"] == cur), None)
            assert run is not None, "antenna %s has no feeding run" % a["id"]
            assert id(run) not in seen, "feed loop at %s" % a["id"]
            seen.add(id(run))
            cur = run["from_id"]
        assert cur in fiber_to and runs and seen
    return len(els(model, "antenna"))


def _loss_to_antenna(cfg, model, ant_id, bid="B2_1900"):
    """Passive loss between the feeding active unit's output port and the
    antenna: cable dB at the band + splitter IL / tap or coupler port loss,
    exactly what the cascade applies along the stored elements."""
    elsmap = {e["id"]: e for fl in model["floors"] for e in fl["elements"]
              if e["type"] != "cable_run"}
    runs = _runmap(model)
    parts = cfg["parts"]
    freq = float(next(b for b in cfg["bands"] if b["id"] == bid)["freq_mhz"])
    total, cur, seen = 0.0, ant_id, set()
    while True:
        run = next(r for r in runs.values() if r.get("to_id") == cur)
        assert run["id"] not in seen
        seen.add(run["id"])
        total += cable_loss_db(cfg["cables"][run["cable_id"]], run["length_m"], freq)
        src = elsmap[run["from_id"]]
        role = run.get("port")
        if src["type"] == "splitter":
            ways = int(src.get("ways", 2))
            total += float(src.get("il_db") or
                           parts["splitters"]["%dway" % ways]["il_db"])
        elif src["type"] == "tapper":
            grp = parts["tappers"][src.get("part_id", "default")]
            total += float((src.get("through_il_db") if src.get("through_il_db") is not None else grp["through_il_db"]) if role == "through" else
                           (src.get("tap_loss_db") if src.get("tap_loss_db") is not None
                            else grp["tap_loss_db"]))
        elif src["type"] == "coupler":
            grp = parts["couplers"][src.get("part_id", "default")]
            total += float((src.get("through_il_db") if src.get("through_il_db") is not None else grp["through_il_db"]) if role == "through" else
                           (src.get("coupled_loss_db")
                            if src.get("coupled_loss_db") is not None
                            else grp["coupled_loss_db"]))
        elif src["type"] in ("remote", "repeater", "source"):
            return total, run["from_id"]   # its node out is the budget start
        cur = run["from_id"]


def _antenna_check(cfg, m, bid="B2_1900"):
    """Solve the model and re-derive every antenna's level from the stored
    elements; asserts the cascade and the topology agree per antenna."""
    sol = solve_cascade(m, cfg)
    assert sol["errors"] == []
    outs = {n["element_id"]: n["out_dbm"]
            for n in sol["bands"][bid]["nodes"]}
    got = {a["element_id"]: a["in_dbm"] for a in sol["bands"][bid]["antennas"]}
    for a in els(m, "antenna"):
        loss, unit_id = _loss_to_antenna(cfg, m, a["id"], bid)
        assert got[a["id"]] == pytest.approx(outs[unit_id] - loss, abs=0.02), a["id"]


def test_star_topology_on_a_rectangular_floor(cfg):
    body = request(remotes={"count": 1}, topology="star")
    m = make_model(600, 400)
    res = autoplace_floor(m, cfg, "f1", body)
    assert res["topology"] == "star" and res["target_met"]
    assert res["remotes"]["count"] == 1
    n = res["antenna_count"]
    assert n >= 3, n
    ants = els(m, "antenna")
    splitters = els(m, "splitter")
    assert res["elements_added"]["splitters"] == len(splitters) >= 1
    assert _every_antenna_fed(m) == n
    # every antenna has one home run of 1/2 in from exactly one splitter
    runs = _runmap(m)
    for a in ants:
        feeds = [r for r in runs.values()
                 if r["to_id"] == a["id"] and r["cable_id"] == "coax_1_2in"]
        assert len(feeds) == 1, a["id"]
    # all splitter stages sit at the remote (the star centre)
    ru = els(m, "remote")[0]
    for sp in splitters:
        assert (sp["x"], sp["y"]) == (ru["x"], ru["y"])
    # the cascade and the star splitter ILs + home-run cable loss agree
    _antenna_check(cfg, m)
    # the BOM counts the real parts
    items = generate_bom(m, cfg)["items"]
    by_ways = {}
    for sp in splitters:
        label = cfg["parts"]["splitters"]["%dway" % sp["ways"]]["label"]
        by_ways[label] = by_ways.get(label, 0) + 1
    splitter_rows = [i for i in items if i["category"] == "splitter"]
    assert sum(i["qty"] for i in splitter_rows) == len(splitters)
    for i in splitter_rows:
        assert i["qty"] == by_ways[i["item"]]
    want_coax = round(sum(r["length_m"] for r in runs.values()
                          if r["cable_id"] != "fiber_sm"), 2)
    got_coax = sum(i["qty"] for i in items if i["category"] == "cable"
                   and i["unit"] == "m" and "Fiber" not in i["item"])
    assert got_coax == pytest.approx(want_coax, abs=0.02)
    # per-remote reporting (item 5 of the brief)
    u = res["remotes"]["units"][0]
    assert u["antennas"] == n
    assert u["coax_m"] >= 0.0
    # deterministic
    again = autoplace_floor(make_model(600, 400), cfg, "f1", copy.deepcopy(body))
    assert again["antennas"] == res["antennas"]
    assert again["elements_added"] == res["elements_added"]


def test_trunk4_topology_on_a_rectangular_floor(cfg):
    # a larger floor so each trunk carries several antennas
    body = request(remotes={"count": 1}, topology="trunk4")
    m = make_model(750, 500)
    res = autoplace_floor(m, cfg, "f1", body)
    assert res["topology"] == "trunk4" and res["target_met"]
    n = res["antenna_count"]
    assert n >= 6, n
    ants = els(m, "antenna")
    splitters = els(m, "splitter")
    devices = els(m, "tapper") + els(m, "coupler")
    assert len(splitters) == 1                          # one splitter at the remote
    assert res["elements_added"]["splitters"] == 1
    trunk_count = sp_ways = splitters[0]["ways"]
    # one tap device per antenna except the last on each trunk, which takes
    # the trunk end directly (2026-11-17 fix: no tap on a trunk's end antenna)
    assert len(devices) == n - trunk_count
    assert res["elements_added"].get("tappers", 0) \
        + res["elements_added"].get("couplers", 0) == n - trunk_count
    ru = els(m, "remote")[0]
    sp = splitters[0]
    assert (sp["x"], sp["y"]) == (ru["x"], ru["y"]) and sp_ways >= 2
    runs = _runmap(m)
    assert _every_antenna_fed(m) == n
    # an antenna is tapped off its own device at the antenna position, or is
    # the trunk end fed from the previous device's through port (or the hub)
    ends = 0
    for a in ants:
        feeds = [r for r in runs.values() if r["to_id"] == a["id"]]
        assert len(feeds) == 1, a["id"]
        if feeds[0].get("port") in ("tap", "coupled"):
            dev = next(e for e in devices if e["id"] == feeds[0]["from_id"])
            assert (dev["x"], dev["y"]) == (a["x"], a["y"])
        else:
            ends += 1
    assert ends == trunk_count
    # the cascade and the trunk path (splitter, taps, through losses, cable) agree
    _antenna_check(cfg, m)
    # taps equalise along each trunk: device loss never rises going outward
    trunks = {}
    for a in ants:
        dx, dy = a["x"] - ru["x"], a["y"] - ru["y"]
        d = ("N" if dy <= 0 else "S") if abs(dy) >= abs(dx) else ("E" if dx > 0 else "W")
        trunks.setdefault(d, []).append(a)
    parts = cfg["parts"]
    ratio_of = {}
    for dev in devices:
        key = parts["tappers" if dev["type"] == "tapper" else "couplers"][
            dev.get("part_id", "default")]
        field = "tap_loss_db" if dev["type"] == "tapper" else "coupled_loss_db"
        ratio_of[dev["id"]] = float(dev.get(field, key[field]))
    for d, grp in trunks.items():
        ordered = sorted(grp, key=lambda a: (a["x"] - ru["x"]) ** 2
                         + (a["y"] - ru["y"]) ** 2)
        # the trunk-end antenna has no device (0 dB coupling)
        vals = [ratio_of.get(next(r["from_id"] for r in runs.values()
                                  if r["to_id"] == a["id"]), 0.0) for a in ordered]
        assert vals == sorted(vals, reverse=True), (d, vals)
    # the tap values spread across the two shipped ratios, so the ports
    # balance within the ratio step plus cable spread on the trunk
    ports = [x["port_dbm"] for x in solve_cascade(m, cfg)["bands"]["B2_1900"]["antennas"]]
    assert max(ports) - min(ports) <= 9.0, ports
    # the BOM carries the devices and the splitter
    items = generate_bom(m, cfg)["items"]
    assert sum(i["qty"] for i in items if i["category"] in ("tapper", "coupler")) == n - trunk_count
    assert sum(i["qty"] for i in items if i["category"] == "splitter") == 1
    # deterministic
    again = autoplace_floor(make_model(750, 500), cfg, "f1", copy.deepcopy(body))
    assert again["antennas"] == res["antennas"]


def test_topologies_default_to_the_unchanged_auto_shape(cfg):
    body = request(threshold_dbm=-70.0, remotes={"count": 1})
    m_a = make_model(600, 400)
    a = autoplace_floor(m_a, cfg, "f1", body)
    b = autoplace_floor(make_model(600, 400), cfg, "f1", dict(body, topology="auto"))
    assert a["antennas"] == b["antennas"] and a["topology"] == "auto"
    # "auto" keeps the existing equal-splitter shape (no tap devices without taps)
    m_b = make_model(600, 400)
    autoplace_floor(m_b, cfg, "f1", body)
    assert not els(m_b, "tapper") and not els(m_b, "coupler")
    with pytest.raises(AutoplaceError):
        autoplace_floor(make_model(600, 400), cfg, "f1", dict(body, topology="bus"))
    with pytest.raises(AutoplaceError):
        autoplace_floor(make_model(600, 400), cfg, "f1",
                        dict(body, topology="star", taps=True))


# ==================================================== 5. remote count sizing

def test_remote_count_rises_when_per_remote_power_falls(cfg):
    body = request()
    base = autoplace_floor(make_model(900, 600), cfg, "f1",
                           dict(body, remotes={"max_dl_out_dbm": 33.0}))
    low = autoplace_floor(make_model(900, 600), cfg, "f1",
                          dict(body, remotes={"max_dl_out_dbm": 12.0}))
    assert base["remotes"]["count"] == 1 and base["remotes"]["budget_closed"]
    assert low["remotes"]["count"] > 1
    # remotes.count stays a fixed override: a fixed count is kept even when a
    # smaller count would close
    fixed = autoplace_floor(make_model(900, 600), cfg, "f1",
                            dict(body, remotes={"max_dl_out_dbm": 33.0,
                                                "count": 2}))
    assert fixed["remotes"]["count"] == 2


def test_remote_count_rises_with_the_operator_split(cfg):
    body = request()
    one = autoplace_floor(make_model(900, 600), cfg, "f1",
                          dict(body, remotes={"max_dl_out_dbm": 33.0,
                                              "operators": 1}))
    three = autoplace_floor(make_model(900, 600), cfg, "f1",
                            dict(body, remotes={"max_dl_out_dbm": 33.0,
                                                "operators": 3}))
    assert one["remotes"]["operators_sharing"] == 1.0
    assert three["remotes"]["operators_sharing"] == 3.0
    assert three["remotes"]["count"] >= one["remotes"]["count"]
    assert three["remotes"]["units"][0]["per_operator_out_dbm"]["B2_1900"] == \
        pytest.approx(33.0 - 10 * math.log10(3.0), abs=0.02)
    # with the count pinned at the single-operator size, the split must break
    # the close and say so
    pinned = autoplace_floor(make_model(900, 600), cfg, "f1",
                             dict(body, remotes={"max_dl_out_dbm": 33.0,
                                                 "operators": 3, "count": 1}))
    assert not pinned["remotes"]["budget_closed"]
    assert any("does not close" in w for w in pinned["warnings"])


# ========================================= 6. covered area / floor count inputs

def test_covered_inputs_sources(cfg):
    from romtool.rflink import covered_inputs
    m = {"floors": [
        {"id": "a", "w_px": 600, "h_px": 400, "ppm": 10.0, "level": 0},
        {"id": "b", "w_px": 600, "h_px": 400, "ppm": 10.0, "level": 1}]}
    # 2 x 60 m x 40 m = 4800 m2 = 51666.8 sq ft; 2 floors from the floor plans
    c = covered_inputs(m)
    assert c == {"covered_sq_ft": 51666.8, "covered_sq_ft_source": "floor_plans",
                 "floor_count": 2, "floor_count_source": "floor_plans"}
    # the budget figure backs the area only when the floor plans carry no scale
    c2 = covered_inputs({"floors": [{"id": "a", "w_px": 600, "h_px": 400, "ppm": 0}],
                         "covered_area_sqft": 999.0})
    assert c2["covered_sq_ft"] == 999.0 and c2["covered_sq_ft_source"] == "budget"
    # with a scale the floor plans win over the budget
    c2b = covered_inputs({"floors": [{"id": "a", "w_px": 600, "h_px": 400,
                                      "ppm": 10.0, "level": 0}],
                          "covered_area_sqft": 999.0})
    assert c2b["covered_sq_ft"] == pytest.approx(25833.4, abs=0.1)
    assert c2b["covered_sq_ft_source"] == "floor_plans"
    assert c2["floor_count_source"] == "floor_plans"
    # the estimator's inputs override both, and say so
    c3 = covered_inputs(dict(m, covered_sq_ft=123456.0, floor_count=4))
    assert c3["covered_sq_ft"] == 123456.0 and c3["covered_sq_ft_source"] == "project_input"
    assert c3["floor_count"] == 4 and c3["floor_count_source"] == "project_input"
    # a project without a scale or floors of zero size reports bare values
    c4 = covered_inputs({"floors": [{"id": "a", "w_px": 600, "h_px": 400, "ppm": 0}]})
    assert c4["covered_sq_ft"] is None and c4["floor_count"] == 1


def test_covered_area_and_floor_count_api(client, cfg):
    pid = client.post("/projects", json={"name": "Covered"}).json()["id"]
    pid2 = client.post("/projects", json={"name": "Covered2",
                                          "covered_sq_ft": 50000.0,
                                          "floor_count": 3}).json()["id"]
    assert client.get("/projects/%d" % pid2).json()["model"]["covered_sq_ft"] == 50000.0
    assert client.get("/projects/%d" % pid2).json()["model"]["floor_count"] == 3
    # validation
    assert client.patch("/projects/%d" % pid,
                        json={"covered_sq_ft": 0}).status_code == 422
    assert client.patch("/projects/%d" % pid,
                        json={"covered_sq_ft": "abc"}).status_code == 422
    assert client.patch("/projects/%d" % pid,
                        json={"floor_count": 0}).status_code == 422
    assert client.patch("/projects/%d" % pid,
                        json={"floor_count": 2.5}).status_code == 422
    assert client.patch("/projects/%d" % pid,
                        json={"covered_sq_ft": 50000.0, "floor_count": 3}
                        ).status_code == 200
    model = client.get("/projects/%d" % pid).json()["model"]
    assert model["covered_sq_ft"] == 50000.0 and model["floor_count"] == 3
    # clearing falls back to the derived values
    assert client.patch("/projects/%d" % pid,
                        json={"covered_sq_ft": None, "floor_count": None}
                        ).status_code == 200
    model = client.get("/projects/%d" % pid).json()["model"]
    assert "covered_sq_ft" not in model and "floor_count" not in model


def test_draft_basis_reports_the_input_source(cfg):
    """The RF-link draft reports covered area and floor count with the source
    that supplied each. (The draft-estimate endpoint loads the labor catalog,
    which is absent in this copy, so the module function is driven directly
    with a stub catalog carrying the rflink-mapped ids.)"""
    import json

    from romtool.rflink import draft_from_model, load_rflink_config

    ids = ["A01", "A02", "A04", "B01", "B03", "B09", "C01", "C02", "C06", "C07",
           "D05", "E01", "E02", "E03", "E04", "E05", "E06", "E10", "H01", "H02",
           "H04", "H05", "H06", "H07", "H09", "H10", "I01", "I02", "I03", "I05",
           "I06", "J01", "J02", "J04", "L02", "L03"]
    catalog = {"units": [{"id": i, "zone": "E-Cellular-passive"} for i in ids]}
    floors = [
        {"id": "a", "name": "L1", "level": 0, "ppm": 10.0, "w_px": 600, "h_px": 400,
         "elements": [
             {"id": "s", "type": "source", "subtype": "direct", "injection_dbm": 15.0},
             {"id": "a1", "type": "antenna", "antenna_id": "omni_das", "x": 100,
              "y": 100},
             {"id": "a2", "type": "antenna", "antenna_id": "omni_das", "x": 300,
              "y": 200},
             {"id": "r1", "type": "cable_run", "from_id": "s", "to_id": "a1",
              "cable_id": "coax_1_2in", "length_m": 10.0},
             {"id": "r2", "type": "cable_run", "from_id": "s", "to_id": "a2",
              "cable_id": "coax_1_2in", "length_m": 10.0}]},
        {"id": "b", "name": "L2", "level": 1, "ppm": 10.0, "w_px": 600, "h_px": 400,
         "elements": [
             {"id": "a3", "type": "antenna", "antenna_id": "omni_das", "x": 200,
              "y": 200}]}]
    m = {"name": "Covered", "floors": floors}
    rf = load_rflink_config()
    basis = draft_from_model(m, load_config(), catalog=catalog,
                             rflink_cfg=rf)["basis"]
    assert basis["covered_sq_ft"] == pytest.approx(51666.8, abs=0.2)
    assert basis["covered_sq_ft_source"] == "floor_plans"
    assert basis["floor_count"] == 2
    assert basis["floor_count_source"] == "floor_plans"
    # the estimator's inputs override and the draft definition says so
    m["covered_sq_ft"] = 50000.0
    m["floor_count"] = 3
    out = draft_from_model(m, load_config(), catalog=catalog, rflink_cfg=rf)
    assert out["basis"]["covered_sq_ft"] == 50000.0
    assert out["basis"]["covered_sq_ft_source"] == "project_input"
    assert out["basis"]["floor_count"] == 3
    assert out["basis"]["floor_count_source"] == "project_input"
    assert out["definition"]["covered_sq_ft"] == 50000.0
    assert out["definition"]["floor_count"] == 3
    assert out["definition"]["covered_input_sources"] == {
        "covered_sq_ft": "project_input", "floor_count": "project_input"}
    # clearing falls back to the derived sources
    del m["covered_sq_ft"]
    del m["floor_count"]
    basis = draft_from_model(m, load_config(), catalog=catalog, rflink_cfg=rf)["basis"]
    assert basis["covered_sq_ft_source"] == "floor_plans"
    assert basis["floor_count_source"] == "floor_plans"


def test_covered_sq_ft_feeds_the_oem_density(cfg):
    # the project input overrides the budget covered-area figure the OEM
    # spec-package density sizing uses
    assert settings_for({"covered_sq_ft": 777.0, "covered_area_sqft": 999.0},
                        cfg)["covered_area_sqft"] == 777.0
    assert settings_for({"covered_area_sqft": 999.0}, cfg)["covered_area_sqft"] == 999.0
    assert settings_for({}, cfg)["covered_area_sqft"] is None


# ================================================== 7. legacy-combination pin

def test_legacy_combination_reproduces_previous_results(cfg):
    """topology auto + uplink off + operators 1 is the pre-update engine: the
    numbers below are what the HEAD engine (before this change) returns for
    this fixture (measured snapshot; see PHYSICS-REPORT.md)."""
    legacy = {"topology": "auto", "uplink": False}
    res = autoplace_floor(make_model(600, 400), cfg, "f1",
                          request(bands=["B2_1900", "B5_850"], **legacy))
    assert res["antenna_count"] == 7
    assert [(a["x_px"], a["y_px"]) for a in res["antennas"]] == [
        (297.5, 199.5), (534.8, 199.5), (309.6, 32.5), (293.3, 367.4),
        (61.3, 26.7), (533.3, 373.2), (56.0, 374.0)]
    assert res["planning_eirp_dbm"] == {"B2_1900": 14.69, "B5_850": 17.91}
    assert res["elements_added"] == {"antennas": 7, "splitters": 3, "cable_runs": 11}
    r2 = autoplace_floor(make_model(900, 600), cfg, "f1",
                         request(remotes=True, **legacy))
    assert (r2["remotes"]["count"], r2["remotes"]["worst_antenna_port_dbm"],
            r2["antenna_count"]) == (1, 13.96, 10)
    r3 = autoplace_floor(make_model(900, 600), cfg, "f1",
                         request(remotes={"max_dl_out_dbm": 12.0}, **legacy))
    assert (r3["remotes"]["count"],
            round(r3["remotes"]["worst_antenna_port_dbm"], 2),
            r3["antenna_count"]) == (12, 0.49, 45)


def test_defaults_track_legacy_where_uplink_cannot_bind(cfg):
    """On this fixture the cell-edge uplink budget is far above the downlink
    budget, so plain defaults (uplink on, operators 1, auto) place exactly the
    legacy layout."""
    plain = autoplace_floor(make_model(600, 400), cfg, "f1",
                            request(bands=["B2_1900", "B5_850"]))
    legacy = autoplace_floor(make_model(600, 400), cfg, "f1",
                             request(bands=["B2_1900", "B5_850"],
                                     topology="auto", uplink=False))
    assert [a["x_px"] for a in plain["antennas"]] == \
        [a["x_px"] for a in legacy["antennas"]]
    assert plain["limiting"]["limiting_direction"] == "downlink"
    assert plain["remotes"] is None and legacy["remotes"] is None
