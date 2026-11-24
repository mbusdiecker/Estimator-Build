"""Walls from floor plans: DXF line work and vector PDF imports.

ROM jobs arrive as floor plan PDFs or DXF files, not iBwave models. This
module turns the line work on a plan into a ROMtool floor: wall segments in
metres, a scale derived from a known floor area, and a floor dict that
autoplace accepts with loss_mode "physics" (free-space exponent plus measured
wall crossings per metre x the wall material's loss). The walls drawn on the
floor are counted explicitly per straight antenna-to-point ray by predict and
autoplace; the physics clutter map built here (crossings per metre per
square) backs loss_mode "physics" when walls are ever cleared.

Pipeline (see the CLI at the bottom):

1. `walls_from_dxf` / `walls_from_pdf` read the drawing and return raw wall
   segments in DRAWING UNITS plus the bounding box. DXF: LINE, LWPOLYLINE and
   POLYLINE entities, preferring wall-looking layers (WALL, A-WALL,
   PARTITION, case-insensitive) when any line work sits on them; otherwise
   every line. PDF: vector line work from PyMuPDF `page.get_drawings()`,
   with anything drawn inside a text block and sub-millimetre scribbles
   dropped; when the page has no usable vector line work (or a placed image
   covers most of it, so the plan is a picture) the page is rendered and
   traced by `walls_from_raster` instead, and the result reports which path
   was used. `walls_from_raster(image)` traces a raster floor-plan image
   directly: saturation cut for colour overlays, Otsu threshold, small-blob
   removal, Zhang-Suen thinning, probabilistic Hough, then the same merge
   rules as the vector path. OCR is deliberately out of scope.
2. the estimator's building box (`crop_to_box`, run by `scale_to_area`
   first when a box is given): the box drawn on the plan page keeps only
   the segments whose midpoint is inside it — the default way to keep the
   sheet frame, title block and legends out of the plan. `isolate_building`
   (the heuristic cleanup, `isolate=True`) alternatively finds the building
   on the sheet by dropping the frame, the title-block strip and small
   margin items; off by default since the 2026-11-18 real-plan QA (it kept
   the title block instead of the building on 5 of 16 sheets).
3. `scale_to_area` scales drawing units to metres so the building outline
   (the perimeter traced by perimeter.py when it succeeds, else the kept
   line work's bounding box) covers the given floor area, then cleans the
   segments: near-duplicates collapse, collinear overlapping pieces merge,
   wall faces drawn as two parallel lines less than `double_line_m` (0.4 m)
   apart collapse to one midline wall, and pieces shorter than `min_wall_m`
   (0.3 m) after scaling drop (text, hatching, dimension ticks).
4. `floor_from_plan` builds the floor dict: walls in floor pixels with a
   material, a measured clutter map, loss_mode "physics" and, when the
   perimeter trace succeeded, the traced outline. The `plan` block reports
   the cleanup (kept share, dropped counts by reason), the cleanup flags and
   the outline-restricted density.
5. `wall_density` reports wall metres per square metre and the mean wall
   crossings per metre along straight rays across the floor.

Deterministic: no randomness (the ray fan of wall_density is fixed).
"""

import argparse
import json
import math
import sys

from .clutter_map import build_clutter_map
from .config import load_config
from .geometry import seg_cross
from .perimeter import (PerimeterError, _dilate, _label, polygon_area,
                        trace_outline)

SQFT_TO_M2 = 0.09290304

WALL_LAYER_HINTS = ("wall", "partition")
DEFAULT_MIN_WALL_M = 0.3       # dropped after scaling: text, hatch, tick marks
DEFAULT_DOUBLE_LINE_M = 0.4    # two parallel faces closer than this = one wall
DEFAULT_PPM = 2.0
DEFAULT_MATERIAL = "drywall"
PERIMETER_RASTER_PX = 600.0    # longest raster side used for the trace attempt
ANG_STEP_DEG = 0.5             # collinear/double-line grouping angle quantum
OFF_STEP_M = 0.01              # collinear grouping offset quantum (metres)


class PlanWallsError(Exception):
    """Bad plan file, no usable line work, or an unusable area."""


# ------------------------------------------------------------------ helpers

def _segments_of(segments):
    """Accept a bare [[x1, y1, x2, y2], ...] list or a result dict that
    carries one under "segments". Returns a list of 4-float lists."""
    if isinstance(segments, dict):
        raw = segments.get("segments")
    else:
        raw = segments
    out = []
    for s in raw or []:
        try:
            out.append([float(s[0]), float(s[1]), float(s[2]), float(s[3])])
        except (TypeError, ValueError, IndexError):
            raise PlanWallsError("each wall segment needs [x1, y1, x2, y2]")
    return out


def _ordered(s):
    """Endpoint order made canonical, so the same physical line always gets
    the same direction (smaller endpoint first, lexicographically)."""
    x1, y1, x2, y2 = s
    if (x1, y1) > (x2, y2):
        return [x2, y2, x1, y1]
    return [x1, y1, x2, y2]


def _len(s):
    return math.hypot(s[2] - s[0], s[3] - s[1])


def bbox_of(segments):
    """(minx, miny, maxx, maxy) of the line work; None when empty."""
    if not segments:
        return None
    xs = [s[0] for s in segments] + [s[2] for s in segments]
    ys = [s[1] for s in segments] + [s[3] for s in segments]
    return (min(xs), min(ys), max(xs), max(ys))


def _line_of(s):
    """(ux, uy, ang_deg, off) of an ordered segment: unit direction (positive
    x or, when vertical, positive y), angle in [0, 180) and the signed
    perpendicular distance of the line from the origin."""
    dx, dy = s[2] - s[0], s[3] - s[1]
    L = math.hypot(dx, dy)
    ux, uy = dx / L, dy / L
    ang = math.degrees(math.atan2(uy, ux)) % 180.0
    off = s[0] * uy - s[1] * ux
    return ux, uy, ang, off


# ------------------------------------------------------------------ imports

def _polyline_segments(out, layer, pts, closed):
    """Emit the straight segments of a polyline vertex list into `out`."""
    if len(pts) < 2:
        return
    pairs = list(zip(pts, pts[1:]))
    if closed and len(pts) > 2:
        pairs.append((pts[-1], pts[0]))
    for (x1, y1), (x2, y2) in pairs:
        out.append((layer, [x1, y1, x2, y2]))


def walls_from_dxf(path, layer_filter=None):
    """Wall line work from a DXF file (DWG must be delivered as DXF: ezdxf
    does not read DWG).

    Reads LINE, LWPOLYLINE and POLYLINE entities (2-D only; the z of every
    point is ignored). `layer_filter` (optional list of layer names)
    restricts the import to those layers, case-insensitive. Without it,
    layers whose names contain "wall" or "partition" (case-insensitive, so
    WALL, A-WALL, PARTITION_CW and friends) are preferred when any line work
    sits on them; otherwise every line is taken. Returns {"segments"
    (drawing units), "bbox", "layers", "wall_layers", "layer_filtered",
    "n_raw", "reason", "source"}.
    """
    try:
        import ezdxf
    except ImportError:
        raise PlanWallsError("ezdxf is required to read DXF plans "
                             "(uv run --with ezdxf ...)")
    try:
        doc = ezdxf.readfile(str(path))
    except Exception as exc:                      # ezdxf.DXFStructureError et al.
        raise PlanWallsError("could not read the DXF file: %s" % exc)
    msp = doc.modelspace()
    raw = []
    for e in msp:
        try:
            dxf_type = e.dxf.dxftype
        except Exception:
            continue
        if dxf_type == "LINE":
            raw.append((str(e.dxf.layer), [e.dxf.start.x, e.dxf.start.y,
                                           e.dxf.end.x, e.dxf.end.y]))
        elif dxf_type == "LWPOLYLINE":
            _polyline_segments(raw, e.dxf.layer,
                               [(p[0], p[1]) for p in e.get_points("xy")],
                               bool(e.closed))
        elif dxf_type == "POLYLINE" and e.is_poly_face_mesh:
            # Polyface mesh (iBwave exports walls as extruded 3-D faces). Its vertex list also holds face records
            # located at (0,0); read each face's real vertices instead and keep the plan-view edges. A vertical
            # wall face projects to one line; duplicate edges are merged later.
            for face in e.faces():
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in face
                       if not getattr(v, "is_face_record", False)]
                for k in range(len(pts)):
                    a, b = pts[k], pts[(k + 1) % len(pts)]
                    if math.hypot(b[0] - a[0], b[1] - a[1]) > 1e-9:
                        raw.append((str(e.dxf.layer), [a[0], a[1], b[0], b[1]]))
        elif dxf_type == "POLYLINE" and not e.is_polygon_mesh:
            _polyline_segments(raw, e.dxf.layer,
                               [(v.dxf.location.x, v.dxf.location.y)
                                for v in e.vertices],
                               bool(e.is_closed))
    raw = [(lay, _ordered([float(v) for v in s])) for lay, s in raw
           if _len(s) > 1e-9]
    layers = sorted({lay for lay, _s in raw})
    if layer_filter:
        wanted = {str(x).lower() for x in layer_filter}
        used = [lay for lay in layers if lay.lower() in wanted]
        if not used:
            raise PlanWallsError("none of the DXF layers matches layer_filter "
                                 "%s (the file has: %s)" % (sorted(wanted), layers))
        wall_layers, filtered = used, True
    else:
        wall_layers = [lay for lay in layers
                       if any(h in lay.lower() for h in WALL_LAYER_HINTS)]
        used = wall_layers or layers
        filtered = bool(wall_layers)
    segs = [s for lay, s in raw if lay in used]
    return {
        "segments": segs,
        "bbox": bbox_of(segs),
        "layers": layers,
        "wall_layers": wall_layers,
        "layer_filtered": filtered,
        "n_raw": len(segs),
        "lines_dropped_by_layer": len(raw) - len(segs),
        "reason": (None if segs else
                   "no LINE/LWPOLYLINE/POLYLINE line work in the DXF modelspace"),
        "source": "dxf",
    }


