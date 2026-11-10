"""BOM generation from the chain graph: counts, units, feet conversion, CSV."""

import csv
import io

import pytest

from romtool.bom import M_PER_FT, bom_csv, generate_bom

from test_cascade import chain_model, test_cfg


def test_bom_counts_from_hand_example():
    items = generate_bom(chain_model(), test_cfg())["items"]
    by_key = {(it["category"], it["item"], it["unit"]): it for it in items}

    # cable: 1.0 + 2.0 + 1.0 + 1.5 = 5.5 m -> m and ft rows
    assert by_key[("cable", "test cable", "m")]["qty"] == pytest.approx(5.5, abs=1e-9)
    assert by_key[("cable", "test cable", "ft")]["qty"] == pytest.approx(18.0, abs=0.1)

    # connectors: 2 per run x 4 runs = 8
    conn = [it for it in items if it["category"] == "connectors"]
    assert len(conn) == 1
    assert conn[0]["qty"] == 8
    assert conn[0]["unit"] == "ea"

    # splitter 2-way x1, antennas omni x2, source x1, repeater x1
    assert by_key[("splitter", "Splitter, 2-way", "ea")]["qty"] == 1
    assert by_key[("antenna", "Omni test", "ea")]["qty"] == 2
    assert by_key[("source", "SRC", "ea")]["qty"] == 1
    assert by_key[("repeater", "REP", "ea")]["qty"] == 1


def test_bom_feet_conversion_row():
    items = generate_bom(chain_model(), test_cfg())["items"]
    cable_rows = [it for it in items if it["category"] == "cable"]
    assert len(cable_rows) == 2
    m_row = next(it for it in cable_rows if it["unit"] == "m")
    ft_row = next(it for it in cable_rows if it["unit"] == "ft")
    assert m_row["qty"] == pytest.approx(5.5, abs=1e-9)
    assert ft_row["qty"] == pytest.approx(round(5.5 / M_PER_FT, 1), abs=1e-9)
    assert 18.0 <= ft_row["qty"] < 18.1
    assert ft_row["item"] == m_row["item"]


def test_bom_3way_splitter_label():
    items = generate_bom(chain_model(ways=3), test_cfg())["items"]
    sp = next(it for it in items if it["category"] == "splitter")
    assert sp["item"] == "Splitter, 3-way"
    assert sp["qty"] == 1


def test_bom_empty_project():
    model = {"floors": [{"id": "f", "elements": []}]}
    assert generate_bom(model, test_cfg())["items"] == []


def test_bom_unknown_cable_raises_valueerror():
    model = chain_model()

    def bad(m):
        r_fd = next(el for fl in m["floors"] for el in fl["elements"]
                    if el.get("id") == "r_fd")
        r_fd["cable_id"] = "bogus"
    bad(model)
    with pytest.raises(ValueError):
        generate_bom(model, test_cfg())


def test_bom_csv_shape_and_quoting():
    model = chain_model()
    text = bom_csv(generate_bom(model, test_cfg())["items"])
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["category", "item", "qty", "unit"]
    # the item name "test cable" needs no quoting; use a comma-named part to test
    lookup = {(r[0], r[1]): r for r in rows}
    assert lookup[("antenna", "Omni test")][2] == "2"
    assert lookup[("repeater", "REP")][2] == "1"
    assert text.startswith("category,item,qty,unit")
