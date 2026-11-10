"""Building perimeter tracing from a floor plan image (stdlib only).

Traces the outer boundary of a building on a plan image (PNG, JPEG or a PDF page
rendered to gray). Interior walls, doors, holes, text and dimension strings do
not change the result. The output is a polygon in floor pixels, which is stored
as floor["outline"]. Autoplace, the clutter-map exterior and the floor drawings
already read that field.

Steps. Every step runs on a work grid no larger than `work_max_side` cells a
side; one work cell is `s` image pixels.

1. Ink. A pixel is dark when gray < `dark_threshold`. A work cell is ink when at
   least `ink_fraction` of its pixels are dark.
2. Specks and frames. Ink components (8-connected) whose bounding box is smaller
   than `min_feature_m` in both directions are dropped (text glyphs, symbols,
   dimension ticks). A component spanning `frame_frac` of the page in both
   directions is dropped as a sheet border.
3. Gap close. Ink is dilated by `gap_m`, so door and window gaps in exterior
   walls close.
4. Flood fill. Background connected to the image border (4-connected) is
   outside. Everything not reached is footprint, which also fills courtyards
   and rooms, so interior walls and holes are ignored.
5. Regions. Footprint components are ranked by area. The largest
   `keep_regions` with area at least `region_min_frac` of the largest are kept.
6. Erode. The kept footprint is eroded by the same radius, which moves the
   boundary back to the outer face of the exterior walls.
7. Boundary. The outer loop of the eroded footprint is followed along cell
   corners. If several regions are kept, the convex hull of their loops is used
   and a warning says so.
8. Simplify. Douglas-Peucker with a tolerance of `simplify_m`, then edges within
   `snap_deg` of horizontal or vertical are made exactly axis-aligned.

Without a scale (ppm unknown) the metre values fall back to pixel values taken
from `gap_frac_unknown` times the short side of the image.

Scale from area: the trace is run once to get the outline area in pixels, then
ppm = sqrt(area_px / area_m2). The trace is run again with that ppm, so the gap
and simplification distances are in metres.
"""

import math
from itertools import accumulate

from .clutter_map import DEFAULT_MAX_PIXELS

DEFAULTS = {
    "dark_threshold": 128,
    "work_max_side": 800,
    "ink_fraction": 0.1,
    "min_feature_m": 0.6,
    "frame_frac": 0.85,
    "frame_min_span": 0.5,
    "frame_margin_frac": 0.03,
    "frame_blank_frac": 0.5,
    "frame_peel_max": 3,
    "roi_cut_min_px": 2,
    "roi_line_m": 0.2,
    "roi_line_px": 3,
    "roi_edge_m": 1.0,
    "roi_edge_px": 8,
    "roi_edge_share": 0.4,
    "roi_border_gap_m": 0.3,
    "roi_border_gap_px": 6,
    "gap_m": 1.0,
    "gap_frac_unknown": 0.03,
    "keep_regions": 1,
    "region_min_frac": 0.05,
    "max_footprint_frac": 0.9,
    "simplify_m": 0.3,
    "snap_deg": 3.0,
}


class PerimeterError(Exception):
    pass