def walls_from_pdf(path, page=1, min_len_pt=2.0, raster=None, raster_dpi=180,
                   image_frac=0.25, raster_opts=None):
    """Wall line work from one page of a PDF plan, in PDF points (1/72 in).

    Two paths, reported in the result as `path_used`:

    - "vector": straight strokes and the four sides of drawn rectangles from
      PyMuPDF `page.get_drawings()`. Drops: curves, strokes shorter than
      `min_len_pt` on the page, and anything whose midpoint lies inside a
      text block (dimension text, glyph strokes). Used when the result has
      line work AND no placed image covers at least `image_frac` (0.25) of
      the page — a full-page picture under a few stray vectors means the
      plan itself is a raster, not vector line work.
    - "raster": `raster="force"`, or the fallback when the vector path finds
      nothing usable or a large image sits on the page. The page renders
      gray at `raster_dpi` (default 180; keeps hairlines wider than 2 px)
      and `walls_from_raster` traces it; pixel segments convert back to
      points, so scaling works exactly as on the vector path.

    `raster`: None = auto (vector first, fallback as above), True = always
    raster, False = never (vector only). `raster_opts` (dict) passes
    through to `walls_from_raster` (min_len_px, sat_cut, ...). The physical
    0.3 m filter is applied after scaling, in `scale_to_area`, when the
    real scale is known. Returns {"segments", "bbox", "page", "pages",
    "width_pt", "height_pt", "drawings", "path_used", "image_frac",
    "raster" (raster result + dpi/zoom, when used), "n_raw", "reason",
    "warnings", "source"}.
    """
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf                # the legacy name
        except ImportError:
            raise PlanWallsError("pymupdf is required to read PDF plans "
                                 "(uv run --with pymupdf ...)")
    doc = pymupdf.open(str(path))
    idx = int(page) - 1
    if idx < 0 or idx >= doc.page_count:
        raise PlanWallsError("page %d is out of range (the PDF has %d page(s))"
                             % (page, doc.page_count))
    pg = doc[idx]
    text_boxes = []
    for b in pg.get_text("blocks"):
        # block tuple (x0, y0, x1, y1, text, block_no, type); type 0 = text
        if len(b) > 6 and b[6] == 0:
            text_boxes.append((b[0], b[1], b[2], b[3]))

    def inside_text(s):
        mx, my = 0.5 * (s[0] + s[2]), 0.5 * (s[1] + s[3])
        return any(x0 <= mx <= x1 and y0 <= my <= y1
                   for x0, y0, x1, y1 in text_boxes)

    segs, curves = [], 0
    paths = pg.get_drawings()
    for d in paths:
        for item in d.get("items", []):
            op = item[0]
            if op == "l":                                 # straight stroke
                p, q = item[1], item[2]
                segs.append([p.x, p.y, q.x, q.y])
            elif op == "re":                              # rectangle path
                r = item[1]
                segs.extend([[r.x0, r.y0, r.x1, r.y0],
                             [r.x1, r.y0, r.x1, r.y1],
                             [r.x1, r.y1, r.x0, r.y1],
                             [r.x0, r.y1, r.x0, r.y0]])
            else:
                curves += 1
    kept, n_text, n_short = [], 0, 0
    for s in segs:
        s = _ordered(s)
        if _len(s) <= 1e-6:
            continue
        if inside_text(s):
            n_text += 1
        elif _len(s) < float(min_len_pt):
            n_short += 1
        else:
            kept.append(s)

    # largest fraction of the page covered by one placed image: a full-page
    # picture means the plan is a raster draw, not vector line work
    image_frac_out = 0.0
    try:
        page_area = max(pg.rect.get_area(), 1e-9)
        for info in (pg.get_image_info() or []):
            bb = info.get("bbox") or (0, 0, 0, 0)
            fw = max(0.0, min(bb[2], pg.rect.x1) - max(bb[0], pg.rect.x0))
            fh = max(0.0, min(bb[3], pg.rect.y1) - max(bb[1], pg.rect.y0))
            image_frac_out = max(image_frac_out, (fw * fh) / page_area)
    except Exception:
        pass
    want_raster = (raster is True) or (
        raster is None and (not kept or image_frac_out >= float(image_frac)))

    if not want_raster:
        return {
            "segments": kept,
            "bbox": bbox_of(kept),
            "page": idx + 1, "pages": doc.page_count,
            "width_pt": pg.rect.width, "height_pt": pg.rect.height,
            "drawings": len(paths), "curves_dropped": curves,
            "text_block_dropped": n_text, "short_dropped": n_short,
            "path_used": "vector", "image_frac": round(image_frac_out, 4),
            "n_raw": len(kept),
            "reason": (None if kept else
                       "no vector line work on page %d and the raster path "
                       "is off (raster=False): the page is raster-only (a "
                       "scan) or its drawing is not paths; OCR is not "
                       "attempted" % (idx + 1)),
            "warnings": [],
            "source": "pdf",
        }

    # raster fallback: render the page gray and trace the image
    try:
        import numpy as np
    except ImportError:
        raise PlanWallsError("numpy is required for the raster PDF path "
                             "(uv run --with numpy --with opencv-python-headless ...)")
    zoom = float(raster_dpi) / 72.0
    # colour render: walls_from_raster drops saturated pixels (carrier
    # overlays) itself; a gray render would turn those overlays into ink
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                        colorspace=pymupdf.csRGB, alpha=False)
    rimg = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, 3).copy()
    rr = walls_from_raster(rimg, **(raster_opts or {}))
    zoom_eff = zoom * float(rr.get("image_downscale") or 1.0)
    rsegs = [[c / zoom_eff for c in s] for s in rr["segments"]]
    rinfo = {k: v for k, v in rr.items() if k != "segments"}
    rinfo["dpi"] = float(raster_dpi)
    rinfo["zoom"] = zoom
    return {
        "segments": rsegs,
        "bbox": bbox_of(rsegs),
        "page": idx + 1, "pages": doc.page_count,
        "width_pt": pg.rect.width, "height_pt": pg.rect.height,
        "drawings": len(paths), "curves_dropped": curves,
        "text_block_dropped": n_text, "short_dropped": n_short,
        "path_used": "raster", "image_frac": round(image_frac_out, 4),
        "n_raw": len(rsegs),
        "reason": rr.get("reason"),
        "warnings": list(rr.get("warnings") or []),
        "raster": rinfo,
        "source": "pdf",
    }


# ------------------------------------------------------------------ raster

def _thin_zhang_suen(mask, max_iter=200):
    """Zhang-Suen skeletonisation: a 0/1 mask thinned to 1-px strokes.
    numpy only; each pass is vectorized, so a whole-page render stays fast.
    Subpass 1 deletes a pixel when north/east/south or east/south/west
    triples are empty, subpass 2 the mirrored condition; repeat until no
    pixel changes."""
    import numpy as np
    img = (np.asarray(mask) > 0).astype(np.uint8)
    h, w = img.shape
    if h < 3 or w < 3 or not img.any():
        return img
    I = np.pad(img, 1)
    for _ in range(max_iter):
        changed = False
        for phase in (0, 1):
            c = I[1:-1, 1:-1]
            n, s = I[:-2, 1:-1], I[2:, 1:-1]
            e, wst = I[1:-1, 2:], I[1:-1, :-2]
            ne, nw = I[:-2, 2:], I[:-2, :-2]
            se, sw = I[2:, 2:], I[2:, :-2]
            B = n + ne + e + se + s + sw + wst + nw
            A = np.zeros(B.shape, np.uint8)
            for a, b in ((n, ne), (ne, e), (e, se), (se, s),
                         (s, sw), (sw, wst), (wst, nw), (nw, n)):
                A += ((a == 0) & (b == 1)).astype(np.uint8)
            if phase == 0:
                cond = ((B >= 2) & (B <= 6) & (A == 1)
                        & ((n & e & s) == 0) & ((e & s & wst) == 0))
            else:
                cond = ((B >= 2) & (B <= 6) & (A == 1)
                        & ((n & e & wst) == 0) & ((n & s & wst) == 0))
            cond &= c == 1
            if cond.any():
                c[cond] = 0
                changed = True
        if not changed:
            break
    return I[1:-1, 1:-1]


def _ink_bridged(ink, ux, uy, off, t0, t1):
    """True when the raster ink continues along the line (ux, uy, off) for
    t in [t0, t1] (a 1-px neighbourhood at ~1-px steps, 90 % occupancy)."""
    import numpy as np
    h, w = ink.shape
    n = max(2, int(math.ceil(t1 - t0)))
    hit = 0
    for k in range(n + 1):
        t = t0 + (t1 - t0) * k / n
        x = int(round(t * ux + off * uy))
        y = int(round(t * uy - off * ux))
        found = False
        for yy in (y - 1, y, y + 1):
            if found or not (0 <= yy < h):
                continue
            for xx in (x - 1, x, x + 1):
                if 0 <= xx < w and ink[yy, xx]:
                    found = True
                    break
        hit += found
    return hit >= 0.9 * (n + 1)


def _stitch_fragments(segs, ink):
    """Collinear Hough fragments merge back into one wall when the raster
    ink is continuous across the gap between them.

    Probabilistic Hough splits long clean lines into pieces whose gaps can
    be far larger than its own maxLineGap, so extent-touching alone (the
    vector-path rule) is not enough. Here pieces on the same line (within
    2 degrees and 1.5 px of offset) with a gap of up to the shorter piece's
    own length rejoin ONLY where ink spans the gap — a real opening (door,
    window) has no ink and stays split. Returns the stitched segments.
    """
    items = []
    for s in segs:
        ux, uy, ang, off = _line_of(s)
        items.append([ux, uy, ang, off,
                      s[0] * ux + s[1] * uy, s[2] * ux + s[3] * uy])
    merged_any = True
    while merged_any:
        merged_any = False
        for i in range(len(items)):
            a = items[i]
            if a is None:
                continue
            for j in range(i + 1, len(items)):
                b = items[j]
                if b is None:
                    continue
                dang = min(abs(a[2] - b[2]), 180.0 - abs(a[2] - b[2]))
                if dang > 2.0 or abs(a[3] - b[3]) > 1.5:
                    continue
                lo_t, lo_b = a, b
                if b[4] < a[4]:
                    lo_t, lo_b = b, a
                g0, g1 = max(a[4], b[4]), min(a[5], b[5])
                if g1 >= g0:                # overlapping or touching extents
                    continue                # (merge_segments unions those)
                shorter = min(a[5] - a[4], b[5] - b[4])
                if (g0 - g1) > max(2.0, shorter):
                    continue
                if not _ink_bridged(ink, a[0], a[1], a[3], g1, g0):
                    continue
                wa, wb = a[5] - a[4], b[5] - b[4]
                a[3] = (a[3] * wa + b[3] * wb) / (wa + wb)   # offset midline
                a[4], a[5] = min(a[4], b[4]), max(a[5], b[5])
                items[j] = None
                merged_any = True
    out = []
    for it in items:
        if it is None:
            continue
        ux, uy, off, t0, t1 = it[0], it[1], it[3], it[4], it[5]
        out.append(_ordered([t0 * ux + off * uy, t0 * uy - off * ux,
                             t1 * ux + off * uy, t1 * uy - off * ux]))
    return out


