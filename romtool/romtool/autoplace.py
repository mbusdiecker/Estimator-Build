"""Automatic antenna placement for one floor.

Given a floor (canvas rectangle or an optional outline polygon), target bands,
an antenna type, a source/repeater configuration and a coverage target, place
as few antennas as practical, build the splitter/cable distribution chain for
them, and leave everything in the model so the existing cascade solver, predict
endpoint and BOM generator work on it unchanged.

Algorithm (deterministic: no randomness, fixed iteration and tie-break order)
-----------------------------------------------------------------------------
Planning runs on a coarse planning grid (at most `max_plan_points` points; the
requested step when the floor is small). For one assumed per-band antenna EIRP:

1. Seed: a hex lattice centred on the floor, with node spacing set from the
   link budget (radius where one antenna alone reaches the threshold, using the
   floor's loss model, most demanding band wins).
2. Fill: while a requirement is unmet, evaluate candidate positions (the
   worst-covered point, a sample of uncovered points, and the local centroid of
   the uncovered points around each) and add the one that newly covers the most
   uncovered points (area-fraction weighted across unmet requirements).
3. Prune: try removing antennas, fewest best-server points first, keeping every
   requirement met.

Because the antenna count decides the splitter tree and therefore each
antenna's EIRP, an outer loop builds the real chain (source -> repeater ->
splitter tree -> antennas, spatially clustered, Manhattan cable lengths), runs
the cascade solver, and re-plans with the 25th-percentile EIRP of the solved
antennas until the solved layout meets the target. The final layout is then
verified at the requested grid step with the same arithmetic predict_floor uses.

Requirements: cellular bands need `threshold_dbm` over `coverage_target_pct` of
the floor; public safety bands (config autoplace.public_safety_band_ids, or all
bands with nfpa=true) use the NFPA two-tier preset (tier 1 whole floor, tier 2
critical-area polygons, or the whole floor when none are drawn).
"""

import copy
import math
import time
from array import array

from . import config as cfgmod
from .cascade import remote_max_out_dbm, solve_cascade
from .clutter_map import ClutterField, ClutterMapError
from .lossmodel import LossModelError, resolve_loss_model, system_types
from .physics import scale_wall_loss_db
from .geometry import build_wall_index, ray_wall_crossings

AUTO_FLAG = "auto"

DEFAULTS = {
    "public_safety_band_ids": ["VHF_150", "UHF_450", "PS700", "PS800"],
    "max_antennas": 300,
    "max_outer_iterations": 6,
    "max_plan_points": 6000,
    "verify_max_points": 100000,
    "plan_margin_pct": 0.5,
    "min_radius_m": 2.0,
    "min_antenna_port_dbm": 0.0,
    "max_split_ways": 4,
    "trunk_cable_id": "coax_7_8in",
    "branch_cable_id": "coax_1_2in",
    "donor_cable_id": "lmr400",
    "donor_feeder_length_m": 2.0,
    "slack_m_per_run": 3.0,
    "floor_height_m": 4.0,
    "max_remotes": 16,
    "fiber_cable_id": "fiber_sm",
    "tap_min_gain_db": 0.5,
}

SOURCE_KEYS = ("subtype", "donor_gain_db", "feeder_loss_db", "injection_dbm",
               "ambient_dbm")
REPEATER_KEYS = ("dl_gain_db", "max_dl_out_dbm", "ports", "dl_gain_db_per_band")
REMOTE_KEYS = ("dl_gain_db", "max_dl_out_dbm", "max_dl_out_dbm_per_band", "ports")

_FILL_SAMPLES = 24
_MIN_CRIT_POINTS = 12


class AutoplaceError(Exception):
    """Bad request or an infeasible link budget; .errors lists the details."""

    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        super().__init__("; ".join(errors))
        self.errors = list(errors)


# ------------------------------------------------------------------ helpers

def _ap_cfg(config):
    out = dict(DEFAULTS)
    out.update(config.get("autoplace") or {})
    return out


def _num(value, name):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise AutoplaceError("%s must be a number, got %r" % (name, value))


def _percentile25(vals):
    if not vals:  # no auto antennas (existing ones already meet the target)
        return 0.0
    s = sorted(vals)
    return s[max(0, int(math.ceil(0.25 * len(s))) - 1)]


def _prep_poly(points):
    return [(float(p[0]), float(p[1])) for p in points]


def _inside(px, py, pts):
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        x1, y1 = pts[j]
        x2, y2 = pts[i]
        if (y2 > py) != (y1 > py):
            if px < x2 + (py - y2) * (x1 - x2) / (y1 - y2):
                inside = not inside
        j = i
    return inside


def _poly_area(pts):
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


# ---------------------------------------------------------------- grid + bands

class _Grid:
    """Compact list of evaluation points: the full canvas grid at `step`
    (identical coordinates to predict_floor), or only the points inside the
    outline polygon."""

    def __init__(self, floor, step, outline, crit_polys):
        self.step = step
        w_px, h_px = int(floor["w_px"]), int(floor["h_px"])
        self.cols = max(1, int(math.ceil(w_px / step)))
        self.rows = max(1, int(math.ceil(h_px / step)))
        self.xs = []
        self.ys = []
        for ri in range(self.rows):
            y = ri * step
            for ci in range(self.cols):
                x = ci * step
                if outline is None or _inside(x, y, outline):
                    self.xs.append(float(x))
                    self.ys.append(float(y))
        self.n = len(self.xs)
        self.crit_idx = None
        self.crit_counts = []
        if crit_polys:
            union = []
            self.crit_counts = [0] * len(crit_polys)
            for i in range(self.n):
                hit = False
                for k, poly in enumerate(crit_polys):
                    if _inside(self.xs[i], self.ys[i], poly):
                        self.crit_counts[k] += 1
                        hit = True
                if hit:
                    union.append(i)
            self.crit_idx = union
        self.max_x = (self.cols - 1) * step
        self.max_y = (self.rows - 1) * step


class _Band:
    """Per-band propagation constants for the grid floor."""

    def __init__(self, model, config, floor, band_id, env, ppm, field=None,
                 system_type=None):
        try:
            band = cfgmod.get_band(config, band_id)
        except KeyError:
            raise AutoplaceError("unknown band '%s'" % band_id)
        self.id = band_id
        self.label = band.get("label", band_id)
        self.f = float(band["freq_mhz"])
        self.const20 = 20.0 * math.log10(self.f)
        try:
            self.loss = resolve_loss_model(model, config, floor, band, env,
                                           system_type=system_type)
        except LossModelError as exc:
            raise AutoplaceError(str(exc))
        self.n = self.loss["n"]
        self.clutter = self.loss["clutter_db_per_m"]
        # clutter map: the per-square field replaces the uniform clutter term;
        # seeding uses the map mean dB/m, candidate windows the smallest class
        self.field = field if self.loss.get("clutter_map") else None
        self.clutter_min = self.clutter
        if self.field is not None:
            self.clutter = self.field.mean_db
            self.clutter_min = self.field.min_db
        self.expo = -self.n / 10.0
        wall_scale = float(band.get("wall_loss_scale_db") or 0.0)
        ref = float(config.get("wall_loss_ref_mhz", 2000.0))
        self.mats = {mid: scale_wall_loss_db(m["loss_db"], self.f, ref) + wall_scale
                     for mid, m in config["wall_materials"].items()}
        self.floor_loss = float(config.get("floor_loss_db_per_floor", 15.4))
        self.ppm = ppm
        self.band = band

    def k_lin(self, eirp, extra_db=0.0):
        """Linear mW received at d = 1 m (before the distance and clutter terms)."""
        return 10.0 ** ((eirp - self.const20 + 28.0 - extra_db) / 10.0)

    def radius_m(self, eirp, thr_dbm, clutter=None):
        """Distance (no walls) where one antenna's RSSI falls to thr_dbm."""
        clutter = self.clutter if clutter is None else clutter

        def level(d):
            return (eirp - self.const20 - self.n * math.log10(d)
                    - clutter * d + 28.0)
        if level(0.01) < thr_dbm:
            return 0.0
        lo, hi = 0.01, 3000.0
        if level(hi) >= thr_dbm:
            return hi
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if level(mid) >= thr_dbm:
                lo = mid
            else:
                hi = mid
        return lo


def _contrib(grid, b, ax, ay, eirp, extra_db, walls):
    """Linear mW contribution of one antenna at every grid point (same math as
    predict_floor: P.1238 distance term, clutter, wall crossings or floor loss)."""
    ppm = b.ppm
    k = b.k_lin(eirp, extra_db)
    expo = b.expo
    hyp = math.hypot
    xs, ys = grid.xs, grid.ys
    if b.field is not None:
        cl = b.field.loss_list(b.field.entry(ax, ay), xs, ys)
        out = []
        for x, y, c in zip(xs, ys, cl):
            d = hyp(x - ax, y - ay) / ppm
            if d < 0.01:
                d = 0.01
            out.append(k * d ** expo * 10.0 ** (-c / 10.0))
        return out
    if walls:
        out = []
        mats = b.mats
        cl = -b.clutter / 10.0
        for x, y in zip(xs, ys):
            d = hyp(x - ax, y - ay) / ppm
            if d < 0.01:
                d = 0.01
            loss = 0.0
            for w in ray_wall_crossings(walls, (ax, ay), (x, y)):
                loss += mats[w[8]["material"]]
            v = k * d ** expo * 10.0 ** (-loss / 10.0)
            if cl:
                v *= 10.0 ** (cl * d)
            out.append(v)
        return out
    if b.clutter:
        cl = -b.clutter / 10.0
        return [k * (d if d > 0.01 else 0.01) ** expo * 10.0 ** (cl * d)
                for d in (hyp(x - ax, y - ay) / ppm for x, y in zip(xs, ys))]
    return [k * (d if d > 0.01 else 0.01) ** expo
            for d in (hyp(x - ax, y - ay) / ppm for x, y in zip(xs, ys))]