def perimeter_params(config, **over):
    """Tuning values from config `perimeter` (grade E), with keyword overrides."""
    cfg = (config or {}).get("perimeter") or {}
    p = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in cfg:
            p[k] = cfg[k]
    for k, v in over.items():
        if v is not None:
            if k not in DEFAULTS:
                raise PerimeterError("unknown perimeter option '%s'" % k)
            p[k] = v
    try:
        for k in DEFAULTS:
            p[k] = float(p[k])
    except (TypeError, ValueError):
        raise PerimeterError("perimeter options must be numbers")
    p["dark_threshold"] = int(p["dark_threshold"])
    p["work_max_side"] = int(p["work_max_side"])
    p["keep_regions"] = int(p["keep_regions"])
    p["frame_peel_max"] = int(p["frame_peel_max"])
    if not 1 <= p["dark_threshold"] <= 254:
        raise PerimeterError("dark_threshold must be 1..254")
    if not 50 <= p["work_max_side"] <= 4000:
        raise PerimeterError("work_max_side must be 50..4000")
    if not 0 < p["ink_fraction"] <= 1:
        raise PerimeterError("ink_fraction must be above 0 and at most 1")
    if p["gap_m"] < 0 or p["min_feature_m"] < 0 or p["simplify_m"] <= 0:
        raise PerimeterError("gap_m and min_feature_m must be >= 0 and simplify_m > 0")
    if not 0.2 <= p["frame_frac"] <= 1.0:
        raise PerimeterError("frame_frac must be 0.2..1")
    if not 0.2 <= p["frame_min_span"] <= 1.0 or not 0 < p["frame_margin_frac"] <= 0.5:
        raise PerimeterError("frame_min_span must be 0.2..1 and frame_margin_frac 0..0.5")
    if p["roi_cut_min_px"] < 1:
        raise PerimeterError("roi_cut_min_px must be at least 1")
    if min(p["roi_line_m"], p["roi_line_px"], p["roi_edge_m"], p["roi_edge_px"],
           p["roi_border_gap_m"], p["roi_border_gap_px"]) < 0 \
            or not 0 < p["roi_edge_share"] <= 1:
        raise PerimeterError("roi line and edge distances must be >= 0 and roi_edge_share in (0, 1]")
    if not 0 < p["frame_blank_frac"] <= 1 or p["frame_peel_max"] < 0:
        raise PerimeterError("frame_blank_frac must be in (0, 1] and frame_peel_max >= 0")
    if p["keep_regions"] < 1:
        raise PerimeterError("keep_regions must be at least 1")
    if not 0 < p["region_min_frac"] <= 1 or not 0 < p["max_footprint_frac"] <= 1:
        raise PerimeterError("region_min_frac and max_footprint_frac must be in (0, 1]")
    return p


# ------------------------------------------------------------ binary grids

_INVERT = bytes(1 if v == 0 else 0 for v in range(256))


def _invert(mask):
    return bytearray(mask.translate(_INVERT))


def _dilate_line(line, r):
    """1-D dilation of 0/1 bytes by radius r (window of 2r+1)."""
    n = len(line)
    pre = list(accumulate(line, initial=0))
    if pre[-1] == 0:
        return bytes(n)
    return bytes(1 if pre[min(n, x + r + 1)] > pre[max(0, x - r)] else 0 for x in range(n))


def _dilate(mask, W, H, r):
    """Square dilation by radius r of a flat W x H 0/1 mask (row-major)."""
    if r <= 0:
        return bytearray(mask)
    tmp = bytearray(W * H)
    for y in range(H):
        base = y * W
        row = mask[base:base + W]
        if 1 in row:
            tmp[base:base + W] = _dilate_line(row, r)
    out = bytearray(W * H)
    for x in range(W):
        col = tmp[x::W]
        if 1 in col:
            out[x::W] = _dilate_line(col, r)
    return out


def _erode(mask, W, H, r):
    """Erosion = complement of the dilated complement. Outside the grid counts as empty."""
    if r <= 0:
        return bytearray(mask)
    return _invert(_dilate(_invert(mask), W, H, r))


_N4 = ((-1, 0), (1, 0), (0, -1), (0, 1))
_N8 = _N4 + ((-1, -1), (1, -1), (-1, 1), (1, 1))


def _label(mask, W, H, eight):
    """Connected components of a 0/1 mask. Returns (labels, sizes, bboxes); label 0
    is background, components are numbered from 1, bboxes are (x0, y0, x1, y1)."""
    lab = [0] * (W * H)
    sizes = [0]
    boxes = [None]
    nbr = _N8 if eight else _N4
    for start in range(W * H):
        if not mask[start] or lab[start]:
            continue
        L = len(sizes)
        sizes.append(0)
        x0 = x1 = start % W
        y0 = y1 = start // W
        lab[start] = L
        stack = [start]
        while stack:
            i = stack.pop()
            sizes[L] += 1
            x, y = i % W, i // W
            if x < x0:
                x0 = x
            elif x > x1:
                x1 = x
            if y < y0:
                y0 = y
            elif y > y1:
                y1 = y
            for dx, dy in nbr:
                nx, ny = x + dx, y + dy
                if 0 <= nx < W and 0 <= ny < H:
                    j = ny * W + nx
                    if mask[j] and not lab[j]:
                        lab[j] = L
                        stack.append(j)
        boxes.append((x0, y0, x1, y1))
    return lab, sizes, boxes


def _ink_grid(gray, iw, ih, s, W, H, dark_threshold, ink_fraction):
    table = bytes(1 if v < dark_threshold else 0 for v in range(256))
    dark = bytes(gray).translate(table)
    need = max(1.0, ink_fraction * s * s)
    ink = bytearray(W * H)
    for wy in range(H):
        acc = [0] * W
        for y in range(wy * s, min(ih, wy * s + s)):
            row = dark[y * iw:(y + 1) * iw]
            if 1 not in row:
                continue
            for bx in range(W):
                acc[bx] += sum(row[bx * s:bx * s + s])
        ink[wy * W:(wy + 1) * W] = bytes(1 if a >= need else 0 for a in acc)
    return ink


