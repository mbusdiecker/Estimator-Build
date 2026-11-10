"""Floor-level coverage prediction and KPI evaluation.

For a given floor and band:
- every solved DAS antenna contributes RSSI = EIRP - path loss at each floor
  grid point (step in pixels),
- path loss is ITU-R P.1238 (environment N, floor-loss term for antennas on
  other levels), plus per-material wall-crossing losses summed along the
  straight ray for same-floor antennas (ITU-R P.2040 log-domain scaling),
- composite per grid point = linear (mW) sum over co-channel antennas, and a
  best-server grid (strongest per-antenna RSSI) is exposed as well,
- KPI = % of floor grid area with composite >= threshold, evaluated for the
  single-tier and NFPA two-tier presets from config; bands opt in to the
  5G NR SS-RSRP preset (phase 3) via their config "technology" and get a
  per-class (excellent/good/fair/edge) area summary on the SS-RSRP estimate
  (composite + optional per-band ss_rsrp_offset_db).
"""

import math

from . import config as cfgmod
from .cascade import CascadeError, solve_cascade
from .geometry import build_wall_index, polygon_membership, ray_wall_crossings
from .physics import composite_dbm, itu_p1238_pl_db, scale_wall_loss_db


class PredictError(Exception):
    pass


def _floors_by_id(model):
    return {fl.get("id"): fl for fl in model.get("floors", [])}


def _has_band(config, band_id):
    return any(b["id"] == band_id for b in config.get("bands", []))


def collect_antennas(model, solve_result, band_id):
    """Solved antennas joined with their model coordinates."""
    bands = solve_result.get("bands") or {}
    if band_id not in bands:
        raise PredictError("band '%s' was not solved (available: %s)"
                           % (band_id, ", ".join(sorted(bands)) or "none"))
    ants = {a["element_id"]: a for a in bands[band_id]["antennas"]}
    out = []
    for fl in model.get("floors", []):
        for el in fl.get("elements", []):
            if el.get("type") != "antenna":
                continue
            rec = ants.get(el.get("id"))
            if rec is None:
                raise PredictError(
                    "antenna '%s' is not part of the solved cascade for band '%s'"
                    % (el.get("id"), band_id)
                )
            out.append({
                "element_id": el["id"],
                "label": el.get("label") or el["id"],
                "x_px": float(el.get("x", 0)),
                "y_px": float(el.get("y", 0)),
                "level": fl.get("level", 0),
                "floor_id": fl.get("id"),
                "eirp_dbm": rec["eirp_dbm"],
            })
    if not out:
        raise PredictError("no antennas available for prediction")
    return out


def _n_exponent_for_band(config, env, band, model_warnings):
    """N exponent for this prediction: per-band override wins, else the
    environment value. Records which source answered."""
    envs = config.get("environments", {})
    env_n = float(envs[env]["n"])
    overrides = band.get("n_exponent_overrides") or {}
    if env in overrides:
        return float(overrides[env]), "band_override"
    return env_n, "environment"


