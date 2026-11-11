"""iBwave-style equipment list for the estimating tool's BOM Import paste zone.

Columns, in order (A to I): Type | Manufacturer | (blank) | Model | Description
| (blank) | (blank) | Inventory# | Qty. With by_floor a Floor column follows Qty.
One row per part (per part and floor with by_floor). Lines come from the
autoplace/element BOM (generate_bom) and the builders; each line resolves to a
part through config/parts_catalog.json. Cable is counted in feet, from the ft
rows of the element BOM, so the metre rows are skipped.

No prices are written here or read from the catalog.
"""

import csv
import io

from romtool.bom import generate_bom
from romtool.builders import builder_items, load_parts_catalog
from romtool.grades import grade_items

HEADER = ["Type", "Manufacturer", "", "Model", "Description", "", "", "Inventory#", "Qty"]
FLOOR_HEADER = "Floor"
SITE = "Site"


class IBwaveError(ValueError):
    """A BOM line has no catalog part, or its unit does not match the part."""


def ibwave_table(model, config, by_floor=False, catalog=None, settings=None):
    """Return (header, rows) for the equipment list.

    settings (from grades.settings_for) applies the grade: graded lines use the
    part chosen for that grade and budget brand. Without settings the catalog
    roles are used as before."""
    cat = catalog or load_parts_catalog()
    floors = model.get("floors", [])

    lines = []  # (floor label, item)
    if by_floor:
        for fl in floors:
            label = fl.get("name") or fl.get("id") or ""
            for it in generate_bom({"floors": [fl]}, config)["items"]:
                lines.append((label, it))
        for it in builder_items(model, config, cat):
            lines.append((SITE, it))
    else:
        for it in generate_bom(model, config)["items"]:
            lines.append(("", it))
        for it in builder_items(model, config, cat):
            lines.append(("", it))

    if settings is not None:
        # Grading can add adapter lines, so each line carries its floor label
        # through the call instead of being matched by position.
        tagged = [dict(it, _floor=label) for label, it in lines]
        graded, overlay, _notes = grade_items(tagged, config, settings, cat)
        lines = [(g.pop("_floor", ""), g) for g in graded]
        cat = dict(cat)
        cat.update(overlay)

    floor_order = {}
    for label in [fl.get("name") or fl.get("id") or "" for fl in floors] + [SITE]:
        floor_order.setdefault(label, len(floor_order))

    totals = {}  # (part key, floor label) -> qty
    parts = {}   # part key -> part dict
    for floor, it in lines:
        if it["unit"] == "m":
            continue  # the ft row carries the same cable
        role = it.get("graded_role") or it.get("role")
        if role not in cat:
            raise IBwaveError("no catalog part for role %r (line %r); add it to "
                              "config/parts_catalog.json" % (role, it.get("item")))
        part = cat[role]
        if it["unit"] != part["unit"]:
            raise IBwaveError("line %r is in %s but role %r is counted in %s"
                              % (it.get("item"), it["unit"], role, part["unit"]))
        key = (part["manufacturer"], part["model"]) if part["model"] else ("", part["description"])
        parts.setdefault(key, part)
        totals[(key, floor)] = totals.get((key, floor), 0.0) + float(it["qty"])

    rows = []
    for (key, floor) in sorted(totals, key=lambda kf: (
            parts[kf[0]]["ibwave_type"], kf[0][0], kf[0][1], floor_order.get(kf[1], 0))):
        part = parts[key]
        qty = totals[(key, floor)]
        qty = round(qty, 1) if part["unit"] == "ft" else int(round(qty))
        if not qty:
            continue
        row = [part["ibwave_type"], part["manufacturer"], "", part["model"],
               part["description"], "", "", "", qty]
        if by_floor:
            row.append(floor)
        rows.append(row)

    header = list(HEADER) + ([FLOOR_HEADER] if by_floor else [])
    return header, rows


def ibwave_csv(header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def ibwave_sheet(header, rows):
    """Sheet spec for romtool.xlsx.encode_xlsx."""
    return {"name": "Equipment List", "rows": [header] + [list(r) for r in rows]}