def walls_from_raster(image, min_len_px=None, min_blob_area_px=None,
                      sat_cut=70.0, ang_tol_deg=10.0, long_diag_px=None,
                      double_line_px=None, max_gap_px=None,
                      min_confidence=0.5, max_pixels=16_000_000):
    """Wall line work from a raster floor-plan image (a scan, or a carrier
    design package whose plan is a picture): segments in PIXEL units plus a
    confidence score.

    `image` is a numpy array, grayscale 2-D or colour 3-channel (channel
    order does not matter for the saturation test). Steps:

    1. Colour overlays off: pixels whose HSV saturation is above `sat_cut`
       (0-255; 70 keeps neutral black/grey line work, drops the saturated
       antennas, cable runs and heat-map fills carriers draw over a grey
       plan) are blanked before thresholding. No saturation test runs on a
       2-D grayscale image.
    2. Threshold: Otsu on the neutral image; dark pixels are ink. Images
       larger than `max_pixels` (16 MPx) are downscaled first, recorded as
       `image_downscale`.
    3. Blob removal: connected components with area under
       `min_blob_area_px` (default: (2 % of the long side)^2) AND long side
       under `min_len_px` are text-like blobs and drop — the size guard
       keeps a genuinely elongated piece (a wall) even when it is thin.
    4. Zhang-Suen thinning to 1-px strokes (see `_thin_zhang_suen`).
    5. Probabilistic Hough (cv2.HoughLinesP, seeded for determinism — the
       probabilistic pass shuffles candidates with OpenCV's global RNG)
       with `minLineLength` = `min_len_px` (default 4 % of the long side)
       and `maxLineGap` (default 5 % of that). Near-axis segments within
       `ang_tol_deg` of horizontal/vertical survive; other diagonals only
       when longer than `long_diag_px` (default 2.5 x min_len_px) — short
       diagonals are hatching, arrows and symbols.
    6. Stitch and merge: collinear Hough fragments on one line whose gap
       the raster ink bridges (`_stitch_fragments`; votes split long clean
       lines, and a real opening has no ink so it stays split) rejoin, then
       the same merge rules as the vector path (`merge_segments`) run —
       collinear fragments union, double-line faces closer than
       `double_line_px` (default 1 % of the long side, min 4 px) collapse
       to their midline — with coarser angle/offset buckets suited to
       pixels.

    `confidence` is the share of the neutral dark ink (after blob removal)
    covered by the kept segments drawn back at their measured stroke width
    (+-2 px for width jitter); low confidence means most ink is not straight
    wall line work (hatching, curves, dense text) and the result says so
    (`low_confidence`, a warning string) rather than returning walls
    silently. OCR is not attempted.
    Returns {"segments" (px), "source", "image_w", "image_h", "n_hough",
    "n_stitched", "diagonals_dropped", "blob_merges", "double_line_merges",
    "blobs_removed", "otsu_threshold", "stroke_px", "min_len_px", "sat_cut",
    "double_line_px", "max_gap_px", "confidence", "min_confidence",
    "low_confidence", "image_downscale", "warnings", "reason"}.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise PlanWallsError("opencv-python (cv2) and numpy are required for "
                             "raster plans (uv run --with opencv-python-headless "
                             "--with numpy ...)")
    img = np.asarray(image)
    if img.ndim == 3 and img.shape[2] == 4:
        img = img[:, :, :3]
    if img.ndim == 2:
        gray = img
        sat = None
    elif img.ndim == 3 and img.shape[2] == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        sat = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 1]
    else:
        raise PlanWallsError("image must be a 2-D grayscale or 3-channel "
                             "array, got shape %r" % (tuple(img.shape),))
    h, w = gray.shape[:2]

    def empty_result(reason, **over):
        res = {
            "segments": [], "source": "raster",
            "image_w": w, "image_h": h,
            "n_hough": 0, "diagonals_dropped": 0, "blob_merges": 0,
            "double_line_merges": 0,
            "blobs_removed": 0, "otsu_threshold": None, "stroke_px": None,
            "min_len_px": None, "sat_cut": float(sat_cut),
            "double_line_px": None, "max_gap_px": None,
            "confidence": 0.0, "min_confidence": float(min_confidence),
            "low_confidence": False, "image_downscale": 1.0,
            "warnings": [], "reason": reason,
        }
        res.update(over)
        return res

    # 1. colour overlays off
    if sat is not None:
        keep = sat <= float(sat_cut)
        if int(keep.sum()) < 0.01 * h * w:
            return empty_result(
                "nearly every pixel is saturated colour overlay at "
                "sat_cut=%g; no neutral line work to trace" % sat_cut)
        neutral = np.where(keep, gray, 255).astype(np.uint8)
    else:
        neutral = gray

    # 2. downscale guard + Otsu threshold
    f = 1.0
    if int(gray.shape[0]) * int(gray.shape[1]) > int(max_pixels):
        f = math.sqrt(float(max_pixels) / (gray.shape[0] * gray.shape[1]))
        neutral = cv2.resize(neutral, (0, 0), fx=f, fy=f,
                             interpolation=cv2.INTER_AREA)
        h, w = neutral.shape[:2]
    otsu_t, bw = cv2.threshold(neutral, 0, 255,
                               cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink = (bw > 0).astype(np.uint8)
    del bw, neutral

    # 3. blob removal (text, symbols): small area AND short — a thin but
    #    elongated piece is a wall, not a blob, whatever its area
    min_len_eff = (float(min_len_px) if min_len_px is not None
                   else max(10.0, 0.04 * max(w, h)))
    min_blob_eff = (float(min_blob_area_px) if min_blob_area_px is not None
                    else max(25.0, (0.02 * max(w, h)) ** 2))
    n_blob = 0
    if ink.any():
        n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
            ink, connectivity=8)
        drop = np.zeros(n_labels, dtype=bool)
        for lab in range(1, n_labels):
            area = int(stats[lab, cv2.CC_STAT_AREA])
            bw_ = int(stats[lab, cv2.CC_STAT_WIDTH])
            bh_ = int(stats[lab, cv2.CC_STAT_HEIGHT])
            if area < min_blob_eff and max(bw_, bh_) < min_len_eff:
                drop[lab] = True
        if drop.any():
            ink[drop[labels]] = 0
            n_blob = int(drop.sum())
    ink_total = int(ink.sum())
    if ink_total == 0:
        return empty_result(
            "no neutral dark line work found in the raster image at the "
            "Otsu threshold %g (%d blob-like component(s) removed); OCR is "
            "not attempted" % (otsu_t, n_blob),
            otsu_threshold=float(otsu_t), blobs_removed=n_blob,
            min_len_px=min_len_eff)
    del labels, stats

    # 4/5. skeleton, then multi-pass Hough: extracting a segment wipes the
    #      accumulator votes its line shared with collinear wall pieces
    #      (both x=400 wall pieces across a door gap sit on one infinite
    #      line), so one pass can lose a whole wall. After each pass the
    #      found segments are erased from the skeleton and Hough runs again
    #      until nothing new appears. Seeded (HoughLinesP shuffles
    #      candidates with OpenCV's global RNG).
    skel = _thin_zhang_suen(ink)
    gap_eff = (float(max_gap_px) if max_gap_px is not None
               else max(2.0, 0.05 * min_len_eff))
    thr = max(2, int(round(0.5 * min_len_eff)))
    sin_tol = math.sin(math.radians(ang_tol_deg))
    long_eff = (float(long_diag_px) if long_diag_px is not None
                else 2.5 * min_len_eff)
    segs, n_hough, n_diag, n_pass = [], 0, 0, 0
    remaining = skel.copy()
    for _pass in range(6):
        n_pass += 1
        cv2.setRNGSeed(_pass)
        lines = cv2.HoughLinesP(remaining, 1, math.pi / 180.0, thr,
                                minLineLength=int(round(min_len_eff)),
                                maxLineGap=int(round(gap_eff)))
        fresh = []
        if lines is not None:
            for l4 in np.asarray(lines).reshape(-1, 4):
                x1, y1, x2, y2 = [float(v) for v in l4]
                L = math.hypot(x2 - x1, y2 - y1)
                if L < 1e-6:
                    continue
                n_hough += 1
                if (min(abs(x2 - x1), abs(y2 - y1)) / L <= sin_tol
                        or L >= long_eff):
                    fresh.append(_ordered([x1, y1, x2, y2]))
                else:
                    n_diag += 1
        if not fresh:
            break
        segs.extend(fresh)
        eraser = np.zeros_like(remaining)
        for s in fresh:
            cv2.line(eraser, (int(round(s[0])), int(round(s[1]))),
                     (int(round(s[2])), int(round(s[3]))), 1, 3)
        remaining[eraser > 0] = 0
        if not remaining.any():
            break
    del skel
    if not segs:
        return empty_result(
            "no straight wall line work found in the raster image: %d px of "
            "dark ink, %d Hough segment(s) all shorter than "
            "min_len_px=%g or diagonal; OCR is not attempted"
            % (ink_total, n_hough, min_len_eff),
            otsu_threshold=float(otsu_t), blobs_removed=n_blob,
            n_hough=n_hough, diagonals_dropped=n_diag, min_len_px=min_len_eff)

    # 5b. stitch collinear fragments across ink-bridged gaps (Hough votes
    #     split long clean lines), then 6. merge like the vector path with
    #     pixel-suited buckets
    n_before_stitch = len(segs)
    segs = _stitch_fragments(segs, ink)
    n_stitched = n_before_stitch - len(segs)
    dbl_eff = (float(double_line_px) if double_line_px is not None
               else max(4.0, 0.01 * max(w, h)))
    mg = merge_segments(segs, double_line_m=dbl_eff, min_wall_m=0.0,
                        ang_step_deg=3.0, off_step=max(1.0, dbl_eff / 4.0))
    out = mg["segments"]

    # confidence: ink covered by the kept segments at their stroke width
    stroke = 1.0
    dt = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    dtv = dt[dt > 0]
    if dtv.size:
        stroke = 2.0 * float(np.median(dtv))
    del dt, dtv
    th = max(1, int(round(stroke)))
    canvas = np.zeros((h, w), np.uint8)
    for s in out:
        cv2.line(canvas, (int(round(s[0])), int(round(s[1]))),
                 (int(round(s[2])), int(round(s[3]))), 255, th)
    canvas = cv2.dilate(canvas, np.ones((5, 5), np.uint8))  # +-2 px width jitter
    conf = float(np.logical_and(canvas > 0, ink > 0).sum()) / ink_total

    res = {
        "segments": out, "source": "raster",
        "image_w": w, "image_h": h,
        "n_hough": n_hough, "diagonals_dropped": n_diag,
        "hough_passes": n_pass, "n_stitched": n_stitched,
        "blob_merges": mg["counts"]["collinear"],
        "double_line_merges": mg["counts"]["double_line"],
        "blobs_removed": n_blob, "otsu_threshold": float(otsu_t),
        "stroke_px": round(stroke, 2),
        "min_len_px": min_len_eff, "sat_cut": float(sat_cut),
        "double_line_px": dbl_eff, "max_gap_px": gap_eff,
        "confidence": round(conf, 4),
        "min_confidence": float(min_confidence),
        "low_confidence": conf < float(min_confidence),
        "image_downscale": round(f, 5),
        "warnings": [], "reason": None,
    }
    if res["low_confidence"]:
        res["warnings"].append(
            "raster confidence %.2f is below %.2f: only %.0f%% of the dark "
            "line work is explained by straight wall segments (hatching, "
            "curved or dense line work); check the walls against the plan"
            % (conf, float(min_confidence), 100.0 * round(conf, 2)))
    return res


# ------------------------------------------------------------------- scale
# ---------------------------------------------------------- segment merge

def merge_segments(segments, double_line_m=DEFAULT_DOUBLE_LINE_M,
                   min_wall_m=DEFAULT_MIN_WALL_M,
                   ang_step_deg=ANG_STEP_DEG, off_step=OFF_STEP_M):
    """Clean one plan's raw segments into one segment per wall, in the units
    given (call it on metre coordinates for physical tolerances, on pixels
    for a raster trace).

    In order: exact duplicates (same endpoints, within 1 mm) collapse;
    collinear pieces on one line whose extents touch or overlap merge into
    one piece; wall faces drawn as two parallel lines closer than
    `double_line_m` with overlapping extents collapse to one midline wall; at
    the end pieces shorter than `min_wall_m` drop. Walls separated by a door
    gap stay separate: only touching or overlapping extents merge. Grouping
    buckets angles to `ang_step_deg` and line offsets to `off_step` (both in
    the units given), so pieces quantized apart stay separate (conservative:
    an unmerged pair costs a double crossing, a wrongly merged pair loses
    one). Returns {"segments", "counts": {"raw", "duplicates", "collinear",
    "double_line", "short"}}.
    """
    segs = []
    for s in _segments_of(segments):
        s = _ordered(s)
        if _len(s) > 1e-9:
            segs.append(s)
    counts = {"raw": len(segs), "duplicates": 0, "collinear": 0,
              "double_line": 0, "short": 0}

    # 1. exact duplicates
    seen, uniq = {}, []
    for s in segs:
        key = (round(s[0], 3), round(s[1], 3), round(s[2], 3), round(s[3], 3))
        if key in seen:
            counts["duplicates"] += 1
        else:
            seen[key] = s
            uniq.append(s)

    # 2. collinear overlapping pieces: fine buckets of (angle, offset), then
    #    touching or overlapping extents merge along the line. Each line is
    #    [t0, t1, off, ux, uy]; t is the projection onto the unit direction,
    #    off the signed perpendicular offset.
    def point_at(ux, uy, off, t):
        return (t * ux + off * uy, t * uy - off * ux)

    groups = {}
    for s in uniq:
        ux, uy, ang, off = _line_of(s)
        qa = round(ang / ang_step_deg) * ang_step_deg
        if qa >= 180.0:
            qa -= 180.0
        qo = round(off / off_step)
        t0 = s[0] * ux + s[1] * uy
        t1 = s[2] * ux + s[3] * uy
        groups.setdefault((qa, qo), []).append([t0, t1, off, ux, uy])
    lines = []
    for _key in sorted(groups):
        cur = None
        for t0, t1, off, ux, uy in sorted(groups[_key]):
            if cur is not None and t0 <= cur[1] + 1e-9:
                # touching or overlapping extents on the same line
                cur[1] = max(cur[1], t1)
                counts["collinear"] += 1
            else:
                cur = [t0, t1, off, ux, uy]
                lines.append(cur)

    # 3. double-line faces: same angle quantum, offsets closer than
    #    double_line_m, extents overlapping at least half the shorter face;
    #    both faces collapse to one midline wall (the mean offset, extents
    #    unioned), weighted by extent length
    def overlaps(a, b):
        ov = min(a[1], b[1]) - max(a[0], b[0])
        return ov >= 0.5 * min(a[1] - a[0], b[1] - b[0])

    by_ang = {}
    for t0, t1, off, ux, uy in lines:
        qa = round((math.degrees(math.atan2(uy, ux)) % 180.0) / ang_step_deg)
        by_ang.setdefault(qa, []).append([t0, t1, off, ux, uy])
    midline = {}
    for qa in sorted(by_ang):
        bucket = midline.setdefault(qa, [])
        for t0, t1, off, ux, uy in sorted(by_ang[qa], key=lambda r: r[2]):
            if bucket and abs(off - bucket[-1][2]) < double_line_m \
                    and overlaps((bucket[-1][0], bucket[-1][1]), (t0, t1)):
                prev = bucket[-1]
                w_prev = prev[1] - prev[0]
                w_new = t1 - t0
                prev[2] = (prev[2] * w_prev + off * w_new) / (w_prev + w_new)
                prev[0], prev[1] = min(prev[0], t0), max(prev[1], t1)
                counts["double_line"] += 1
            else:
                bucket.append([t0, t1, off, ux, uy])

    out = []
    for qa in sorted(midline):
        for t0, t1, off, ux, uy in midline[qa]:
            x1, y1 = point_at(ux, uy, off, t0)
            x2, y2 = point_at(ux, uy, off, t1)
            s = _ordered([x1, y1, x2, y2])
            if _len(s) < min_wall_m:
                counts["short"] += 1
            else:
                out.append(s)
    counts["walls"] = len(out)
    return {"segments": out, "counts": counts}


def _raster_lines(segments, width, height, origin=(0.0, 0.0), scale=1.0):
    """Sample segments into a W x H gray byte grid (0 = dark) at `scale`
    raster pixels per drawing unit; origin is the drawing coordinate of the
    raster's (0, 0) corner."""
    gray = bytearray([255]) * (width * height)
    ox, oy = float(origin[0]), float(origin[1])
    for s in segments:
        x1, y1 = (s[0] - ox) * scale, (s[1] - oy) * scale
        x2, y2 = (s[2] - ox) * scale, (s[3] - oy) * scale
        n = max(2, int(math.ceil(math.hypot(x2 - x1, y2 - y1) * 2)))
        for i in range(n + 1):
            f = i / float(n)
            px = int(x1 + (x2 - x1) * f)
            py = int(y1 + (y2 - y1) * f)
            if 0 <= px < width and 0 <= py < height:
                gray[py * width + px] = 0
    return gray