def _filter_ink(ink, W, H, min_feat_cells, frame_frac, gap_cells):
    """Drop sheet frames, then specks. A component smaller than min_feat_cells in
    both directions is a speck only when it stands alone: no other ink lies within
    gap_cells of it. Columns and short wall pieces that sit a gap apart therefore
    stay, and so does a line of them. Returns (keep, frames, specks, content):
    keep is the filtered ink; content is ink with the sheet frames removed (specks
    included), used by the blank-margin test."""
    lab, sizes, boxes = _label(ink, W, H, eight=True)
    frame_ids = set()
    small = []
    keep_ids = set()
    for L in range(1, len(sizes)):
        x0, y0, x1, y1 = boxes[L]
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if bw >= frame_frac * W and bh >= frame_frac * H:
            frame_ids.add(L)
        elif max(bw, bh) < min_feat_cells:
            small.append(L)
        else:
            keep_ids.add(L)
    frames = len(frame_ids)
    content = bytearray(ink)
    if frame_ids:
        for i in range(W * H):
            if lab[i] in frame_ids:
                content[i] = 0
    specks = 0
    if small:
        # ink (frames removed) grown by half the gap: two components merge when they
        # are within gap_cells of each other
        grown = _dilate(content, W, H, max(1, gap_cells // 2))
        glab, _, _ = _label(grown, W, H, eight=True)
        first = {}
        for i in range(W * H):
            L = lab[i]
            if L and L not in first and L not in frame_ids:
                first[L] = i
        group_count = {}
        for L, i in first.items():
            g = glab[i]
            group_count[g] = group_count.get(g, 0) + 1
        for L in small:
            if L in first and group_count.get(glab[first[L]], 0) >= 2:
                keep_ids.add(L)
            else:
                specks += 1
    keep = bytearray(1 if L in keep_ids else 0 for L in lab)
    return keep, frames, specks, content


def _flood_outside(grown, W, H):
    reached = bytearray(W * H)
    stack = []

    def seed(i):
        if not grown[i] and not reached[i]:
            reached[i] = 1
            stack.append(i)

    for x in range(W):
        seed(x)
        seed((H - 1) * W + x)
    for y in range(H):
        seed(y * W)
        seed(y * W + W - 1)
    while stack:
        i = stack.pop()
        x, y = i % W, i // W
        for dx, dy in _N4:
            nx, ny = x + dx, y + dy
            if 0 <= nx < W and 0 <= ny < H:
                j = ny * W + nx
                if not grown[j] and not reached[j]:
                    reached[j] = 1
                    stack.append(j)
    return reached


# ---------------------------------------------------------- boundary, simplify

def _boundary_loops(mask, W, H):
    """Boundary loops of a 0/1 mask as lists of cell-corner vertices (x, y).
    Mask cells are on the right of each directed edge (y grows downward)."""
    nxt = {}

    def add(a, b):
        nxt.setdefault(a, []).append(b)

    for y in range(H):
        for x in range(W):
            if not mask[y * W + x]:
                continue
            if y == 0 or not mask[(y - 1) * W + x]:
                add((x, y), (x + 1, y))
            if x == W - 1 or not mask[y * W + x + 1]:
                add((x + 1, y), (x + 1, y + 1))
            if y == H - 1 or not mask[(y + 1) * W + x]:
                add((x + 1, y + 1), (x, y + 1))
            if x == 0 or not mask[y * W + x - 1]:
                add((x, y + 1), (x, y))
    loops = []
    while nxt:
        start = next(iter(nxt))
        pts = [start]
        cur = start
        d_prev = None
        while True:
            outs = nxt.get(cur)
            if not outs:
                break
            if len(outs) == 1 or d_prev is None:
                nv = outs.pop(0)
            else:
                nv = _pick_turn(cur, outs, d_prev)
                outs.remove(nv)
            if not outs:
                del nxt[cur]
            d_prev = (nv[0] - cur[0], nv[1] - cur[1])
            cur = nv
            if cur == start:
                break
            pts.append(cur)
        if len(pts) >= 4:
            loops.append(pts)
    return loops


def _pick_turn(cur, outs, d_prev):
    """At a pinch vertex prefer a right turn, then straight, then left."""
    dx, dy = d_prev
    order = [(-dy, dx), (dx, dy), (dy, -dx)]
    for want in order:
        for nv in outs:
            if (nv[0] - cur[0], nv[1] - cur[1]) == want:
                return nv
    return outs[0]


def _area(pts):
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def polygon_area(pts):
    """Shoelace area of a closed polygon in the units of its points."""
    if not pts or len(pts) < 3:
        return 0.0
    return _area([(float(p[0]), float(p[1])) for p in pts])


def _drop_collinear(pts):
    out = []
    for p in pts:
        if out and out[-1] == p:
            continue
        out.append(p)
    changed = True
    while changed and len(out) > 3:
        changed = False
        res = []
        n = len(out)
        for i in range(n):
            a, b, c = out[i - 1], out[i], out[(i + 1) % n]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if cross == 0:
                changed = True
                continue
            res.append(b)
        out = res
    return out


def _seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _dp_open(pts, tol):
    n = len(pts)
    if n < 3:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        best, idx = -1.0, None
        for i in range(a + 1, b):
            d = _seg_dist(pts[i], pts[a], pts[b])
            if d > best:
                best, idx = d, i
        if idx is not None and best > tol:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(pts, keep) if k]


def _simplify_closed(pts, tol):
    n = len(pts)
    if n < 4:
        return list(pts)
    far = max(range(n), key=lambda i: (pts[i][0] - pts[0][0]) ** 2 + (pts[i][1] - pts[0][1]) ** 2)
    first = _dp_open(pts[:far + 1], tol)
    second = _dp_open(pts[far:] + [pts[0]], tol)
    return first[:-1] + second[:-1]


def _snap_axes(pts, snap_deg):
    t = math.tan(math.radians(snap_deg))
    p = [[float(x), float(y)] for x, y in pts]
    n = len(p)
    for i in range(n):
        j = (i + 1) % n
        dx = p[j][0] - p[i][0]
        dy = p[j][1] - p[i][1]
        if dx != 0 and abs(dy) <= t * abs(dx):
            y = (p[i][1] + p[j][1]) / 2.0
            p[i][1] = p[j][1] = y
        elif dy != 0 and abs(dx) <= t * abs(dy):
            x = (p[i][0] + p[j][0]) / 2.0
            p[i][0] = p[j][0] = x
    return [(round(x, 2), round(y, 2)) for x, y in p]


def _hull(points):
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _peel_round(keep_ink, content, W, H, d_cells, p):
    """Ink rings that look like a sheet frame. A candidate is a closed ring whose
    bounding box spans at least frame_min_span of the grid in both directions. It
    is a frame when enclosed content exists and at least frame_blank_frac of its
    ring cells have no other ink within d_cells on the inside. Other large rings
    count as ink, so a double border is peeled one ring per round (inner first).
    The margin test counts all content (specks included, so furniture-sized
    blocks count), but not sheet-border lines. A building with rooms or furniture
    against its walls fails the margin test.
    Returns the ring masks to remove."""
    lab, sizes, boxes = _label(keep_ink, W, H, eight=True)
    n = W * H
    found = []
    for L in range(1, len(sizes)):
        x0, y0, x1, y1 = boxes[L]
        if (x1 - x0 + 1) < p["frame_min_span"] * W or (y1 - y0 + 1) < p["frame_min_span"] * H:
            continue
        ring = bytearray(1 if lab[i] == L else 0 for i in range(n))
        inside = _invert(_flood_outside(ring, W, H))
        others = bytearray(1 if (content[i] and not ring[i] and inside[i]) else 0
                           for i in range(n))
        if not any(others):
            continue
        # the content inside must span most of the ring's box: a frame encloses the
        # sheet's drawing, while a building with one far partition does not qualify
        ox0, oy0, ox1, oy1 = W, H, -1, -1
        for i in range(n):
            if others[i]:
                x, y = i % W, i // W
                ox0, ox1 = min(ox0, x), max(ox1, x)
                oy0, oy1 = min(oy0, y), max(oy1, y)
        rw, rh = x1 - x0 + 1, y1 - y0 + 1
        if (ox1 - ox0 + 1) < p["frame_min_span"] * rw or (oy1 - oy0 + 1) < p["frame_min_span"] * rh:
            continue
        bad = _dilate(others, W, H, d_cells)
        ring_cells = blank = 0
        for i in range(n):
            if not ring[i]:
                continue
            x, y = i % W, i // W
            on_inside = False
            for dx, dy in _N4:
                nx, ny = x + dx, y + dy
                if 0 <= nx < W and 0 <= ny < H:
                    j = ny * W + nx
                    if inside[j] and not ring[j]:
                        on_inside = True
                        break
            if on_inside:
                ring_cells += 1
                if not bad[i]:
                    blank += 1
        if ring_cells and blank >= p["frame_blank_frac"] * ring_cells:
            found.append(ring)
    return found


def normalize_roi(roi):
    """Region of interest as a closed polygon [[x, y], ...] in image pixels, or None.
    Accepts [x0, y0, x1, y1] (a rectangle), a list of at least 3 [x, y] points, or
    {"rect": ...} / {"polygon": ...}."""
    if roi is None:
        return None
    if isinstance(roi, dict):
        if "rect" in roi:
            roi = roi["rect"]
        elif "polygon" in roi:
            roi = roi["polygon"]
        else:
            raise PerimeterError("roi needs a rect or a polygon")
    if isinstance(roi, (list, tuple)) and len(roi) == 4 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in roi):
        x0, x1 = sorted((float(roi[0]), float(roi[2])))
        y0, y1 = sorted((float(roi[1]), float(roi[3])))
        if x1 - x0 < 2 or y1 - y0 < 2:
            raise PerimeterError("roi rectangle is too small")
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    if isinstance(roi, (list, tuple)) and len(roi) >= 3 and all(
            isinstance(p, (list, tuple)) and len(p) == 2 for p in roi):
        pts = [[float(x), float(y)] for x, y in roi]
        if polygon_area(pts) < 4:
            raise PerimeterError("roi polygon has no area")
        return pts
    raise PerimeterError("roi must be [x0, y0, x1, y1] or a list of at least 3 [x, y] points")


