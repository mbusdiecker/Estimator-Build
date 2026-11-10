"""Clutter map: a per-square obstruction class for a floor, scored from a plan image.

A floor plan image (PNG; JPEG needs Pillow; PDF needs PyMuPDF) is laid under a
square grid (default 3 m). Each square is scored by wall-line density and given
a clutter class from config `clutter_map.classes` (open, light office, dense
rooms, core, exterior), each with its own dB/m. Predict and autoplace then
replace the uniform clutter term with the sum over squares of
(dB/m x path length through that square) along the straight antenna-to-point
line.

Image decoding (which one was chosen)
-------------------------------------
PNG is decoded here with the standard library only (zlib + struct): 8-bit and
16-bit gray, gray+alpha, RGB, RGBA and palette images, plus 1/2/4-bit gray and
palette; non-interlaced. JPEG and PDF are optional: Pillow and PyMuPDF are
imported lazily inside decode_image(), and a clear ClutterMapError says what to
install (`uv run --with pillow` / `--with pymupdf`) when they are missing. The
RF core (predict, autoplace, cascade) never imports an image library.

Scoring (per square, all thresholds in config, grade E)
-------------------------------------------------------
1. Dark pixels: gray below `dark_threshold`.
2. Text / hatching suppression: a dark pixel counts as wall only when it lies
   in a straight horizontal or vertical run of at least `min_wall_len_m`
   (text strokes and diagonal hatching break into short runs and are dropped).
3. Wall crossings per metre: along every horizontal and vertical scan line,
   each wall run is one crossing (independent of line thickness). The density is
   the mean of the horizontal and vertical crossings per metre, summed over a
   (2*smooth_cells+1)-square window so a square between two walls is not scored
   as empty.
4. Class: exterior (empty squares connected to the grid border, or outside the
   floor outline) > core (ink fraction >= core_fill_pct: solid poche) > dense
   rooms (density >= dense_cpm) > light office (>= light_cpm) > open.
Stairs, shafts and hatched cores that are not solid ink need a manual override.
"""

import math
import re
import struct
import zlib

from .geometry import point_in_polygon

PNG_SIG = b"\x89PNG\r\n\x1a\n"
DEFAULT_MAX_PIXELS = 8000000


class ClutterMapError(Exception):
    pass


# ------------------------------------------------------------------ PNG I/O

def _gray_of_palette(plte):
    return bytes((299 * plte[i] + 587 * plte[i + 1] + 114 * plte[i + 2]) // 1000
                 for i in range(0, len(plte) - 2, 3))


def _unpack_bits(line, depth, width):
    """Unpack 1/2/4-bit samples (MSB first) into one byte per sample."""
    per = 8 // depth
    mask = (1 << depth) - 1
    out = bytearray()
    for b in line:
        for k in range(per):
            out.append((b >> (8 - depth * (k + 1))) & mask)
    return bytes(out[:width])


