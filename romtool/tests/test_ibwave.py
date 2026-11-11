"""iBwave-style equipment list: column order and header, aggregation across
floors, per-floor option, unit handling, CSV/XLSX endpoints."""

import csv
import io
import zipfile

import pytest

from romtool.builders import load_parts_catalog
from romtool.config import load_config
from romtool.ibwave import HEADER, IBwaveError, ibwave_csv, ibwave_table

HEADER_LINE = "Type,Manufacturer,,Model,Description,,,Inventory#,Qty"


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def cat():
    return load_parts_catalog()


def _floor(fid, name, elements):
    return {"id": fid, "name": name, "level": 0, "ppm": 10.0, "w_px": 100, "h_px": 100,
            "walls": [], "elements": elements}


def _two_floor_model():
    f1 = _floor("f1", "Level 1", [
        {"id": "r1", "type": "cable_run", "cable_id": "coax_1_2in", "length_m": 10.0},
        {"id": "a1", "type": "antenna", "antenna_id": "omni_das"},
    ])
    f2 = _floor("f2", "Level 2", [
        {"id": "r2", "type": "cable_run", "cable_id": "coax_1_2in", "length_m": 5.0},
        {"id": "a2", "type": "antenna", "antenna_id": "omni_das"},
        {"id": "a3", "type": "antenna", "antenna_id": "omni_das"},
    ])
    return {"environment": "office", "floors": [f1, f2]}


def test_header_is_the_paste_zone_order(cfg, cat):
    header, _ = ibwave_table(_two_floor_model(), cfg, catalog=cat)
    assert header == ["Type", "Manufacturer", "", "Model", "Description", "", "",
                      "Inventory#", "Qty"]
    assert len(header) == 9
    assert HEADER == header


def test_csv_first_line_and_row_shape(cfg, cat):
    header, rows = ibwave_table(_two_floor_model(), cfg, catalog=cat)
    text = ibwave_csv(header, rows)
    assert text.splitlines()[0] == HEADER_LINE
    parsed = list(csv.reader(io.StringIO(text)))
    assert all(len(r) == 9 for r in parsed)
    ant = next(r for r in parsed if r[3] == "CMAX-O-43-UW-i53")
    assert ant == ["Antenna", "CommScope", "", "CMAX-O-43-UW-i53",
                   "Cell-Max low PIM omni in-building antenna, 617 to 6000 MHz",
                   "", "", "", "3"]


def test_cable_aggregates_across_floors_in_feet(cfg, cat):
    # 10 m = 32.8 ft and 5 m = 16.4 ft on the same cable; one row, 49.2 ft
    header, rows = ibwave_table(_two_floor_model(), cfg, catalog=cat)
    cable = [r for r in rows if r[0] == "Cable"]
    assert len(cable) == 1
    assert cable[0][3] == "HL4RPV-50"
    assert cable[0][8] == pytest.approx(49.2)


def test_by_floor_adds_floor_column_after_qty(cfg, cat):
    header, rows = ibwave_table(_two_floor_model(), cfg, by_floor=True, catalog=cat)
    assert header[-1] == "Floor" and header[-2] == "Qty" and len(header) == 10
    cable = [r for r in rows if r[0] == "Cable"]
    assert [(r[8], r[9]) for r in cable] == [(32.8, "Level 1"), (16.4, "Level 2")]
    ants = [(r[8], r[9]) for r in rows if r[0] == "Antenna"]
    assert ants == [(1, "Level 1"), (2, "Level 2")]


def test_unknown_role_is_an_error(cfg, cat):
    bad = load_config()
    bad["cables"]["zz_unmapped"] = {"label": "Unmapped", "loss_db_per_100m_ghz": {"2.0": 1.0}}
    model = {"floors": [_floor("f1", "L1", [
        {"id": "r", "type": "cable_run", "cable_id": "zz_unmapped", "length_m": 3.0}])]}
    with pytest.raises(IBwaveError, match="cable.zz_unmapped"):
        ibwave_table(model, bad, catalog=cat)


def test_builder_lines_join_the_table(cfg, cat):
    model = _two_floor_model()
    model["builders"] = {"closet": {"fiber_runs": 2}}
    header, rows = ibwave_table(model, cfg, catalog=cat)
    fiber = [r for r in rows if r[0] == "Fiber panel"]
    assert {r[3] for r in fiber} == {"CCH-01U", "CCH-CS12-A9-P00RE"}
    cassette = next(r for r in fiber if r[3] == "CCH-CS12-A9-P00RE")
    assert cassette[8] == 2


def test_empty_project_gives_header_only(cfg, cat):
    header, rows = ibwave_table({"floors": []}, cfg, catalog=cat)
    assert rows == []
    assert ibwave_csv(header, rows).splitlines() == [HEADER_LINE]


# ------------------------------------------------------------ endpoints

def test_endpoints_csv_and_xlsx_on_example_project(client):
    pid = next(p["id"] for p in client.get("/projects").json()["projects"] if p["example"])

    r = client.get("/projects/%d/bom_ibwave.csv" % pid)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0] == HEADER_LINE
    assert any(line.startswith("Antenna,") for line in r.text.splitlines())

    r = client.get("/projects/%d/bom_ibwave.csv?by_floor=true" % pid)
    assert r.status_code == 200
    assert r.text.splitlines()[0] == HEADER_LINE + ",Floor"

    r = client.get("/projects/%d/bom_ibwave.xlsx" % pid)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert '<c r="A1" t="inlineStr"><is><t xml:space="preserve">Type</t></is></c>' in sheet
    assert '<c r="I1"' in sheet

    assert client.get("/projects/99999/bom_ibwave.csv").status_code == 404


def test_builder_endpoints_store_list_and_join_bom(client):
    pid = client.post("/projects", json={"name": "builder test"}).json()["id"]

    r = client.post("/projects/%d/builders/closet" % pid, json={"fiber_runs": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["builder"] == "closet" and body["inputs"] == {"fiber_runs": 3}
    assert any(ln["role"] == "fiber.splice_cassette" and ln["qty"] == 3 for ln in body["lines"])

    r = client.post("/projects/%d/builders/gps" % pid, json={"coax_ft": 60})
    assert r.status_code == 200

    listed = client.get("/projects/%d/builders" % pid).json()
    assert set(listed["builders"]) == {"closet", "gps"}
    assert "rooftop" in listed["available"]

    bom = client.get("/projects/%d/bom" % pid).json()
    cats = {it["category"] for it in bom["items"]}
    assert {"builder: closet", "builder: gps"} <= cats

    csv_text = client.get("/exports/projects/%d/bom.csv" % pid).text
    assert "builder: gps" in csv_text

    ib = client.get("/projects/%d/bom_ibwave.csv" % pid).text
    assert "Antenna,PCTel,,GPS-TMG-HR-26NCM," in ib
    assert "Connector,JMA Wireless,,UPL-4MT-12," in ib
    assert "Fiber panel,Corning,,CCH-CS12-A9-P00RE,\"Splice cassette, 12 fiber\",,,,3" in ib


def test_builder_endpoint_errors(client):
    pid = client.post("/projects", json={"name": "builder errors"}).json()["id"]
    assert client.post("/projects/%d/builders/nope" % pid, json={}).status_code == 404
    r = client.post("/projects/%d/builders/rooftop" % pid, json={})
    assert r.status_code == 422
    r = client.post("/projects/%d/builders/closet" % pid, json={"racks": -2})
    assert r.status_code == 422
    assert client.post("/projects/99999/builders/closet", json={}).status_code == 404