# ------------------------------------------------- sheet building isolation
# Grades and references: all thresholds below are grade D — provisional,
# from the 2026-11-18 QA of 16 job plans; tune on more plans. Recorded in
# config/default.json under "plan_walls".

PLAN_WALLS_DEFAULTS = {
    # sheet frame: a cluster whose box sits this close to the raw bounding box
    # on this many sides, is hollow, and has other line work inside it
    "frame_edge_tol_frac": 0.015,
    "frame_min_sides": 3.0,
    "frame_hollow_band_frac": 0.35,
    "frame_hollow_frac": 0.15,
    # title block: a strip along one bbox edge found from line density profiles
    "tb_slabs": 40.0,
    "tb_blank_frac": 0.10,
    "tb_blank_lines": 12.0,
    "tb_gap_frac": 0.02,
    "tb_axis_share": 0.80,
    "tb_span_frac": 0.50,
    "tb_min_share": 0.03,
    "tb_max_share": 0.75,
    "tb_dense_frac": 0.20,
    "tb_max_depth_frac": 0.45,
    "tb_ang_tol_deg": 5.0,
    "tb_contact_frac": 0.005,
    # building cluster
    "cluster_raster_px": 600.0,
    "cluster_gap_frac": 0.02,
    "bridge_frac": 0.005,
    "sat_min_share": 0.15,
    "sat_gap_frac": 0.05,
    "sat_min_dim_frac": 0.15,
    # flags
    "flag_keep_share": 0.50,
    "flag_touch_tol_frac": 0.01,
    "flag_frame_sides": 3.0,
    "flag_outline_bbox_frac": 0.90,
    "flag_density_half": 0.35,
    # no-building guard: kept line work hugging the sheet bbox, small
    "hug_tol_frac": 0.02,
    "hug_min_sides": 2.0,
    "hug_max_area_frac": 0.35,
}


def plan_walls_params(config=None, **over):
    """Isolation thresholds from config `plan_walls` (grade D), with keyword
    overrides (raise on unknown names, like perimeter.perimeter_params)."""
    cfg = (config or {}).get("plan_walls") or {}
    p = dict(PLAN_WALLS_DEFAULTS)
    for k in p:
        if k in cfg:
            p[k] = cfg[k]
    for k, v in over.items():
        if v is not None:
            if k not in p:
                raise PlanWallsError("unknown sheet-isolation option '%s'" % k)
            p[k] = v
    try:
        for k in p:
            p[k] = float(p[k])
    except (TypeError, ValueError):
        raise PlanWallsError("sheet-isolation options must be numbers")
    for k in ("frame_min_sides", "flag_frame_sides", "hug_min_sides"):
        p[k] = int(p[k])
    if not 0 < p["frame_edge_tol_frac"] < 0.25:
        raise PlanWallsError("frame_edge_tol_frac must be in (0, 0.25)")
    if not 2 <= p["frame_min_sides"] <= 4:
        raise PlanWallsError("frame_min_sides must be 2..4")
    if not 0 < p["frame_hollow_band_frac"] < 0.5 \
            or not 0 < p["frame_hollow_frac"] < 1:
        raise PlanWallsError("frame_hollow_band_frac must be in (0, 0.5) and "
                             "frame_hollow_frac in (0, 1)")
    if p["tb_slabs"] < 4 or not 0 < p["tb_blank_frac"] <= 1 \
            or p["tb_blank_lines"] < 0:
        raise PlanWallsError("tb_slabs must be >= 4, tb_blank_frac in (0, 1] "
                             "and tb_blank_lines >= 0")
    if not 0 < p["tb_gap_frac"] < 0.5 or not 0.1 < p["tb_axis_share"] <= 1 \
            or not 0.1 < p["tb_span_frac"] <= 1:
        raise PlanWallsError("tb_gap_frac must be in (0, 0.5), tb_axis_share "
                             "and tb_span_frac in (0.1, 1]")
    if not 0 < p["tb_min_share"] <= p["tb_max_share"] <= 1 \
            or not 0 < p["tb_dense_frac"] <= 1:
        raise PlanWallsError("tb_min_share must be <= tb_max_share <= 1 and "
                             "tb_dense_frac in (0, 1]")
    if not 0 < p["tb_max_depth_frac"] <= 1 \
            or not 0 < p["tb_ang_tol_deg"] < 45 \
            or not 0 <= p["tb_contact_frac"] < 0.1:
        raise PlanWallsError("tb_max_depth_frac in (0, 1], tb_ang_tol_deg in "
                             "(0, 45), tb_contact_frac in [0, 0.1)")
    if p["cluster_raster_px"] < 50:
        raise PlanWallsError("cluster_raster_px must be at least 50")
    for k in ("cluster_gap_frac", "bridge_frac", "sat_gap_frac"):
        if not 0 <= p[k] < 0.25:
            raise PlanWallsError("%s must be in [0, 0.25)" % k)
    if not 0 < p["sat_min_share"] <= 1 or not 0 < p["sat_min_dim_frac"] <= 1:
        raise PlanWallsError("sat_min_share and sat_min_dim_frac in (0, 1]")
    if not 0 < p["flag_keep_share"] <= 1 or not 1 <= p["flag_frame_sides"] <= 4 \
            or not 0 < p["flag_touch_tol_frac"] < 0.25 \
            or not 0 < p["flag_outline_bbox_frac"] <= 1 \
            or not 0 < p["flag_density_half"] <= 1:
        raise PlanWallsError("flag thresholds are out of range")
    if not 0 < p["hug_tol_frac"] < 0.25 or not 0 < p["hug_min_sides"] <= 4 \
            or not 0 < p["hug_max_area_frac"] <= 1:
        raise PlanWallsError("hug thresholds are out of range")
    return p


def _seg_seg_dist(a, b):
    """Minimum distance between two segments (0 when they cross or touch)."""
    if seg_cross((a[0], a[1]), (a[2], a[3]), (b[0], b[1]), (b[2], b[3])):
        return 0.0
    return min(_pt_seg_dist(a[0], a[1], b), _pt_seg_dist(a[2], a[3], b),
               _pt_seg_dist(b[0], b[1], a), _pt_seg_dist(b[2], b[3], a))


def _pt_seg_dist(px, py, s):
    dx, dy = s[2] - s[0], s[3] - s[1]
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - s[0], py - s[1])
    t = max(0.0, min(1.0, ((px - s[0]) * dx + (py - s[1]) * dy) / L2))
    return math.hypot(px - (s[0] + t * dx), py - (s[1] + t * dy))


def _sides_close(box, bb, tol):
    """How many of bb's four sides box lies within tol of (for (x0,y0,x1,y1)
    boxes, e.g. a cluster's box hugging the raw bounding box -> frame)."""
    n = 0
    n += 1 if abs(box[0] - bb[0]) <= tol else 0
    n += 1 if abs(box[2] - bb[2]) <= tol else 0
    n += 1 if abs(box[1] - bb[1]) <= tol else 0
    n += 1 if abs(box[3] - bb[3]) <= tol else 0
    return n


def _raster_ink(segments, bb, raster_px):
    """Segments rasterized over bb: (ink mask 0/1, W, H, k px per unit)."""
    w = max(bb[2] - bb[0], 1e-9)
    h = max(bb[3] - bb[1], 1e-9)
    k = raster_px / max(w, h)
    W = max(2, int(math.ceil(w * k)) + 1)
    H = max(2, int(math.ceil(h * k)) + 1)
    gray = _raster_lines(segments, W, H, origin=(bb[0], bb[1]), scale=k)
    ink = bytearray(1 if v == 0 else 0 for v in gray)
    return ink, W, H, k


def _px_of(x, y, bb, k, W, H):
    return (min(W - 1, max(0, int(round((x - bb[0]) * k)))),
            min(H - 1, max(0, int(round((y - bb[1]) * k)))))


def _assign_comp(segments, ids, bb, k, W, H, lab):
    """Assign each segment (by majority of sampled pixels) to a component
    label. Returns ({seg index: label}, {label: wall length})."""
    seg_lab, comp_len = {}, {}
    for i in ids:
        s = segments[i]
        samples = max(2, int(math.ceil(_len(s) * k * 2)))
        cnt = {}
        for j in range(samples + 1):
            f = j / float(samples)
            px, py = _px_of(s[0] + (s[2] - s[0]) * f, s[1] + (s[3] - s[1]) * f,
                            bb, k, W, H)
            c = lab[py * W + px]
            if c:
                cnt[c] = cnt.get(c, 0) + 1
        if cnt:
            L = max(cnt, key=lambda c: (cnt[c], -c))
            seg_lab[i] = L
            comp_len[L] = comp_len.get(L, 0.0) + _len(s)
    return seg_lab, comp_len


