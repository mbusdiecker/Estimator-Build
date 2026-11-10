"""Parts BOM generated from the cascade chain graph.

Everything falls out of the model's elements: cable per type (meters AND
feet), connectors (2 per cable run), splitters/taps/couplers by count and
ways, antennas by type, sources/repeaters. No prices (per SPEC non-goals).
"""

import csv
import io

M_PER_FT = 0.3048


def _elements(model):
    for fl in model.get("floors", []):
        for el in fl.get("elements", []):
            yield el


def generate_bom(model, config):
    cables = config.get("cables", {})
    parts = config.get("parts", {})
    antennas = config.get("antennas", {})
    src_cfg = config.get("source", {})
    rep_cfg = config.get("repeater", {})

    cable_len = {}
    cable_runs = {}
    splitter_ways = {}
    tapper_count = 0
    coupler_count = 0
    antenna_count = {}
    source_count = 0
    repeater_count = 0

    for el in _elements(model):
        t = el.get("type")
        if t == "cable_run":
            cid = el.get("cable_id")
            if cid not in cables:
                raise ValueError("cable run '%s' references unknown cable '%s'"
                                 % (el.get("id"), cid))
            cable_len[cid] = cable_len.get(cid, 0.0) + float(el.get("length_m", 0.0))
            cable_runs[cid] = cable_runs.get(cid, 0) + 1
        elif t == "splitter":
            ways = int(el.get("ways", 2))
            splitter_ways[ways] = splitter_ways.get(ways, 0) + 1
        elif t == "tapper":
            tapper_count += 1
        elif t == "coupler":
            coupler_count += 1
        elif t == "antenna":
            aid = el.get("antenna_id")
            if aid not in antennas:
                raise ValueError("antenna '%s' references unknown type '%s'"
                                 % (el.get("id"), aid))
            antenna_count[aid] = antenna_count.get(aid, 0) + 1
        elif t == "source":
            source_count += 1
        elif t == "repeater":
            repeater_count += 1
        elif t is not None:
            raise ValueError("element '%s' has unknown type '%s'" % (el.get("id"), t))

    items = []
    for cid in sorted(cable_len):
        label = cables[cid].get("label", cid)
        meters = round(cable_len[cid], 2)
        items.append({"category": "cable", "item": label, "qty": meters, "unit": "m"})
        items.append({
            "category": "cable", "item": label,
            "qty": round(cable_len[cid] / M_PER_FT, 1), "unit": "ft",
        })
        items.append({
            "category": "connectors", "item": "N-type connectors (pairs per %s run)" % label,
            "qty": 2 * cable_runs[cid], "unit": "ea",
        })
    for ways in sorted(splitter_ways):
        key = "%dway" % ways
        label = parts.get("splitters", {}).get(key, {}).get("label", "Splitter, %d-way" % ways)
        items.append({"category": "splitter", "item": label,
                      "qty": splitter_ways[ways], "unit": "ea"})
    if tapper_count:
        label = parts.get("tappers", {}).get("default", {}).get("label", "Tap (through + taps)")
        items.append({"category": "tapper", "item": label, "qty": tapper_count, "unit": "ea"})
    if coupler_count:
        label = (parts.get("couplers", {}).get("default", {}).get("label")
                 or "Directional coupler")
        items.append({"category": "coupler", "item": label, "qty": coupler_count, "unit": "ea"})
    for aid in sorted(antenna_count):
        label = antennas[aid].get("label", aid)
        items.append({"category": "antenna", "item": label,
                      "qty": antenna_count[aid], "unit": "ea"})
    if source_count:
        items.append({"category": "source",
                      "item": src_cfg.get("label", "Off-air source (donor)"),
                      "qty": source_count, "unit": "ea"})
    if repeater_count:
        items.append({"category": "repeater",
                      "item": rep_cfg.get("label", "Off-air repeater (BDA)"),
                      "qty": repeater_count, "unit": "ea"})

    return {"items": items}


def bom_csv(items):
    """Render BOM items as CSV text (header row included)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["category", "item", "qty", "unit"])
    for it in items:
        writer.writerow([it.get("category", ""), it.get("item", ""),
                         it.get("qty", ""), it.get("unit", "")])
    return buf.getvalue()