class _Req:
    """One coverage requirement: a band, a threshold, an area share, a scope."""

    def __init__(self, band_id, tier, scope, thr_dbm, min_pct, idx, n_in):
        self.band = band_id
        self.tier = tier
        self.scope = scope
        self.thr_dbm = thr_dbm
        # predict_floor rounds the composite to 0.1 dB before comparing
        self.thr_lin = 10.0 ** ((thr_dbm - 0.05) / 10.0)
        self.min_pct = min_pct
        self.idx = idx            # None = whole grid, else list of grid indices
        self.n_in = n_in


def _count(req, lin_b, n):
    t = req.thr_lin
    if req.idx is None:
        return sum(map(t.__le__, lin_b))
    return sum(1 for i in req.idx if lin_b[i] >= t)


def _pct(cov, n_in):
    return round(100.0 * cov / n_in, 2) if n_in else None


# ---------------------------------------------------------- branch hardware

def _parse_branch_losses(config, raw, n_bands):
    """`branch_losses`: omitted/false (off, the default), true (counts and
    losses from config branch_hardware), or {connectors, jumpers, diplexers,
    connector_loss_db, jumper_loss_db, diplexer_loss_db}. Returns the
    per-antenna allowance and its parts."""
    if raw is None or raw is False:
        return {"enabled": False}
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        raise AutoplaceError("branch_losses must be true or an object")
    if raw.get("enabled") is False:
        return {"enabled": False}
    hw = config.get("branch_hardware") or {}
    if not hw:
        raise AutoplaceError("config has no branch_hardware block")
    allowed = ("connectors", "jumpers", "diplexers", "connector_loss_db",
               "jumper_loss_db", "diplexer_loss_db", "enabled")
    for k in raw:
        if k not in allowed:
            raise AutoplaceError("branch_losses option '%s' not allowed (use %s)"
                                 % (k, ", ".join(allowed[:-1])))
    parts, total, detail = {}, 0.0, {}
    for key in ("connector", "jumper", "diplexer"):
        spec = hw.get(key) or {}
        loss = _num(raw.get(key + "_loss_db", spec.get("loss_db", 0.0)),
                    key + "_loss_db")
        if key == "diplexer":
            default_n = (int(spec.get("per_branch", 1))
                         if n_bands > int(spec.get("when_bands_gt", 1)) else 0)
        else:
            default_n = int(spec.get("per_branch", 0))
        n = int(_num(raw.get(key + "s", default_n), key + "s"))
        if n < 0 or loss < 0:
            raise AutoplaceError("branch_losses counts and losses must be >= 0")
        parts[key + "s"] = n
        detail[key] = {"each_db": loss, "count": n, "db": round(n * loss, 4)}
        total += n * loss
    return {"enabled": True, "total_db": round(total, 4), "parts": parts,
            "detail": detail}


# ------------------------------------------------------------- tap planning

def _tap_devices(config):
    """Tap and coupler parts from config: (kind, part_id, through_il_db,
    branch_loss_db), sorted for determinism."""
    parts = config.get("parts") or {}
    out = []
    for kind, group, bkey in (("tapper", "tappers", "tap_loss_db"),
                              ("coupler", "couplers", "coupled_loss_db")):
        for pid in sorted(parts.get(group) or {}):
            d = parts[group][pid]
            try:
                out.append((kind, pid, float(d["through_il_db"]), float(d[bkey])))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _equal_loss(n, ways, spl):
    """(worst_db, best_db) device loss of the equal-splitter tree the chain
    builder makes over n antennas (same bottom-up grouping, on abstract
    nodes in index order)."""
    items = [{"x": float(i), "y": 0.0, "id": i, "wb": (0.0, 0.0)} for i in range(n)]
    while len(items) > ways:
        groups = _partition(items, int(math.ceil(len(items) / float(ways))), ways)
        nxt = []
        for grp in groups:
            if len(grp) == 1:
                nxt.append(grp[0])
                continue
            il = spl[len(grp)]
            nxt.append({"x": sum(g["x"] for g in grp) / len(grp), "y": 0.0,
                        "id": min(g["id"] for g in grp),
                        "wb": (il + max(g["wb"][0] for g in grp),
                               il + min(g["wb"][1] for g in grp))})
        items = nxt
    if len(items) == 1:
        return items[0]["wb"]
    il = spl[len(items)]
    return (il + max(g["wb"][0] for g in items), il + min(g["wb"][1] for g in items))