def _hollow(mask, box, W, band_frac, hollow_frac):
    """True when a component's own ink concentrates near its bounding box
    edges: at most hollow_frac of it lies deeper than band_frac of the box
    from each edge (a sheet frame ring keeps its ink at the edges; a building
    has interior walls filling the middle)."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    bx0, bx1 = x0 + int(band_frac * bw), x1 - int(band_frac * bw)
    by0, by1 = y0 + int(band_frac * bh), y1 - int(band_frac * bh)
    tot = inside = 0
    for y in range(y0, y1 + 1):
        base = y * W
        for x in range(x0, x1 + 1):
            if mask[base + x]:
                tot += 1
                if bx0 <= x <= bx1 and by0 <= y <= by1:
                    inside += 1
    return tot > 0 and inside <= hollow_frac * tot


def _segs_hug(seg_list, bb, ftol, axis):
    """True when both endpoints of the segment lie within ftol of bb's edge
    line on the given axis (0 = the vertical sides x=x0/x1, 1 = horizontal)."""
    if axis == 0:
        return (abs(seg_list[0] - bb[0]) <= ftol and
                abs(seg_list[2] - bb[0]) <= ftol) or \
               (abs(seg_list[0] - bb[2]) <= ftol and
                abs(seg_list[2] - bb[2]) <= ftol)
    return (abs(seg_list[1] - bb[1]) <= ftol and
            abs(seg_list[3] - bb[1]) <= ftol) or \
           (abs(seg_list[1] - bb[3]) <= ftol and
            abs(seg_list[3] - bb[3]) <= ftol)


def is_frame_ring(ink, W, H, box, L, lab, band_frac, hollow_frac, inset_px):
    """A component is sheet-frame-like when its box hugs the sheet bounding
    box (checked by the caller), its own ink is hollow, and at least one
    OTHER component has ink strictly inside its box inset by inset_px (the
    drawing inside the frame). A lone closed rectangle — an outline-only
    building — has nothing inside and is never a frame."""
    x0, y0, x1, y1 = box
    ix0, ix1 = x0 + inset_px, max(x0 + inset_px, x1 - inset_px)
    iy0, iy1 = y0 + inset_px, max(y0 + inset_px, y1 - inset_px)
    if ix1 - ix0 < 2 or iy1 - iy0 < 2:
        return False
    cmask = bytearray(W * H)
    for i in range(W * H):
        if lab[i] == L:
            cmask[i] = 1
    if not _hollow(cmask, box, W, band_frac, hollow_frac):
        return False
    others = 0
    for y in range(iy0, iy1 + 1):
        base = y * W
        for x in range(ix0, ix1 + 1):
            c = lab[base + x]
            if c and c != L:
                others += 1
                if others >= 4:
                    return True
    return False


def _slab_profile(arr, from_end, tb_slabs):
    """Slab sums of a 1-px line/column profile, slab 0 at the chosen edge."""
    n = len(arr)
    sw = max(1, int(math.ceil(n / tb_slabs)))
    m = max(1, int(math.ceil(n / sw)))
    out = [0] * m
    for i, v in enumerate(arr):
        j = (n - 1 - i) // sw if from_end else i // sw
        out[j] += v
    return out, sw


def _strip_proposals(cur, p, dimc):
    """Title-block strip candidates: per sheet-bbox edge, a band of line work
    found from line-density profiles that is separated from the rest by a
    near-empty band of at least `tb_gap_frac` of the drawing. The strip is
    the slab range up to the last dense slab within `tb_max_depth_frac` of
    the profile, provided at least `tb_dense_frac` of its slabs are dense (a
    title block is patchy: long parallel verticals with sparse divider rows
    between them). Tests the strip's share of the line length, axis-aligned
    structure and extent along the edge; a strip whose segments attach to
    line work outside the proposals (the building) is cancelled later by
    `_resolve_strips`. Returns {edge name: set of segment indices}."""
    bbc = bbox_of(cur)
    if bbc is None:
        return {}
    ink, W, H, k = _raster_ink(cur, bbc, p["cluster_raster_px"])
    total_px = 0
    colsum = [0] * W
    rowsum = [0] * H
    for y in range(H):
        base = y * W
        for x in range(W):
            if ink[base + x]:
                total_px += 1
                colsum[x] += 1
                rowsum[y] += 1
    total_len = sum(_len(s) for s in cur)
    if total_len <= 0 or total_px <= 0:
        return {}
    proposals = {}
    for name, axis, from_end in (("right", 0, True), ("left", 0, False),
                                 ("top", 1, False), ("bottom", 1, True)):
        arr = colsum if axis == 0 else rowsum
        prof, sw = _slab_profile(arr, from_end, p["tb_slabs"])
        nsl = len(prof)
        thr = max(p["tb_blank_frac"] * total_px / nsl, p["tb_blank_lines"] * sw)
        imax = max(1, int(nsl * p["tb_max_depth_frac"]))
        d = 0
        for i in range(imax):
            if prof[i] > thr:
                d = i + 1
        if d == 0 or d > imax:
            continue
        dense = sum(1 for i in range(d) if prof[i] > thr)
        if dense < p["tb_dense_frac"] * d:
            continue
        j = d
        while j < nsl and prof[j] <= thr:
            j += 1
        if j >= nsl:
            continue                     # nothing beyond the gap: no strip
        perp_ext = (bbc[2] - bbc[0]) if axis == 0 else (bbc[3] - bbc[1])
        edge_len = (bbc[3] - bbc[1]) if axis == 0 else (bbc[2] - bbc[0])
        gap_u = (j - d) * sw / k
        if gap_u < p["tb_gap_frac"] * dimc:
            continue
        strip_u = (d + 1) * sw / k
        ids = set()
        for i, s in enumerate(cur):
            mp = 0.5 * (s[axis] + s[axis + 2])
            if name == "right":
                ok = mp >= bbc[2] - strip_u
            elif name == "left":
                ok = mp <= bbc[0] + strip_u
            elif name == "top":
                ok = mp <= bbc[1] + strip_u
            else:
                ok = mp >= bbc[3] - strip_u
            if ok:
                ids.add(i)
        if not ids:
            continue
        strip_len = sum(_len(cur[i]) for i in ids)
        share = strip_len / total_len
        if share < p["tb_min_share"] or share > p["tb_max_share"]:
            continue
        sin_t = math.sin(math.radians(p["tb_ang_tol_deg"]))
        axis_len = 0.0
        ext_lo, ext_hi = None, None
        for i in ids:
            s = cur[i]
            L = _len(s)
            dx, dy = abs(s[2] - s[0]), abs(s[3] - s[1])
            if (dx <= sin_t * L) or (dy <= sin_t * L):
                axis_len += L
            if axis == 0:
                c, c2 = s[1], s[3]
            else:
                c, c2 = s[0], s[2]
            ext_lo = c if ext_lo is None else min(ext_lo, c)
            ext_hi = c2 if ext_hi is None else max(ext_hi, c2)
        if axis_len < p["tb_axis_share"] * strip_len:
            continue
        if ext_lo is None or (ext_hi - ext_lo) < p["tb_span_frac"] * edge_len:
            continue
        proposals[name] = ids
    return proposals


def _resolve_strips(cur, proposals, p, dimc, bb0=None):
    """Attachment resolution at segment level: a proposed strip segment is
    cancelled when it touches segment line work that is not itself part of a
    proposal (a building's exterior wall always touches its interior work; a
    sheet frame ring and a title-block strip do not). Iterated to a fixpoint,
    since cancelling a segment puts it back into the rest and can cancel its
    neighbours. Additionally, a touching-cluster of cur that the proposals
    cover completely but that does NOT hug the raw bounding box on
    `hug_min_sides` sides is kept whole: two edge strips can jointly cover a
    building that stands clear of the sheet edges, and that cluster is not
    title-block line work (a frame or title-block-only remnant hugs the raw
    box, like the no-building case). Returns {segment index: reason}."""
    alive = set()
    for ids in proposals.values():
        alive |= ids
    eps = p["tb_contact_frac"] * dimc
    if bb0 is not None:
        n = len(cur)
        seen = [False] * n
        for start in range(n):
            if seen[start]:
                continue
            comp = [start]
            seen[start] = True
            stack = [start]
            while stack:
                i = stack.pop()
                for j in range(n):
                    if seen[j]:
                        continue
                    if _seg_seg_dist(cur[i], cur[j]) <= eps:
                        seen[j] = True
                        comp.append(j)
                        stack.append(j)
            if all(i in alive for i in comp):
                box = bbox_of([cur[i] for i in comp])
                sides = _sides_close(box, bb0,
                                     p["hug_tol_frac"] * max(bb0[2] - bb0[0],
                                                             bb0[3] - bb0[1]))
                if sides < p["hug_min_sides"]:
                    alive -= set(comp)
    while True:
        rest = [i for i in range(len(cur)) if i not in alive]
        if not rest:
            break
        dead = set()
        for i in sorted(alive):
            s = cur[i]
            for j in rest:
                if _seg_seg_dist(s, cur[j]) <= eps:
                    dead.add(i)
                    break
        if not dead:
            break
        alive -= dead
    bbc = bbox_of(cur)
    ftol = p["frame_edge_tol_frac"] * dimc
    drops = {}
    for i in sorted(alive):
        reason = None
        for e, ids in sorted(proposals.items()):
            if i in ids:
                reason = ("frame" if _segs_hug(cur[i], bbc, ftol,
                                               0 if e in ("right", "left")
                                               else 1)
                          else "title_block")
                break
        drops[i] = reason or "title_block"
    return drops


def isolate_building(segments, config=None, params=None, **over):
    """Find the building on a plan sheet and drop the rest.

    Runs on raw imported segments (drawing units), before the perimeter
    trace:

    1. Connected line-work clusters on a light bridge dilation. A cluster is
       a sheet frame when its box hugs the raw bounding box on
       `frame_min_sides` sides, its own ink is hollow (ink concentrated near
       its box edges, `frame_hollow_frac`), and other line work sits inside
       it. Dropped; a segment lying with both endpoints within
       `frame_edge_tol_frac` of a bounding-box edge is reported as frame,
       the rest of such a cluster (title-block strip attached to the border)
       as title block. A lone closed rectangle - an outline-only building -
       has nothing inside it and is never dropped.
    2. Title block: per bbox edge, line-density slabs find a band of line
       work separated from the rest by a near-empty band of at least
       `tb_gap_frac` of the drawing. The band is a title-block strip when it
       holds `tb_min_share`..`tb_max_share` of the line length, is mostly
       axis-aligned segments (`tb_axis_share`), spans `tb_span_frac` of the
       edge and is at least `tb_density_mult` times denser than the rest.
       A proposed strip whose segments touch segment line work outside the
       proposals (the building) is cancelled, iterated to a fixpoint: a
       building's exterior wall always touches its interior work, a frame
       ring does not.
    3. Building cluster: the remaining line work is rasterized, gaps closed
       by `cluster_gap_frac` of the drawing, and connected components
       labeled. The component with the most wall length is the building;
       other components within `sat_gap_frac` of it, holding at least
       `sat_min_share` of its wall length and at least `sat_min_dim_frac`
       of its bounding box in both directions (another wing) are kept. The
       rest (legend boxes, north arrow, scale bar, key plans) are dropped.

    Returns {"kept", "dropped": {"frame", "title_block", "small_cluster"},
    "kept_len", "total_len", "kept_share", "params"}.
    """
    p = params if params is not None else plan_walls_params(config, **over)
    raw = [_ordered(s) for s in _segments_of(segments)]
    raw = [s for s in raw if _len(s) > 1e-9]
    dropped_idx = {"frame": [], "title_block": [], "small_cluster": []}
    res = {"kept": [], "dropped": {k: [] for k in dropped_idx},
           "kept_len": 0.0, "total_len": 0.0, "params": p}
    if not raw:
        return res
    total = sum(_len(s) for s in raw)
    res["total_len"] = total
    if len(raw) < 4:
        res["kept"] = list(raw)
        res["kept_len"] = total
        res["kept_share"] = 1.0
        return res
    bb0 = bbox_of(raw)
    dim0 = max(bb0[2] - bb0[0], bb0[3] - bb0[1])
    cur = list(range(len(raw)))

    # 1. connected clusters; frame-like clusters drop
    ink, W, H, k = _raster_ink([raw[i] for i in cur], bb0, p["cluster_raster_px"])
    r0 = max(1, int(round(p["bridge_frac"] * dim0 * k)))
    lab, sizes, _boxes = _label(_dilate(ink, W, H, r0), W, H, True)
    seg_lab, comp_len = _assign_comp(raw, cur, bb0, k, W, H, lab)
    if comp_len:
        ftol = p["frame_edge_tol_frac"] * dim0
        inset = max(2, int(round(2.0 * ftol * k)))
        frame_like = set()
        for L, clen in comp_len.items():
            segs_L = [i for i in cur if seg_lab.get(i) == L]
            box = bbox_of([raw[i] for i in segs_L])
            if box is None:
                continue
            if _sides_close(box, bb0, ftol) < p["frame_min_sides"]:
                continue
            if not is_frame_ring(ink, W, H, _px_box(box, bb0, k, W, H), L, lab,
                                 p["frame_hollow_band_frac"],
                                 p["frame_hollow_frac"], inset):
                continue
            frame_like.add(L)
        for i in cur:
            if seg_lab.get(i) in frame_like:
                dropped_idx["frame" if _segs_hug(raw[i], bb0, ftol, 0) or
                            _segs_hug(raw[i], bb0, ftol, 1)
                            else "title_block"].append(i)
    dropped = set(i for v in dropped_idx.values() for i in v)
    cur = [i for i in cur if i not in dropped]
    a1_dropped = bool(dropped)

    # 2. title-block strips
    cursegs = [raw[i] for i in cur]
    if len(cursegs) >= 4:
        bbc2 = bbox_of(cursegs)
        dimc = max(bbc2[2] - bbc2[0], bbc2[3] - bbc2[1])
        proposals = _strip_proposals(cursegs, p, dimc)
        strip_drops = _resolve_strips(cursegs, proposals, p, dimc, bb0)
        if strip_drops and not a1_dropped and len(strip_drops) == len(cursegs):
            pass                     # sole line work: keep it (a bare outline)
        else:
            for i, reason in strip_drops.items():
                dropped_idx[reason or "title_block"].append(cur[i])
            dropped = set(i for v in dropped_idx.values() for i in v)
            cur = [i for i in cur if i not in dropped]

    # 3. building cluster among what is left
    kept = []
    if cur:
        cc = [raw[i] for i in cur]
        bbc = bbox_of(cc)
        dimc = max(bbc[2] - bbc[0], bbc[3] - bbc[1])
        ink, W, H, k = _raster_ink(cc, bbc, p["cluster_raster_px"])
        r1 = max(1, int(round(p["cluster_gap_frac"] * dimc * k)))
        lab, sizes, _boxes = _label(_dilate(ink, W, H, r1), W, H, True)
        seg_lab, comp_len = _assign_comp(raw, cur, bbc, k, W, H, lab)
        if comp_len:
            r2 = max(r1, int(round(p["sat_gap_frac"] * dimc * k)))
            lab2, _s2, _b2 = _label(_dilate(ink, W, H, r2), W, H, True)

            def first_px_label(labels, L):
                for i in range(W * H):
                    if lab[i] == L:
                        return labels[i]
                return None

            main = max(comp_len, key=lambda L: (comp_len[L], -L))
            main2 = first_px_label(lab2, main)
            kept = [i for i in cur if seg_lab.get(i) == main]
            mseg = [raw[i] for i in kept]
            mbox = bbox_of(mseg)
            for L in sorted(comp_len):
                if L == main or comp_len[L] <= 0:
                    continue
                segs_L = [i for i in cur if seg_lab.get(i) == L]
                box = bbox_of([raw[i] for i in segs_L])
                ok = (comp_len[L] >= p["sat_min_share"] * comp_len[main]
                      and first_px_label(lab2, L) == main2
                      and box is not None
                      and (box[2] - box[0]) >=
                      p["sat_min_dim_frac"] * (mbox[2] - mbox[0])
                      and (box[3] - box[1]) >=
                      p["sat_min_dim_frac"] * (mbox[3] - mbox[1]))
                if ok:
                    kept.extend(segs_L)
                else:
                    dropped_idx["small_cluster"].extend(segs_L)
    kept = sorted(set(kept) - set(i for v in dropped_idx.values() for i in v))
    res["kept"] = [raw[i] for i in kept]
    res["dropped"] = {kk: [raw[i] for i in vv] for kk, vv in dropped_idx.items()}
    res["kept_len"] = sum(_len(s) for s in res["kept"])
    res["kept_share"] = (res["kept_len"] / total) if total > 0 else 0.0
    return res


def _px_box(box, bb, k, W, H):
    """A unit-coordinate bbox as a pixel bbox (inclusive)."""
    x0, y0 = _px_of(box[0], box[1], bb, k, W, H)
    x1, y1 = _px_of(box[2], box[3], bb, k, W, H)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _perimeter_scale(segments, area_m2):
    """Scale (m per drawing unit) from the building outline that perimeter.py
    traces over a raster of the line work. Returns (m_per_unit or None,
    info). The trace is the preferred source over the bounding box; on any
    failure it reports a reason and the caller falls back to the bounding
    box. The raster keeps >15 % blank margin around the line work so the
    building is not mistaken for a sheet frame or most of the page."""
    bb = bbox_of(segments)
    w = max(bb[2] - bb[0], 1e-9)
    h = max(bb[3] - bb[1], 1e-9)
    k = PERIMETER_RASTER_PX / max(w, h)
    pad = max(8.0, 0.15 * PERIMETER_RASTER_PX)
    iw = int(math.ceil(w * k)) + int(2 * pad)
    ih = int(math.ceil(h * k)) + int(2 * pad)
    gray = _raster_lines(segments, iw, ih, scale=k,
                         origin=(bb[0] - pad / k, bb[1] - pad / k))
    try:
        trace = trace_outline(gray, iw, ih, None)
    except PerimeterError as exc:
        return None, {"attempted": True, "succeeded": False,
                      "reason": "perimeter trace failed: %s" % exc}
    outline = trace.get("outline") or []
    if len(outline) < 3 or not trace.get("area_px"):
        return None, {"attempted": True, "succeeded": False,
                      "reason": "the traced outline was not usable"}
    area_u2 = float(trace["area_px"]) / (k * k)
    if area_u2 <= 0:
        return None, {"attempted": True, "succeeded": False,
                      "reason": "the traced outline has no area"}
    outline_u = [[bb[0] - pad / k + px / k, bb[1] - pad / k + py / k]
                 for px, py in outline]
    info = {"attempted": True, "succeeded": True, "area_units_sq": area_u2,
            "area_px": trace["area_px"], "outline": outline_u, "reason": None,
            "warnings": list(trace.get("warnings", []))}
    return math.sqrt(area_m2 / area_u2), info


def _busiest_quarter_density(segments, bb):
    """Max wall-length density (length per square unit) over the four
    quadrants of the bounding box, assigned by segment midpoint."""
    if bb is None:
        return 0.0
    mx = 0.5 * (bb[0] + bb[2])
    my = 0.5 * (bb[1] + bb[3])
    aw = max(0.5 * (bb[2] - bb[0]), 1e-9)
    ah = max(0.5 * (bb[3] - bb[1]), 1e-9)
    best = 0.0
    for qx, qy in ((bb[0], bb[1]), (mx, bb[1]), (bb[0], my), (mx, my)):
        L = 0.0
        for s in segments:
            px, py = 0.5 * (s[0] + s[2]), 0.5 * (s[1] + s[3])
            if qx <= px <= qx + aw and qy <= py <= qy + ah:
                L += _len(s)
        best = max(best, L / (aw * ah))
    return best


def crop_to_box(segments, box):
    """Keep the segments whose midpoint lies inside the building box.
    box: [x0, y0, x1, y1] in drawing units, or {"frac": [x0, y0, x1, y1]}
    as fractions of the segments' bounding box. Returns (kept, info)."""
    segs = _segments_of(segments)
    if isinstance(box, dict):
        if "frac" not in box:
            raise PlanWallsError("box needs [x0, y0, x1, y1] or {'frac': [...]}")
        bb = bbox_of(segs)
        f = [float(v) for v in box["frac"]]
        box = [bb[0] + f[0] * (bb[2] - bb[0]), bb[1] + f[1] * (bb[3] - bb[1]),
               bb[0] + f[2] * (bb[2] - bb[0]), bb[1] + f[3] * (bb[3] - bb[1])]
    if len(box) != 4:
        raise PlanWallsError("box must be [x0, y0, x1, y1]")
    x0, x1 = sorted((float(box[0]), float(box[2])))
    y0, y1 = sorted((float(box[1]), float(box[3])))
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        raise PlanWallsError("box has no area")
    kept = [s for s in segs
            if x0 <= 0.5 * (s[0] + s[2]) <= x1 and y0 <= 0.5 * (s[1] + s[3]) <= y1]
    tot = sum(_len(s) for s in segs) or 1.0
    return kept, {"box_units": [x0, y0, x1, y1], "n_in": len(segs), "n_kept": len(kept),
                  "kept_share": round(sum(_len(s) for s in kept) / tot, 4)}


