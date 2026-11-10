#!/usr/bin/env python3
"""Regenerate config/labor_units.json from the DAS build-unit catalog CSV.

Usage:
    .venv/bin/python scripts/build_labor_units.py [src_csv] [out_json]

Defaults: source = /root/assistant/research/das-build-unit-catalog-v2.csv
          output = config/labor_units.json

The CSV is the source of truth (owner-maintained, keeps growing); the JSON is
the data-driven store the engine loads. Regenerate after every catalog edit --
never hand-edit the JSON. Output is deterministic (CSV row order, indent=2, no
sort) so regen diffs stay reviewable.

Row kinds emitted:
  minutes -> quantity line: minutes_low/high carry the takeoff math
  factor  -> FACTOR rows: multipliers (access/condition/schedule), never line qty
  quote   -> 0-minute rows (DAY/LOT/EA): cost-carry / recurring quote lines,
             no hours (engine derives their dollars separately, e.g. M01 lockout)
"""

import csv
import json
import os
import sys

DEFAULT_SRC = "/root/assistant/research/das-build-unit-catalog-v2.csv"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(REPO_ROOT, "config", "labor_units.json")


def _num(raw):
    raw = (raw or "").strip()
    return float(raw) if raw else None


def build(src):
    """Parse the catalog CSV into the JSON store structure."""
    units, zones = [], []
    with open(src, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        required = {"zone", "id", "item", "unit", "minutes_low", "minutes_high",
                    "source_quality", "reference", "notes"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit("catalog CSV missing columns: %s" % sorted(missing))
        for r in reader:
            zone = (r["zone"] or "").strip()
            unit = (r["unit"] or "").strip()
            lo, hi = _num(r["minutes_low"]), _num(r["minutes_high"])
            if unit.upper() == "FACTOR":
                kind, lo, hi = "factor", None, None
            elif lo == 0 and hi == 0:
                kind = "quote"
            else:
                kind = "minutes"
            if zone not in zones:
                zones.append(zone)
            units.append({
                "zone": zone,
                "id": (r["id"] or "").strip(),
                "item": (r["item"] or "").strip(),
                "unit": unit,
                "minutes_low": lo,
                "minutes_high": hi,
                "source_quality": (r["source_quality"] or "").strip(),
                "reference": (r["reference"] or "").strip(),
                "notes": (r["notes"] or "").strip(),
                "kind": kind,
            })
    ids = [u["id"] for u in units]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise SystemExit("duplicate catalog ids: %s" % dupes)
    return {
        "source_csv": os.path.abspath(src),
        "regen_cmd": "python scripts/build_labor_units.py",
        "row_count": len(units),
        "zones": zones,
        "units": units,
    }


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC
    out = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
    data = build(src)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    kinds = {}
    for u in data["units"]:
        kinds[u["kind"]] = kinds.get(u["kind"], 0) + 1
    print("wrote %s" % out)
    print("  source: %s" % data["source_csv"])
    print("  rows: %d (%s), zones: %d" % (data["row_count"], kinds, len(data["zones"])))


if __name__ == "__main__":
    main()