def _tap_table(config, ways, nmax, min_gain_db, use_taps=True):
    """Best distribution stage for a tree over n antennas, n = 1..nmax.

    Entry n is (worst_db, best_db, plan): the highest and lowest device loss
    from the tree input to any antenna (cable loss is not in the choice; the
    cascade adds it afterwards). plan is ("leaf",), ("equal",) for the
    equal-splitter tree the builder makes today, or ("tap", kind, part_id, m):
    a tap/coupler whose coupled port feeds m antennas (a sub-tree) and whose
    through port feeds the other n - m. An equal splitter costs the same loss
    per branch however many antennas hang below it, so a coupler stage wins
    when it feeds a light branch and passes most of the power on to a heavy
    one: it lowers the worst port (the budget limit) and the spread. A tap
    stage replaces the equal tree only when it lowers the worst loss by
    min_gain_db, or ties it (within 0.05 dB) and lowers the spread by
    min_gain_db; otherwise the layout is the existing equal-splitter tree.
    Deterministic: fixed iteration and tie-break order."""
    spl = {}
    for w in range(2, ways + 1):
        spl[w] = float(config["parts"]["splitters"]["%dway" % w]["il_db"])
    devs = _tap_devices(config) if use_taps else []
    tbl = [None, (0.0, 0.0, ("leaf",))]
    for n in range(2, nmax + 1):
        ew, eb = _equal_loss(n, ways, spl)
        best_s = (None, (ew, eb, ("equal",)))
        best_t = None
        for kind, pid, thr, cpl in devs:
            for m in range(1, n // 2 + 1):
                a, b = tbl[m], tbl[n - m]
                worst = max(cpl + a[0], thr + b[0])
                best = min(cpl + a[1], thr + b[1])
                key = (round(worst, 6), round(worst - best, 6), kind, pid, m)
                if best_t is None or key < best_t[0]:
                    best_t = (key, (worst, best, ("tap", kind, pid, m)))
        pick = best_s[1]
        if best_t is not None:
            sw, ss = pick[0], pick[0] - pick[1]
            tw, ts = best_t[1][0], best_t[1][0] - best_t[1][1]
            if tw <= sw - min_gain_db + 1e-9 or (
                    tw <= sw + 0.05 and ts <= ss - min_gain_db + 1e-9):
                pick = best_t[1]
        tbl.append(pick)
    return tbl


def _parse_taps(config, ap, raw, ways, nmax):
    """`taps`: omitted/false (off, the default), true, or {min_gain_db}."""
    if raw is None or raw is False:
        return {"enabled": False}
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        raise AutoplaceError("taps must be true or an object")
    if raw.get("enabled") is False:
        return {"enabled": False}
    for k in raw:
        if k not in ("min_gain_db", "enabled"):
            raise AutoplaceError("taps option '%s' not allowed (use min_gain_db)" % k)
    gain = _num(raw.get("min_gain_db", ap["tap_min_gain_db"]), "taps.min_gain_db")
    if gain < 0:
        raise AutoplaceError("taps.min_gain_db must be >= 0")
    if not _tap_devices(config):
        raise AutoplaceError("config parts has no tappers or couplers to use")
    return {"enabled": True, "min_gain_db": gain,
            "table": _tap_table(config, ways, nmax, gain),
            "equal": _tap_table(config, ways, nmax, gain, use_taps=False)}


# ------------------------------------------------------------------ requests

def _parse_remotes(config, ap, raw, feed_element_id):
    """`remotes`: omitted/false (off, the default), true (auto-size), an int
    (fixed count) or a dict {count, max_count, fiber_cable_id, dl_gain_db,
    max_dl_out_dbm, max_dl_out_dbm_per_band, ports, enabled}."""
    if raw is None or raw is False:
        return {"enabled": False}
    if raw is True:
        raw = {}
    elif isinstance(raw, int) and not isinstance(raw, bool):
        raw = {"count": raw}
    if not isinstance(raw, dict):
        raise AutoplaceError("remotes must be true, a count, or an object")
    if raw.get("enabled") is False:
        return {"enabled": False}
    if not isinstance(config.get("remote"), dict):
        raise AutoplaceError("config has no 'remote' element block")
    if feed_element_id:
        raise AutoplaceError("remotes need their own head-end: feed_element_id "
                             "cannot be combined with remotes")
    count = raw.get("count")
    if count is not None:
        count = int(_num(count, "remotes.count"))
        if count < 1:
            raise AutoplaceError("remotes.count must be >= 1")
    max_count = int(_num(raw.get("max_count", ap["max_remotes"]), "remotes.max_count"))
    if max_count < 1:
        raise AutoplaceError("remotes.max_count must be >= 1")
    fiber = raw.get("fiber_cable_id") or ap["fiber_cable_id"]
    if fiber not in config.get("cables", {}):
        raise AutoplaceError("fiber_cable_id '%s' is not in config cables" % fiber)
    element = {}
    for k in REMOTE_KEYS:
        if k in raw:
            element[k] = raw[k]
    for k in raw:
        if k not in REMOTE_KEYS + ("count", "max_count", "fiber_cable_id", "enabled"):
            raise AutoplaceError("remotes option '%s' not allowed (use %s)"
                                 % (k, ", ".join(REMOTE_KEYS + ("count", "max_count",
                                                                "fiber_cable_id"))))
    return {"enabled": True, "count": count, "max_count": max_count,
            "fiber": fiber, "element": element}


WALL_INSET_M = 0.5


def _wall_bbox_outline(floor):
    """Placement outline for a floor that has walls and no outline: the walls'
    bounding box, inset by WALL_INSET_M. None when the floor has no walls or
    the inset leaves no area."""
    walls = floor.get("walls") or []
    if not walls:
        return None
    xs = [float(w[k]) for w in walls for k in ("x1", "x2")]
    ys = [float(w[k]) for w in walls for k in ("y1", "y2")]
    inset = WALL_INSET_M * float(floor.get("ppm") or 10.0)
    x0, x1 = min(xs) + inset, max(xs) - inset
    y0, y1 = min(ys) + inset, max(ys) - inset
    if x1 <= x0 or y1 <= y0:
        return None
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _parse_request(model, config, floor, request):
    ap = _ap_cfg(config)
    req = request or {}
    bands = req.get("bands")
    if isinstance(bands, str):
        bands = [b.strip() for b in bands.split(",") if b.strip()]
    if not bands or not isinstance(bands, list):
        raise AutoplaceError("bands is required: a list of band ids to cover")
    seen, ids = set(), []
    for b in bands:
        if b not in seen:
            seen.add(b)
            ids.append(b)
    known = {b["id"] for b in config.get("bands", [])}
    for b in ids:
        if b not in known:
            raise AutoplaceError("unknown band '%s'" % b)

    ants = config.get("antennas", {})
    antenna_id = req.get("antenna_id") or ("omni_das" if "omni_das" in ants
                                           else (sorted(ants)[0] if ants else None))
    if antenna_id not in ants:
        raise AutoplaceError("unknown antenna_id '%s'" % antenna_id)

    single = (config.get("kpi_presets") or {}).get("single_tier", {})
    thr = _num(req.get("threshold_dbm", single.get("threshold_dbm", -95.0)),
               "threshold_dbm")
    pct = _num(req.get("coverage_target_pct", single.get("min_area_pct", 95.0)),
               "coverage_target_pct")
    if not 0.0 < pct <= 100.0:
        raise AutoplaceError("coverage_target_pct must be in (0, 100]")
    rsrp_offset = _num(req.get("rsrp_offset_db", 0.0), "rsrp_offset_db")
    verify_max = int(_num(req.get("verify_max_points", ap["verify_max_points"]),
                          "verify_max_points"))
    nfpa = req.get("nfpa", None)
    if nfpa not in (None, True, False):
        raise AutoplaceError("nfpa must be true, false or omitted (auto)")
    step = int(_num(req.get("step_px", req.get("step", 1)), "step_px"))
    if step < 1:
        raise AutoplaceError("step_px must be >= 1")
    cap = int(_num(req.get("max_antennas", ap["max_antennas"]), "max_antennas"))
    if cap < 1:
        raise AutoplaceError("max_antennas must be >= 1")

    src = {}
    for k, v in (req.get("source") or {}).items():
        if k not in SOURCE_KEYS:
            raise AutoplaceError("source override '%s' not allowed (use %s)"
                                 % (k, ", ".join(SOURCE_KEYS)))
        src[k] = v
    rep = {}
    for k, v in (req.get("repeater") or {}).items():
        if k not in REPEATER_KEYS:
            raise AutoplaceError("repeater override '%s' not allowed (use %s)"
                                 % (k, ", ".join(REPEATER_KEYS)))
        rep[k] = v

    cables = config.get("cables", {})
    trunk = req.get("trunk_cable_id") or ap["trunk_cable_id"]
    branch = req.get("branch_cable_id") or ap["branch_cable_id"]
    donor = req.get("donor_cable_id") or ap["donor_cable_id"]
    for nm, cid in (("trunk_cable_id", trunk), ("branch_cable_id", branch),
                    ("donor_cable_id", donor)):
        if cid not in cables:
            raise AutoplaceError("%s '%s' is not in config cables" % (nm, cid))

    parts = (config.get("parts") or {}).get("splitters", {})
    max_ways = 1
    for w in (2, 3, 4):
        if "%dway" % w in parts:
            max_ways = w
        else:
            break
    if max_ways < 2:
        raise AutoplaceError("config parts.splitters needs at least a 2way entry")
    ways = min(int(_num(req.get("max_split_ways", ap["max_split_ways"]),
                        "max_split_ways")), max_ways)
    if ways < 2:
        raise AutoplaceError("max_split_ways must be >= 2")

    outline = req.get("outline")
    if outline is None:
        outline = floor.get("outline")
    if outline:
        try:
            outline = _prep_poly(outline)
        except (TypeError, ValueError, IndexError):
            raise AutoplaceError("outline must be a list of [x, y] points")
        if len(outline) < 3:
            raise AutoplaceError("outline needs at least 3 points")
    else:
        outline = None
    if outline is None and req.get("outline") is None:
        outline = _wall_bbox_outline(floor)

    head = req.get("headend") or {}
    remotes = _parse_remotes(config, ap, req.get("remotes"), req.get("feed_element_id"))
    taps = _parse_taps(config, ap, req.get("taps"), ways, max(cap, 2))
    branch_hw = _parse_branch_losses(config, req.get("branch_losses"), len(ids))
    st = req.get("system_type") or None
    if st is not None and st not in system_types(config):
        raise AutoplaceError("unknown system_type '%s' (known: %s)"
                             % (st, ", ".join(system_types(config)) or "none"))
    return {
        "system_type": st,
        "ap": ap, "bands": ids, "antenna_id": antenna_id,
        "threshold_dbm": thr, "coverage_target_pct": pct, "nfpa": nfpa,
        "rsrp_offset_db": rsrp_offset, "verify_max": verify_max,
        "step": step, "cap": cap, "source": src, "rep": rep,
        "use_repeater": bool(req.get("use_repeater", True)),
        "trunk": trunk, "branch": branch, "donor": donor, "ways": ways,
        "outline": outline, "headend": head, "remotes": remotes, "taps": taps,
        "branch_hw": branch_hw,
        "feed_element_id": req.get("feed_element_id"),
    }


def _build_requirements(config, floor, grid, bands, p):
    """Per-band requirement list on `grid`."""
    ap = p["ap"]
    ps_ids = set(ap.get("public_safety_band_ids") or [])
    tiers = (config.get("kpi_presets") or {}).get("nfpa_two_tier", [])
    has_cas = grid.crit_idx is not None
    reqs, nfpa_bands, notes = [], [], []
    for bid in bands:
        use_nfpa = p["nfpa"] if p["nfpa"] is not None else (bid in ps_ids)
        if use_nfpa and tiers:
            nfpa_bands.append(bid)
            for ti, tier in enumerate(tiers):
                scope = tier.get("scope")
                if scope not in ("whole_floor", "critical_areas"):
                    scope = "critical_areas" if (ti > 0 and has_cas) else "whole_floor"
                idx = grid.crit_idx if (scope == "critical_areas" and has_cas) else None
                n_in = len(idx) if idx is not None else grid.n
                if n_in == 0:
                    notes.append("band %s tier %s: scope has no grid points; skipped"
                                 % (bid, tier.get("tier", ti + 1)))
                    continue
                reqs.append(_Req(bid, tier.get("tier", ti + 1), scope,
                                 float(tier.get("threshold_dbm", -95.0)),
                                 float(tier.get("min_area_pct", 95.0)), idx, n_in))
        else:
            # RSRP = composite + rsrp_offset_db, so the composite threshold is
            # threshold_dbm - rsrp_offset_db (offset 0: same domain as predict)
            reqs.append(_Req(bid, 1, "whole_floor",
                             p["threshold_dbm"] - p["rsrp_offset_db"],
                             p["coverage_target_pct"], None, grid.n))
    return reqs, nfpa_bands, notes


def _plan_step(floor, p, outline, crit_polys):
    """Planning step: requested step for small floors, coarser for big ones;
    refined until each critical polygon has enough points."""
    w_px, h_px = int(floor["w_px"]), int(floor["h_px"])
    area = _poly_area(outline) if outline else float(w_px * h_px)
    max_pts = int(p["ap"]["max_plan_points"])
    step = max(p["step"], int(math.ceil(math.sqrt(max(area, 1.0) / max(max_pts, 1)))))
    return step


def _verify_step(floor, p, outline):
    """Step for the final verification: the requested step, coarsened only
    when that grid would exceed verify_max_points (0 = never coarsen)."""
    w_px, h_px = int(floor["w_px"]), int(floor["h_px"])
    area = _poly_area(outline) if outline else float(w_px * h_px)
    limit = p["verify_max"]
    if limit and area / float(p["step"] * p["step"]) > limit:
        return max(p["step"], int(math.ceil(math.sqrt(area / limit))))
    return p["step"]


# --------------------------------------------------------------- chain build

def route_points(frm, to, axis="x"):
    """Orthogonal (L-shaped) route between two points: one leg along `axis`
    first, then the perpendicular leg. Returns [[x, y], ...] with one bend
    point (none when the points share a row/column). Points are rounded to
    0.1 px, the same resolution as element coordinates."""
    x1, y1 = round(float(frm[0]), 1), round(float(frm[1]), 1)
    x2, y2 = round(float(to[0]), 1), round(float(to[1]), 1)
    pts = [[x1, y1]]
    bend = [x2, y1] if axis == "x" else [x1, y2]
    if bend != pts[0] and bend != [x2, y2]:
        pts.append(bend)
    pts.append([x2, y2])
    return pts


def route_length_px(route):
    """Sum of the segment lengths of an orthogonal route, in px."""
    return sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(route, route[1:]))