def apply_roi(gray, iw, ih, poly):
    """Gray bytes with everything outside the polygon set to blank paper (255)."""
    out = bytearray(b"\xff") * (iw * ih)
    n = len(poly)
    for y in range(ih):
        cy = y + 0.5
        xs = []
        j = n - 1
        for i in range(n):
            x1, y1 = poly[j]
            x2, y2 = poly[i]
            if (y1 > cy) != (y2 > cy):
                xs.append(x1 + (cy - y1) * (x2 - x1) / (y2 - y1))
            j = i
        xs.sort()
        base = y * iw
        for a, b in zip(xs[0::2], xs[1::2]):
            xa = max(0, int(math.ceil(a - 0.5)))
            xb = min(iw, int(math.floor(b - 0.5)) + 1)
            if xb > xa:
                out[base + xa:base + xb] = gray[base + xa:base + xb]
    return bytes(out)


def _clear_lines(ink, W, H, fixed_row, xa, xb, edge_cells, line_cells, share):
    """Horizontal edge at row fixed_row, spanning columns xa..xb. A cell is thin when
    its vertical run of ink is at most line_cells and lies within edge_cells of the
    edge. A horizontal run of thin cells covering at least share of the edge length is
    cleared. Thick ink (a wall meeting the line) is not thin, so the line is cut there
    and the wall stays."""
    out = bytearray(ink)
    r0 = max(0, fixed_row - edge_cells)
    r1 = min(H - 1, fixed_row + edge_cells)
    if r0 > r1 or xa > xb:
        return out
    thin = bytearray(W * H)
    for x in range(xa, xb + 1):
        col = ink[x::W]
        i = 0
        while i < H:
            if col[i]:
                j = i
                while j < H and col[j]:
                    j += 1
                if j - i <= line_cells:
                    for r in range(max(i, r0), min(j, r1 + 1)):
                        thin[r * W + x] = 1
                i = j
            else:
                i += 1
    need = share * (xb - xa + 1)
    for r in range(r0, r1 + 1):
        base = r * W
        x = xa
        while x <= xb:
            if thin[base + x]:
                j = x
                while j <= xb and thin[base + j]:
                    j += 1
                if j - x >= need:
                    out[base + x:base + j] = bytes(j - x)
                x = j
            else:
                x += 1
    return out


