"""One-line diagram and floor layout drawings for ROMtool.

Each drawing is first built as a scene: a list of primitives (rect, circle,
polygon, polyline, text, image, plus begin/end group markers) in one coordinate
system. The SVG renderer writes XML text from the scene. The PNG renderer paints
the same primitives with report.py's stdlib encoder and bitmap font, so both
formats show the same elements. Stdlib only.

Public API (all return str for SVG, bytes for PNG):
  one_line_svg(model, config, solve_result=None, project_name=None, date=None,
               power_mode="worst")
  one_line_png(...same arguments...)
  floor_layout_svg(model, config, floor_id, include_image=True, project_name=None)
  floor_layout_png(...same arguments...)

power_mode is "worst" (lowest port power over the bands) or "first" (first band
in the solve result). Ports below 0 dBm are drawn in red.
"""

import base64
import math
from xml.sax.saxutils import escape

from .clutter_map import ClutterMapError, decode_png
from .report import FONT, WHITE, draw_disc, encode_png, make_canvas, put_px, text_width
from .zones import resolve_zones


class DrawingError(ValueError):
    """Bad input for a drawing (unknown floor, empty model)."""


FT_PER_M = 3.28084
NODE_TYPES = ("source", "repeater", "remote", "splitter", "tapper", "coupler", "antenna")
TYPE_ORDER = ("source", "repeater", "remote", "splitter", "tapper", "coupler", "antenna")
TYPE_LABELS = {"source": "Source (head end)", "repeater": "Repeater", "remote": "Remote unit",
               "splitter": "Splitter", "tapper": "Tapper", "coupler": "Coupler",
               "antenna": "Antenna"}
CLUTTER_ORDER = ("open", "light_office", "dense_rooms", "core", "exterior")

# ANS palette (brand rule for ANS work). Red is a status color.
NAVY = "#112732"
DEEP = "#072833"
BLUE = "#02548d"
CYAN = "#0895cd"
BRIGHT = "#05cdfb"
ORANGE = "#f37d28"
CHARCOAL = "#232426"
GRAY = "#868e92"
LIGHT = "#e7e7e7"
PAPER = "#f2f1f0"
WHITE_HEX = "#ffffff"
RED = "#b00020"

CABLE_COLORS = {"lmr400": BLUE, "coax_1_2in": CYAN, "coax_7_8in": NAVY}
CABLE_DEFAULT = GRAY
CLUTTER_STYLE = {"open": (BRIGHT, 0.18), "light_office": (LIGHT, 0.5),
                 "dense_rooms": (ORANGE, 0.22), "core": (CHARCOAL, 0.4),
                 "exterior": (GRAY, 0.12)}
# zone fills by zone class (translucent; outlined in navy)
ZONE_STYLE = {"warehouse_racked": (BLUE, 0.22), "high_bay_open": (CYAN, 0.2),
              "office_open": (LIGHT, 0.5), "office_standard": (LIGHT, 0.7),
              "office_dense": (CHARCOAL, 0.25), "hospital_rooms": (ORANGE, 0.22),
              "hotel_rooms": (ORANGE, 0.12), "retail": (BRIGHT, 0.14),
              "parking_garage": (GRAY, 0.25), "stadium_bowl": (BLUE, 0.12)}

NODE_STYLE = {
    "source": {"fill": NAVY, "text": WHITE_HEX, "stroke": NAVY},
    "repeater": {"fill": BLUE, "text": WHITE_HEX, "stroke": BLUE},
    "remote": {"fill": CYAN, "text": NAVY, "stroke": NAVY},
    "splitter": {"fill": LIGHT, "text": NAVY, "stroke": NAVY},
    "tapper": {"fill": PAPER, "text": NAVY, "stroke": CYAN},
    "coupler": {"fill": PAPER, "text": NAVY, "stroke": CYAN},
    "antenna": {"fill": WHITE_HEX, "text": NAVY, "stroke": ORANGE},
}

# Metrics in logical units. SVG is looser; PNG is tighter so a tree stays
# readable at 1:1 pixels. t1 / t2 are the title and detail baselines in a box.
ONE_LINE_SVG = {"row": 56, "box_w": 170, "box_h": 40, "gap": 200, "head": 26,
                "group": 14, "left": 24, "title": 84, "t1": 15, "t2": 33, "chars": 24}
ONE_LINE_PNG = {"row": 36, "box_w": 170, "box_h": 30, "gap": 150, "head": 22,
                "group": 10, "left": 12, "title": 84, "t1": 13, "t2": 27, "chars": 19}

FLOOR_PAD = 12
FLOOR_TITLE = 70
FLOOR_LEGEND_MIN = 460
FLOOR_PNG_MAX_SIDE = 1600


# ------------------------------------------------------------ primitives

def _rect(x, y, w, h, fill=None, stroke=None, sw=1.0, op=1.0, rx=0):
    return {"t": "rect", "x": x, "y": y, "w": w, "h": h, "fill": fill,
            "stroke": stroke, "sw": sw, "op": op, "rx": rx}


def _circ(cx, cy, r, fill=None, stroke=None, sw=1.0):
    return {"t": "circle", "cx": cx, "cy": cy, "r": r, "fill": fill,
            "stroke": stroke, "sw": sw}