def scale_to_area(segments, sq_ft, use_perimeter=True,
                  double_line_m=DEFAULT_DOUBLE_LINE_M,
                  min_wall_m=DEFAULT_MIN_WALL_M, merge=True,
                  isolate=False, config=None, box=None):
    """Scale drawing units to metres so the building outline covers `sq_ft`
    square feet (1 sq ft = 0.09290304 m2).

    `box` is the building box the estimator draws on the plan page:
    [x0, y0, x1, y1] in drawing units, or {"frac": [x0, y0, x1, y1]} as
    fractions (0 to 1) of the line work's bounding box. Only segments whose
    midpoint lies inside it are kept, before the trace, the scale and the
    walls. This is the default way to keep the sheet frame, title block and
    legends out of the plan (Matt 2026-11-18).

    With `isolate` (off by default since the 2026-11-18 real-plan QA, where
    it kept the title block instead of the building on 5 of 16 sheets) the sheet is cleaned first
    (`isolate_building`): the sheet frame, inner borders, the title-block
    strip and small margin items (legend boxes, north arrows, scale bars)
    are dropped, and the trace, the scale and the walls are computed from
    the kept building line work only. The outline is the perimeter that
    perimeter.py traces over a raster of the kept line work when it
    succeeds (the real footprint, so anything outside the building cannot
    inflate the scale); otherwise the kept line work's bounding box. The
    result reports the scale used ("m_per_unit") and its source
    ("scale_source": "perimeter" or "bbox"). With `merge` the scaled
    segments are cleaned by merge_segments (duplicates, collinear overlap,
    double-line faces, pieces under `min_wall_m` in metres dropped).
    Returns {"m_per_unit", "scale_source", "area_m2", "sq_ft",
    "bbox_units", "bbox_units_area", "perimeter", "segments" (metres),
    "n_raw", "n_kept", "isolate", "flags", "merge_counts", "warnings"}.
    `flags` names the cleanup-suspicion conditions that fired
    (low_keep_share, outline_touches_raw_bbox, outline_mostly_empty,
    no_building_like_content) and `warnings` carries a readable line for
    each; `isolate` holds the cleanup report (kept share of the line
    length, dropped segment counts by reason)."""
    raw = _segments_of(segments)
    if not raw:
        raise PlanWallsError("the plan has no wall segments to scale")
    box_info = None
    if box is not None:
        raw, box_info = crop_to_box(raw, box)
        if not raw:
            raise PlanWallsError("no line work inside the building box")
    try:
        area_m2 = float(sq_ft) * SQFT_TO_M2
    except (TypeError, ValueError):
        raise PlanWallsError("sq_ft must be a number of square feet")
    if area_m2 <= 0:
        raise PlanWallsError("sq_ft must be > 0")
    p = plan_walls_params(config)
    warnings = []
    flags = []
    info = {"attempted": False, "succeeded": False, "reason": None}
    m_per_unit = None

    # --- sheet cleanup ----------------------------------------------------
    iso = {"attempted": False, "kept_share": 1.0, "n_kept": len(raw),
           "dropped": {"frame": 0, "title_block": 0, "small_cluster": 0}}
    if isolate and len(raw) >= 4:
        r = isolate_building(raw, params=p)
        kept = r["kept"]
        iso = {"attempted": True,
               "kept_share": round(r["kept_share"], 4),
               "n_kept": len(kept),
               "dropped": {kk: len(v) for kk, v in r["dropped"].items()}}
        if not kept:
            raise PlanWallsError(
                "sheet cleanup kept no line work (%d frame and %d "
                "title-block segment(s) dropped): no building line work on "
                "the sheet" % (iso["dropped"]["frame"],
                               iso["dropped"]["title_block"]))
        if r["kept_share"] < p["flag_keep_share"]:
            flags.append("low_keep_share")
            warnings.append(
                "sheet cleanup kept only %.0f%% of the line length (under "
                "%.0f%%): most line work was dropped as frame, title-block "
                "or margin items; check the cleanup against the plan before "
                "trusting the walls" % (100.0 * r["kept_share"],
                                        100.0 * p["flag_keep_share"]))
        bb0 = bbox_of(raw)
        bbk = bbox_of(kept)
        htol = p["hug_tol_frac"] * max(bb0[2] - bb0[0], bb0[3] - bb0[1])
        h_sides = _sides_close(bbk, bb0, htol)
        h_area = (bbk[2] - bbk[0]) * (bbk[3] - bbk[1])
        if h_sides >= p["hug_min_sides"] and \
                h_area <= p["hug_max_area_frac"] * (bb0[2] - bb0[0]) * (bb0[3] - bb0[1]):
            flags.append("no_building_like_content")
            warnings.append(
                "the cleaned line work hugs the raw bounding box on %d "
                "side(s) and covers only %.0f%% of it: no dense building "
                "cluster was found; the sheet may hold only frame, "
                "title-block or legend line work"
                % (h_sides, 100.0 * h_area / ((bb0[2] - bb0[0]) * (bb0[3] - bb0[1]))))
    else:
        kept = list(raw)
    n_kept = len(kept)
    bb0 = bbox_of(raw)

    # --- outline and scale ------------------------------------------------
    bb = bbox_of(kept)
    bbox_area = (bb[2] - bb[0]) * (bb[3] - bb[1])
    if bbox_area <= 0:
        raise PlanWallsError("the line work has no bounding-box area")
    if use_perimeter and len(kept) >= 3:
        m_per_unit, info = _perimeter_scale(kept, area_m2)
        if m_per_unit is None:
            warnings.append("%s; scaled from the line work's bounding box "
                            "instead" % info["reason"])
    if m_per_unit is None:
        m_per_unit = math.sqrt(area_m2 / bbox_area)
        source = "bbox"
    else:
        source = "perimeter"
        ratio = bbox_area * m_per_unit * m_per_unit / area_m2
        if ratio > 1.25:
            warnings.append(
                "the line work's bounding box covers %.0f%% of the floor area "
                "at the traced scale: stray line work may sit outside the "
                "building; check the scale" % (100.0 * round(ratio, 2)))

    # --- outline-suspicion flags ------------------------------------------
    outline = (info.get("outline") if info.get("succeeded") else None) or []
    if outline:
        dim0 = max(bb0[2] - bb0[0], bb0[3] - bb0[1]) if bb0 else max(
            bb[2] - bb[0], bb[3] - bb[1])
        bb0_area = (bb0[2] - bb0[0]) * (bb0[3] - bb0[1])
        ttol = p["flag_touch_tol_frac"] * dim0
        touch = _sides_close(_outline_box(outline), bb0, ttol)
        o_area = float(info.get("area_units_sq") or 0.0)
        # frame symptom: the outline both hugs the raw bounding box on
        # flag_frame_sides sides AND spans essentially all of it (a building
        # inset from a frame does neither; a frame-like outline does both)
        if touch >= p["flag_frame_sides"] and o_area > 0 and \
                o_area >= p["flag_outline_bbox_frac"] * bb0_area:
            flags.append("outline_touches_raw_bbox")
            warnings.append(
                "the traced outline hugs the raw line-work bounding box on "
                "%d side(s) and spans most of it: it still follows "
                "sheet-frame-like line work; check the outline against the "
                "plan" % touch)
        if bbox_area > 0:
            if o_area > 0 and o_area >= p["flag_outline_bbox_frac"] * bbox_area:
                inside_len = sum(
                    _inside_frac(s, outline) * _len(s) for s in kept)
                ins_dens = inside_len / o_area
                # busiest quarter of the cleaned line work: ring-shaped
                # buildings with partitions on one side must not trip the
                # flag, a hull over empty margin line work must
                qdens = _busiest_quarter_density(kept, bb)
                if qdens > 0 and ins_dens < p["flag_density_half"] * qdens:
                    flags.append("outline_mostly_empty")
                    warnings.append(
                        "the traced outline covers %.0f%% of the cleaned "
                        "line work's bounding box but only %.0f%% of the "
                        "busiest quarter's wall density is inside it "
                        "(%.3f vs %.3f wall-length units per square unit): "
                        "mostly empty area inside the outline; check the "
                        "outline against the plan"
                        % (100.0 * o_area / bbox_area,
                           100.0 * ins_dens / qdens, ins_dens, qdens))

    scaled = [[c * m_per_unit for c in s] for s in kept]
    res = {
        "m_per_unit": m_per_unit, "scale_source": source,
        "area_m2": round(area_m2, 2), "sq_ft": float(sq_ft),
        "bbox_units": bb, "bbox_units_area": bbox_area,
        "perimeter": info, "warnings": warnings,
        "n_raw": len(raw), "n_kept": n_kept,
        "isolate": iso, "flags": flags, "box": box_info,
    }
    if merge:
        mg = merge_segments(scaled, double_line_m, min_wall_m)
        res["segments"] = mg["segments"]
        res["merge_counts"] = mg["counts"]
    else:
        res["segments"] = scaled
        res["merge_counts"] = None
    res["n_walls"] = len(res["segments"])
    if not res["segments"]:
        raise PlanWallsError("no wall segments survived merging; check the "
                             "plan and the area")
    return res