def _clear_border_runs(ink, W, H, fixed_row, xa, xb, edge_cells, gap_cells, share):
    """Sheet border along a horizontal edge: ink within edge_cells of the edge that is
    continuous along at least share of the edge length (gaps of at most gap_cells) is
    cleared in the band, whatever its thickness. Ink made of separate blocks with
    regular gaps (a column pattern) is not continuous and stays. A solid wall this close
    to the edge is removed too, so the box must sit at least roi_edge_m outside walls."""
    out = bytearray(ink)
    r0 = max(0, fixed_row - edge_cells)
    r1 = min(H - 1, fixed_row + edge_cells)
    if r0 > r1 or xa > xb:
        return out
    present = []
    for x in range(xa, xb + 1):
        col = ink[x::W][r0:r1 + 1]
        present.append(1 in col)
    segs = []
    start = last = None
    for i, v in enumerate(present):
        if v:
            if start is None:
                start = i
            elif i - last - 1 > gap_cells:
                segs.append((start, last))
                start = i
            last = i
    if start is not None:
        segs.append((start, last))
    need = share * (xb - xa + 1)
    for a, b in segs:
        if b - a + 1 >= need:
            for x in range(xa + a, xa + b + 1):
                for r in range(r0, r1 + 1):
                    out[r * W + x] = 0
    return out