def _poly(pts, fill=None, stroke=None, sw=1.0, op=1.0, dash=None, closed=True):
    return {"t": "poly", "pts": pts, "fill": fill, "stroke": stroke, "sw": sw,
            "op": op, "dash": dash, "closed": closed}


def _line(pts, stroke, sw=1.0, dash=None, meta=None, title=None):
    return {"t": "line", "pts": pts, "stroke": stroke, "sw": sw, "dash": dash,
            "meta": meta, "title": title}


def _txt(x, y, s, size=11, fill=CHARCOAL, anchor="start", bold=False):
    return {"t": "text", "x": x, "y": y, "s": str(s), "size": size, "fill": fill,
            "anchor": anchor, "bold": bold}


def _image(x, y, w, h, mime, b64):
    return {"t": "image", "x": x, "y": y, "w": w, "h": h, "mime": mime, "b64": b64}


def _begin(kind, eid):
    return {"t": "begin", "kind": kind, "id": eid}


def _end():
    return {"t": "end"}


# ------------------------------------------------------------ small helpers

def _n(v, nd=1):
    return ("%." + str(nd) + "f") % float(v)


def _len_text(m):
    return "%s m / %.0f ft" % (_n(m), float(m) * FT_PER_M)


def _clip(s, n):
    s = str(s)
    return s if len(s) <= n else s[: max(1, n - 2)] + ".."


def _rgb(hexcolor):
    h = hexcolor.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _is_fiber(cfg, cid):
    info = (cfg.get("cables") or {}).get(cid) or {}
    return info.get("medium") == "fiber" or str(cid).startswith("fiber")


def _cable_color(cfg, cid):
    if _is_fiber(cfg, cid):
        return ORANGE
    return CABLE_COLORS.get(cid, CABLE_DEFAULT)


def _cable_label(cfg, cid):
    return ((cfg.get("cables") or {}).get(cid) or {}).get("label", cid)


def _nice_scale_m(ppm, k, max_px=180):
    best = 1
    for m in (1, 2, 5, 10, 20, 50, 100):
        if m * ppm * k <= max_px:
            best = m
    return best


def _symbol(t, x, y, r):
    """Primitives for one equipment symbol centred on (x, y)."""
    if t == "antenna":
        return [_circ(x, y, r, fill=WHITE_HEX, stroke=ORANGE, sw=max(1.5, r * 0.3)),
                _circ(x, y, max(1.0, r * 0.4), fill=ORANGE)]
    if t == "remote":
        return [_rect(x - r, y - r, 2 * r, 2 * r, fill=CYAN, stroke=NAVY, sw=1)]
    if t in ("source", "repeater"):
        fill = NAVY if t == "source" else BLUE
        s = 1.2 * r
        return [_rect(x - s, y - s, 2 * s, 2 * s, fill=fill, stroke=NAVY, sw=1)]
    if t == "splitter":
        return [_poly([(x - r, y - r), (x + r, y), (x - r, y + r)], fill=LIGHT,
                      stroke=NAVY, sw=1)]
    if t in ("tapper", "coupler"):
        fill = PAPER if t == "tapper" else WHITE_HEX
        stroke = CYAN if t == "tapper" else BLUE
        return [_poly([(x, y - r), (x + r, y), (x, y + r), (x - r, y)], fill=fill,
                      stroke=stroke, sw=2)]
    return [_circ(x, y, r, fill=GRAY)]


# ------------------------------------------------------------ one-line diagram

def _build_tree(model):
    by_id, fl_of, runs_from, has_parent = {}, {}, {}, set()
    for fl in model.get("floors", []):
        for el in fl.get("elements", []):
            t = el.get("type")
            if t == "cable_run":
                runs_from.setdefault(el.get("from_id"), []).append(el)
                has_parent.add(el.get("to_id"))
            elif t in NODE_TYPES and el.get("id") is not None:
                by_id[el["id"]] = el
                fl_of[el["id"]] = fl

    def sort_key(nid):
        el = by_id[nid]
        return (int(fl_of[nid].get("level", 0)), float(el.get("y", 0) or 0),
                float(el.get("x", 0) or 0), str(nid))

    for runs in runs_from.values():
        runs.sort(key=lambda r: sort_key(r["to_id"]) if r.get("to_id") in by_id
                  else (10 ** 9, 0.0, 0.0, ""))
    roots = sorted((nid for nid in by_id if nid not in has_parent), key=sort_key)
    return by_id, fl_of, runs_from, roots