def decode_png(data):
    """Decode a PNG to (width, height, gray) with gray a bytes of w*h values
    (0 black .. 255 white). Transparency is composited over white."""
    if data[:8] != PNG_SIG:
        raise ClutterMapError("not a PNG file")
    pos, idat, plte = 8, [], b""
    w = h = depth = ctype = interlace = None
    while pos + 8 <= len(data):
        ln, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif typ == b"PLTE":
            plte = body
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if w is None or not idat:
        raise ClutterMapError("PNG has no image data")
    if interlace:
        raise ClutterMapError("interlaced PNG is not supported; re-save it without interlacing")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None or depth not in (1, 2, 4, 8, 16):
        raise ClutterMapError("unsupported PNG type %s / depth %s" % (ctype, depth))
    if depth < 8 and ctype not in (0, 3):
        raise ClutterMapError("unsupported PNG type %s / depth %s" % (ctype, depth))
    if w * h > DEFAULT_MAX_PIXELS * 2:
        raise ClutterMapError("image is %d x %d px; downscale it first" % (w, h))
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as exc:
        raise ClutterMapError("corrupt PNG data: %s" % exc)
    bits = channels * depth
    bpp = max(1, bits // 8)
    stride = (w * bits + 7) // 8
    if len(raw) < h * (stride + 1):
        raise ClutterMapError("truncated PNG data")
    pal_gray = _gray_of_palette(plte) if ctype == 3 else None
    gray = bytearray()
    prev = bytearray(stride)
    pos = 0
    for _ in range(h):
        ft = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += stride + 1
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ft == 2:
            line = bytearray((a + b) & 255 for a, b in zip(line, prev))
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        elif ft != 0:
            raise ClutterMapError("bad PNG filter type %d" % ft)
        prev = line
        if ctype == 0:
            if depth == 8:
                gray += line[:w]
            elif depth == 16:
                gray += line[0::2][:w]
            else:
                scale = 255 // ((1 << depth) - 1)
                gray += bytes(v * scale for v in _unpack_bits(line, depth, w))
        elif ctype == 3:
            idx = line[:w] if depth == 8 else _unpack_bits(line, depth, w)
            gray += bytes(pal_gray[i] if i < len(pal_gray) else 0 for i in idx)
        else:
            step = depth // 8
            pix = [line[k::channels * step] for k in range(channels)] if step == 1 else \
                  [line[k * step::channels * step] for k in range(channels)]
            if ctype == 4:
                g, a = pix[0][:w], pix[1][:w]
                gray += bytes((gv * av + 255 * (255 - av)) // 255 for gv, av in zip(g, a))
            elif ctype == 2:
                gray += bytes((299 * r + 587 * g + 114 * b) // 1000
                              for r, g, b in zip(pix[0][:w], pix[1][:w], pix[2][:w]))
            else:  # RGBA
                gray += bytes(
                    (((299 * r + 587 * g + 114 * b) // 1000) * a + 255 * (255 - a)) // 255
                    for r, g, b, a in zip(pix[0][:w], pix[1][:w], pix[2][:w], pix[3][:w]))
    return w, h, bytes(gray)


def encode_gray_png(width, height, gray):
    """Encode 8-bit gray pixels (bytes of w*h) as a PNG (for tests and tooling)."""
    def chunk(typ, body):
        return (struct.pack(">I", len(body)) + typ + body
                + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF))
    rows = b"".join(b"\x00" + gray[y * width:(y + 1) * width] for y in range(height))
    return (PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def decode_image(data, floor_w_px=None, pdf_page=0):
    """Decode PNG (stdlib), JPEG (Pillow) or PDF (PyMuPDF) to (w, h, gray)."""
    if data[:8] == PNG_SIG:
        return decode_png(data)
    if data[:2] == b"\xff\xd8":
        try:
            from PIL import Image  # optional
        except ImportError:
            raise ClutterMapError(
                "JPEG needs Pillow, which is not installed. Run ROMtool with "
                "`uv run --with pillow ...` or upload a PNG instead.")
        import io
        img = Image.open(io.BytesIO(data)).convert("L")
        return img.size[0], img.size[1], img.tobytes()
    if data[:5] == b"%PDF-":
        try:
            import fitz  # PyMuPDF, optional
        except ImportError:
            raise ClutterMapError(
                "PDF needs PyMuPDF, which is not installed. Run ROMtool with "
                "`uv run --with pymupdf ...` or export the plan as a PNG.")
        doc = fitz.open(stream=data, filetype="pdf")
        if pdf_page < 0 or pdf_page >= len(doc):
            raise ClutterMapError("pdf_page %d out of range (document has %d pages)"
                                  % (pdf_page, len(doc)))
        page = doc[pdf_page]
        zoom = (float(floor_w_px) / page.rect.width) if floor_w_px else 1.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY,
                              alpha=False)
        return pix.width, pix.height, bytes(pix.samples)
    raise ClutterMapError("unsupported image: need a PNG (JPEG with Pillow, PDF with PyMuPDF)")


# ------------------------------------------------------------------ scoring

def class_table(config):
    """(classes dict, code->id dict) from config clutter_map.classes."""
    cm = config.get("clutter_map") or {}
    classes = cm.get("classes") or {}
    if not classes:
        raise ClutterMapError("config has no clutter_map.classes")
    codes = {}
    for cid, c in classes.items():
        codes[str(c.get("code") or cid[:1])] = cid
    return classes, codes


def clutter_params(config, **over):
    cm = config.get("clutter_map") or {}
    p = {"cell_m": 3.0, "dark_threshold": 128, "min_wall_len_m": 0.9,
         "smooth_cells": 1, "core_fill_pct": 55.0, "light_cpm": 0.05,
         "dense_cpm": 0.20, "exterior_max_dark_pct": 0.1, "detect_exterior": True}
    for k in p:
        if k in cm:
            p[k] = cm[k]
    for k, v in over.items():
        if v is not None:
            if k not in p:
                raise ClutterMapError("unknown clutter map option '%s'" % k)
            p[k] = v
    try:
        p["cell_m"] = float(p["cell_m"])
        p["dark_threshold"] = int(p["dark_threshold"])
        p["min_wall_len_m"] = float(p["min_wall_len_m"])
        p["smooth_cells"] = int(p["smooth_cells"])
        p["core_fill_pct"] = float(p["core_fill_pct"])
        p["light_cpm"] = float(p["light_cpm"])
        p["dense_cpm"] = float(p["dense_cpm"])
        p["exterior_max_dark_pct"] = float(p["exterior_max_dark_pct"])
        p["detect_exterior"] = bool(p["detect_exterior"])
    except (TypeError, ValueError):
        raise ClutterMapError("clutter map options must be numbers")
    if not 0.5 <= p["cell_m"] <= 50.0:
        raise ClutterMapError("cell_m must be between 0.5 and 50")
    if not 1 <= p["dark_threshold"] <= 254:
        raise ClutterMapError("dark_threshold must be 1..254")
    if p["min_wall_len_m"] <= 0 or p["smooth_cells"] < 0 or p["smooth_cells"] > 5:
        raise ClutterMapError("min_wall_len_m must be > 0 and smooth_cells 0..5")
    if p["light_cpm"] >= p["dense_cpm"]:
        raise ClutterMapError("light_cpm must be below dense_cpm")
    return p


def _bounds(n_cells, cell_px_img, size):
    b = [min(size, int(round(c * cell_px_img))) for c in range(n_cells + 1)]
    b[-1] = size
    return b


def _index_of(bounds, size):
    idx = [0] * size
    for c in range(len(bounds) - 1):
        for v in range(bounds[c], bounds[c + 1]):
            idx[v] = c
    return idx


def build_clutter_map(gray, iw, ih, floor, config, outline=None, **options):
    """Score the plan image under the floor's grid; returns the map dict to
    store on floor["clutter_map"] (with `grid`, `auto_grid`, `overrides`)."""
    classes, codes = class_table(config)
    for need in ("open", "light_office", "dense_rooms", "core", "exterior"):
        if need not in classes:
            raise ClutterMapError("config clutter_map.classes needs '%s'" % need)
    p = clutter_params(config, **options)
    w_px, h_px = int(floor.get("w_px", 0)), int(floor.get("h_px", 0))
    ppm = float(floor.get("ppm", 10.0))
    if w_px <= 0 or h_px <= 0 or ppm <= 0:
        raise ClutterMapError("floor needs w_px, h_px and ppm before a clutter map")
    if iw * ih > DEFAULT_MAX_PIXELS:
        raise ClutterMapError("image is %d x %d px (limit %d px): downscale it"
                              % (iw, ih, DEFAULT_MAX_PIXELS))
    warnings = []
    sx, sy = iw / float(w_px), ih / float(h_px)
    if abs(sx - sy) / max(sx, sy) > 0.02:
        warnings.append("image aspect (%dx%d) differs from the floor (%dx%d); the plan is "
                        "stretched to the floor canvas" % (iw, ih, w_px, h_px))
    cell_px = p["cell_m"] * ppm
    cols = max(1, int(math.ceil(w_px / cell_px)))
    rows = max(1, int(math.ceil(h_px / cell_px)))
    xb = _bounds(cols, cell_px * sx, iw)
    yb = _bounds(rows, cell_px * sy, ih)
    col_of = _index_of(xb, iw)
    row_of = _index_of(yb, ih)

    thr = p["dark_threshold"]
    table = bytes(1 if v < thr else 0 for v in range(256))
    dark = bytes(gray).translate(table)
    run_len = max(2, int(round(p["min_wall_len_m"] * ppm * min(sx, sy))))
    long_run = re.compile(b"\x01{%d,}" % run_len)
    wall = bytearray(iw * ih)
    for y in range(ih):
        base = y * iw
        row = dark[base:base + iw]
        if 1 not in row:
            continue
        for m in long_run.finditer(row):
            s, e = m.span()
            wall[base + s:base + e] = b"\x01" * (e - s)
    for x in range(iw):
        col = dark[x::iw]
        if 1 not in col:
            continue
        for m in long_run.finditer(col):
            s, e = m.span()
            wall[s * iw + x:e * iw + x:iw] = b"\x01" * (e - s)

    any_run = re.compile(b"\x01+")
    hx = [[0] * cols for _ in range(rows)]
    vx = [[0] * cols for _ in range(rows)]
    dk = [[0] * cols for _ in range(rows)]
    for y in range(ih):
        base = y * iw
        r = row_of[y]
        wrow = bytes(wall[base:base + iw])
        if 1 in wrow:
            for m in any_run.finditer(wrow):
                hx[r][col_of[m.start()]] += 1
        drow = dark[base:base + iw]
        if 1 in drow:
            for c in range(cols):
                dk[r][c] += drow.count(1, xb[c], xb[c + 1])
    for x in range(iw):
        c = col_of[x]
        wcol = bytes(wall[x::iw])
        if 1 in wcol:
            for m in any_run.finditer(wcol):
                vx[row_of[m.start()]][c] += 1

    lines_h = [yb[r + 1] - yb[r] for r in range(rows)]
    lines_v = [xb[c + 1] - xb[c] for c in range(cols)]
    cell_area = [[(xb[c + 1] - xb[c]) * (yb[r + 1] - yb[r]) for c in range(cols)]
                 for r in range(rows)]
    s = p["smooth_cells"]
    ppm_img_x, ppm_img_y = ppm * sx, ppm * sy

    cpm = [[0.0] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            hs = vs = hl = vl = 0
            for rr in range(max(0, r - s), min(rows, r + s + 1)):
                for cc in range(max(0, c - s), min(cols, c + s + 1)):
                    hs += hx[rr][cc]
                    vs += vx[rr][cc]
                    hl += lines_h[rr]
                    vl += lines_v[cc]
            ch_h = (hs / hl) / p["cell_m"] if hl else 0.0
            ch_v = (vs / vl) / p["cell_m"] if vl else 0.0
            cpm[r][c] = 0.5 * (ch_h + ch_v)

    empty = [[dk[r][c] <= p["exterior_max_dark_pct"] / 100.0 * cell_area[r][c]
              for c in range(cols)] for r in range(rows)]
    exterior = [[False] * cols for _ in range(rows)]
    if p["detect_exterior"]:
        stack = [(r, c) for r in range(rows) for c in range(cols)
                 if (r in (0, rows - 1) or c in (0, cols - 1)) and empty[r][c]]
        for r, c in stack:
            exterior[r][c] = True
        while stack:
            r, c = stack.pop()
            for rr, cc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if 0 <= rr < rows and 0 <= cc < cols and empty[rr][cc] \
                        and not exterior[rr][cc]:
                    exterior[rr][cc] = True
                    stack.append((rr, cc))
    if outline:
        for r in range(rows):
            for c in range(cols):
                cx, cy = (c + 0.5) * cell_px, (r + 0.5) * cell_px
                if not point_in_polygon(cx, cy, outline):
                    exterior[r][c] = True

    inv = {v: k for k, v in codes.items()}
    auto = []
    stats = {cid: 0 for cid in classes}
    for r in range(rows):
        line = []
        for c in range(cols):
            fill = 100.0 * dk[r][c] / cell_area[r][c] if cell_area[r][c] else 0.0
            if exterior[r][c]:
                cid = "exterior"
            elif fill >= p["core_fill_pct"]:
                cid = "core"
            elif cpm[r][c] >= p["dense_cpm"]:
                cid = "dense_rooms"
            elif cpm[r][c] >= p["light_cpm"]:
                cid = "light_office"
            else:
                cid = "open"
            stats[cid] += 1
            line.append(inv[cid])
        auto.append("".join(line))
    cmap = {
        "version": 1, "cell_m": p["cell_m"], "cell_px": cell_px, "cols": cols,
        "rows": rows, "ppm": ppm, "codes": dict(codes),
        "grid": list(auto), "auto_grid": list(auto), "overrides": {},
        "source": {"image_w": iw, "image_h": ih, "options": p, "run_px": run_len},
        "stats": stats, "warnings": warnings,
    }
    return cmap


# ---------------------------------------------------------------- overrides

def _cell_of(cmap, spec, ppm):
    if "row" in spec and "col" in spec:
        return int(spec["row"]), int(spec["col"])
    if "x_m" in spec and "y_m" in spec:
        cs = cmap["cell_px"]
        return int(float(spec["y_m"]) * ppm // cs), int(float(spec["x_m"]) * ppm // cs)
    raise ClutterMapError("an override needs row+col or x_m+y_m")


def _cells_of(cmap, spec, ppm):
    rows, cols = cmap["rows"], cmap["cols"]
    if "rect_m" in spec:
        x0, y0, x1, y1 = [float(v) for v in spec["rect_m"]]
        cs = cmap["cell_px"]
        c0, c1 = int(min(x0, x1) * ppm // cs), int(max(x0, x1) * ppm // cs)
        r0, r1 = int(min(y0, y1) * ppm // cs), int(max(y0, y1) * ppm // cs)
        return [(r, c) for r in range(max(r0, 0), min(r1, rows - 1) + 1)
                for c in range(max(c0, 0), min(c1, cols - 1) + 1)]
    return [_cell_of(cmap, spec, ppm)]


def regrid(cmap):
    """Rebuild cmap['grid'] from auto_grid plus overrides; refresh stats."""
    codes = cmap["codes"]
    inv = {v: k for k, v in codes.items()}
    grid = [list(row) for row in cmap["auto_grid"]]
    for key, cid in cmap["overrides"].items():
        r, c = (int(v) for v in key.split(","))
        if 0 <= r < cmap["rows"] and 0 <= c < cmap["cols"] and cid in inv:
            grid[r][c] = inv[cid]
    cmap["grid"] = ["".join(row) for row in grid]
    stats = {cid: 0 for cid in codes.values()}
    for row in cmap["grid"]:
        for ch in row:
            stats[codes[ch]] += 1
    cmap["stats"] = stats
    return cmap


def apply_overrides(cmap, config, ppm, set_cells=None, clear_cells=None, clear_all=False):
    """Manual per-square overrides. set_cells: [{row,col|x_m,y_m|rect_m, class}];
    a class of null/'auto' clears. clear_cells: [{row,col|x_m,y_m|rect_m}]."""
    classes, _ = class_table(config)
    if clear_all:
        cmap["overrides"] = {}
    for spec in clear_cells or []:
        for r, c in _cells_of(cmap, spec, ppm):
            cmap["overrides"].pop("%d,%d" % (r, c), None)
    for spec in set_cells or []:
        cid = spec.get("class")
        for r, c in _cells_of(cmap, spec, ppm):
            if not (0 <= r < cmap["rows"] and 0 <= c < cmap["cols"]):
                raise ClutterMapError("cell (%d, %d) is outside the %d x %d grid"
                                      % (r, c, cmap["rows"], cmap["cols"]))
            if cid in (None, "", "auto"):
                cmap["overrides"].pop("%d,%d" % (r, c), None)
            else:
                if cid not in classes:
                    raise ClutterMapError("unknown clutter class '%s' (known: %s)"
                                          % (cid, ", ".join(sorted(classes))))
                cmap["overrides"]["%d,%d" % (r, c)] = cid
    return regrid(cmap)


# -------------------------------------------------------------------- field

class _Entry:
    """Lazily computed residual loss at the lattice nodes for one antenna."""

    __slots__ = ("field", "ax", "ay", "base", "vals")

    def __init__(self, field, ax, ay):
        self.field = field
        self.ax = ax
        self.ay = ay
        self.base = field.db_at(ax, ay)
        self.vals = [None] * ((field.cols + 1) * (field.rows + 1))

    def node(self, i, j):
        k = j * (self.field.cols + 1) + i
        v = self.vals[k]
        if v is None:
            f = self.field
            x, y = i * f.cs, j * f.cs
            d_m = math.hypot(x - self.ax, y - self.ay) / f.ppm
            v = f.trace(self.ax, self.ay, x, y) - self.base * d_m
            self.vals[k] = v
        return v


class ClutterField:
    """Path clutter loss (dB) from the stored class grid.

    loss(A -> P) = sum over squares of dB/m(square) x length of the straight
    line A->P inside the square (grid traversal, exact: trace()). For speed the
    exact value is computed at the square-corner lattice nodes only and the
    points in between use  base * d + bilinear(residual), where base is the dB/m of
    the antenna's own square and d the distance: the residual is zero inside
    that square (so a uniform map is exact everywhere) and varies smoothly
    elsewhere. Predict and autoplace both call this class, so they agree.
    """

    _CACHE_MAX = 600

    def __init__(self, cmap, config, ppm):
        classes, _ = class_table(config)
        self.cols, self.rows = int(cmap["cols"]), int(cmap["rows"])
        self.cs = float(cmap["cell_px"])
        self.ppm = float(ppm)
        codes = cmap["codes"]
        db = {}
        for ch, cid in codes.items():
            if cid not in classes:
                raise ClutterMapError("clutter class '%s' is not in config" % cid)
            db[ch] = float(classes[cid].get("db_per_m") or 0.0)
        self.db = []
        live = []
        for row in cmap["grid"]:
            for ch in row:
                self.db.append(db[ch])
                if codes[ch] != "exterior":
                    live.append(db[ch])
        if len(self.db) != self.cols * self.rows:
            raise ClutterMapError("clutter map grid does not match its size")
        self.mean_db = sum(live) / len(live) if live else 0.0
        self.min_db = min(live) if live else 0.0
        self.max_db = max(live) if live else 0.0
        self._cache = {}

    def db_at(self, x, y):
        i = min(max(int(x // self.cs), 0), self.cols - 1)
        j = min(max(int(y // self.cs), 0), self.rows - 1)
        return self.db[j * self.cols + i]

    def trace(self, ax, ay, px, py):
        """Exact sum of dB/m x length (m) of the line through the squares."""
        cs = self.cs
        inv = 1.0 / cs
        x0, y0, x1, y1 = ax * inv, ay * inv, px * inv, py * inv
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length <= 0.0:
            return 0.0
        ci, cj = int(math.floor(x0)), int(math.floor(y0))
        inf = float("inf")
        if dx > 0:
            sx, tmx, tdx = 1, (ci + 1 - x0) / dx, 1.0 / dx
        elif dx < 0:
            sx, tmx, tdx = -1, (ci - x0) / dx, -1.0 / dx
        else:
            sx, tmx, tdx = 0, inf, inf
        if dy > 0:
            sy, tmy, tdy = 1, (cj + 1 - y0) / dy, 1.0 / dy
        elif dy < 0:
            sy, tmy, tdy = -1, (cj - y0) / dy, -1.0 / dy
        else:
            sy, tmy, tdy = 0, inf, inf
        cols, rows, db = self.cols, self.rows, self.db
        total = 0.0
        t = 0.0
        for _ in range(cols + rows + 6):
            tn = tmx if tmx < tmy else tmy
            if tn > 1.0:
                tn = 1.0
            if 0 <= ci < cols and 0 <= cj < rows:
                total += db[cj * cols + ci] * (tn - t)
            if tn >= 1.0:
                break
            t = tn
            if tmx < tmy:
                ci += sx
                tmx += tdx
            else:
                cj += sy
                tmy += tdy
        return total * (length * cs / self.ppm)

    def entry(self, ax, ay, cache=True):
        if not cache:
            return _Entry(self, ax, ay)
        key = (round(ax, 1), round(ay, 1))
        ent = self._cache.get(key)
        if ent is None:
            if len(self._cache) >= self._CACHE_MAX:
                self._cache.clear()
            ent = self._cache[key] = _Entry(self, ax, ay)
        return ent

    def loss_at(self, ent, x, y, d_m):
        """Interpolated path loss (dB) from the entry's antenna to (x, y)."""
        inv = 1.0 / self.cs
        u = x * inv
        v = y * inv
        cols, rows = self.cols, self.rows
        if u < 0.0:
            u = 0.0
        elif u > cols:
            u = float(cols)
        if v < 0.0:
            v = 0.0
        elif v > rows:
            v = float(rows)
        i = int(u)
        j = int(v)
        if i >= cols:
            i = cols - 1
        if j >= rows:
            j = rows - 1
        fu, fv = u - i, v - j
        node = ent.node
        r = ((1.0 - fu) * (1.0 - fv) * node(i, j) + fu * (1.0 - fv) * node(i + 1, j)
             + (1.0 - fu) * fv * node(i, j + 1) + fu * fv * node(i + 1, j + 1))
        return ent.base * d_m + r

    def loss_list(self, ent, xs, ys):
        """Path loss (dB) from the entry's antenna to each (x, y)."""
        ax, ay, ppm = ent.ax, ent.ay, self.ppm
        hyp = math.hypot
        la = self.loss_at
        return [la(ent, x, y, hyp(x - ax, y - ay) / ppm) for x, y in zip(xs, ys)]
