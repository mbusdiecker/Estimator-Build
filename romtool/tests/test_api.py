"""API smoke + CRUD: SPEC acceptance 'create -> solve -> bom', plus the
preloaded example project, image upload, exports and error paths."""

import base64
import struct

import pytest


def _png_bytes(w=8, h=6):
    # minimal valid RGBA PNG via the stdlib writer (import engine for it)
    from romtool.report import encode_png

    rows = [bytes(b"".join([b"\x40\x60\x80\xff"] * w)) for _ in range(h)]
    return encode_png(rows, w, h)


def _post_json(client, url, body):
    return client.post(url, json=body)


def test_health_and_config(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["phase"] == 4
    assert r.json()["subphase"] == "E5"
    assert r.json()["version"] == "0.8.0"
    r = client.get("/config")
    cfg = r.json()
    assert [b["id"] for b in cfg["bands"]] == [
        "B12_700", "B5_850", "B2_1900", "B66_2100",
        "B14_FIRSTNET", "VHF_150", "UHF_450", "PS700", "PS800",
        "N71_600", "N41_2500", "N77_CBAND"]
    assert cfg["itu_p1238_valid_mhz"] == [300.0, 6000.0]
    assert cfg["wall_materials"]["concrete"]["loss_db"] == 15.0


def test_example_project_seeded_and_solves(client):
    r = client.get("/projects")
    projects = r.json()["projects"]
    ex = [p for p in projects if p["example"]]
    assert ex, "example project must be preloaded"
    pid = ex[0]["id"]

    proj = client.get("/projects/%d" % pid).json()
    assert len(proj["model"]["floors"]) == 2
    antenna_count = sum(
        1 for fl in proj["model"]["floors"] for el in fl["elements"]
        if el["type"] == "antenna")
    assert antenna_count == 4

    solve = client.post("/projects/%d/cascade/solve" % pid)
    assert solve.status_code == 200, solve.text
    body = solve.json()
    assert body["errors"] == []
    assert set(body["bands"]) == {
        "B12_700", "B5_850", "B2_1900", "B66_2100",
        "B14_FIRSTNET", "VHF_150", "UHF_450", "PS700", "PS800",
        "N71_600", "N41_2500", "N77_CBAND"}
    for band in body["bands"].values():
        assert len(band["antennas"]) == 4
        for a in band["antennas"]:
            assert a["eirp_dbm"] == pytest.approx(a["port_dbm"] + 2.0, abs=1e-6)

    # predict (step 2 keeps the test fast; full 1-px bench lives in test_bench)
    fid = proj["model"]["floors"][0]["id"]
    pr = client.post("/projects/%d/floors/%s/predict?band=B12_700&step=2" % (pid, fid))
    assert pr.status_code == 200, pr.text
    grid = pr.json()["grid"]
    assert grid["cols"] * grid["rows"] == (120 // 2) * (92 // 2)
    assert pr.json()["kpi"]["covered_pct"] > 0

    # BOM + exports
    bom = client.get("/projects/%d/bom" % pid).json()
    cats = {it["category"] for it in bom["items"]}
    assert {"cable", "connectors", "splitter", "antenna", "source", "repeater"} <= cats
    ant_items = [it for it in bom["items"] if it["category"] == "antenna"]
    assert sum(it["qty"] for it in ant_items) == 4
    sp = next(it for it in bom["items"] if it["category"] == "splitter")
    assert sp["qty"] == 3 and "2-way" in sp["item"]

    csv_res = client.get("/exports/projects/%d/bom.csv" % pid)
    assert csv_res.status_code == 200
    assert csv_res.headers["content-type"].startswith("text/csv")
    assert csv_res.text.startswith("category,item,qty,unit")
    json_res = client.get("/exports/projects/%d/bom.json" % pid)
    assert json_res.status_code == 200
    assert "items" in json_res.json()

    hm = client.get("/projects/%d/floors/%s/heatmap.png?band=B12_700&step=2" % (pid, fid))
    assert hm.status_code == 200
    assert hm.headers["content-type"] == "image/png"
    assert hm.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_full_crud_chain_create_solve_bom(client):
    # create
    p = _post_json(client, "/projects", {"name": "CRUD Smoke"}).json()
    pid = p["id"]
    f = _post_json(client, "/projects/%d/floors" % pid,
                   {"name": "One", "level": 0, "w_px": 100, "h_px": 80}).json()["floor"]
    fid = f["id"]

    # walls
    w = _post_json(client, "/projects/%d/floors/%s/walls" % (pid, fid),
                   {"x1": 0, "y1": 0, "x2": 100, "y2": 0, "material": "concrete"}).json()["walls"][0]
    r = _post_json(client, "/projects/%d/floors/%s/walls" % (pid, fid),
                   {"walls": [{"x1": 50, "y1": 0, "x2": 50, "y2": 80, "material": "drywall"}]})
    assert r.status_code == 200 and len(r.json()["walls"]) == 1
    assert client.post("/projects/%d/floors/%s/walls" % (pid, fid),
                       json={"material": "unobtanium"}).status_code == 422

    # chain elements
    els = [
        {"type": "source", "x": 5, "y": 5},
        {"type": "repeater", "x": 15, "y": 15},
        {"type": "splitter", "x": 50, "y": 40, "ways": 2},
        {"type": "antenna", "x": 20, "y": 70, "antenna_id": "omni_das"},
        {"type": "antenna", "x": 80, "y": 20, "antenna_id": "omni_das"},
    ]
    ids = []
    for el in els:
        r = _post_json(client, "/projects/%d/floors/%s/elements" % (pid, fid), el)
        assert r.status_code == 200, r.text
        ids.append(r.json()["element"]["id"])
    src, rep, sp, a1, a2 = ids
    runs = [(src, rep, 1.0), (rep, sp, 2.5), (sp, a1, 1.0), (sp, a2, 1.5)]
    for frm, to, ln in runs:
        r = _post_json(client, "/projects/%d/floors/%s/elements" % (pid, fid),
                       {"type": "cable_run", "from_id": frm, "to_id": to,
                        "cable_id": "lmr400", "length_m": ln})
        assert r.status_code == 200, r.text
    # auto labels fell out
    proj = client.get("/projects/%d" % pid).json()
    labels = [el.get("label") for fl in proj["model"]["floors"] for el in fl["elements"]]
    assert "A1" in labels and "A2" in labels and "SP1" in labels

    # bad element type
    assert _post_json(client, "/projects/%d/floors/%s/elements" % (pid, fid),
                      {"type": "jetpack"}).status_code == 422

    # solve
    solve = client.post("/projects/%d/cascade/solve" % pid)
    assert solve.status_code == 200, solve.text
    ports = {a["label"]: a["port_dbm"] for a in solve.json()["bands"]["B12_700"]["antennas"]}
    assert len(ports) == 2
    assert all(v < 0 for v in ports.values())  # LTE DAS port powers are negative dBm/band

    # predict + bom
    pr = client.post("/projects/%d/floors/%s/predict?band=B12_700" % (pid, fid))
    assert pr.status_code == 200
    assert pr.json()["grid"]["n_points"] == 100 * 80
    bom = client.get("/projects/%d/bom" % pid).json()
    lm = next(it for it in bom["items"] if it["category"] == "cable" and it["unit"] == "m")
    assert lm["qty"] == pytest.approx(6.0, abs=1e-6)  # 1.0+2.5+1.0+1.5

    # PATCH element (cable length) then re-solve
    run_id = next(el["id"] for fl in proj["model"]["floors"] for el in fl["elements"]
                  if el["type"] == "cable_run" and el["to_id"] == a1)
    r = client.patch("/projects/%d/elements/%s" % (pid, run_id),
                     json={"length_m": 3.0})
    assert r.status_code == 200
    bom2 = client.get("/projects/%d/bom" % pid).json()
    lm2 = next(it for it in bom2["items"] if it["category"] == "cable" and it["unit"] == "m")
    assert lm2["qty"] == pytest.approx(8.0, abs=1e-6)

    # image upload + scale patch + image fetch
    img = _png_bytes()
    up = client.post("/projects/%d/floors/%s/image" % (pid, fid),
                     files={"file": ("plan.png", img, "image/png")})
    assert up.status_code == 200, up.text
    assert up.json()["floor"]["image"]["w"] == 8
    r = client.patch("/projects/%d/floors/%s" % (pid, fid), json={"ppm": 4.0})
    assert r.status_code == 200 and r.json()["floor"]["ppm"] == 4.0
    got = client.get("/projects/%d/floors/%s/image" % (pid, fid))
    assert got.status_code == 200 and got.content == img

    # delete antenna removes attached runs; solve still fine with a1's run gone
    r = client.delete("/projects/%d/elements/%s" % (pid, a1))
    assert r.status_code == 200
    solve2 = client.post("/projects/%d/cascade/solve" % pid)
    assert solve2.status_code == 200
    assert len(solve2.json()["bands"]["B12_700"]["antennas"]) == 1

    # delete project
    assert client.delete("/projects/%d" % pid).json()["deleted"] == pid
    assert client.get("/projects/%d" % pid).status_code == 404


def test_solve_reports_graph_errors_as_422(client):
    p = _post_json(client, "/projects", {"name": "Broken"}).json()
    pid = p["id"]
    f = _post_json(client, "/projects/%d/floors" % pid, {"name": "F", "level": 0}).json()["floor"]
    fid = f["id"]
    a = _post_json(client, "/projects/%d/floors/%s/elements" % (pid, fid),
                   {"type": "antenna", "antenna_id": "omni_das", "x": 5, "y": 5}).json()
    r = client.post("/projects/%d/cascade/solve" % pid)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any("no feeding cable run" in msg for msg in detail["errors"])

    # unknown band in predict -> 400
    pr = client.post("/projects/%d/floors/%s/predict?band=NOPE" % (pid, fid))
    assert pr.status_code == 400

    # cleanup
    client.delete("/projects/%d" % pid)


def test_project_config_override_bands(client):
    """Data-driven proof: a project overrides bands -> phase additions need no code."""
    p = _post_json(client, "/projects", {"name": "Override"}).json()
    pid = p["id"]
    r = client.patch("/projects/%d" % pid, json={
        "config_overrides": {"bands": [
            {"id": "N77_C", "label": "NR C-band (phase-3 preset)", "freq_mhz": 3700.0,
             "ambient_dbm": -90.0}
        ]}})
    assert r.status_code == 200
    f = _post_json(client, "/projects/%d/floors" % pid, {"name": "F", "level": 0}).json()["floor"]
    fid = f["id"]
    ids = []
    for el in ({"type": "source", "x": 5, "y": 5},
               {"type": "repeater", "x": 15, "y": 15},
               {"type": "antenna", "x": 50, "y": 50, "antenna_id": "omni_das"}):
        ids.append(_post_json(client, "/projects/%d/floors/%s/elements" % (pid, fid), el)
                   .json()["element"]["id"])
    for frm, to in ((ids[0], ids[1]), (ids[1], ids[2])):
        assert _post_json(client, "/projects/%d/floors/%s/elements" % (pid, fid),
                          {"type": "cable_run", "from_id": frm, "to_id": to,
                           "cable_id": "lmr400", "length_m": 1.0}).status_code == 200
    sol = client.post("/projects/%d/cascade/solve" % pid)
    assert sol.status_code == 200, sol.text
    assert list(sol.json()["bands"]) == ["N77_C"]
    client.delete("/projects/%d" % pid)


def test_missing_project_is_404(client):
    assert client.get("/projects/99999").status_code == 404
    assert client.post("/projects/99999/cascade/solve").status_code == 404
    assert client.get("/exports/projects/99999/bom.csv").status_code == 404


def test_index_and_static_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "ROMtool" in r.text
    r = client.get("/static/app.js")
    assert r.status_code == 200 and "ROMtool canvas UI" in r.text
