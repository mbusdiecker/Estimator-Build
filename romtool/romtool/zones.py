"""Area zones on a floor.

A zone is a named polygon or rectangle (floor canvas px, the same coordinates
as walls, antennas and critical areas) with a zone class from config
`clutter_map.classes`. Each class carries a default ceiling height band and a
default cable pathway. A zone may override the height (a band name or metres)
and the pathway.

Effects
-------
Clutter. A square whose centre lies in a zone takes the zone class (its dB/m).
Later zones win where they overlap. Squares outside every zone keep the floor
clutter map's automatic class. A floor with zones and no clutter map gets a
grid built from the zones alone: squares outside every zone take the floor's
building type clutter (dB/m), or 0 when no building type is set. Zones act only
when no walls are drawn, as the clutter map does. Zones override the automatic
class and manual square overrides where they overlap.

Antenna height. An antenna inside a zone sits at the zone's ceiling height. The
distance term of the propagation model then uses the slant distance
sqrt(d_h^2 + dv^2), where d_h is the horizontal distance and dv is the ceiling
height minus autoplace.floor_height_m (the floor default). Antennas outside
every zone, and every antenna on a floor without zones, have dv = 0 and are
unchanged. Clutter still uses the horizontal path. Cross-floor antennas are not
height-adjusted. Antenna height is the ceiling height (mounted at the ceiling).

Pathway. The default pathway is recorded on the zone and reported. It does not
change RF results or BOM quantities.
"""

import copy
import math

from .clutter_map import FALLBACK_CODE, FALLBACK_ID, class_table, clutter_params
from .geometry import point_in_polygon

ZONE_NAME_MAX = 80
HEIGHT_M_MIN = 1.5
HEIGHT_M_MAX = 20.0


class ZoneError(Exception):
    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        super().__init__("; ".join(errors))
        self.errors = list(errors)


def _bands(config):
    return ((config.get("clutter_map") or {}).get("ceiling_height_bands")) or {}


def _pathways(config):
    return list((config.get("clutter_map") or {}).get("cable_pathways") or [])


def _base_height(config):
    return float((config.get("autoplace") or {}).get("floor_height_m", 4.0))


def _points(raw, errors):
    if not isinstance(raw, list) or len(raw) < 3:
        errors.append("zone polygon needs at least 3 points")
        return None
    pts = []
    for p in raw:
        try:
            if isinstance(p, dict):
                pts.append([round(float(p["x"]), 3), round(float(p["y"]), 3)])
            else:
                pts.append([round(float(p[0]), 3), round(float(p[1]), 3)])
        except (KeyError, IndexError, TypeError, ValueError):
            errors.append("zone point must be [x, y] or {x, y}: %r" % (p,))
            return None
    return pts


def _rect_points(raw, errors):
    try:
        x0, y0, x1, y1 = [float(v) for v in raw]
    except (TypeError, ValueError):
        errors.append("zone rect must be [x0, y0, x1, y1]")
        return None
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        errors.append("zone rect must have a positive width and height")
        return None
    return [[round(x0, 3), round(y0, 3)], [round(x1, 3), round(y0, 3)],
            [round(x1, 3), round(y1, 3)], [round(x0, 3), round(y1, 3)]]


