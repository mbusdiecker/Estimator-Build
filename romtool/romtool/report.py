"""Report PNG rendering: floor heatmap + coverage legend + KPI badge.

A zero-dependency PNG writer (RGBA, zlib/struct from the stdlib) plus a tiny
3x5 bitmap font so the exported report image can carry the band id, threshold
and pass/fail badge without any image library.
"""

import struct
import zlib

FONT = {
    "0": ["111", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "011", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "001", "010", "010"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
    ".": ["000", "000", "000", "000", "010"],
    "-": ["000", "000", "111", "000", "000"],
    ":": ["000", "010", "000", "010", "000"],
    "%": ["101", "001", "010", "100", "101"],
    ">": ["100", "010", "001", "010", "100"],
    "=": ["000", "111", "000", "111", "000"],
    " ": ["000", "000", "000", "000", "000"],
    "A": ["010", "101", "111", "101", "101"],
    "B": ["110", "101", "110", "101", "110"],
    "D": ["110", "101", "101", "101", "110"],
    "E": ["111", "100", "111", "100", "111"],
    "F": ["111", "100", "111", "100", "100"],
    "I": ["111", "010", "010", "010", "111"],
    "L": ["100", "100", "100", "100", "111"],
    "M": ["101", "111", "101", "101", "101"],
    "P": ["111", "101", "111", "100", "100"],
    "R": ["110", "101", "110", "101", "101"],
    "S": ["011", "100", "010", "001", "110"],
    "T": ["111", "010", "010", "010", "010"],
    "V": ["101", "101", "101", "101", "010"],
    "G": ["111", "100", "101", "101", "111"],
    "O": ["111", "101", "101", "101", "111"],
    "U": ["101", "101", "101", "101", "111"],
    "N": ["101", "111", "111", "101", "101"],
    "C": ["111", "100", "100", "100", "111"],
    "H": ["101", "101", "111", "101", "101"],
    "K": ["101", "101", "110", "101", "101"],
    "W": ["101", "101", "101", "111", "101"],
    "Y": ["101", "101", "010", "010", "010"],
    "J": ["001", "001", "001", "101", "010"],
    "Q": ["111", "101", "101", "111", "001"],
    "X": ["101", "101", "010", "101", "101"],
    "Z": ["111", "001", "010", "100", "111"],
    ",": ["000", "000", "000", "010", "100"],
    "/": ["001", "001", "010", "100", "100"],
    "(": ["010", "100", "100", "100", "010"],
    ")": ["010", "001", "001", "001", "010"],
    "+": ["000", "010", "111", "010", "000"],
}

WHITE = (255, 255, 255)
INK = (40, 40, 40)


def encode_png(pixels, w, h):
    """pixels: list of rows, each row a bytes/bytearray of w*4 RGBA bytes."""
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter: none
        raw.extend(row)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA
    idat = zlib.compress(bytes(raw), 6)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def color_for_dbm(value, color_scale):
    """Piecewise-linear interpolation across config report.color_scale stops.

    Stops may be listed in any order; the ramp is applied in ascending-dBm
    order (strongest signal = first stop).
    """
    stops = sorted(((float(s["dbm"]), tuple(s["rgb"])) for s in color_scale),
                   key=lambda s: s[0])
    if not stops:
        return (128, 128, 128)
    if value is None:
        value = stops[0][0] - 1.0
    if value <= stops[0][0]:
        return stops[0][1]
    if value >= stops[-1][0]:
        return stops[-1][1]
    for (d0, c0), (d1, c1) in zip(stops, stops[1:]):
        if d0 <= value <= d1:
            span = d1 - d0
            t = 0.0 if span == 0 else (value - d0) / span
            return (int(c0[0] + (c1[0] - c0[0]) * t),
                    int(c0[1] + (c1[1] - c0[1]) * t),
                    int(c0[2] + (c1[2] - c0[2]) * t))
    return stops[-1][1]


def make_canvas(w, h, rgb=WHITE):
    row = bytearray()
    for _ in range(w):
        row.extend(bytes(rgb))
        row.append(255)
    return [bytearray(row) for _ in range(h)]


def put_px(canvas, x, y, rgb):
    if 0 <= x < len(canvas[0]) // 4 and 0 <= y < len(canvas):
        base = x * 4
        canvas[y][base] = rgb[0]
        canvas[y][base + 1] = rgb[1]
        canvas[y][base + 2] = rgb[2]
        canvas[y][base + 3] = 255


def draw_line(canvas, x0, y0, x1, y1, rgb, thickness=1):
    """Bresenham line with optional square thickness."""
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    while True:
        for ox in range(thickness):
            for oy in range(thickness):
                put_px(canvas, x + ox, y + oy, rgb)
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy


def draw_disc(canvas, cx, cy, r, rgb):
    for y in range(cy - r, cy + r + 1):
        for x in range(cx - r, cx + r + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                put_px(canvas, x, y, rgb)


def draw_text(canvas, x, y, text, rgb, scale=2):
    cx = x
    for ch in text:
        glyph = FONT.get(ch)
        if glyph is None:
            glyph = FONT[" "]
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    for sx in range(scale):
                        for sy in range(scale):
                            put_px(canvas, cx + gx * scale + sx, y + gy * scale + sy, rgb)
        cx += (len(glyph[0]) + 1) * scale
    return cx


def text_width(text, scale=2):
    if not text:
        return 0
    return (len(text) * 4 - 1) * scale  # 3 px glyph + 1 px gap per char


def band_short(band_id):
    s = str(band_id or "").upper()
    for sep in ("_", "-"):
        if sep in s:
            s = s.split(sep)[0]
    # keep to glyph-supported chars
    return "".join(c for c in s if c in FONT)


def _fmt_pct(p):
    return ("%.1f" % float(p)) + "%"


def _poly_centroid(points):
    if not points:
        return (0, 0)
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def _pt_in_canvas_poly(px, py, pts):
    """Canvas-space ray-casting (same rule as geometry.point_in_polygon)."""
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


def _sanitize(text):
    up = str(text).upper()
    return "".join(c if (c in FONT or c == " ") else " " for c in up)


def render_heatmap_png(predict_result, config, title_extra=""):
    """Render the coverage heatmap for one predict result.

    Layout: [floor heatmap][legend column 96px] with a badge strip at the
    bottom. Walls and antennas are drawn in ink; grid cells are colored with
    the config color scale.
    """
    grid = predict_result["grid"]
    cols, rows, step = grid["cols"], grid["rows"], grid["step_px"]
    scale = 4 if cols * step <= 250 else 2
    fw = cols * step * scale
    fh = rows * step * scale
    legend_w = 96
    kpi = predict_result["kpi"]
    cak = kpi.get("critical_areas") or {}
    has_cas = bool(cak.get("evaluated"))
    badge_h = 26 + (18 if has_cas else 0)
    W = fw + legend_w
    stops_preview = (predict_result.get("color_scale")
                     or (config or {}).get("report", {}).get("color_scale", []) or [])
    # grow vertically only if the legend column needs more room than the
    # heatmap + badge stack (tiny test floors) — floors that fit keep the
    # height contract fh + badge_h. Legend layout: "DBM" header at y=6, first
    # stop at y=16, each stop block 10 px tall, last block 8 px of paint.
    H = max(fh + badge_h, 16 + 10 * (len(stops_preview) - 1) + 8 + 2)
    canvas = make_canvas(W, H, WHITE)

    rcfg = config.get("report", {}) if config else {}
    color_scale = predict_result.get("color_scale") or (rcfg.get("color_scale") or [])
    cas_stroke = tuple(rcfg.get("critical_area_stroke_rgb") or (200, 40, 200))
    cas_fill = tuple(rcfg.get("critical_area_fill_rgb") or (200, 40, 200))
    values = predict_result["values_dbm"]

    # heatmap cells
    for ri in range(rows):
        y0 = ri * step * scale
        base = ri * cols
        for ci in range(cols):
            rgb = color_for_dbm(values[base + ci], color_scale)
            x0 = ci * step * scale
            for y in range(y0, y0 + step * scale):
                rowpix = canvas[y]
                for x in range(x0, x0 + step * scale):
                    b = x * 4
                    rowpix[b] = rgb[0]
                    rowpix[b + 1] = rgb[1]
                    rowpix[b + 2] = rgb[2]
                    rowpix[b + 3] = 255

    # critical-area polygons: light fill + outline, drawn over the heatmap
    floor = predict_result.get("floor_geom") or {}
    for ca in floor.get("critical_areas") or []:
        pts = ca.get("points") or []
        if len(pts) < 3:
            continue
        to_c = lambda px_py: (int(round(float(px_py[0]) / floor.get("step_px", step) * step * scale)),
                              int(round(float(px_py[1]) / floor.get("step_px", step) * step * scale)))
        sp = [to_c(p) for p in pts]
        scx, scy = _poly_centroid(sp)
        for y in range(fh):
            for x in range(fw):
                if _pt_in_canvas_poly(x + 0.5, y + 0.5, sp):
                    b = x * 4
                    canvas[y][b] = (canvas[y][b] + cas_fill[0]) // 2
                    canvas[y][b + 1] = (canvas[y][b + 1] + cas_fill[1]) // 2
                    canvas[y][b + 2] = (canvas[y][b + 2] + cas_fill[2]) // 2
        for p, q in zip(sp, sp[1:] + [sp[0]]):
            draw_line(canvas, int(p[0]), int(p[1]), int(q[0]), int(q[1]),
                      cas_stroke, thickness=2)
        if ca.get("label"):
            draw_text(canvas, int(scx), int(scy) - 6,
                      _sanitize(ca["label"])[:14], cas_stroke, scale=1)

    # wall lines come from the caller-provided floor geometry (below), drawn
    # over the heatmap for context.
    for wl in floor.get("walls", []):
        x1 = int(round(float(wl["x1"]) / floor.get("step_px", step) * step * scale))
        y1 = int(round(float(wl["y1"]) / floor.get("step_px", step) * step * scale))
        x2 = int(round(float(wl["x2"]) / floor.get("step_px", step) * step * scale))
        y2 = int(round(float(wl["y2"]) / floor.get("step_px", step) * step * scale))
        draw_line(canvas, x1, y1, x2, y2, INK, thickness=max(1, scale // 2 + 1))
    for ant in predict_result.get("antennas", []):
        if ant.get("floor_id") != floor.get("id"):
            continue
        ax = int(round(float(ant["x_px"]) / floor.get("step_px", step) * step * scale))
        ay = int(round(float(ant["y_px"]) / floor.get("step_px", step) * step * scale))
        draw_disc(canvas, ax, ay, max(2, scale), (160, 20, 20))

    # legend: color stops top-down with dBm labels
    lx = fw + 8
    y0 = 6
    draw_text(canvas, lx, y0, "DBM", INK, scale=1)
    y0 += 10
    stops = [(float(s["dbm"]), tuple(s["rgb"])) for s in color_scale]
    for d, rgb in stops:
        for yy in range(y0, y0 + 8):
            for xx in range(lx, lx + 16):
                base = xx * 4
                canvas[yy][base] = rgb[0]
                canvas[yy][base + 1] = rgb[1]
                canvas[yy][base + 2] = rgb[2]
                canvas[yy][base + 3] = 255
        draw_text(canvas, lx + 22, y0 + 1, str(int(d)), INK, scale=1)
        y0 += 10

    # badge strip
    band_s = band_short(predict_result["band"]["id"])
    primary = kpi["presets"][0]
    verdict = "PASS" if primary["pass"] else "FAIL"
    badge_rgb = (22, 122, 66) if primary["pass"] else (176, 32, 32)
    thr = str(int(round(kpi["threshold_dbm"])))
    badge = "%s %sDBM %s %s" % (band_s, thr, _fmt_pct(kpi["covered_pct"]), verdict)
    draw_text(canvas, 6, fh + 7, _sanitize(badge), badge_rgb, scale=2)
    # NFPA tier-2 (critical areas) badge line when polygons were evaluated
    if has_cas:
        t2 = cak
        crit_pct = t2.get("covered_pct_union")
        crit_pass = t2.get("pass_union")
        if crit_pct is None:
            crit_badge = "CRIT NO GRID PTS"
            crit_rgb = (120, 40, 140)
        else:
            crit_badge = "CRIT %s %s" % (_fmt_pct(crit_pct),
                                         "PASS" if crit_pass else "FAIL")
            crit_rgb = (122, 32, 160) if crit_pass else (176, 32, 32)
        draw_text(canvas, 6, fh + 31, _sanitize(crit_badge), crit_rgb, scale=2)
    if title_extra:
        draw_text(canvas, W - text_width(_sanitize(title_extra), 1) - 6, fh + 12,
                  _sanitize(title_extra), INK, scale=1)

    return encode_png(canvas, W, H)