def _outline_box(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _point_in_poly(x, y, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    return inside


def _inflate_poly(poly, factor=1.002):
    """Polygon scaled about its centroid, so wall line work that lies exactly
    on the traced outline still counts as inside it."""
    cx, cy = _poly_centroid(poly)
    return [[cx + factor * (x - cx), cy + factor * (y - cy)] for x, y in poly]


def _inside_frac(s, poly, steps=10):
    """Share of a segment's length whose sampled points lie inside the
    polygon (sampled uniformly, endpoints excluded; the polygon is compared
    slightly inflated so boundary line work counts as inside)."""
    big = _inflate_poly(poly)
    hits = 0
    for j in range(1, steps + 1):
        f = j / float(steps + 1)
        if _point_in_poly(s[0] + (s[2] - s[0]) * f, s[1] + (s[3] - s[1]) * f,
                          big):
            hits += 1
    return hits / float(steps)


def _ray_seg_t(px, py, ex, ey, ax, ay, bx, by):
    """Parameter t in [0, 1] along (p -> e) where it crosses the segment
    a-b, or None."""
    dx, dy = ex - px, ey - py
    rx, ry = bx - ax, by - ay
    den = dx * ry - dy * rx
    if abs(den) < 1e-15:
        return None
    u = ((ax - px) * ry - (ay - py) * rx) / den
    if u < 0.0 or u > 1.0:
        return None
    return ((ax - px) * dy - (ay - py) * dx) / den


def _ray_poly_intervals(px, py, ex, ey, poly):
    """(t0, t1) parameter intervals of a ray from an inside point that lie
    inside a polygon, by even-odd crossing counting."""
    ts = []
    n = len(poly)
    for i in range(n):
        t = _ray_seg_t(px, py, ex, ey, poly[i][0], poly[i][1],
                       poly[(i + 1) % n][0], poly[(i + 1) % n][1])
        if t is not None and 0.0 <= t <= 1.0:
            ts.append(t)
    ts.sort()
    if not ts:
        return [(0.0, 1.0)] if _point_in_poly(px, py, poly) else []
    if len(ts) % 2 == 1:
        ts = ts[:-1]
    if not ts:
        return [(0.0, 1.0)]
    ivs = [(0.0, ts[0])]
    for i in range(1, len(ts) - 1, 2):
        ivs.append((ts[i], min(1.0, ts[i + 1])))
    return ivs


def _poly_centroid(pts):
    a = sx = sy = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        cr = x1 * y2 - x2 * y1
        a += cr
        sx += (x1 + x2) * cr
        sy += (y1 + y2) * cr
    if abs(a) < 1e-12:
        return (sum(q[0] for q in pts) / n, sum(q[1] for q in pts) / n)
    return sx / (3.0 * a), sy / (3.0 * a)


# -------------------------------------------------------------- floor build

def floor_from_plan(segments, sq_ft, ppm=DEFAULT_PPM, material=DEFAULT_MATERIAL,
                    config=None, use_perimeter=True,
                    double_line_m=DEFAULT_DOUBLE_LINE_M,
                    min_wall_m=DEFAULT_MIN_WALL_M, cell_m=None, floor_id=None,
                    isolate=None, box=None):
    """Build a ROMtool floor dict ready for autoplace with loss_mode
    "physics": walls in floor px (x1, y1, x2, y2, material), a measured
    clutter map (wall crossings per metre per square, the physics field),
    an empty element list and -- when the perimeter trace succeeded -- the
    traced outline in floor px. `segments` is a scale_to_area result (a dict
    with "m_per_unit") or raw segments, which are then scaled from `sq_ft`
    first (heuristic sheet cleanup only with `isolate=True`; off by default;
    `box`, the estimator's building box in drawing units or a
    {"frac": [...]} dict of fractions of the line work's bounding box, crops
    first). ppm is the floor's px-per-metre scale (default 2.0)."""
    cfg = config or load_config()
    scaled = segments
    if not (isinstance(scaled, dict) and "m_per_unit" in scaled):
        kw = {} if isolate is None else {"isolate": isolate}
        if box is not None:
            kw["box"] = box
        scaled = scale_to_area(scaled, sq_ft, use_perimeter=use_perimeter,
                               double_line_m=double_line_m, min_wall_m=min_wall_m,
                               config=cfg, **kw)
    segs = scaled["segments"]
    if not segs:
        raise PlanWallsError("the plan has no wall segments after cleaning")
    ppm = float(ppm)
    if ppm <= 0:
        raise PlanWallsError("ppm must be > 0")
    bb = bbox_of(segs)
    mx, my = bb[0], bb[1]

    def to_px(u, v):
        return (u - mx) * ppm, (v - my) * ppm

    walls = []
    for x1, y1, x2, y2 in segs:
        px1, py1 = to_px(x1, y1)
        px2, py2 = to_px(x2, y2)
        walls.append({"x1": round(px1, 1), "y1": round(py1, 1),
                      "x2": round(px2, 1), "y2": round(py2, 1),
                      "material": material})
    wmax = max(max(w["x1"], w["x2"]) for w in walls)
    hmax = max(max(w["y1"], w["y2"]) for w in walls)
    pad = 2.0 * ppm                     # room for autoplace's 0.5 m wall inset
    w_px = int(math.ceil(wmax + pad))
    h_px = int(math.ceil(hmax + pad))
    floor = {"walls": walls, "elements": [], "ppm": ppm,
             "w_px": w_px, "h_px": h_px, "loss_mode": "physics"}
    if floor_id:
        floor["id"] = floor_id

    # measured crossings per metre per square, from the drawn walls: the
    # physics loss field (also valid if the walls are cleared later)
    gray = _raster_lines([(float(w["x1"]), float(w["y1"]),
                           float(w["x2"]), float(w["y2"]))
                          for w in walls], w_px, h_px)
    opts = {"cell_m": cell_m} if cell_m else {}
    floor["clutter_map"] = build_clutter_map(gray, w_px, h_px, floor, cfg,
                                             **opts)
    outline = (scaled.get("perimeter") or {}).get("outline")
    if scaled.get("scale_source") == "perimeter" and outline:
        # the traced outline is in drawing units; the segments above are in
        # metres, so scale it to metres before the shared px transform
        m = float(scaled["m_per_unit"])
        floor["outline"] = [[round(px, 1), round(py, 1)]
                            for px, py in (to_px(u * m, v * m) for u, v in outline)]
    floor["plan"] = {
        "sq_ft": scaled.get("sq_ft"), "area_m2": scaled.get("area_m2"),
        "m_per_unit": scaled.get("m_per_unit"),
        "scale_source": scaled.get("scale_source"),
        "walls_raw": scaled.get("n_raw"), "walls": len(walls),
        "merge_counts": scaled.get("merge_counts"),
        "scale_warnings": scaled.get("warnings", []),
        "isolate": scaled.get("isolate"),
        "flags": list(scaled.get("flags") or []),
        "box": scaled.get("box")}
    od = outline_density(floor)
    if od:
        floor["plan"]["outline_density"] = od
    return floor


def outline_density(floor, n_rays=180, sample_steps=11):
    """Wall metres per square metre and the mean wall crossings per metre
    measured inside the traced outline only (floor["outline"]), or None when
    the floor has no outline.

    The same fixed ray fan as wall_density, clipped to the outline: each ray
    scores a crossing only where it is inside the polygon and is divided by
    the length it spends inside it, so margin area outside the outline does
    not dilute the metric. Returns {"wall_m_per_m2", "crossings_per_m_mean",
    "area_m2", "n_rays"}."""
    outline = floor.get("outline") or []
    if len(outline) < 3:
        return None
    ppm = float(floor.get("ppm", 2.0))
    if ppm <= 0:
        return None
    poly = [(float(x), float(y)) for x, y in outline]
    area_px = polygon_area(poly)
    if area_px <= 0:
        return None
    walls = [(float(w["x1"]), float(w["y1"]), float(w["x2"]), float(w["y2"]))
             for w in floor.get("walls") or []]
    inside_px = sum(_inside_frac(w, poly, sample_steps) * _len(w)
                    for w in walls)
    cx, cy = _poly_centroid(poly)
    w_px, h_px = float(floor.get("w_px", 0)), float(floor.get("h_px", 0))
    R = 1.5 * math.hypot(w_px, h_px)
    ray_inside_px = 0.0
    crossings = 0
    if walls and (w_px > 0 or h_px > 0):
        for k in range(n_rays):
            ang = 2.0 * math.pi * k / n_rays
            ex, ey = cx + R * math.cos(ang), cy + R * math.sin(ang)
            for t0, t1 in _ray_poly_intervals(cx, cy, ex, ey, poly):
                ray_inside_px += (t1 - t0) * R
            for w in walls:
                if not seg_cross((cx, cy), (ex, ey), (w[0], w[1]),
                                 (w[2], w[3])):
                    continue
                t = _ray_seg_t(cx, cy, ex, ey, w[0], w[1], w[2], w[3])
                if t is None:
                    continue
                if _point_in_poly(cx + t * (ex - cx), cy + t * (ey - cy),
                                  poly):
                    crossings += 1
    area_m2 = area_px / (ppm * ppm)
    ray_m = ray_inside_px / ppm
    return {
        "wall_m_per_m2": (round(inside_px / ppm / area_m2, 4)
                          if area_m2 > 0 else None),
        "crossings_per_m_mean": (round(crossings / ray_m, 4)
                                 if ray_m > 0.5 else None),
        "area_m2": round(area_m2, 2),
        "n_rays": n_rays,
    }


# ----------------------------------------------------------------- density

def wall_density(floor, n_rays=180):
    """Wall metres per square metre of floor and the mean wall crossings per
    metre along straight rays across the floor (a fixed fan of evenly spaced
    azimuths from the floor centre: deterministic, so repeatable reports).

    Returns {"n_walls", "wall_m_total", "floor_area_m2", "wall_m_per_m2",
    "crossings_per_m_mean", "n_rays"}."""
    ppm = float(floor.get("ppm", 10.0))
    walls = floor.get("walls") or []
    w_px, h_px = float(floor.get("w_px", 0)), float(floor.get("h_px", 0))
    area_m2 = w_px * h_px / (ppm * ppm)
    total_m = sum(_len([w["x1"], w["y1"], w["x2"], w["y2"]]) for w in walls) / ppm
    cx, cy = 0.5 * w_px, 0.5 * h_px
    wpoly = [(float(w["x1"]), float(w["y1"]), float(w["x2"]), float(w["y2"]))
             for w in walls]
    bb = bbox_of(wpoly) if wpoly else None
    per_ray = []
    if bb is not None and (bb[2] - bb[0]) > 0 and (bb[3] - bb[1]) > 0:
        R = 1.5 * math.hypot(w_px, h_px)
        for k in range(n_rays):
            ang = 2.0 * math.pi * k / n_rays
            ex, ey = cx + R * math.cos(ang), cy + R * math.sin(ang)
            hits = sum(1 for w in wpoly
                       if seg_cross((cx, cy), (ex, ey), (w[0], w[1]), (w[2], w[3])))
            # ray length inside the walls' bounding box
            L_px = _ray_box_length(cx, cy, ex, ey, bb)
            if L_px * ppm > 0.5:
                per_ray.append(hits / (L_px * ppm))
    return {
        "n_walls": len(walls),
        "wall_m_total": round(total_m, 2),
        "floor_area_m2": round(area_m2, 2),
        "wall_m_per_m2": (round(total_m / area_m2, 4) if area_m2 > 0 else None),
        "crossings_per_m_mean": (round(sum(per_ray) / len(per_ray), 4)
                                 if per_ray else None),
        "n_rays": len(per_ray),
    }


def _ray_box_length(px, py, ex, ey, bb):
    """Length of the segment (px, py) -> (ex, ey) inside the axis-aligned box
    bb = (x0, y0, x1, y1), by slab clipping. 0 when the ray misses the box."""
    dx, dy = ex - px, ey - py
    t0, t1 = 0.0, 1.0
    for start, d, lo, hi in ((px, dx, bb[0], bb[2]), (py, dy, bb[1], bb[3])):
        if d == 0:
            if start < lo or start > hi:
                return 0.0
            continue
        ta, tb = (lo - start) / d, (hi - start) / d
        if ta > tb:
            ta, tb = tb, ta
        t0, t1 = max(t0, ta), min(t1, tb)
        if t0 > t1:
            return 0.0
    return (t1 - t0) * math.hypot(dx, dy)


# --------------------------------------------------------------------- CLI

def _walls_from_file(path, page=1, layer_filter=None, force_raster=False):
    low = str(path).lower()
    if low.endswith(".dxf"):
        return walls_from_dxf(path, layer_filter=layer_filter)
    if low.endswith(".pdf"):
        return walls_from_pdf(path, page=page,
                              raster=True if force_raster else None)
    raise PlanWallsError("unsupported file type %r: expected .dxf or .pdf"
                         % str(path))


def _parse_box(text, name):
    """Four comma-separated numbers off the CLI: [x0, y0, x1, y1]."""
    try:
        vals = [float(v) for v in str(text).split(",")]
    except ValueError:
        raise PlanWallsError("--%s needs four numbers x0,y0,x1,y1, got %r"
                             % (name, text))
    if len(vals) != 4:
        raise PlanWallsError("--%s needs four numbers x0,y0,x1,y1, got %r"
                             % (name, text))
    return vals


def write_preview(path, segments, box=None, outline=None, dpi=150.0):
    """Render the imported line work to a PNG so an estimator can read the
    building box off the plan.

    The segments draw in black over a labelled 10 % grid: the line work's
    bounding box divided at 0.0, 0.1, ..., 1.0 on BOTH axes, each gridline
    labelled with its fraction in grey. `box` (a crop_to_box box: drawing
    units or a {"frac": [...]} dict) draws in green dashes and the traced
    `outline` (drawing units, from the scale_to_area perimeter result) in
    red, so the fractions at the box's edges can be read straight off the
    image and passed to --box-frac. Rendered with pymupdf at `dpi`.
    """
    try:
        import pymupdf
    except ImportError:
        raise PlanWallsError("pymupdf is required for --preview "
                             "(uv run --with pymupdf ...)")
    segs = _segments_of(segments)
    bb = bbox_of(segs)
    if bb is None:
        raise PlanWallsError("no line work to preview")
    w = bb[2] - bb[0]
    h = bb[3] - bb[1]
    big = max(w, h)
    lab = max(10.0, 0.07 * big)          # margin reserved for the labels
    fs = max(4.0, 0.02 * big)            # grid-label font size
    ox, oy = lab, lab                    # drawing area origin on the page

    def to_page(x, y):
        # drawing y grows upward, the page's y grows downward: flip y so the
        # preview keeps the plan's orientation
        return (ox + (x - bb[0]), oy + (bb[3] - y))

    doc = pymupdf.open()
    page = doc.new_page(width=w + 2.0 * lab, height=h + 2.0 * lab)
    # labelled 10 % grid over the line work's bounding box
    for k in range(11):
        f = k / 10.0
        x_top = to_page(bb[0] + f * w, bb[3])
        x_bot = to_page(bb[0] + f * w, bb[1])
        page.draw_line(x_top, x_bot, color=(0.78, 0.78, 0.78), width=0.4)
        y_left = to_page(bb[0], bb[1] + f * h)
        y_right = to_page(bb[2], bb[1] + f * h)
        page.draw_line(y_left, y_right, color=(0.78, 0.78, 0.78), width=0.4)
        text = "%.1f" % f
        tw = pymupdf.get_text_length(text, fontsize=fs)
        page.insert_text((x_top[0] - 0.5 * tw, oy + h + fs * 1.2), text,
                         fontsize=fs, color=(0.25, 0.25, 0.25))
        page.insert_text((ox - 2.0 - tw, y_left[1] + 0.35 * fs), text,
                         fontsize=fs, color=(0.25, 0.25, 0.25))
    # the line work
    ink_w = max(0.3, 0.001 * big)
    for s in segs:
        page.draw_line(to_page(s[0], s[1]), to_page(s[2], s[3]),
                       color=(0.0, 0.0, 0.0), width=ink_w)
    if box is not None:
        kept, info = crop_to_box(segs, box)
        bx = info["box_units"]
        lw = max(0.8, 0.003 * big)
        corners = [to_page(bx[0], bx[1]), to_page(bx[2], bx[1]),
                   to_page(bx[2], bx[3]), to_page(bx[0], bx[3])]
        for i in range(4):
            page.draw_line(corners[i], corners[(i + 1) % 4],
                           color=(0.0, 0.55, 0.0), width=lw, dashes="[6 4] 0")
        if outline and len(outline) >= 3:
            pts = [to_page(px, py) for px, py in outline]
            for i in range(len(pts)):
                page.draw_line(pts[i], pts[(i + 1) % len(pts)],
                               color=(0.85, 0.1, 0.1), width=ink_w)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72.0, dpi / 72.0))
    pix.save(str(path))
    return str(path)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="romtool.plan_walls",
        description="Import the walls of a floor plan (DXF or vector/raster "
                    "PDF) as a ROMtool physics-mode floor.")
    ap.add_argument("file", help="plan file (.dxf or .pdf)")
    ap.add_argument("--sq-ft", type=float, required=True, dest="sq_ft",
                    help="floor area in square feet (sets the scale)")
    ap.add_argument("--page", type=int, default=1, help="PDF page (1-based)")
    ap.add_argument("--raster", action="store_true",
                    help="force the raster path for PDFs: render the page "
                         "and trace it with walls_from_raster (default: "
                         "vector line work first, raster fallback)")
    ap.add_argument("--out", "-o", default=None,
                    help="write the floor JSON here")
    ap.add_argument("--ppm", type=float, default=DEFAULT_PPM,
                    help="floor px per metre (default %g)" % DEFAULT_PPM)
    ap.add_argument("--material", default=DEFAULT_MATERIAL,
                    help="wall material id from wall_materials "
                         "(default %s)" % DEFAULT_MATERIAL)
    ap.add_argument("--layer", action="append", default=None,
                    help="DXF layer to import (repeatable; default: "
                         "wall-looking layers, else every layer)")
    ap.add_argument("--no-perimeter", action="store_true",
                    help="scale from the line work's bounding box only "
                         "(skip the perimeter-trace attempt)")
    ap.add_argument("--box", default=None, dest="box",
                    help="building box x0,y0,x1,y1 in DRAWING UNITS: only "
                         "line work whose midpoint is inside it is kept "
                         "(the default way to isolate the building; the "
                         "estimator draws the box on the plan page)")
    ap.add_argument("--box-frac", default=None, dest="box_frac",
                    help="building box as fractions x0,y0,x1,y1 of the line "
                         "work's bounding box (0.0-1.0 on both axes; read "
                         "them off the --preview image)")
    ap.add_argument("--isolate", action="store_true",
                    help="also run the heuristic sheet cleanup "
                         "(isolate_building: drop frame, title block and "
                         "margin items) after the box crop; off by default "
                         "since the 2026-11-18 real-plan QA, where it kept "
                         "the title block instead of the building on 5 of "
                         "16 sheets — draw a --box instead")
    ap.add_argument("--preview", default=None, dest="preview",
                    help="write a PNG preview here: the imported line work "
                         "over a labelled 10%% grid (fractions of the line "
                         "work's bounding box, 0.0-1.0 on both axes) and, "
                         "when a box is given, the box and the traced "
                         "outline, so box fractions can be read off the "
                         "image")
    args = ap.parse_args(argv)
    if args.box and args.box_frac:
        print("error: give --box or --box-frac, not both", file=sys.stderr)
        return 2
    box = None
    try:
        if args.box:
            box = _parse_box(args.box, "box")
        elif args.box_frac:
            box = {"frac": _parse_box(args.box_frac, "box-frac")}
    except PlanWallsError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    cfg = load_config()
    try:
        plan = _walls_from_file(args.file, page=args.page,
                                layer_filter=args.layer,
                                force_raster=args.raster)
        if not plan["segments"]:
            for wmsg in plan.get("warnings") or []:
                print("warning             : %s" % wmsg, file=sys.stderr)
            print("no walls imported: %s" % plan["reason"], file=sys.stderr)
            return 2
        scaled = scale_to_area(plan, args.sq_ft,
                               use_perimeter=not args.no_perimeter,
                               isolate=args.isolate,
                               box=box, config=cfg)
        floor = floor_from_plan(scaled, args.sq_ft, ppm=args.ppm,
                                material=args.material, floor_id="f1",
                                config=cfg)
        if args.preview:
            outline = None
            if box is not None and scaled["scale_source"] == "perimeter":
                outline = (scaled.get("perimeter") or {}).get("outline")
            write_preview(args.preview, plan["segments"], box=box,
                          outline=outline)
    except (PlanWallsError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    dens = wall_density(floor)
    mc = scaled["merge_counts"]
    print("plan file           : %s (page %s)" % (args.file, plan.get("page")))
    for wmsg in plan.get("warnings") or []:
        print("warning             : %s" % wmsg)
    pu = plan.get("path_used", "vector")
    if pu == "raster":
        rr = plan.get("raster") or {}
        print("path used           : raster (confidence %.2f, Otsu %g, "
              "render %g dpi)" % (rr.get("confidence") or 0.0,
                                  rr.get("otsu_threshold") or 0.0,
                                  rr.get("dpi") or 0.0))
    else:
        print("path used           : vector")
    print("segments            : %d imported -> %d kept after box crop / "
          "sheet cleanup -> %d walls after merge "
          "(%d duplicate(s), %d collinear merge(s), %d double-line face(s), "
          "%d short dropped)"
          % (scaled.get("n_raw", mc["raw"]), scaled.get("n_kept", mc["raw"]),
             mc["walls"], mc["duplicates"], mc["collinear"],
             mc["double_line"], mc["short"]))
    bx = scaled.get("box")
    if bx:
        print("building box        : kept %d of %d segment(s) inside the box "
              "(%.0f%% of the line length)"
              % (bx.get("n_kept", 0), bx.get("n_in", 0),
                 100.0 * (bx.get("kept_share") or 0.0)))
    print("scale               : %.6g m per drawing unit (from the %s)"
          % (scaled["m_per_unit"], "perimeter trace"
             if scaled["scale_source"] == "perimeter" else "bounding box"))
    iso = scaled.get("isolate") or {}
    if iso.get("attempted"):
        dr = iso.get("dropped") or {}
        print("sheet cleanup       : kept %.0f%% of the line length (%d of %d "
              "segments); dropped frame %d, title block %d, small item(s) %d"
              % (100.0 * iso.get("kept_share", 1.0), iso.get("n_kept", 0),
                 scaled.get("n_raw", 0), dr.get("frame", 0),
                 dr.get("title_block", 0), dr.get("small_cluster", 0)))
    fl = scaled.get("flags") or []
    for w in scaled["warnings"]:
        print("warning             : %s" % w)
    print("flags               : %s"
          % (", ".join(fl) if fl else "none"))
    print("floor size          : %g x %g m at %g px/m; loss_mode=physics"
          % (floor["w_px"] / floor["ppm"], floor["h_px"] / floor["ppm"],
             floor["ppm"]))
    print("wall density        : %g m of wall per m2; %g crossings per m "
          "(mean of %d ray(s))"
          % (dens["wall_m_per_m2"], dens["crossings_per_m_mean"], dens["n_rays"]))
    od = (floor.get("plan") or {}).get("outline_density") or {}
    if od.get("area_m2"):
        print("outline density     : %g wall m per m2 inside the traced "
              "outline (%g m2); %g crossings per m inside the outline "
              "(mean of %d ray(s))"
              % (od.get("wall_m_per_m2") or 0.0, od.get("area_m2") or 0.0,
                 od.get("crossings_per_m_mean") or 0.0, od.get("n_rays") or 0))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(floor, fh, indent=1)
        print("floor JSON          : %s" % args.out)
    if args.preview:
        print("preview PNG         : %s" % args.preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