def _partition(items, groups, ways):
    """Split items into `groups` spatially compact groups of size <= ways."""
    if groups <= 1:
        return [items]
    n = len(items)
    xs = [it["x"] for it in items]
    ys = [it["y"] for it in items]
    axis = "x" if (max(xs) - min(xs)) >= (max(ys) - min(ys)) else "y"
    other = "y" if axis == "x" else "x"
    srt = sorted(items, key=lambda it: (it[axis], it[other], it["id"]))
    g_l = groups // 2
    g_r = groups - g_l
    lo = max(g_l, n - g_r * ways)
    hi = min(g_l * ways, n - g_r)
    left = int(round(n * g_l / float(groups)))
    left = min(max(left, lo), hi)
    return (_partition(srt[:left], g_l, ways)
            + _partition(srt[left:], g_r, ways))


class _Chain:
    """Elements for one planned layout."""

    def __init__(self, floor, ppm, p, config):
        self.fid = p["_prefix"]
        self.ppm = ppm
        self.p = p
        self.slack = float(p["ap"]["slack_m_per_run"])
        self.elements = []
        self.counts = {"antenna": 0, "splitter": 0, "run": 0, "remote": 0,
                       "fiber_run": 0, "tapper": 0, "coupler": 0}
        self.tree_stats = []      # per distribution tree: (antennas, plan entry)
        self.remote_info = []
        self.fiber_m = 0.0

    def _run(self, frm, to, cable_id, fiber=False, port=None):
        route = route_points((frm["x"], frm["y"]), (to["x"], to["y"]),
                             self.p.get("_axis", "x"))
        length = round(route_length_px(route) / self.ppm + self.slack, 2)
        self.counts["run"] += 1
        if fiber:
            self.counts["fiber_run"] += 1
            self.fiber_m += length
        run = {
            "id": "%s_auto_r%d" % (self.fid, self.counts["run"]),
            "type": "cable_run", "label": "AR%d" % self.counts["run"],
            "from_id": frm["id"], "to_id": to["id"], "cable_id": cable_id,
            "length_m": length, "route": route, AUTO_FLAG: True}
        if port:
            run["port"] = port
        self.elements.append(run)
        return length

    def _cable_for(self, node):
        return self.p["branch"] if node["kind"] == "antenna" else self.p["trunk"]

    def antenna(self, x, y):
        self.counts["antenna"] += 1
        n = self.counts["antenna"]
        el = {"id": "%s_auto_a%d" % (self.fid, n), "type": "antenna",
              "label": "AP%d" % n, "antenna_id": self.p["antenna_id"],
              "x": x, "y": y, AUTO_FLAG: True}
        bh = self.p["branch_hw"]
        if bh["enabled"] and bh["total_db"]:
            el["branch_loss_db"] = bh["total_db"]
            el["branch_parts"] = dict(bh["parts"])
        self.elements.append(el)
        return {"id": el["id"], "x": x, "y": y, "kind": "antenna"}

    def splitter(self, x, y, ways):
        self.counts["splitter"] += 1
        n = self.counts["splitter"]
        el = {"id": "%s_auto_s%d" % (self.fid, n), "type": "splitter",
              "label": "ASP%d" % n, "ways": ways, "x": round(x, 1),
              "y": round(y, 1), AUTO_FLAG: True}
        self.elements.append(el)
        return {"id": el["id"], "x": el["x"], "y": el["y"], "kind": "splitter"}

    def remote(self, x, y):
        self.counts["remote"] += 1
        n = self.counts["remote"]
        el = {"id": "%s_auto_u%d" % (self.fid, n), "type": "remote",
              "label": "RU%d" % n, "x": round(x, 1), "y": round(y, 1),
              AUTO_FLAG: True}
        el.update(copy.deepcopy(self.p["remotes"]["element"]))
        self.elements.append(el)
        return {"id": el["id"], "x": el["x"], "y": el["y"], "kind": "remote"}

    def tap_device(self, kind, part_id, x, y):
        self.counts[kind] += 1
        n = self.counts[kind]
        el = {"id": "%s_auto_%s%d" % (self.fid, "t" if kind == "tapper" else "c", n),
              "type": kind, "label": ("ATP%d" if kind == "tapper" else "ACP%d") % n,
              "part_id": part_id, "x": round(x, 1), "y": round(y, 1), AUTO_FLAG: True}
        self.elements.append(el)
        return {"id": el["id"], "x": el["x"], "y": el["y"], "kind": kind}

    def tree(self, ants, feed_xy=None):
        """Distribution tree over antenna nodes; returns its root node (the
        single antenna when only one). Equal splitters, or the tap-aware plan
        when the request asks for taps."""
        taps = self.p["taps"]
        if taps["enabled"] and len(ants) > 1:
            if feed_xy is None:
                feed_xy = (sum(a["x"] for a in ants) / len(ants),
                           sum(a["y"] for a in ants) / len(ants))
            self.tree_stats.append((len(ants), taps["table"][len(ants)],
                                    taps["equal"][len(ants)]))
            return self._node(list(ants), feed_xy)
        return self.tree_equal(ants)

    def _node(self, items, feed_xy):
        n = len(items)
        if n == 1:
            return items[0]
        plan = self.p["taps"]["table"][n][2]
        if plan[0] == "equal":
            return self.tree_equal(items)
        _, kind, pid, m = plan
        fx, fy = feed_xy
        order = sorted(items, key=lambda c: (abs(c["x"] - fx) + abs(c["y"] - fy),
                                             c["x"], c["y"], c["id"]))
        dev = self.tap_device(kind, pid, order[0]["x"], order[0]["y"])
        coupled = self._node(order[:m], (dev["x"], dev["y"]))
        through = self._node(order[m:], (dev["x"], dev["y"]))
        self._run(dev, coupled, self._cable_for(coupled),
                  port="tap" if kind == "tapper" else "coupled")
        self._run(dev, through, self._cable_for(through), port="through")
        return dev

    def tree_equal(self, ants):
        """Equal-split splitter tree over antenna nodes; returns its root
        node (the single antenna when only one)."""
        ways = self.p["ways"]
        cur = list(ants)
        while len(cur) > ways:
            groups = _partition(cur, int(math.ceil(len(cur) / float(ways))), ways)
            nxt = []
            for grp in groups:
                if len(grp) == 1:
                    nxt.append(grp[0])
                    continue
                sp = self.splitter(sum(c["x"] for c in grp) / len(grp),
                                   sum(c["y"] for c in grp) / len(grp), len(grp))
                for c in grp:
                    self._run(sp, c, self._cable_for(c))
                nxt.append(sp)
            cur = nxt
        if len(cur) == 1:
            return cur[0]
        root = self.splitter(sum(c["x"] for c in cur) / len(cur),
                             sum(c["y"] for c in cur) / len(cur), len(cur))
        for c in cur:
            self._run(root, c, self._cable_for(c))
        return root

    def _head(self, headend_xy, n_remotes=0):
        """Source (+ repeater) at the head-end; returns (source element,
        the node the first run hangs from)."""
        p = self.p
        hx, hy = headend_xy
        src_el = {"id": "%s_auto_src" % self.fid, "type": "source", "label": "ASRC",
                  "x": hx, "y": hy, AUTO_FLAG: True}
        src_el.update(copy.deepcopy(p["source"]))
        self.elements.append(src_el)
        head = {"id": src_el["id"], "x": hx, "y": hy, "kind": "source"}
        if p["use_repeater"]:
            rep_el = {"id": "%s_auto_rep" % self.fid, "type": "repeater",
                      "label": "ARPTR", "x": hx, "y": hy, AUTO_FLAG: True}
            rep_el.update(copy.deepcopy(p["rep"]))
            if n_remotes and "ports" not in p["rep"]:
                rep_el["ports"] = max(n_remotes, 2)  # one fiber port per remote
            self.elements.append(rep_el)
            self.counts["run"] += 1
            self.elements.append({
                "id": "%s_auto_r%d" % (self.fid, self.counts["run"]),
                "type": "cable_run", "label": "AR%d" % self.counts["run"],
                "from_id": src_el["id"], "to_id": rep_el["id"],
                "cable_id": p["donor"],
                "length_m": float(p["ap"]["donor_feeder_length_m"]),
                AUTO_FLAG: True})
            head = {"id": rep_el["id"], "x": hx, "y": hy, "kind": "repeater"}
        return src_el, head

    def build(self, positions, headend_xy, feed):
        """Tree of splitters over `positions`. `feed` is None (build a
        source + repeater head-end) or an existing element dict to feed from."""
        p = self.p
        cur = [self.antenna(x, y) for x, y in positions]
        root = self.tree(cur, headend_xy)
        root_cable = self._cable_for(root)

        hx, hy = headend_xy
        info = {}
        if feed is not None:
            fx, fy = float(feed.get("x", hx)), float(feed.get("y", hy))
            route = route_points((fx, fy), (root["x"], root["y"]),
                                 self.p.get("_axis", "x"))
            extra = abs(int(feed["_level"]) - int(self.p["_level"])) * float(
                p["ap"]["floor_height_m"])
            self.counts["run"] += 1
            self.elements.append({
                "id": "%s_auto_r%d" % (self.fid, self.counts["run"]),
                "type": "cable_run", "label": "AR%d" % self.counts["run"],
                "from_id": feed["id"], "to_id": root["id"], "cable_id": p["trunk"],
                "length_m": round(route_length_px(route) / self.ppm + self.slack
                                  + extra, 2),
                "route": route, AUTO_FLAG: True})
            info["feed"] = feed["id"]
            return root, info

        src_el, head = self._head(headend_xy)
        self._run(head, root, root_cable)
        info["source"] = src_el["id"]
        return root, info

    def build_remotes(self, positions, headend_xy, n_remotes):
        """Head-end (source + repeater) at one location, `n_remotes` remote
        units fed by fiber, each with its own splitter tree over a zone of
        antennas. Zones come from the same recursive bisection as the
        splitter clustering; each remote sits at its zone's load centre (the
        centroid of the zone's antennas; the nearest antenna when an outline
        excludes the centroid)."""
        p = self.p
        ants = [self.antenna(x, y) for x, y in positions]
        n_remotes = max(1, min(int(n_remotes), len(ants)))
        if n_remotes == 1:
            zones = [ants]
        else:
            zones = _partition(ants, n_remotes,
                               int(math.ceil(len(ants) / float(n_remotes))))
        _, head = self._head(headend_xy, n_remotes)
        for zone in zones:
            cx = sum(a["x"] for a in zone) / len(zone)
            cy = sum(a["y"] for a in zone) / len(zone)
            ol = p["outline"]
            if ol is not None and not _inside(cx, cy, ol):
                near = min(zone, key=lambda a: ((a["x"] - cx) ** 2 + (a["y"] - cy) ** 2,
                                                a["id"]))
                cx, cy = near["x"], near["y"]
            ru = self.remote(cx, cy)
            root = self.tree(zone, (ru["x"], ru["y"]))
            self._run(ru, root, self._cable_for(root))
            fiber_len = self._run(head, ru, p["remotes"]["fiber"], fiber=True)
            self.remote_info.append({
                "element_id": ru["id"], "label": "RU%d" % self.counts["remote"],
                "x_px": ru["x"], "y_px": ru["y"],
                "x_m": round(ru["x"] / self.ppm, 2),
                "y_m": round(ru["y"] / self.ppm, 2),
                "antennas": len(zone), "fiber_length_m": fiber_len,
                "antenna_ids": [a["id"] for a in zone]})
        return head