def _layout(by_id, fl_of, runs_from, roots, m, multi):
    """Depth-first layout: one row per leaf, parents centred on their children.
    Subtrees are contiguous in row order, so boxes in one column never overlap.
    With several floors the leaves are grouped into one band per run of floor."""
    depth, children, edges, leaves, seen = {}, {}, [], [], set()

    def walk(nid, d):
        seen.add(nid)
        depth[nid] = d
        kids = []
        for run in runs_from.get(nid, []):
            cid = run.get("to_id")
            if cid not in by_id or cid in seen:
                continue
            walk(cid, d + 1)
            kids.append(cid)
            edges.append((run, nid, cid))
        children[nid] = kids
        if not kids:
            leaves.append(nid)

    for r in roots:
        if r not in seen:
            walk(r, 0)

    ypos, blocks = {}, []
    cursor = m["title"] + 6
    prev = object()
    for i, nid in enumerate(leaves):
        fid = fl_of[nid].get("id")
        if fid != prev:
            if i:
                cursor += m["group"]
            if multi:
                blocks.append({"fl": fl_of[nid], "top": cursor, "bottom": cursor,
                               "leaves": 0})
                cursor += m["head"]
            prev = fid
        ypos[nid] = cursor + m["row"] / 2.0
        if multi:
            blocks[-1]["bottom"] = cursor + m["row"]
            blocks[-1]["leaves"] += 1
        cursor += m["row"]

    def place(nid):
        kids = children.get(nid) or []
        if not kids:
            return ypos[nid]
        vals = [place(c) for c in kids]
        ypos[nid] = sum(vals) / float(len(vals))
        return ypos[nid]

    for r in roots:
        if r in depth:
            place(r)

    col = m["box_w"] + m["gap"]
    xpos = {nid: m["left"] + m["box_w"] / 2.0 + depth[nid] * col for nid in depth}
    maxd = max(depth.values()) if depth else 0
    return {"x": xpos, "y": ypos, "depth": depth, "edges": edges, "blocks": blocks,
            "width": m["left"] + maxd * col + m["box_w"] + m["left"],
            "height": cursor + m["left"]}


def _png_text_width(s, size):
    """Width in pixels of a text primitive as the PNG renderer draws it."""
    scale = 3 if size >= 15 else (2 if size >= 11 else 1)
    clean = "".join(c if c in FONT else " " for c in str(s).upper())
    return text_width(clean, scale)


def _widen_to_text(prims, width, left):
    """Widen a canvas so every left-aligned text line fits."""
    longest = max((p["x"] + _png_text_width(p["s"], p["size"]) for p in prims
                   if p["t"] == "text" and p["anchor"] == "start"), default=0)
    return max(width, longest + left)


def _antenna_power(solve_result, mode):
    """{element_id: (dbm, band label)} from the cascade solve, worst or first band."""
    out = {}
    if not solve_result or solve_result.get("errors"):
        return out
    for bid, band in (solve_result.get("bands") or {}).items():
        label = band.get("label") or bid
        for row in band.get("antennas") or []:
            p = row.get("port_dbm")
            eid = row.get("element_id")
            if p is None or eid is None:
                continue
            if eid not in out or (mode != "first" and p < out[eid][0]):
                out[eid] = (float(p), label)
    return out


def _node_text(el, cfg, power):
    """(title, detail, flagged) for one node box."""
    t = el.get("type")
    name = el.get("label") or el.get("id") or "?"
    parts = cfg.get("parts") or {}
    flag = False
    if t == "antenna":
        title = ("%s %s" % (name, el.get("antenna_id") or "")).strip()
        detail = ""
        if el.get("id") in power:
            p = power[el["id"]][0]
            detail = "port %s dBm" % _n(p)
            flag = p < 0
    elif t == "source":
        title = name
        sub = str(el.get("subtype") or "source").replace("_", " ")
        if sub == "direct" and el.get("injection_dbm") is not None:
            detail = "direct %s dBm" % _n(el["injection_dbm"])
        else:
            detail = sub
    elif t == "repeater":
        title = name
        detail = ("max out %s dBm" % _n(el["max_dl_out_dbm"])
                  if el.get("max_dl_out_dbm") is not None else "repeater")
    elif t == "remote":
        title = name
        detail = ("max out %s dBm" % _n(el["max_dl_out_dbm"])
                  if el.get("max_dl_out_dbm") is not None else "remote unit")
    elif t == "splitter":
        title = name
        ways = int(el.get("ways") or 2)
        il = ((parts.get("splitters") or {}).get("%dway" % ways) or {}).get("il_db")
        detail = "%d-way, %s dB IL" % (ways, _n(il)) if il is not None else "%d-way" % ways
    elif t == "tapper":
        title = name
        part = (parts.get("tappers") or {}).get(el.get("part_id") or "default") or {}
        detail = "tap %s dB, thru %s dB" % (_n(part.get("tap_loss_db", 0)),
                                            _n(part.get("through_il_db", 0)))
    else:  # coupler
        title = name
        part = (parts.get("couplers") or {}).get(el.get("part_id") or "default") or {}
        detail = "coupled %s dB, thru %s dB" % (_n(part.get("coupled_loss_db", 0)),
                                                _n(part.get("through_il_db", 0)))
    return title, detail, flag