def _model_validity_warnings(config, band, model_warnings):
    """P.1238 validity check against config itu_p1238_valid_mhz."""
    valid = config.get("itu_p1238_valid_mhz")
    try:
        f_mhz = float(band["freq_mhz"])
        lo, hi = float(valid[0]), float(valid[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return
    if f_mhz < lo - 1e-9 or f_mhz > hi + 1e-9:
        model_warnings.append(
            "Band '%s' at %.1f MHz is outside the ITU-R P.1238 valid range "
            "(%.0f-%.0f MHz); N exponent / wall losses are extrapolated. "
            "Use per-band n_exponent_overrides and wall_loss_scale_db to calibrate."
            % (band.get("id"), f_mhz, lo, hi)
        )


def _tier_scope(tier, tier_index, has_critical_areas):
    """NFPA tier evaluation scope: tier 1 = whole floor; tier 2 = inside the
    critical-area polygons when they exist. A 'scope' field on the tier
    (whole_floor | critical_areas) overrides the default."""
    scope = tier.get("scope")
    if scope in ("whole_floor", "critical_areas"):
        return scope
    return "critical_areas" if (tier_index > 0 and has_critical_areas) else "whole_floor"


def _covered_summary(values, mask, threshold, n_pts):
    """Covered % over the masked subset of grid points."""
    n_in = 0
    n_cov = 0
    for v, m in zip(values, mask):
        if not m:
            continue
        n_in += 1
        if v is not None and v >= threshold - 1e-9:
            n_cov += 1
    pct = round(100.0 * n_cov / n_in, 2) if n_in else None
    return {"n_points": n_in, "n_covered": n_cov, "covered_pct": pct}


def predict_floor(model, config, floor_id, band_id, step_px=1, solve_result=None):
    """Grid prediction for one floor + band. Raises PredictError on bad input."""
    step = max(1, int(step_px or 1))

    floors = _floors_by_id(model)
    floor = floors.get(floor_id)
    if floor is None:
        raise PredictError("unknown floor '%s'" % floor_id)

    if not _has_band(config, band_id):
        raise PredictError("unknown band '%s'" % band_id)
    band = cfgmod.get_band(config, band_id)
    f_mhz = float(band["freq_mhz"])

    if solve_result is None:
        solve_result = solve_cascade(model, config)
        if solve_result["errors"]:
            raise CascadeError(solve_result["errors"])
    antennas = collect_antennas(model, solve_result, band_id)

    w_px = int(floor.get("w_px", 0))
    h_px = int(floor.get("h_px", 0))
    if w_px <= 0 or h_px <= 0:
        raise PredictError("floor '%s' has no grid size (w_px/h_px) set" % floor_id)
    ppm = float(floor.get("ppm", 10.0))
    if ppm <= 0:
        raise PredictError("floor '%s' has an invalid ppm scale" % floor_id)
    env = model.get("environment", config.get("default_environment", "office"))
    envs = config.get("environments", {})
    if env not in envs:
        raise PredictError("unknown environment '%s'" % env)
    model_warnings = []
    _model_validity_warnings(config, band, model_warnings)
    n_exp, n_source = _n_exponent_for_band(config, env, band, model_warnings)
    wall_scale_db = float(band.get("wall_loss_scale_db") or 0.0)
    floor_loss = float(config.get("floor_loss_db_per_floor", 15.4))
    ref_mhz = float(config.get("wall_loss_ref_mhz", 2000.0))
    materials = config["wall_materials"]

    grid_level = floor.get("level", 0)
    same_floor_walls = build_wall_index(floor.get("walls", []))
    mat_scaled = {}
    for mid in materials:
        mat_scaled[mid] = scale_wall_loss_db(materials[mid]["loss_db"], f_mhz, ref_mhz) + wall_scale_db

    cols = max(1, int(math.ceil(w_px / step)))
    rows = max(1, int(math.ceil(h_px / step)))
    n_pts = rows * cols
    const20 = 20.0 * math.log10(f_mhz)

    lin = [0.0] * n_pts
    best_val = [None] * n_pts
    best_id = [""] * n_pts

    for ant in antennas:
        ax, ay = ant["x_px"], ant["y_px"]
        eirp = ant["eirp_dbm"]
        aid = ant["element_id"]
        same = ant["floor_id"] == floor_id
        floor_term = 0.0
        if not same:
            dlevel = abs(int(ant["level"] or 0) - int(grid_level or 0))
            floor_term = floor_loss * dlevel
        for ri in range(rows):
            y = ri * step
            dy = y - ay
            dy2 = dy * dy
            base = ri * cols
            for ci in range(cols):
                x = ci * step
                dx = x - ax
                d_m = math.sqrt(dx * dx + dy2) / ppm
                pl = const20 + n_exp * math.log10(max(d_m, 0.01)) - 28.0
                if same:
                    for w in ray_wall_crossings(same_floor_walls, (ax, ay), (x, y)):
                        pl += mat_scaled[w[8]["material"]]
                else:
                    pl += floor_term
                r = eirp - pl
                i = base + ci
                lin[i] += 10.0 ** (r / 10.0)
                bv = best_val[i]
                if bv is None or r > bv:
                    best_val[i] = r
                    best_id[i] = aid

    values = []
    for i in range(n_pts):
        comp = 10.0 * math.log10(lin[i]) if lin[i] > 0 else None
        values.append(round(comp, 1) if comp is not None else None)
    grid_w = cols * step
    grid_h = rows * step

    # Critical-area polygons on this floor (NFPA tier 2): evaluated per
    # polygon and as a union, on the same grid coordinates as values.
    critical_areas = floor.get("critical_areas") or []
    has_cas = bool(critical_areas)
    if has_cas:
        members, per_poly = polygon_membership(
            cols, rows, step, [ca.get("points", []) for ca in critical_areas])
    else:
        members = [True] * n_pts
        per_poly = []

    # KPI: % of grid points at/above threshold, per preset.
    def covered_pct_whole(threshold):
        cnt = 0
        for v in values:
            if v is not None and v >= threshold - 1e-9:
                cnt += 1
        return round(100.0 * cnt / n_pts, 2) if n_pts else 0.0

    def polygon_row(ca, mask, threshold, min_pct):
        summary = _covered_summary(values, mask, threshold, n_pts)
        pct = summary["covered_pct"]
        return {
            "id": ca.get("id"),
            "label": ca.get("label") or ca.get("id"),
            "n_points": summary["n_points"],
            "n_covered": summary["n_covered"],
            "covered_pct": pct,
            "pass": bool(pct is not None and pct >= min_pct),
        }

    presets_meta = []
    single = config.get("kpi_presets", {}).get("single_tier", {})
    presets_meta.append({
        "preset": "single_tier",
        "name": single.get("name", "Single tier"),
        "threshold_dbm": single.get("threshold_dbm", -95.0),
        "min_area_pct": single.get("min_area_pct", 95.0),
        "scope": "whole_floor",
        "covered_pct": covered_pct_whole(single.get("threshold_dbm", -95.0)),
        "pass": False,
    })
    presets_meta[0]["pass"] = bool(
        presets_meta[0]["covered_pct"] >= float(single.get("min_area_pct", 95.0)))

    nfpa_tiers = config.get("kpi_presets", {}).get("nfpa_two_tier", [])
    tier2_eval = None
    for ti, tier in enumerate(nfpa_tiers):
        tier_no = tier.get("tier", ti + 1)
        min_pct = float(tier.get("min_area_pct", 95.0))
        entry = {
            "preset": "nfpa_two_tier",
            "name": "Tier %s (%s)" % (tier_no, tier.get("name", "")),
            "tier": tier_no,
            "threshold_dbm": tier.get("threshold_dbm", -95.0),
            "min_area_pct": tier.get("min_area_pct", 95.0),
        }
        scope = _tier_scope(tier, ti, has_cas)
        if scope == "critical_areas":
            union_summary = _covered_summary(
                values, members, float(entry["threshold_dbm"]), n_pts)
            if union_summary["n_points"] == 0:
                entry["scope"] = scope
                entry["covered_pct"] = None
                entry["pass"] = None
                entry["note"] = "critical areas cover no grid points at this step"
                presets_meta.append(entry)
                continue
            entry["scope"] = scope
            entry["n_points"] = union_summary["n_points"]
            entry["n_covered"] = union_summary["n_covered"]
            entry["covered_pct"] = union_summary["covered_pct"]
            entry["pass"] = bool(union_summary["covered_pct"] >= min_pct)
            entry["per_polygon"] = [
                polygon_row(ca, mask, float(entry["threshold_dbm"]), min_pct)
                for ca, mask in zip(critical_areas, per_poly)
            ]
            if entry["tier"] == 2 or ti == len(nfpa_tiers) - 1:
                tier2_eval = entry
        else:
            entry["scope"] = "whole_floor"
            entry["covered_pct"] = covered_pct_whole(float(entry["threshold_dbm"]))
            entry["pass"] = bool(entry["covered_pct"] >= min_pct)
            if tier_no == 2 and not has_cas:
                entry["note"] = ("no critical-area polygons on this floor; "
                                 "tier 2 evaluated over the whole floor")
        presets_meta.append(entry)

    # 5G NR SS-RSRP preset (phase 3): evaluated only for bands that opt in
    # with a "technology" field matching the preset's. Threshold KPI and the
    # per-class area summary run on the SS-RSRP estimate = composite grid
    # shifted by the optional per-band ss_rsrp_offset_db (0 when unset).
    nr_preset = (config.get("kpi_presets") or {}).get("nr_ssrsp") or None
    if nr_preset and band.get("technology") == nr_preset.get("technology", "5G NR"):
        rsrp_offset = float(band.get("ss_rsrp_offset_db") or 0.0)
        rsrp_values = [None if v is None else v + rsrp_offset for v in values]

        def covered_rsrp(threshold):
            cnt = 0
            for v in rsrp_values:
                if v is not None and v >= threshold - 1e-9:
                    cnt += 1
            return round(100.0 * cnt / n_pts, 2) if n_pts else 0.0

        nr_thr = float(nr_preset.get("threshold_dbm", -95.0))
        nr_min = float(nr_preset.get("min_area_pct", 95.0))
        # Disjoint planning buckets from the class table: the top class owns
        # everything at/above its min; each lower class is bounded above by
        # the next class's min; anything below the lowest min is "none".
        ordered = sorted(
            ((c.get("name", "class"), float(c.get("min_dbm", -999.0)))
             for c in (nr_preset.get("class_table") or [])),
            key=lambda t: -t[1])
        class_pct = {}
        for ci, (cname, cmin) in enumerate(ordered):
            hi = ordered[ci - 1][1] if ci > 0 else None
            cnt = 0
            for v in rsrp_values:
                if v is None:
                    continue
                if v < cmin - 1e-9:
                    continue
                if hi is not None and v >= hi - 1e-9:
                    continue
                cnt += 1
            class_pct[cname] = round(100.0 * cnt / n_pts, 2) if n_pts else 0.0
        if ordered:
            floor_db = ordered[-1][1]
            class_pct["none"] = round(100.0 * sum(
                1 for v in rsrp_values if v is not None and v < floor_db - 1e-9
            ) / n_pts, 2) if n_pts else 0.0
        nr_entry = {
            "preset": "nr_ssrsp",
            "name": nr_preset.get("name", "5G NR SS-RSRP"),
            "technology": nr_preset.get("technology", "5G NR"),
            "ss_rsrp_offset_db": rsrp_offset,
            "threshold_dbm": nr_thr,
            "min_area_pct": nr_min,
            "scope": "whole_floor",
            "rsrp_class_pct": class_pct,
            "class_table": [{"name": nm, "min_dbm": mn} for nm, mn in ordered],
        }
        nr_entry["covered_pct"] = covered_rsrp(nr_thr)
        nr_entry["pass"] = bool(nr_entry["covered_pct"] >= nr_min)
        presets_meta.append(nr_entry)

    primary = presets_meta[0]

    result = {
        "project_environment": env,
        "n_exponent": n_exp,
        "n_exponent_source": n_source,
        "wall_loss_scale_db": wall_scale_db,
        "model_warnings": model_warnings,
        "floor": {"id": floor_id, "name": floor.get("name"), "level": grid_level,
                  "ppm": ppm, "w_px": w_px, "h_px": h_px},
        "band": {"id": band_id, "label": band.get("label", band_id), "freq_mhz": f_mhz,
                 "technology": band.get("technology")},
        "grid": {"step_px": step, "cols": cols, "rows": rows,
                 "w_px": grid_w, "h_px": grid_h, "n_points": n_pts},
        "values_dbm": values,
        "best_server": best_id,
        "antennas": [{"element_id": a["element_id"], "label": a["label"],
                      "floor_id": a["floor_id"], "level": a["level"],
                      "x_px": a["x_px"], "y_px": a["y_px"],
                      "eirp_dbm": a["eirp_dbm"]} for a in antennas],
        "kpi": {
            "threshold_dbm": primary["threshold_dbm"],
            "covered_pct": primary["covered_pct"],
            "presets": presets_meta,
            "critical_areas": {
                "evaluated": has_cas,
                "count": len(critical_areas),
                "n_points_union": tier2_eval["n_points"] if tier2_eval else None,
                "covered_pct_union": tier2_eval["covered_pct"] if tier2_eval else None,
                "pass_union": tier2_eval["pass"] if tier2_eval else None,
                "polygons": (tier2_eval or {}).get("per_polygon") or [],
            },
        },
        "floor_geom": {"id": floor_id, "walls": floor.get("walls", []), "step_px": step,
                       "critical_areas": critical_areas},
        "color_scale": config.get("report", {}).get("color_scale", []),
    }
    return result