def _strip_roi_lines(ink, W, H, roi_poly, s, ppm, p):
    """Remove thin lines that run along an axis-aligned box edge (sheet-border lines
    inside the box). Metre distances use the scale; without one, the pixel values
    roi_line_px and roi_edge_px are used."""
    if ppm:
        ppm_w = ppm / s
        line_cells = max(1, int(round(p["roi_line_m"] * ppm_w)))
        edge_cells = max(1, int(round(p["roi_edge_m"] * ppm_w)))
        gap_cells = max(line_cells, int(round(p["roi_border_gap_m"] * ppm_w)))
    else:
        line_cells = max(1, int(round(p["roi_line_px"] / s)))
        edge_cells = max(1, int(round(p["roi_edge_px"] / s)))
        gap_cells = max(line_cells, int(round(p["roi_border_gap_px"] / s)))
    share = p["roi_edge_share"]
    n = len(roi_poly)
    out = bytearray(ink)
    for k in range(n):
        a, b = roi_poly[k], roi_poly[(k + 1) % n]
        if abs(a[1] - b[1]) < 1e-9 and abs(a[0] - b[0]) > 1e-9:
            row = int(round(a[1] / s))
            xa = max(0, int(min(a[0], b[0]) / s))
            xb = min(W - 1, int(max(a[0], b[0]) / s))
            out = _clear_lines(out, W, H, row, xa, xb, edge_cells, line_cells, share)
            out = _clear_border_runs(out, W, H, row, xa, xb, edge_cells, gap_cells, share)
        elif abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) > 1e-9:
            col = int(round(a[0] / s))
            ya = max(0, int(min(a[1], b[1]) / s))
            yb = min(H - 1, int(max(a[1], b[1]) / s))
            # transpose so the column becomes a row, clear, transpose back
            tr = bytearray(W * H)
            for x in range(W):
                tr[x * H:(x + 1) * H] = out[x::W]
            tr = _clear_lines(tr, H, W, col, ya, yb, edge_cells, line_cells, share)
            tr = _clear_border_runs(tr, H, W, col, ya, yb, edge_cells, gap_cells, share)
            for x in range(W):
                out[x::W] = tr[x * H:(x + 1) * H]
    return out