def _one_line_scene(model, config, solve_result, m, project_name, date, power_mode,
                    chars):
    cfg = config or {}
    by_id, fl_of, runs_from, roots = _build_tree(model)
    if not by_id:
        raise DrawingError("model has no equipment elements to draw")
    floors = model.get("floors", [])
    multi = len(floors) > 1
    lay = _layout(by_id, fl_of, runs_from, roots, m, multi)
    power = _antenna_power(solve_result, power_mode)
    width = max(lay["width"], 760)
    height = lay["height"]
    prims = []

    # title block
    counts = {t: 0 for t in NODE_TYPES}
    for el in by_id.values():
        counts[el["type"]] += 1
    coax_m = fiber_m = 0.0
    coax_n = fiber_n = 0
    for run in (r for runs in runs_from.values() for r in runs):
        length = float(run.get("length_m") or 0.0)
        if _is_fiber(cfg, run.get("cable_id")):
            fiber_m += length
            fiber_n += 1
        else:
            coax_m += length
            coax_n += 1
    band_ids = []
    if solve_result and not solve_result.get("errors"):
        band_ids = [(b.get("label") or k) for k, b in (solve_result.get("bands") or {}).items()]
    head = "ROMtool one-line diagram" + (": %s" % project_name if project_name else "")
    prims.append(_txt(m["left"], 18, head, size=14, bold=True))
    prims.append(_txt(m["left"], 36, ("Date: %s" % date) if date else
                      "Floors: %d" % len(floors), size=11))
    prims.append(_txt(m["left"], 54,
                      "Antennas %d, remotes %d, splitters %d, tappers %d, couplers %d, "
                      "repeaters %d, sources %d. Cable runs %d (coax %s m, fiber %s m)."
                      % (counts["antenna"], counts["remote"], counts["splitter"],
                         counts["tapper"], counts["coupler"], counts["repeater"],
                         counts["source"], coax_n + fiber_n, _n(coax_m, 0), _n(fiber_m, 0)),
                      size=11))
    if solve_result and not solve_result.get("errors"):
        mode = "first band" if power_mode == "first" else "worst band"
        note = ("Port power in dBm, %s (%s). Red: below 0 dBm. Coax solid, fiber dashed."
                % (mode, ", ".join(band_ids) or "no bands"))
    else:
        note = "No cascade solve: port power not shown. Coax solid, fiber dashed."
    prims.append(_txt(m["left"], 72, note, size=11))

    width = _widen_to_text(prims, width, m["left"])

    # floor bands
    for i, b in enumerate(lay["blocks"]):
        top, bottom = b["top"], b["bottom"]
        prims.append(_rect(0, top, width, bottom - top,
                           fill=PAPER if i % 2 == 0 else WHITE_HEX))
        prims.append(_txt(m["left"], top + m["head"] - 8,
                          "%s (level %s), %d antenna row(s)" % (
                              b["fl"].get("name") or b["fl"].get("id"),
                              b["fl"].get("level", 0), b["leaves"]),
                          size=12, bold=True, fill=NAVY))

    # edges
    bw, bh = m["box_w"], m["box_h"]
    for run, pid, cid in lay["edges"]:
        if pid not in lay["x"] or cid not in lay["x"]:
            continue
        x1 = lay["x"][pid] + bw / 2.0
        y1 = lay["y"][pid]
        x2 = lay["x"][cid] - bw / 2.0
        y2 = lay["y"][cid]
        xm = x1 + 0.2 * (x2 - x1)
        cab = run.get("cable_id")
        fiber = _is_fiber(cfg, cab)
        label = "%s %s" % (cab, _len_text(float(run.get("length_m") or 0.0)))
        if run.get("port"):
            label = "%s: %s" % (run["port"], label)
        prims.append(_line([(x1, y1), (xm, y1), (xm, y2), (x2, y2)],
                           stroke=_cable_color(cfg, cab), sw=1.8,
                           dash=(6, 4) if fiber else None,
                           meta={"kind": "cable_run", "id": run.get("id")},
                           title=label))
        prims.append(_txt((xm + x2) / 2.0, y2 - 6, label, size=10, anchor="middle",
                          fill=CHARCOAL))

    # nodes
    for nid, x in lay["x"].items():
        el = by_id[nid]
        t = el["type"]
        y = lay["y"][nid]
        title, detail, flag = _node_text(el, cfg, power)
        style = NODE_STYLE[t]
        stroke = RED if flag else style["stroke"]
        left = x - bw / 2.0
        top = y - bh / 2.0
        prims.append(_begin(t, el.get("id")))
        prims.append(_rect(left, top, bw, bh, fill=style["fill"], stroke=stroke,
                           sw=2 if (flag or t in ("antenna", "tapper", "coupler")) else 1,
                           rx=4))
        prims.append(_txt(left + 8, top + m["t1"], _clip(title, chars), size=12,
                          fill=style["text"] if t != "antenna" else NAVY, bold=True))
        prims.append(_txt(left + 8, top + m["t2"], _clip(detail, chars), size=11,
                          fill=RED if flag else (style["text"] if t != "antenna" else CHARCOAL)))
        prims.append(_end())

    return {"w": int(math.ceil(width)), "h": int(math.ceil(height)), "prims": prims}


def one_line_svg(model, config, solve_result=None, project_name=None, date=None,
                 power_mode="worst"):
    """One-line diagram as an SVG document (str)."""
    scene = _one_line_scene(model, config, solve_result, ONE_LINE_SVG, project_name,
                            date, power_mode, ONE_LINE_SVG["chars"])
    return _svg_render(scene)


def one_line_png(model, config, solve_result=None, project_name=None, date=None,
                 power_mode="worst"):
    """One-line diagram as PNG bytes (same content, PNG metrics)."""
    scene = _one_line_scene(model, config, solve_result, ONE_LINE_PNG, project_name,
                            date, power_mode, ONE_LINE_PNG["chars"])
    return _render_png(scene)