# ------------------------------------------------------------------- planner

class _Planner:
    def __init__(self, grid, bands, reqs, eirp, fixed_lin, margin, cap, ppm,
                 outline, walls, min_radius_m):
        self.grid = grid
        self.bands = bands                      # id -> _Band
        self.reqs = reqs
        self.eirp = eirp                        # id -> uniform planning EIRP
        self.margin = margin
        self.cap = cap
        self.ppm = ppm
        self.outline = outline
        self.walls = walls
        self.lin = {bid: list(fixed_lin[bid]) for bid in bands}
        self.ants = []                          # [{"x","y","c": {bid: array}}]
        self.tgt = [min(100.0, r.min_pct + margin) for r in reqs]
        self.fill_adds = 0
        self.seed_n = 0
        self.pruned = 0
        self.min_radius_m = min_radius_m
        # coverage radius from the link budget (no walls), most demanding req
        self.radius_m = None
        self.cut_m = {}
        for r in reqs:
            b = bands[r.band]
            rad = b.radius_m(eirp[r.band], r.thr_dbm)
            cut = b.radius_m(eirp[r.band], r.thr_dbm - 10.0, clutter=b.clutter_min)
            self.radius_m = rad if self.radius_m is None else min(self.radius_m, rad)
            self.cut_m[r.band] = max(self.cut_m.get(r.band, 0.0), cut)
        self.k = {bid: bands[bid].k_lin(eirp[bid]) for bid in bands}

    # coverage bookkeeping -------------------------------------------------
    def counts(self):
        return [_count(r, self.lin[r.band], self.grid.n) for r in self.reqs]

    def met(self, counts=None):
        counts = counts if counts is not None else self.counts()
        return all(r.n_in == 0 or 100.0 * c / r.n_in >= t - 1e-9
                   for r, c, t in zip(self.reqs, counts, self.tgt))

    # antenna add/remove ---------------------------------------------------
    def add(self, x, y):
        c = {}
        for bid, b in self.bands.items():
            vals = _contrib(self.grid, b, x, y, self.eirp[bid], 0.0, self.walls)
            c[bid] = array("d", vals)
            self.lin[bid] = [a + v for a, v in zip(self.lin[bid], vals)]
        self.ants.append({"x": x, "y": y, "c": c})

    def remove(self, k):
        a = self.ants.pop(k)
        for bid in self.bands:
            self.lin[bid] = [max(0.0, v - w) for v, w in zip(self.lin[bid], a["c"][bid])]

    # seed -----------------------------------------------------------------
    def seed(self):
        g = self.grid
        if self.met():
            return
        if self.radius_m < self.min_radius_m:
            raise AutoplaceError(
                "link budget supports only a %.1f m coverage radius (minimum %.1f m): "
                "per-antenna EIRP is too low for the threshold. Raise the repeater "
                "gain / injection level or lower the split count before placing."
                % (self.radius_m, self.min_radius_m))
        r_px = self.radius_m * self.ppm
        xs = g.xs
        ys = g.ys
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)
        cx, cy = 0.5 * (minx + maxx), 0.5 * (miny + maxy)
        s = math.sqrt(3.0) * r_px
        row_h = 1.5 * r_px
        pts = []
        nrow = int(math.ceil((maxy - miny) / row_h)) + 2
        ncol = int(math.ceil((maxx - minx) / s)) + 2
        for row in range(-nrow, nrow + 1):
            y = cy + row * row_h
            if y < miny - 1e-9 or y > maxy + 1e-9:
                continue
            off = 0.5 * s if row % 2 else 0.0
            for col in range(-ncol, ncol + 1):
                x = cx + off + col * s
                if x < minx - 1e-9 or x > maxx + 1e-9:
                    continue
                if self.outline is not None and not _inside(x, y, self.outline):
                    continue
                pts.append((round(x, 1), round(y, 1)))
        if not pts:
            pts = [self._nearest_point(cx, cy)]
        pts = sorted(set(pts), key=lambda t: (t[1], t[0]))
        if len(pts) + len(self.ants) > self.cap:
            raise AutoplaceError(
                "link budget would need about %d antennas (> max_antennas %d) at a "
                "%.1f m coverage radius; raise max_antennas or the per-antenna EIRP"
                % (len(pts), self.cap, self.radius_m))
        for x, y in pts:
            self.add(x, y)
        self.seed_n = len(pts)

    def _nearest_point(self, x, y):
        g = self.grid
        best, bi = None, 0
        for i in range(g.n):
            d = (g.xs[i] - x) ** 2 + (g.ys[i] - y) ** 2
            if best is None or d < best:
                best, bi = d, i
        return (g.xs[bi], g.ys[bi])

    # fill -----------------------------------------------------------------
    def fill(self):
        g = self.grid
        stalled = False
        while len(self.ants) < self.cap:
            counts = self.counts()
            if self.met(counts):
                break
            unmet = [(r, c) for r, c, t in zip(self.reqs, counts, self.tgt)
                     if r.n_in and 100.0 * c / r.n_in < t - 1e-9]
            unc = {}
            ratio = {}
            for r, _ in unmet:
                arr = self.lin[r.band]
                thr = r.thr_lin
                idx = r.idx if r.idx is not None else range(g.n)
                lst = [i for i in idx if arr[i] < thr]
                unc[id(r)] = lst
                for i in lst:
                    q = thr / (arr[i] + 1e-30)
                    if q > ratio.get(i, 0.0):
                        ratio[i] = q
            allu = sorted(ratio)
            if not allu:
                break
            worst = max(allu, key=lambda i: (ratio[i], -i))
            stride = max(1, len(allu) // _FILL_SAMPLES)
            sample = allu[::stride][:_FILL_SAMPLES]
            if worst not in sample:
                sample.append(worst)
            r_px = max(self.radius_m * self.ppm, 1.0)
            cands, seen = [], set()
            for i in sample:
                px, py = g.xs[i], g.ys[i]
                for pt in (self._centroid(allu, px, py, r_px), (px, py)):
                    key = (round(pt[0], 1), round(pt[1], 1))
                    if key not in seen:
                        seen.add(key)
                        cands.append(key)
            best_g, best_c = -1.0, None
            for cx, cy in cands:
                gain = self._gain(cx, cy, unmet, unc)
                if gain > best_g + 1e-12:
                    best_g, best_c = gain, (cx, cy)
            if best_c is None or best_g <= 0.0:
                best_c = (g.xs[worst], g.ys[worst])
            before = sum(len(v) for v in unc.values())
            self.add(*best_c)
            self.fill_adds += 1
            after = 0
            for r, _ in unmet:
                arr = self.lin[r.band]
                thr = r.thr_lin
                idx = r.idx if r.idx is not None else range(g.n)
                after += sum(1 for i in idx if arr[i] < thr)
            if after >= before:
                stalled = True
                break
        return stalled

    def _centroid(self, allu, px, py, r_px):
        g = self.grid
        sx = sy = 0.0
        n = 0
        r2 = r_px * r_px
        for i in allu:
            dx = g.xs[i] - px
            dy = g.ys[i] - py
            if dx * dx + dy * dy <= r2:
                sx += g.xs[i]
                sy += g.ys[i]
                n += 1
        if not n:
            return (px, py)
        cx, cy = sx / n, sy / n
        if self.outline is not None and not _inside(cx, cy, self.outline):
            return (px, py)
        return (cx, cy)

    def _gain(self, ax, ay, unmet, unc):
        g = self.grid
        xs, ys = g.xs, g.ys
        ppm = self.ppm
        hyp = math.hypot
        total = 0.0
        ent = None
        for r, _ in unmet:
            b = self.bands[r.band]
            k = self.k[r.band]
            expo = b.expo
            fld = b.field
            if fld is not None and ent is None:
                ent = fld.entry(ax, ay, cache=False)
            cl = 0.0 if fld is not None else -b.clutter / 10.0
            cut_px = self.cut_m[r.band] * ppm
            arr = self.lin[r.band]
            thr = r.thr_lin
            cnt = 0
            for i in unc[id(r)]:
                dx = xs[i] - ax
                if dx > cut_px or dx < -cut_px:
                    continue
                dy = ys[i] - ay
                if dy > cut_px or dy < -cut_px:
                    continue
                d = hyp(dx, dy) / ppm
                if d < 0.01:
                    d = 0.01
                v = k * d ** expo
                if cl:
                    v *= 10.0 ** (cl * d)
                if fld is not None:
                    v *= 10.0 ** (-fld.loss_at(ent, xs[i], ys[i], d) / 10.0)
                if self.walls:
                    loss = 0.0
                    for w in ray_wall_crossings(self.walls, (ax, ay), (xs[i], ys[i])):
                        loss += b.mats[w[8]["material"]]
                    v *= 10.0 ** (-loss / 10.0)
                if arr[i] + v >= thr:
                    cnt += 1
            total += cnt / float(r.n_in)
        return total

    # prune ----------------------------------------------------------------
    def prune(self):
        if not self.ants:
            return
        g = self.grid
        lead = self.reqs[0].band if self.reqs else next(iter(self.bands))
        best_cnt = [0] * len(self.ants)
        cs = [a["c"][lead] for a in self.ants]
        for i in range(g.n):
            bk, bv = 0, -1.0
            for k, c in enumerate(cs):
                if c[i] > bv:
                    bk, bv = k, c[i]
            best_cnt[bk] += 1
        order = sorted(range(len(self.ants)), key=lambda k: (best_cnt[k], k))
        keys = [(self.ants[k]["x"], self.ants[k]["y"]) for k in order]
        for key in keys:
            k = next((j for j, a in enumerate(self.ants)
                      if (a["x"], a["y"]) == key), None)
            if k is None:
                continue
            if self._removable(k):
                self.remove(k)
                self.pruned += 1

    def _removable(self, k):
        a = self.ants[k]
        g = self.grid
        for r, t in zip(self.reqs, self.tgt):
            if not r.n_in:
                continue
            arr = self.lin[r.band]
            c = a["c"][r.band]
            thr = r.thr_lin
            cov = _count(r, arr, g.n)
            need = int(math.ceil(t * r.n_in / 100.0 - 1e-9))
            allowed = cov - need
            if allowed < 0:
                return False
            lose = 0
            idx = r.idx if r.idx is not None else range(g.n)
            for i in idx:
                v = arr[i]
                if v >= thr and v - c[i] < thr:
                    lose += 1
                    if lose > allowed:
                        return False
        return True

    def positions(self):
        return [(a["x"], a["y"]) for a in self.ants]


# ------------------------------------------------------------- orchestration

def _eirps_from_solve(sol, band_ids):
    """{band: {element_id: (eirp_dbm, in_dbm, capped)}}"""
    out = {}
    for bid in band_ids:
        bd = sol["bands"].get(bid)
        if bd is None:
            raise AutoplaceError("band '%s' was not solved" % bid)
        out[bid] = {a["element_id"]: (a["eirp_dbm"],
                                  a.get("port_uncapped_dbm", a["in_dbm"]), a["capped"])
                    for a in bd["antennas"]}
    return out


def _fixed_antennas(model, floor, base_elements):
    """Non-auto antennas anywhere in the model: [(element, floor_dict)]."""
    out = []
    for fl in model.get("floors", []):
        els = base_elements if fl is floor else fl.get("elements", [])
        for el in els:
            if el.get("type") == "antenna" and not el.get(AUTO_FLAG):
                out.append((el, fl))
    return out


def _lin_from(grid, bands, floor, fixed, eirps, auto_ants=()):
    """Composite linear power per band from fixed + auto antennas using each
    antenna's own solved EIRP. auto_ants: [(element_id, x, y)]."""
    lin = {bid: [0.0] * grid.n for bid in bands}
    walls = build_wall_index(floor.get("walls", [])) or None
    grid_level = floor.get("level", 0)
    allants = []
    for el, fl in fixed:
        same = fl is floor
        extra = 0.0
        if not same:
            extra = abs(int(fl.get("level", 0) or 0) - int(grid_level or 0))
        allants.append((el["id"], float(el.get("x", 0)), float(el.get("y", 0)),
                        same, extra))
    for aid, x, y in auto_ants:
        allants.append((aid, x, y, True, 0.0))
    for aid, x, y, same, dlevel in allants:
        for bid, b in bands.items():
            eirp = eirps[bid].get(aid)
            if eirp is None:
                continue
            extra_db = 0.0 if same else b.floor_loss * dlevel
            vals = _contrib(grid, b, x, y, eirp[0], extra_db,
                            walls if same else None)
            lin[bid] = [a + v for a, v in zip(lin[bid], vals)]
    return lin


def _fixed_lin(grid, bands, floor, fixed, eirps):
    return _lin_from(grid, bands, floor, fixed, eirps)


def _summarize(reqs, lin, grid):
    rows = {}
    ok_all = True
    for r in reqs:
        cov = _count(r, lin[r.band], grid.n)
        pct = _pct(cov, r.n_in)
        ok = bool(pct is not None and pct >= r.min_pct)
        ok_all = ok_all and ok
        rows.setdefault(r.band, []).append({
            "tier": r.tier, "scope": r.scope, "threshold_dbm": r.thr_dbm,
            "min_area_pct": r.min_pct, "n_points": r.n_in, "n_covered": cov,
            "covered_pct": pct, "pass": ok})
    return rows, ok_all


def _headend_xy(p, outline, grid, ppm):
    h = p["headend"]
    if "x_px" in h and "y_px" in h:
        return (round(_num(h["x_px"], "headend.x_px"), 1),
                round(_num(h["y_px"], "headend.y_px"), 1))
    inset = 2.0 * ppm
    if outline:
        minx = min(q[0] for q in outline)
        maxy = max(q[1] for q in outline)
    else:
        minx, maxy = 0.0, grid.max_y
    return (round(minx + inset, 1), round(max(maxy - inset, 0.0), 1))


def _stub_positions(grid, outline, ppm, n0, bands_first):
    """Lattice with about n0 nodes, used only to estimate the first EIRP."""
    xs, ys = grid.xs, grid.ys
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    area = max((maxx - minx) * (maxy - miny), 1.0)
    s = math.sqrt(2.0 * area / (math.sqrt(3.0) * max(n0, 1)))
    row_h = s * math.sqrt(3.0) / 2.0
    cx, cy = 0.5 * (minx + maxx), 0.5 * (miny + maxy)
    pts = []
    nrow = int(math.ceil((maxy - miny) / max(row_h, 1e-6))) + 1
    ncol = int(math.ceil((maxx - minx) / max(s, 1e-6))) + 1
    for row in range(-nrow, nrow + 1):
        y = cy + row * row_h
        if y < miny or y > maxy:
            continue
        off = 0.5 * s if row % 2 else 0.0
        for col in range(-ncol, ncol + 1):
            x = cx + off + col * s
            if x < minx or x > maxx:
                continue
            if outline is not None and not _inside(x, y, outline):
                continue
            pts.append((round(x, 1), round(y, 1)))
    pts = sorted(set(pts), key=lambda t: (t[1], t[0]))
    if not pts:
        pts = [(round(cx, 1), round(cy, 1))]
    return pts


def autoplace_floor(model, config, floor_id, request):
    """Place antennas on one floor and build the chain. Mutates `model`
    (previous auto-placed elements on this floor are replaced). Returns the
    result dict; raises AutoplaceError for bad input or an infeasible budget."""
    t0 = time.time()
    floor = None
    for fl in model.get("floors", []):
        if fl.get("id") == floor_id:
            floor = fl
    if floor is None:
        raise AutoplaceError("unknown floor '%s'" % floor_id)
    w_px, h_px = int(floor.get("w_px", 0)), int(floor.get("h_px", 0))
    if w_px <= 0 or h_px <= 0:
        raise AutoplaceError("floor '%s' has no grid size (w_px/h_px) set" % floor_id)
    ppm = float(floor.get("ppm", 10.0))
    if ppm <= 0:
        raise AutoplaceError("floor '%s' has an invalid ppm scale" % floor_id)
    env = model.get("environment", config.get("default_environment", "office"))
    if env not in config.get("environments", {}):
        raise AutoplaceError("unknown environment '%s'" % env)

    p = _parse_request(model, config, floor, request)
    p["_level"] = floor.get("level", 0)
    ap = p["ap"]
    warnings = []

    base_elements = [e for e in floor.get("elements", []) if not e.get(AUTO_FLAG)]
    floor["elements"] = list(base_elements)
    # ids must not collide with elements kept from an earlier (un-flagged) run
    used_ids = {el.get("id") for fl in model.get("floors", [])
                for el in (fl.get("elements", []))}
    prefix, k = "%s_auto" % floor_id, 1
    while any(str(i).startswith(prefix + "_") for i in used_ids):
        k += 1
        prefix = "%s_auto%d" % (floor_id, k)
    p["_prefix"] = prefix

    feed = None
    if p["feed_element_id"]:
        fid_ = p["feed_element_id"]
        for fl in model.get("floors", []):
            els = base_elements if fl is floor else fl.get("elements", [])
            for el in els:
                if el.get("id") == fid_ and not el.get(AUTO_FLAG):
                    feed = dict(el)
                    feed["_level"] = fl.get("level", 0)
        if feed is None:
            raise AutoplaceError("feed_element_id '%s' not found (auto-placed "
                                 "elements cannot be feed points)" % fid_)
        if feed["type"] not in ("source", "repeater", "splitter"):
            raise AutoplaceError("feed_element_id must be a source, repeater or splitter")
        used = sum(1 for fl in model.get("floors", [])
                   for e in (base_elements if fl is floor else fl.get("elements", []))
                   if e.get("type") == "cable_run" and e.get("from_id") == fid_)
        if feed["type"] == "repeater":
            free = int(float(feed.get("ports", config.get("repeater", {}).get("ports", 2)))) - used
        elif feed["type"] == "splitter":
            free = int(feed.get("ways", 2)) - used
        else:
            free = 1 - used
        if free < 1:
            raise AutoplaceError("feed element '%s' has no free output port" % fid_)

    crit = floor.get("critical_areas") or []
    crit_polys = [_prep_poly(ca.get("points", [])) for ca in crit
                  if len(ca.get("points", [])) >= 3]

    field = None
    cmap = floor.get("clutter_map")
    if cmap and len(floor.get("walls") or []) < max(1, int(config.get("building_types_min_walls", 1))):
        try:
            field = ClutterField(cmap, config, ppm)
        except ClutterMapError as exc:
            raise AutoplaceError(str(exc))
    bands = {bid: _Band(model, config, floor, bid, env, ppm, field,
                        system_type=p["system_type"]) for bid in p["bands"]}
    from .predict import _model_validity_warnings  # same validity note as predict
    for bid in p["bands"]:
        _model_validity_warnings(config, bands[bid].band, warnings)

    plan_step = _plan_step(floor, p, p["outline"], crit_polys)
    grid_plan = _Grid(floor, plan_step, p["outline"], crit_polys)
    while (plan_step > p["step"] and crit_polys
           and min(grid_plan.crit_counts or [0]) < _MIN_CRIT_POINTS):
        plan_step = max(p["step"], plan_step // 2)
        grid_plan = _Grid(floor, plan_step, p["outline"], crit_polys)
    if grid_plan.n == 0:
        raise AutoplaceError("outline contains no grid points at step %d" % plan_step)
    # trunk direction: runs travel the floor's long axis first, then the other
    p["_axis"] = ("x" if (grid_plan.max_x >= grid_plan.max_y) else "y")
    vstep = _verify_step(floor, p, p["outline"])
    grid_final = grid_plan if plan_step <= vstep else None

    reqs_plan, nfpa_bands, notes = _build_requirements(config, floor, grid_plan,
                                                       bands, p)
    warnings.extend(notes)
    if not reqs_plan:
        raise AutoplaceError("no coverage requirement could be built for this floor")

    headend_xy = _headend_xy(p, p["outline"], grid_plan, ppm)
    walls = build_wall_index(floor.get("walls", [])) or None
    area_m2 = ((_poly_area(p["outline"]) if p["outline"] else float(w_px * h_px))
               / (ppm * ppm))

    def solve_chain(ch):
        floor["elements"] = list(base_elements) + ch.elements
        sol = solve_cascade(model, config)
        if sol["errors"]:
            raise AutoplaceError(["cascade: " + e for e in sol["errors"]])
        return sol

    def min_port_dbm(ch, sol):
        ids = {e["id"] for e in ch.elements if e["type"] == "antenna"}
        vals = [a.get("port_uncapped_dbm", a["in_dbm"])
                for bid in p["bands"] for a in sol["bands"][bid]["antennas"]
                if a["element_id"] in ids]
        return min(vals) if vals else None

    def install_remotes(positions):
        """Smallest remote count (zones) whose link budget closes: every
        antenna port at or above min_antenna_port_dbm on every band. A fixed
        remotes.count skips the search. When no count closes (for example a
        head-end level that is too low), grow only while the worst port
        return the smallest count whose worst port is within 0.25 dB of the best."""
        rem = p["remotes"]
        n = len(positions)
        r_lo = min(rem["count"] or 1, n)
        r_hi = r_lo if rem["count"] else max(r_lo, min(rem["max_count"], n))
        floor_dbm = float(ap["min_antenna_port_dbm"])
        tried = []
        for r in range(r_lo, r_hi + 1):
            ch = _Chain(floor, ppm, p, config)
            ch.build_remotes(positions, headend_xy, r)
            sol = solve_chain(ch)
            worst = min_port_dbm(ch, sol)
            ch.closed = worst is None or worst >= floor_dbm - 1e-9
            ch.worst_port_dbm = worst
            if ch.closed or rem["count"]:
                return ch, sol
            tried.append((ch, sol, worst))
        best = max(t[2] for t in tried)
        ch, sol, _ = next(t for t in tried if t[2] >= best - 0.25)
        floor["elements"] = list(base_elements) + ch.elements
        return ch, sol

    def install_and_solve(positions):
        if positions and p["remotes"]["enabled"]:
            return install_remotes(positions)
        ch = _Chain(floor, ppm, p, config)
        if positions:  # none needed when existing antennas already meet the target
            ch.build(positions, headend_xy, feed)
        return ch, solve_chain(ch)

    fixed = _fixed_antennas(model, floor, base_elements)

    # --- iteration 0: stub chain to estimate per-antenna EIRP --------------
    n0 = max(1, int(math.ceil(area_m2 / 300.0)))
    stub_pos = _stub_positions(grid_plan, p["outline"], ppm, n0, None)
    ch0, sol0 = install_and_solve(stub_pos)
    eirps = _eirps_from_solve(sol0, p["bands"])
    auto_ids = [e["id"] for e in ch0.elements if e["type"] == "antenna"]
    e_plan = {bid: _percentile25([eirps[bid][a][0] for a in auto_ids])
              for bid in p["bands"]}

    margin = float(ap["plan_margin_pct"])
    history = []
    final = None
    stalled_flag = False
    outer_total = 0
    for attempt in range(3):
        best = None
        last = None
        for it in range(int(ap["max_outer_iterations"])):
            outer_total += 1
            fixed_lin = _fixed_lin(grid_plan, bands, floor, fixed, eirps)
            pl = _Planner(grid_plan, bands, reqs_plan, e_plan, fixed_lin, margin,
                          p["cap"], ppm, p["outline"], walls, float(ap["min_radius_m"]))
            pl.seed()
            stalled = pl.fill()
            pl.prune()
            positions = pl.positions()
            if not positions and not fixed:
                raise AutoplaceError("planner produced no antennas")
            if len(positions) > p["cap"]:
                positions = positions[:p["cap"]]
            ch, sol = install_and_solve(positions)
            eirps = _eirps_from_solve(sol, p["bands"])
            ids = [e["id"] for e in ch.elements if e["type"] == "antenna"]
            auto_xy = [(e["id"], e["x"], e["y"]) for e in ch.elements
                       if e["type"] == "antenna"]
            lin = _lin_from(grid_plan, bands, floor, fixed, eirps, auto_xy)
            tgt_met = True
            for r in reqs_plan:
                if r.n_in and 100.0 * _count(r, lin[r.band], grid_plan.n) / r.n_in \
                        < min(100.0, r.min_pct + margin) - 1e-9:
                    tgt_met = False
            rec = {"outer": outer_total, "n": len(ids), "e_plan": dict(e_plan),
                   "met": tgt_met, "seed": pl.seed_n, "fill": pl.fill_adds,
                   "pruned": pl.pruned, "stalled": stalled}
            history.append(rec)
            cur = {"positions": positions, "chain": ch, "sol": sol,
                   "eirps": eirps, "n": len(ids), "met": tgt_met,
                   "stats": rec, "auto_xy": auto_xy}
            last = cur
            if tgt_met and (best is None or cur["n"] <= best["n"]):
                best = cur
            new_e = {bid: _percentile25([eirps[bid][a][0] for a in ids])
                     for bid in p["bands"]}
            if tgt_met:
                if all(new_e[bid] - e_plan[bid] <= 0.5 for bid in p["bands"]):
                    break
                e_plan = new_e
            else:
                e_plan = {bid: (new_e[bid] if new_e[bid] < e_plan[bid] - 0.1
                                else e_plan[bid] - 1.0) for bid in p["bands"]}
                if stalled:
                    stalled_flag = True
                    break
        final = best or last
        if best is None:
            break
        # --- verify at the requested step with the exact predict arithmetic
        if grid_final is None:
            grid_final = _Grid(floor, vstep, p["outline"], crit_polys)
        reqs_final, _, _ = _build_requirements(config, floor, grid_final, bands, p)
        lin_f = _lin_from(grid_final, bands, floor, fixed, final["eirps"],
                          final["auto_xy"])
        rows, ok_all = _summarize(reqs_final, lin_f, grid_final)
        final["verify"] = (rows, ok_all, grid_final, reqs_final)
        if ok_all:
            break
        short = max((r["min_area_pct"] - (r["covered_pct"] or 0.0))
                    for rs in rows.values() for r in rs if not r["pass"])
        margin += max(0.5, short + 0.5)
        # restore the chain on the model for the next attempt's planning state
        e_plan = {bid: _percentile25([final["eirps"][bid][a][0]
                                      for a in [x[0] for x in final["auto_xy"]]])
                  for bid in p["bands"]}
        final["verify"] = (rows, ok_all, grid_final, reqs_final)

    if final is None:
        raise AutoplaceError("planner produced no layout")

    # leave the model on the chosen layout
    floor["elements"] = list(base_elements) + final["chain"].elements
    sol = final["sol"]
    eirps = final["eirps"]

    if "verify" not in final:
        if grid_final is None:
            grid_final = _Grid(floor, vstep, p["outline"], crit_polys)
        reqs_final, _, _ = _build_requirements(config, floor, grid_final, bands, p)
        lin_f = _lin_from(grid_final, bands, floor, fixed, eirps, final["auto_xy"])
        rows, ok_all = _summarize(reqs_final, lin_f, grid_final)
    else:
        rows, ok_all, grid_final, reqs_final = final["verify"]

    # cascade report for the target bands
    ids = [x[0] for x in final["auto_xy"]]
    min_port = float(ap["min_antenna_port_dbm"])
    min_in, capped_n = {}, {}
    for bid in p["bands"]:
        vals = [eirps[bid][a][1] for a in ids]
        min_in[bid] = round(min(vals), 2) if vals else None
        capped_n[bid] = sum(1 for a in ids if eirps[bid][a][2])
        if vals and min(vals) < min_port:
            warnings.append(
                "band %s: lowest antenna port power %.1f dBm is below the %.1f dBm "
                "budget; the head-end cannot feed this many antennas at this "
                "split depth (raise gain/injection or add a second head-end)"
                % (bid, min(vals), min_port))
        if capped_n[bid]:
            warnings.append("band %s: %d antenna port(s) capped at the antenna "
                            "maximum port power" % (bid, capped_n[bid]))
    if stalled_flag or final["stats"].get("stalled"):
        warnings.append("fill stopped: adding antennas no longer improved coverage "
                        "(unreachable points: raise EIRP or check walls)")
    if len(ids) >= p["cap"] and not ok_all:
        warnings.append("stopped at max_antennas=%d before the target was met" % p["cap"])
    if grid_final.step > p["step"]:
        warnings.append(
            "final coverage was verified on a %d px grid (requested %d px) to bound "
            "runtime; pass verify_max_points=0 to verify at the requested step"
            % (grid_final.step, p["step"]))
    if not ok_all:
        warnings.append("target not met on the verification grid")

    ant_rows = []
    for e in final["chain"].elements:
        if e["type"] != "antenna":
            continue
        ant_rows.append({
            "element_id": e["id"], "label": e["label"], "x_px": e["x"], "y_px": e["y"],
            "x_m": round(e["x"] / ppm, 2), "y_m": round(e["y"] / ppm, 2),
            "eirp_dbm": {bid: eirps[bid][e["id"]][0] for bid in p["bands"]}})

    counts = {"antennas": final["chain"].counts["antenna"],
              "splitters": final["chain"].counts["splitter"],
              "cable_runs": final["chain"].counts["run"]}
    taps_out = None
    if p["taps"]["enabled"]:
        ch = final["chain"]
        counts["tappers"] = ch.counts["tapper"]
        counts["couplers"] = ch.counts["coupler"]
        stats = ch.tree_stats
        used = {}
        for e in ch.elements:
            if e["type"] in ("tapper", "coupler"):
                key = "%ss.%s" % (e["type"], e["part_id"])
                used[key] = used.get(key, 0) + 1
        taps_out = {
            "enabled": True, "min_gain_db": p["taps"]["min_gain_db"],
            "trees": len(stats), "parts_used": used,
            "device_worst_loss_db": {
                "tapped": round(max((s[1][0] for s in stats), default=0.0), 2),
                "equal_splitters": round(max((s[2][0] for s in stats), default=0.0), 2)},
            "device_spread_db": {
                "tapped": round(max((s[1][0] - s[1][1] for s in stats), default=0.0), 2),
                "equal_splitters": round(max((s[2][0] - s[2][1] for s in stats),
                                             default=0.0), 2)},
            "note": "device losses only (cable loss is added by the cascade)"}
    remotes_out = None
    if p["remotes"]["enabled"]:
        ch = final["chain"]
        counts["remotes"] = ch.counts["remote"]
        counts["fiber_runs"] = ch.counts["fiber_run"]
        rem_el = {e["id"]: e for e in ch.elements if e["type"] == "remote"}
        units = []
        for ri in ch.remote_info:
            u = dict(ri)
            u.pop("antenna_ids", None)
            u["out_dbm"] = {bid: next((n["out_dbm"] for n in sol["bands"][bid]["nodes"]
                                       if n["element_id"] == ri["element_id"]), None)
                            for bid in p["bands"]}
            u["max_dl_out_dbm"] = {
                bid: remote_max_out_dbm(rem_el[ri["element_id"]],
                                        config.get("remote", {}), bid)
                for bid in p["bands"]}
            units.append(u)
        closed = getattr(ch, "closed", True)
        remotes_out = {
            "count": ch.counts["remote"], "fixed_count": p["remotes"]["count"],
            "fiber_m": round(ch.fiber_m, 2), "fiber_cable_id": p["remotes"]["fiber"],
            "head_end": {"x_px": headend_xy[0], "y_px": headend_xy[1]},
            "budget_closed": bool(closed),
            "worst_antenna_port_dbm": (round(ch.worst_port_dbm, 2)
                                       if getattr(ch, "worst_port_dbm", None) is not None
                                       else None),
            "units": units}
        if not closed:
            warnings.append(
                "remotes: the link budget does not close at %d remote(s) "
                "(worst antenna port %.1f dBm < %.1f dBm); raise the head-end "
                "level or the remote output, or allow more remotes"
                % (ch.counts["remote"], ch.worst_port_dbm, min_port))
    total_ants = sum(1 for fl in model.get("floors", [])
                     for el in fl.get("elements", []) if el.get("type") == "antenna")

    loss_info = {bid: {"source": bands[bid].loss["source"],
                       "building_type": bands[bid].loss["building_type"],
                       "clutter_source": bands[bid].loss["clutter_source"],
                       "system_type": bands[bid].loss["system_type"],
                       "n": bands[bid].n,
                       "clutter_db_per_m": (0.0 if bands[bid].field is not None
                                            else bands[bid].clutter),
                       "clutter_map_mean_db_per_m": (round(bands[bid].clutter, 4)
                                                     if bands[bid].field is not None else None),
                       "grade_n": bands[bid].loss["grade_n"],
                       "grade_clutter": bands[bid].loss["grade_clutter"]}
                 for bid in p["bands"]}
    coverage = {bid: (rows[bid][0]["covered_pct"] if bid in rows and rows[bid] else None)
                for bid in p["bands"]}

    return {
        "floor_id": floor_id,
        "target_met": bool(ok_all),
        "antenna_count": counts["antennas"],
        "total_antennas_on_floor": total_ants,
        "fixed_antennas_counted": sum(1 for _, _ in fixed),
        "antenna_id": p["antenna_id"],
        "targets": {"bands": p["bands"], "threshold_dbm": p["threshold_dbm"],
                    "coverage_target_pct": p["coverage_target_pct"],
                    "rsrp_offset_db": p["rsrp_offset_db"],
                    "nfpa_bands": nfpa_bands,
                    "outline": "polygon" if p["outline"] else "canvas rectangle"},
        "achieved": rows,
        "coverage_pct_by_band": coverage,
        "grid": {"step_px": p["step"], "verify_step_px": grid_final.step,
                 "n_points": grid_final.n,
                 "plan_step_px": plan_step, "plan_n_points": grid_plan.n},
        "iterations": {
            "outer": outer_total, "seed_antennas": final["stats"]["seed"],
            "fill_added": final["stats"]["fill"],
            "pruned": final["stats"]["pruned"],
            "plan_margin_pct": round(margin, 2),
            "history": history},
        "planning_eirp_dbm": {bid: round(v, 2) for bid, v in e_plan.items()},
        "antennas": ant_rows,
        "elements_added": counts,
        "routing": {"trunk_axis": p["_axis"], "bend": "L-shaped, one bend point",
                    "length_rule": "sum of segment lengths / ppm + slack_m_per_run"},
        "remotes": remotes_out,
        "taps": taps_out,
        "branch_losses": ({"enabled": True, "per_antenna_db": p["branch_hw"]["total_db"],
                           "parts_per_antenna": p["branch_hw"]["parts"],
                           "detail": p["branch_hw"]["detail"]}
                          if p["branch_hw"]["enabled"] else None),
        "headend": {"x_px": headend_xy[0], "y_px": headend_xy[1],
                    "feed_element_id": p["feed_element_id"]},
        "cascade": {"min_antenna_port_dbm": min_in,
                    "capped_antennas": capped_n,
                    "min_antenna_port_budget_dbm": min_port},
        "loss_model": loss_info,
        "warnings": warnings,
        "runtime_s": round(time.time() - t0, 2),
    }


def clear_autoplaced(floor):
    """Remove every auto-placed element from a floor; returns how many."""
    before = len(floor.get("elements", []))
    floor["elements"] = [e for e in floor.get("elements", []) if not e.get(AUTO_FLAG)]
    return before - len(floor["elements"])
