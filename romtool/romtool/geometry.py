"""2-D geometry helpers for wall-crossing counting (pixel coordinates).

A wall crossing is a strict interior intersection between the straight ray
antenna -> grid point and a wall segment. Shared endpoints (a point lying
exactly on a wall) are not counted, which keeps 1-px-step grids from double
counting and matches the "ray passes through the wall" intent.
"""


def seg_cross(a1, a2, b1, b2):
    """True if open segments a1->a2 and b1->b2 strictly cross."""
    rx, ry = a2[0] - a1[0], a2[1] - a1[1]
    sx, sy = b2[0] - b1[0], b2[1] - b1[1]
    denom = rx * sy - ry * sx
    if denom == 0:
        return False
    qx, qy = b1[0] - a1[0], b1[1] - a1[1]
    t = (qx * sy - qy * sx) / denom
    u = (qx * ry - qy * rx) / denom
    return 0.0 < t < 1.0 and 0.0 < u < 1.0


def point_in_polygon(px, py, points):
    """Ray-casting test: is (px, py) inside the closed polygon `points`?

    `points` is an iterable of (x, y) pairs; the polygon is implicitly closed.
    Points exactly on an edge are counted as inside for the first bounding
    scan (>= / <= comparisons) — grid membership for KPI purposes is a
    documented approximation anyway.
    """
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 3:
        return False
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            x_int = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if px < x_int:
                inside = not inside
    return inside


def polygon_membership(cols, rows, step, polygons):
    """Grid membership for a list of polygons.

    Returns (members, per_polygon) where members[i] is True when grid point i
    (row-major, same indexing/values convention as predict) falls inside at
    least one polygon, and per_polygon is a list of boolean lists (one per
    polygon). Grid points are taken at (col*step, row*step), matching the
    prediction grid coordinates.
    """
    members = [False] * (rows * cols)
    per_polygon = []
    for points in polygons:
        mask = [False] * (rows * cols)
        for ri in range(rows):
            y = ri * step
            base = ri * cols
            for ci in range(cols):
                if point_in_polygon(ci * step, y, points):
                    mask[base + ci] = True
                    members[base + ci] = True
        per_polygon.append(mask)
    return members, per_polygon


def build_wall_index(walls):
    """Precompute per-wall bbox tuples for fast rejection in the ray loop.

    walls: iterable of dicts with x1,y1,x2,y2 and a loss lookup handled by
    the caller. Returns list of (x1, y1, x2, y2, minx, maxx, miny, maxy).
    """
    idx = []
    for w in walls:
        x1, y1, x2, y2 = float(w["x1"]), float(w["y1"]), float(w["x2"]), float(w["y2"])
        idx.append(
            (
                x1,
                y1,
                x2,
                y2,
                min(x1, x2),
                max(x1, x2),
                min(y1, y2),
                max(y1, y2),
                w,
            )
        )
    return idx


def ray_wall_crossings(wall_index, p1, p2):
    """Return the list of wall-index entries the ray p1->p2 crosses."""
    hits = []
    p1x, p1y = float(p1[0]), float(p1[1])
    p2x, p2y = float(p2[0]), float(p2[1])
    minx, maxx = min(p1x, p2x), max(p1x, p2x)
    miny, maxy = min(p1y, p2y), max(p1y, p2y)
    for w in wall_index:
        # bbox rejection before the exact segment test
        if w[4] > maxx or w[5] < minx or w[6] > maxy or w[7] < miny:
            continue
        if seg_cross(p1, p2, (w[0], w[1]), (w[2], w[3])):
            hits.append(w)
    return hits