def normalize_zone(raw, config, base=None):
    """Validate a zone payload. `raw` holds the fields to set. `base` is the
    stored zone on a PATCH, whose other fields are kept. Returns the stored
    record without an id (the caller assigns it)."""
    if not isinstance(raw, dict):
        raise ZoneError("zone must be an object")
    classes, _ = class_table(config)
    merged = dict(base or {})
    errors = []
    if "class" in raw or not base:
        cls = raw.get("class")
        if cls not in classes:
            errors.append("zone class '%s' is not a clutter class (known: %s)"
                          % (cls, ", ".join(sorted(classes))))
        merged["class"] = cls
    if "name" in raw or not base:
        name = str(raw.get("name") or "").strip()[:ZONE_NAME_MAX]
        if not name:
            name = str((classes.get(merged.get("class")) or {}).get("label", "Zone"))
        merged["name"] = name
    if "rect" in raw:
        pts = _rect_points(raw["rect"], errors)
        merged["shape"] = "rect"
        if pts:
            merged["points"] = pts
    elif "points" in raw:
        pts = _points(raw["points"], errors)
        merged["shape"] = "polygon"
        if pts:
            merged["points"] = pts
    elif not base:
        errors.append("zone needs rect [x0, y0, x1, y1] or points")
    if "height_band" in raw:
        band = raw["height_band"]
        if band in (None, ""):
            merged["height_band"] = None
        elif band not in _bands(config):
            errors.append("unknown height band '%s' (known: %s)"
                          % (band, ", ".join(_bands(config))))
        else:
            merged["height_band"] = band
    if "ceiling_height_m" in raw:
        h = raw["ceiling_height_m"]
        if h in (None, ""):
            merged["ceiling_height_m"] = None
        else:
            try:
                h = float(h)
            except (TypeError, ValueError):
                errors.append("ceiling_height_m must be a number")
            else:
                if not HEIGHT_M_MIN <= h <= HEIGHT_M_MAX:
                    errors.append("ceiling_height_m must be between %g and %g"
                                  % (HEIGHT_M_MIN, HEIGHT_M_MAX))
                merged["ceiling_height_m"] = round(h, 2)
    if merged.get("height_band") and merged.get("ceiling_height_m") is not None:
        errors.append("give a height band or ceiling_height_m, not both")
    if "pathway" in raw:
        pw = raw["pathway"]
        if pw in (None, ""):
            merged["pathway"] = None
        elif pw not in _pathways(config):
            errors.append("unknown pathway '%s' (known: %s)"
                          % (pw, ", ".join(_pathways(config))))
        else:
            merged["pathway"] = pw
    if errors:
        raise ZoneError(errors)
    if not merged.get("points"):
        raise ZoneError("zone needs a shape")
    for key in ("height_band", "ceiling_height_m", "pathway"):
        merged.setdefault(key, None)
    return merged


def resolve_zone(zone, config):
    """Zone record plus the effective height and pathway and where each came from."""
    classes, _ = class_table(config)
    cls = classes.get(zone.get("class")) or {}
    bands = _bands(config)
    out = dict(zone)
    if zone.get("ceiling_height_m") is not None:
        out["ceiling_height_m"] = float(zone["ceiling_height_m"])
        out["height_band"] = None
        out["height_source"] = "override_m"
    else:
        band = zone.get("height_band") or cls.get("height_band")
        out["height_band"] = band
        out["height_source"] = "override_band" if zone.get("height_band") else "class"
        out["ceiling_height_m"] = float(bands[band]["height_m"]) if band in bands else None
    if zone.get("pathway"):
        out["pathway"], out["pathway_source"] = zone["pathway"], "override"
    else:
        out["pathway"], out["pathway_source"] = cls.get("pathway"), "class"
    out["class_label"] = cls.get("label", zone.get("class"))
    return out


def resolve_zones(zones, config):
    return [resolve_zone(z, config) for z in zones or []]


def zone_at(resolved, x, y):
    """Zone that holds the point (x, y); the later zone wins on overlap."""
    for z in reversed(resolved):
        if point_in_polygon(x, y, z["points"]):
            return z
    return None


def height_offset_fn(floor, config):
    """Callable (x, y) -> dv in metres for an antenna at canvas (x, y), or None
    when the floor has no zones (every antenna then has dv = 0)."""
    zones = floor.get("zones") or []
    if not zones:
        return None
    resolved = resolve_zones(zones, config)
    base = _base_height(config)

    def dv_at(x, y):
        z = zone_at(resolved, x, y)
        if z is None or z.get("ceiling_height_m") is None:
            return 0.0
        return z["ceiling_height_m"] - base
    return dv_at