# ------------------------------------------------------------ floor layout

def _floor_by_id(model, floor_id):
    for fl in model.get("floors", []):
        if fl.get("id") == floor_id:
            return fl
    raise DrawingError("floor %s not found" % floor_id)


def _pick_k(w, h):
    """Pixel multiplier for the PNG: small plates are enlarged, big ones are not."""
    for k in (4, 3, 2, 1):
        if w * k <= FLOOR_PNG_MAX_SIDE and h * k <= FLOOR_PNG_MAX_SIDE:
            return k
    return 1


def _floor_scene(model, config, floor_id, k, include_image, project_name, png_note=""):
    cfg = config or {}
    fl = _floor_by_id(model, floor_id)
    w = int(fl.get("w_px") or 0)
    h = int(fl.get("h_px") or 0)
    if w <= 0 or h <= 0:
        raise DrawingError("floor %s has no w_px/h_px" % floor_id)
    ppm = float(fl.get("ppm") or 10.0)
    ox, oy = FLOOR_PAD, FLOOR_TITLE
    dw, dh = w * k, h * k
    canvas_w = max(dw, FLOOR_LEGEND_MIN) + 2 * FLOOR_PAD
    prims = []
    els = [e for e in fl.get("elements", []) if e.get("type") != "cable_run"]
    by_id = {e.get("id"): e for e in fl.get("elements", [])}
    n_ant = sum(1 for e in els if e.get("type") == "antenna")

    def P(x, y):
        return (ox + float(x) * k, oy + float(y) * k)

    prims.append(_txt(FLOOR_PAD, 18, "Floor layout: %s (level %s)"
                      % (fl.get("name") or floor_id, fl.get("level", 0)),
                      size=14, bold=True))
    lead = ("Project: %s. " % project_name) if project_name else ""
    prims.append(_txt(FLOOR_PAD, 36, "%sPlate %s m x %s m at %s px per m. %d antennas."
                      % (lead, _n(w / ppm), _n(h / ppm), _n(ppm), n_ant), size=11))
    prims.append(_txt(FLOOR_PAD, 54, "Coax solid, fiber dashed. Clutter squares shaded "
                      "by class.%s" % png_note, size=11))

    # background plan
    img = fl.get("image") or {}
    if include_image and img.get("data_b64"):
        prims.append(_image(ox, oy, dw, dh, img.get("mime", "image/png"), img["data_b64"]))

    # clutter map squares
    cm = fl.get("clutter_map") or {}
    classes_seen = []
    if cm.get("grid"):
        codes = cm.get("codes") or {}
        cell = float(cm.get("cell_px") or float(cm.get("cell_m", 3.0)) * ppm)
        for r, row in enumerate(cm["grid"]):
            for c, ch in enumerate(row):
                cid = codes.get(ch)
                if not cid:
                    continue
                color, alpha = CLUTTER_STYLE.get(cid, (GRAY, 0.2))
                if cid not in classes_seen:
                    classes_seen.append(cid)
                x0, y0 = P(c * cell, r * cell)
                prims.append(_rect(x0, y0, cell * k, cell * k, fill=color, op=alpha))

    # area zones: fill by class, outline, label with name, class and ceiling height
    zones = resolve_zones(fl.get("zones") or [], cfg)
    zone_classes = []
    for z in zones:
        color, alpha = ZONE_STYLE.get(z["class"], (GRAY, 0.2))
        if z["class"] not in zone_classes:
            zone_classes.append(z["class"])
        pts = z["points"]
        prims.append(_begin("zone", z.get("id")))
        prims.append(_poly([P(p[0], p[1]) for p in pts], fill=color, stroke=NAVY,
                           sw=max(1.0, float(k)), op=alpha, closed=True))
        lx, ly = P(sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        height = ("%s m" % _n(z["ceiling_height_m"])) if z.get("ceiling_height_m") is not None \
            else "height n/a"
        prims.append(_txt(lx, ly, "%s (%s, %s)" % (z.get("name") or z.get("id"),
                                                   z.get("class_label"), height),
                          size=10, fill=NAVY, anchor="middle"))
        prims.append(_end())

    prims.append(_rect(ox, oy, dw, dh, fill=None, stroke=GRAY, sw=1))

    for wall in fl.get("walls") or []:
        a = P(wall["x1"], wall["y1"])
        b = P(wall["x2"], wall["y2"])
        glass = "glass" in str(wall.get("material", ""))
        prims.append(_line([a, b], stroke=CYAN if glass else CHARCOAL, sw=2 * k))

    outline = fl.get("outline")
    has_outline = bool(outline and len(outline) >= 3)
    if has_outline:
        # building perimeter: bold and solid (traced from the plan or drawn by hand)
        prims.append(_poly([P(p[0], p[1]) for p in outline], fill=None, stroke=NAVY,
                           sw=max(3, 3 * k), closed=True))

    # cable runs (both ends on this floor), following stored route points
    cables_drawn = []
    for run in (e for e in fl.get("elements", []) if e.get("type") == "cable_run"):
        a_el = by_id.get(run.get("from_id"))
        b_el = by_id.get(run.get("to_id"))
        if a_el is None or b_el is None:
            continue
        route = run.get("route") or [[a_el.get("x", 0), a_el.get("y", 0)],
                                     [b_el.get("x", 0), b_el.get("y", 0)]]
        pts = [P(p[0], p[1]) for p in route]
        cab = run.get("cable_id")
        if cab not in cables_drawn:
            cables_drawn.append(cab)
        fiber = _is_fiber(cfg, cab)
        title = "%s %s %s" % (run.get("label") or run.get("id"), cab,
                              _len_text(float(run.get("length_m") or 0.0)))
        prims.append(_line(pts, stroke=_cable_color(cfg, cab), sw=2 * k,
                           dash=(6 * k, 4 * k) if fiber else None,
                           meta={"kind": "cable_run", "id": run.get("id")},
                           title=title))

    # equipment
    labels_seen = []
    for el in els:
        if el.get("x") is None or el.get("y") is None:
            continue
        t = el.get("type")
        if t not in NODE_STYLE:
            continue
        if t not in labels_seen:
            labels_seen.append(t)
        x, y = P(el["x"], el["y"])
        r = max(4.0, 6.0 * k)
        prims.append(_begin(t, el.get("id")))
        prims.extend(_symbol(t, x, y, r))
        prims.append(_txt(x + r + 3, y - r, el.get("label") or el.get("id"), size=10,
                          fill=NAVY))
        prims.append(_end())

    canvas_w = _widen_to_text(prims, canvas_w, FLOOR_PAD)

    # legend
    legend_y = oy + dh + 14
    items = []
    for t in TYPE_ORDER:
        if t in labels_seen:
            items.append(("sym", t, TYPE_LABELS[t]))
    for cid in cables_drawn:
        items.append(("cable", cid, _cable_label(cfg, cid)))
    if has_outline:
        items.append(("outline", "perimeter", "Building perimeter"))
    clut_cfg = (cfg.get("clutter_map") or {}).get("classes") or {}
    for cid in CLUTTER_ORDER:
        if cid in classes_seen:
            items.append(("swatch", cid, (clut_cfg.get(cid) or {}).get("label", cid)))
    for cid in zone_classes:
        items.append(("zone", cid, "Zone: %s" % (clut_cfg.get(cid) or {}).get("label", cid)))
    cols = max(1, (canvas_w - 2 * FLOOR_PAD) // 230)
    legend_rows = int(math.ceil(len(items) / float(cols))) if items else 0
    m_per = _nice_scale_m(ppm, k, max_px=max(60, canvas_w - 2 * FLOOR_PAD - 20))
    bar = m_per * ppm * k
    prims.append(_rect(FLOOR_PAD, legend_y, bar, 4, fill=CHARCOAL))
    prims.append(_txt(FLOOR_PAD, legend_y + 16, "%d m / %.0f ft scale bar" % (
        m_per, m_per * FT_PER_M), size=10))
    base = legend_y + 40
    for i, (kind, key, label) in enumerate(items):
        x = FLOOR_PAD + (i % cols) * 230
        y = base + (i // cols) * 18
        if kind == "sym":
            prims.extend(_symbol(key, x + 7, y - 4, 5))
        elif kind == "cable":
            prims.append(_line([(x, y - 4), (x + 18, y - 4)], stroke=_cable_color(cfg, key),
                               sw=2, dash=(6, 4) if _is_fiber(cfg, key) else None))
        elif kind == "outline":
            prims.append(_line([(x, y - 4), (x + 18, y - 4)], stroke=NAVY, sw=3))
        elif kind == "zone":
            color, alpha = ZONE_STYLE.get(key, (GRAY, 0.2))
            prims.append(_rect(x, y - 10, 14, 10, fill=color, stroke=NAVY, sw=1, op=alpha))
        else:
            color, alpha = CLUTTER_STYLE.get(key, (GRAY, 0.2))
            prims.append(_rect(x, y - 10, 14, 10, fill=color, stroke=GRAY, sw=1, op=alpha))
        prims.append(_txt(x + 22, y, label, size=10))
    canvas_h = base + legend_rows * 18 + 14
    return {"w": int(canvas_w), "h": int(canvas_h), "prims": prims, "k": k}


def floor_layout_svg(model, config, floor_id, include_image=True, project_name=None):
    """Floor layout as an SVG document (str), drawn at the floor's pixel size."""
    scene = _floor_scene(model, config, floor_id, 1, include_image, project_name)
    return _svg_render(scene)


def floor_layout_png(model, config, floor_id, include_image=True, project_name=None):
    """Floor layout as PNG bytes. Small plates are enlarged by an integer factor
    (2 to 4) so the symbols and labels are legible; the factor is in scene["k"]."""
    fl = _floor_by_id(model, floor_id)
    k = _pick_k(int(fl.get("w_px") or 0), int(fl.get("h_px") or 0))
    img = fl.get("image") or {}
    note = ""
    if include_image and img.get("data_b64") and img.get("mime") != "image/png":
        note = " Background plan not drawn in PNG (JPEG)."
    scene = _floor_scene(model, config, floor_id, k, include_image, project_name, note)
    return _render_png(scene)


# ------------------------------------------------------------ SVG renderer

def _a(v):
    return escape(str(v), {'"': "&quot;"})


def _fill_attr(c):
    return "none" if c is None else c


def _dash_attr(dash):
    return "" if not dash else ' stroke-dasharray="%s %s"' % (dash[0], dash[1])


def _svg_prim(p):
    t = p["t"]
    if t == "rect":
        op = "" if p["op"] >= 1 else ' fill-opacity="%.3f"' % p["op"]
        rx = ' rx="%s"' % p["rx"] if p["rx"] else ""
        return ('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f"%s fill="%s" '
                'stroke="%s" stroke-width="%s"%s/>' % (
                    p["x"], p["y"], p["w"], p["h"], rx, _fill_attr(p["fill"]),
                    _fill_attr(p["stroke"]), p["sw"], op))
    if t == "circle":
        return ('<circle cx="%.2f" cy="%.2f" r="%.2f" fill="%s" stroke="%s" '
                'stroke-width="%s"/>' % (p["cx"], p["cy"], p["r"], _fill_attr(p["fill"]),
                                         _fill_attr(p["stroke"]), p["sw"]))
    if t == "poly":
        pts = " ".join("%.2f,%.2f" % (x, y) for x, y in p["pts"])
        tag = "polygon" if p["closed"] else "polyline"
        op = "" if p["op"] >= 1 else ' fill-opacity="%.3f"' % p["op"]
        return ('<%s points="%s" fill="%s" stroke="%s" stroke-width="%s"%s%s/>' % (
            tag, pts, _fill_attr(p["fill"]), _fill_attr(p["stroke"]), p["sw"],
            _dash_attr(p["dash"]), op))
    if t == "line":
        pts = " ".join("%.2f,%.2f" % (x, y) for x, y in p["pts"])
        meta = ""
        if p.get("meta"):
            meta = ' data-kind="%s" data-id="%s"' % (_a(p["meta"]["kind"]),
                                                     _a(p["meta"]["id"]))
        inner = "<title>%s</title>" % escape(p["title"]) if p.get("title") else ""
        body = '<polyline points="%s" fill="none" stroke="%s" stroke-width="%s"%s%s>%s</polyline>'
        return body % (pts, p["stroke"], p["sw"], _dash_attr(p["dash"]), meta, inner)
    if t == "text":
        weight = ' font-weight="bold"' if p["bold"] else ""
        return ('<text x="%.2f" y="%.2f" font-size="%s" text-anchor="%s" fill="%s"%s>%s</text>'
                % (p["x"], p["y"], p["size"], p["anchor"], p["fill"], weight,
                   escape(p["s"])))
    if t == "image":
        return ('<image x="%.2f" y="%.2f" width="%.2f" height="%.2f" preserveAspectRatio="none" '
                'href="data:%s;base64,%s"/>' % (p["x"], p["y"], p["w"], p["h"],
                                                _a(p["mime"]), p["b64"]))
    if t == "begin":
        return '<g data-kind="%s" data-id="%s">' % (_a(p["kind"]), _a(p["id"]))
    if t == "end":
        return "</g>"
    raise DrawingError("unknown primitive %s" % t)


def _svg_render(scene):
    w, h = scene["w"], scene["h"]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'viewBox="0 0 %d %d" font-family="Roboto, Arial, Helvetica, sans-serif">'
           % (w, h, w, h),
           '<rect x="0" y="0" width="%d" height="%d" fill="#ffffff"/>' % (w, h)]
    out.extend(_svg_prim(p) for p in scene["prims"])
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------ PNG renderer

def _blend(canvas, x, y, rgb, a):
    W = len(canvas[0]) // 4
    if 0 <= x < W and 0 <= y < len(canvas):
        b = x * 4
        row = canvas[y]
        row[b] = int(row[b] * (1 - a) + rgb[0] * a)
        row[b + 1] = int(row[b + 1] * (1 - a) + rgb[1] * a)
        row[b + 2] = int(row[b + 2] * (1 - a) + rgb[2] * a)


def _fill_rect(canvas, x0, y0, x1, y1, rgb, a=1.0):
    W, H = len(canvas[0]) // 4, len(canvas)
    xa, xb = max(0, x0), min(W, x1)
    ya, yb = max(0, y0), min(H, y1)
    if xa >= xb or ya >= yb:
        return
    if a >= 1:
        seg = bytearray(bytes(rgb) + b"\xff") * (xb - xa)
        for y in range(ya, yb):
            canvas[y][xa * 4:xb * 4] = seg
        return
    for y in range(ya, yb):
        for x in range(xa, xb):
            _blend(canvas, x, y, rgb, a)


def _line_pixels(x0, y0, x1, y1):
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    while True:
        yield x, y
        if x == x1 and y == y1:
            return
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy


def _stroke(canvas, pts, rgb, sw, dash=None, closed=False):
    pts = list(pts)
    if closed and len(pts) > 1:
        pts.append(pts[0])
    t = max(1, int(round(sw)))
    half = t // 2
    period = (int(dash[0]) + int(dash[1])) if dash else 0
    n = 0
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        for x, y in _line_pixels(int(round(ax)), int(round(ay)),
                                 int(round(bx)), int(round(by))):
            n += 1
            if dash and (n % period) >= int(dash[0]):
                continue
            for ox in range(t):
                for oy in range(t):
                    put_px(canvas, x + ox - half, y + oy - half, rgb)


def _fill_poly(canvas, pts, rgb, a=1.0):
    if len(pts) < 3:
        return
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    W, H = len(canvas[0]) // 4, len(canvas)
    x0, x1 = max(0, int(min(xs))), min(W - 1, int(max(xs)) + 1)
    y0, y1 = max(0, int(min(ys))), min(H - 1, int(max(ys)) + 1)
    n = len(pts)
    for y in range(y0, y1 + 1):
        py = y + 0.5
        for x in range(x0, x1 + 1):
            px = x + 0.5
            inside = False
            for i in range(n):
                xa, ya = pts[i]
                xb, yb = pts[(i + 1) % n]
                if (ya > py) != (yb > py):
                    xi = xa + (py - ya) * (xb - xa) / (yb - ya)
                    if px < xi:
                        inside = not inside
            if inside:
                if a >= 1:
                    put_px(canvas, x, y, rgb)
                else:
                    _blend(canvas, x, y, rgb, a)


def _png_text(canvas, x, y, s, size, fill, anchor, bold):
    scale = 2 if size >= 11 else 1
    if size >= 15:
        scale = 3
    clean = "".join(c if c in FONT else " " for c in str(s).upper())
    width = text_width(clean, scale)
    left = x - width / 2.0 if anchor == "middle" else x
    top = y - 5 * scale  # baseline to glyph top (glyph is 5 px tall at scale 1)
    rgb = _rgb(fill) if fill else (40, 40, 40)
    cx = int(round(left))
    for ch in clean:
        glyph = FONT.get(ch, FONT[" "])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    for sx in range(scale):
                        for sy in range(scale):
                            put_px(canvas, cx + gx * scale + sx,
                                   int(round(top)) + gy * scale + sy, rgb)
        cx += (len(glyph[0]) + 1) * scale


def _png_image(canvas, p):
    if p["mime"] != "image/png":
        return
    try:
        iw, ih, gray = decode_png(base64.b64decode(p["b64"]))
    except (ClutterMapError, ValueError, OSError):
        return
    W, H = len(canvas[0]) // 4, len(canvas)
    dw, dh = int(round(p["w"])), int(round(p["h"]))
    ox, oy = int(round(p["x"])), int(round(p["y"]))
    if dw <= 0 or dh <= 0 or iw <= 0 or ih <= 0:
        return
    xmap = [min(iw - 1, int((i + 0.5) * iw / dw)) for i in range(dw)]
    for j in range(dh):
        y = oy + j
        if y < 0 or y >= H:
            continue
        base = min(ih - 1, int((j + 0.5) * ih / dh)) * iw
        row = bytes(gray[base + xi] for xi in xmap)
        xa, xb = max(0, ox), min(W, ox + dw)
        if xa >= xb:
            continue
        off = xa - ox
        seg = row[off:off + (xb - xa)]
        out = bytearray((xb - xa) * 4)
        out[0::4] = seg
        out[1::4] = seg
        out[2::4] = seg
        out[3::4] = b"\xff" * (xb - xa)
        canvas[y][xa * 4:xb * 4] = out


def _render_png(scene):
    W = max(1, int(scene["w"]))
    H = max(1, int(scene["h"]))
    canvas = make_canvas(W, H, WHITE)
    for p in scene["prims"]:
        t = p["t"]
        if t == "rect":
            x0 = int(round(p["x"]))
            y0 = int(round(p["y"]))
            x1 = int(round(p["x"] + p["w"]))
            y1 = int(round(p["y"] + p["h"]))
            if p["fill"]:
                _fill_rect(canvas, x0, y0, x1, y1, _rgb(p["fill"]), p["op"])
            if p["stroke"]:
                _stroke(canvas, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                        _rgb(p["stroke"]), p["sw"], closed=True)
        elif t == "circle":
            cx, cy, r = int(round(p["cx"])), int(round(p["cy"])), int(round(p["r"]))
            if p["stroke"]:
                draw_disc(canvas, cx, cy, r, _rgb(p["stroke"]))
                inner = r - int(round(p["sw"]))
                if p["fill"] and inner > 0:
                    draw_disc(canvas, cx, cy, inner, _rgb(p["fill"]))
            elif p["fill"]:
                draw_disc(canvas, cx, cy, r, _rgb(p["fill"]))
        elif t == "poly":
            pts = [(float(x), float(y)) for x, y in p["pts"]]
            if p["fill"]:
                _fill_poly(canvas, pts, _rgb(p["fill"]), p["op"])
            if p["stroke"]:
                dash = p["dash"]
                _stroke(canvas, pts, _rgb(p["stroke"]), p["sw"], dash=dash,
                        closed=p["closed"])
        elif t == "line":
            _stroke(canvas, p["pts"], _rgb(p["stroke"]), p["sw"], dash=p["dash"])
        elif t == "text":
            _png_text(canvas, p["x"], p["y"], p["s"], p["size"], p["fill"], p["anchor"],
                      p["bold"])
        elif t == "image":
            _png_image(canvas, p)
        # begin / end markers have no raster form
    return encode_png(canvas, W, H)