def _roi_cut_warnings(gray, iw, ih, poly, dark_threshold, min_px):
    """Warn when ink crosses a box edge: a wall or line runs through the box boundary.
    Checked on the plan before the box masks it, one pixel inside and outside each
    edge, sampled every pixel along the edge."""
    warnings = []
    n = len(poly)
    for k in range(n):
        a, b = poly[k], poly[(k + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length <= 0:
            continue
        nx, ny = -dy / length * 1.5, dx / length * 1.5
        hits = 0
        steps = int(length)
        for t in range(steps + 1):
            x = a[0] + dx * t / max(1, steps)
            y = a[1] + dy * t / max(1, steps)
            p1 = (int(x + nx), int(y + ny))
            p2 = (int(x - nx), int(y - ny))
            if not (0 <= p1[0] < iw and 0 <= p1[1] < ih and 0 <= p2[0] < iw and 0 <= p2[1] < ih):
                continue
            d1 = gray[p1[1] * iw + p1[0]] < dark_threshold
            d2 = gray[p2[1] * iw + p2[0]] < dark_threshold
            if d1 and d2:
                hits += 1
        if hits >= min_px:
            warnings.append("roi cuts through ink along box edge %d (%d px): the building or a "
                            "line runs through the box. Enlarge the box, or trace without it."
                            % (k + 1, hits))
    return warnings


# ------------------------------------------------------------------ tracing

def trace_outline(gray, iw, ih, ppm=None, config=None, roi=None, **options):
    """Trace the outer perimeter of the building on a plan image.

    gray: iw*ih bytes (0 black .. 255 white). ppm: floor pixels per metre, or None
    when the scale is unknown. Returns a dict with `outline` ([[x, y], ...] in image
    pixels, which equal floor pixels), `area_px`, `area_m2` (None without a scale),
    `warnings`, and the work settings used.
    """
    p = perimeter_params(config, **options)
    if iw <= 0 or ih <= 0 or len(gray) != iw * ih:
        raise PerimeterError("plan image bytes do not match its size")
    if iw * ih > DEFAULT_MAX_PIXELS:
        raise PerimeterError("image is %d x %d px (limit %d px): lower the DPI or downscale"
                             % (iw, ih, DEFAULT_MAX_PIXELS))
    warnings = []
    roi_poly = normalize_roi(roi)
    gray_full = gray
    if roi_poly:
        gray = apply_roi(gray, iw, ih, roi_poly)
    s = max(1, -(-max(iw, ih) // p["work_max_side"]))
    W, H = -(-iw // s), -(-ih // s)
    if ppm:
        ppm = float(ppm)
        ppm_w = ppm / s
        gap_cells = max(1, int(round(p["gap_m"] * ppm_w))) if p["gap_m"] > 0 else 0
        min_feat = p["min_feature_m"] * ppm_w
        simplify_px = p["simplify_m"] * ppm
    else:
        ppm = None
        warnings.append("scale not set: gap close and simplification use pixel fallbacks; "
                        "set the scale (calibrated or from area) and trace again")
        gap_cells = max(1, int(round(p["gap_frac_unknown"] * min(iw, ih) / s)))
        min_feat = 2.0 * gap_cells
        simplify_px = max(1.0, 0.5 * gap_cells * s)

    ink = _ink_grid(gray, iw, ih, s, W, H, p["dark_threshold"], p["ink_fraction"])
    if roi_poly:
        ink = _strip_roi_lines(ink, W, H, roi_poly, s, ppm, p)
    keep_ink, frames, specks, content = _filter_ink(ink, W, H, min_feat, p["frame_frac"],
                                                    gap_cells)
    if frames:
        warnings.append("ignored %d sheet-border line(s) spanning the page" % frames)
    d_cells = max(int(round(p["frame_margin_frac"] * min(W, H))), 2 * gap_cells)
    peeled = rounds = 0
    while True:
        grown = _dilate(keep_ink, W, H, gap_cells)
        reached = _flood_outside(grown, W, H)
        if rounds >= p["frame_peel_max"]:
            break
        found = _peel_round(keep_ink, content, W, H, d_cells, p)
        if not found:
            break
        for mask in found:
            for i in range(W * H):
                if mask[i]:
                    keep_ink[i] = 0
                    content[i] = 0
        peeled += len(found)
        rounds += 1
    if peeled:
        warnings.append("peeled %d sheet frame ring(s) by their blank margin" % peeled)
    foot = _invert(reached)
    if sum(foot) == 0:
        raise PerimeterError("no building footprint found: the plan has no closed outline")
    if sum(foot) > p["max_footprint_frac"] * W * H:
        raise PerimeterError("the traced footprint covers most of the page; the exterior is not "
                             "closed (raise gap_m or draw the outline by hand)")

    lab, sizes, _ = _label(foot, W, H, eight=False)
    order = sorted(range(1, len(sizes)), key=lambda L: -sizes[L])
    largest = sizes[order[0]]
    kept = [L for i, L in enumerate(order)
            if i < p["keep_regions"] and sizes[L] >= p["region_min_frac"] * largest]
    dropped = len(order) - len(kept)
    if dropped:
        warnings.append("%d smaller region(s) on the plan were not kept" % dropped)
    kept_set = set(kept)
    region_mask = bytearray(1 if L in kept_set else 0 for L in lab)
    region_mask = _erode(region_mask, W, H, gap_cells)
    loops = _boundary_loops(region_mask, W, H)
    if not loops:
        raise PerimeterError("the footprint disappeared when eroded; the building is too thin "
                             "for this gap_m")
    loops.sort(key=lambda lp: -_area(lp))
    if len(loops) > 1 and _area(loops[1]) >= p["region_min_frac"] * _area(loops[0]):
        warnings.append("%d separate outlines kept: outline is their convex hull; check it "
                        "against the plan" % len(loops))
        raw = _hull([v for lp in loops for v in lp])
    else:
        raw = loops[0]
    px = [(min(iw, x * s), min(ih, y * s)) for x, y in raw]
    px = _drop_collinear(px)
    simp = _simplify_closed(px, simplify_px)
    simp = _drop_collinear(simp)
    if len(simp) < 3:
        raise PerimeterError("traced outline has fewer than 3 points")
    snapped = _snap_axes(simp, p["snap_deg"])
    if roi_poly:
        warnings.extend(_roi_cut_warnings(gray_full, iw, ih, roi_poly, p["dark_threshold"],
                                          p["roi_cut_min_px"]))
    area_px = polygon_area(snapped)
    area_m2 = area_px / (ppm * ppm) if ppm else None
    return {
        "outline": [[x, y] for x, y in snapped],
        "area_px": round(area_px, 2),
        "area_m2": round(area_m2, 2) if area_m2 is not None else None,
        "ppm": ppm,
        "warnings": warnings,
        "work": {"scale": s, "cols": W, "rows": H, "gap_cells": gap_cells,
                 "gap_m": (gap_cells * s / ppm) if ppm else None,
                 "specks_dropped": specks, "frames_dropped": frames,
                 "frames_peeled": peeled,
                 "regions_kept": [sizes[L] for L in kept], "raw_vertices": len(raw)},
        "params": p,
    }


def scale_from_area(area_px, area_m2):
    """ppm such that an outline of area_px pixels covers area_m2 square metres."""
    if area_m2 is None or float(area_m2) <= 0:
        raise PerimeterError("area must be above 0")
    if area_px <= 0:
        raise PerimeterError("outline has no area")
    return math.sqrt(float(area_px) / float(area_m2))


def trace_floor(gray, iw, ih, ppm_known, config, area_m2=None, roi=None, **options):
    """Trace with the scale in hand. ppm_known is the floor's calibrated ppm, or None
    when the scale is not known. With area_m2 the scale is set from the traced area
    (two passes, so the metre-based settings use the new scale). Returns the trace
    dict plus `ppm` (the final scale) and `scale_source` ("from_area", "known" or
    "unknown")."""
    if area_m2 is not None:
        first = trace_outline(gray, iw, ih, None, config, roi=roi, **options)
        ppm1 = scale_from_area(first["area_px"], area_m2)
        res = trace_outline(gray, iw, ih, ppm1, config, roi=roi, **options)
        ppm2 = scale_from_area(res["area_px"], area_m2)
        res = dict(res)
        res["ppm"] = ppm2
        res["area_m2"] = round(res["area_px"] / (ppm2 * ppm2), 2)
        res["scale_source"] = "from_area"
        res["ppm_area_m2"] = float(area_m2)
        return res
    res = trace_outline(gray, iw, ih, ppm_known, config, roi=roi, **options)
    res["scale_source"] = "known" if ppm_known else "unknown"
    return res


def edit_outline(outline, ppm=None, points=None, nudges=None):
    """Manual override. `points` replaces the outline ([[x, y], ...], at least 3).
    `nudges` moves single vertices: [{"index": i, "dx_m": .., "dy_m": ..}] or with
    "dx_px"/"dy_px". Returns the new outline (list of [x, y] floats)."""
    if points is not None:
        if not isinstance(points, list) or len(points) < 3:
            raise PerimeterError("points must be a list of at least 3 [x, y] pairs")
        out = []
        for pt in points:
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise PerimeterError("points must be [x, y] pairs")
            out.append([float(pt[0]), float(pt[1])])
        return out
    if not outline:
        raise PerimeterError("the floor has no outline to nudge")
    out = [[float(x), float(y)] for x, y in outline]
    for nd in nudges or []:
        try:
            i = int(nd["index"])
        except (KeyError, TypeError, ValueError):
            raise PerimeterError("each nudge needs an integer index")
        if not 0 <= i < len(out):
            raise PerimeterError("nudge index %d is outside the %d-point outline" % (i, len(out)))
        if "dx_m" in nd or "dy_m" in nd:
            if not ppm:
                raise PerimeterError("nudging in metres needs a scale; use dx_px/dy_px instead")
            dx = float(nd.get("dx_m", 0.0)) * float(ppm)
            dy = float(nd.get("dy_m", 0.0)) * float(ppm)
        else:
            dx = float(nd.get("dx_px", 0.0))
            dy = float(nd.get("dy_px", 0.0))
        out[i][0] += dx
        out[i][1] += dy
    return out