def height_offset_range(floor, config):
    """(min dv, max dv) over the floor, counting 0 for squares outside zones."""
    zones = floor.get("zones") or []
    if not zones:
        return 0.0, 0.0
    base = _base_height(config)
    dvs = [0.0] + [z["ceiling_height_m"] - base for z in resolve_zones(zones, config)
                   if z.get("ceiling_height_m") is not None]
    return min(dvs), max(dvs)


def _cells_in_polygon(points, rows, cols, cell_px):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    c0 = max(0, int(math.floor(min(xs) / cell_px)))
    c1 = min(cols - 1, int(math.floor(max(xs) / cell_px)))
    r0 = max(0, int(math.floor(min(ys) / cell_px)))
    r1 = min(rows - 1, int(math.floor(max(ys) / cell_px)))
    out = []
    for r in range(r0, r1 + 1):
        cy = (r + 0.5) * cell_px
        for c in range(c0, c1 + 1):
            if point_in_polygon((c + 0.5) * cell_px, cy, points):
                out.append((r, c))
    return out


def compose_clutter_map(stored, zones, config, floor, fallback_db=0.0):
    """Clutter map for the floor's grid with zones applied. Returns `stored`
    unchanged when there are no zones. Otherwise returns a new map (the stored
    one is not modified): the stored grid with zone squares replaced, or a grid
    built from the zones alone when there is no stored map."""
    if not zones:
        return stored
    _, codes_cfg = class_table(config)
    ppm = float(floor.get("ppm", 10.0))
    if stored:
        cmap = copy.deepcopy(stored)
        codes = dict(cmap.get("codes") or {})
        for ch, cid in codes_cfg.items():
            codes.setdefault(ch, cid)
        cmap["codes"] = codes
        grid = [list(row) for row in cmap["grid"]]
        rows, cols = int(cmap["rows"]), int(cmap["cols"])
        cell_px = float(cmap["cell_px"])
    else:
        p = clutter_params(config)
        cell_px = p["cell_m"] * ppm
        cols = max(1, int(math.ceil(int(floor["w_px"]) / cell_px)))
        rows = max(1, int(math.ceil(int(floor["h_px"]) / cell_px)))
        codes = dict(codes_cfg)
        codes[FALLBACK_CODE] = FALLBACK_ID
        grid = [[FALLBACK_CODE] * cols for _ in range(rows)]
        cmap = {"version": 1, "cell_m": p["cell_m"], "cell_px": cell_px, "cols": cols,
                "rows": rows, "ppm": ppm, "codes": codes, "overrides": {},
                "source": {"zones_only": True, "options": p},
                "fallback_db_per_m": float(fallback_db or 0.0), "warnings": []}
        cmap["auto_grid"] = ["".join(r) for r in grid]
    inv = {v: k for k, v in codes.items()}
    applied = []
    for z in zones:
        cid = z.get("class")
        if cid not in inv:
            raise ZoneError("zone '%s': class '%s' has no clutter code"
                            % (z.get("id"), cid))
        cells = _cells_in_polygon(z["points"], rows, cols, cell_px)
        if not cells:
            cmap.setdefault("warnings", []).append(
                "zone '%s' covers no grid square (smaller than one %g m square)"
                % (z.get("name") or z.get("id"), cell_px / ppm))
        for r, c in cells:
            grid[r][c] = inv[cid]
        applied.append({"id": z.get("id"), "name": z.get("name"), "class": cid,
                        "squares": len(cells)})
    cmap["grid"] = ["".join(r) for r in grid]
    stats = {cid: 0 for cid in codes.values()}
    for row in cmap["grid"]:
        for ch in row:
            stats[codes[ch]] += 1
    cmap["stats"] = stats
    cmap["zones_applied"] = applied
    return cmap


def effective_clutter_map(floor, config, fallback_db=0.0):
    """Stored floor clutter map with zones applied (see compose_clutter_map),
    or None when the floor has neither a map nor zones."""
    stored = floor.get("clutter_map") or None
    zones = floor.get("zones") or []
    if not zones:
        return stored
    return compose_clutter_map(stored, zones, config, floor, fallback_db)
