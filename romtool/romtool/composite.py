"""Multi-band composite / best-band view (phase 3 5G NR).

Given one floor and a selection of bands (e.g. n71 600 / n41 2500 / n77
C-band), runs the existing per-band P.1238 + wall-crossing prediction for
every selected band on the same pixel grid and aggregates:

- per-band KPI (primary/single-tier preset, plus the band's full KPI block),
- composite grid: per grid point the strongest band's composite dBm (element
  wise max across the selected bands) and which band serves the point,
- best server across bands: the antenna (element id) of the serving band that
  provides the point maximum,
- union KPI: % of grid points where ANY selected band is at/above that band's
  own primary design threshold,

Note the semantics: bands do not combine into one signal (different NR
carriers are not co-channel). The composite view answers "what is the best
coverage available at this point, from which band and which antenna?" The
union KPI is therefore an OR across bands, not a linear sum.
"""

from .predict import PredictError, predict_floor


def _dedupe_band_ids(band_ids):
    """Preserve first-seen order; tolerate a comma-joined string."""
    if isinstance(band_ids, str):
        band_ids = [part.strip() for part in band_ids.split(",")]
    seen = []
    for bid in band_ids or []:
        bid = str(bid or "").strip()
        if bid and bid not in seen:
            seen.append(bid)
    return seen


def _grid_key(grid):
    return (grid["cols"], grid["rows"], grid["step_px"])


def predict_floor_multiband(model, config, floor_id, band_ids, step_px=1,
                            solve_result=None):
    """Composite view for a floor over selected bands.

    Raises PredictError on unknown floor/band, empty selection, bad grid
    geometry or cascade errors (same input contract as predict_floor).
    """
    bids = _dedupe_band_ids(band_ids)
    if not bids:
        raise PredictError(
            "no bands selected (pass a non-empty 'bands' list or comma string)")

    available = [b["id"] for b in config.get("bands", [])]
    unknown = [b for b in bids if not any(b == a for a in available)]
    if unknown:
        raise PredictError(
            "unknown band(s) %s (available: %s)"
            % (", ".join(unknown), ", ".join(available)))

    per_band = []
    for bid in bids:
        res = predict_floor(model, config, floor_id, bid, step_px=step_px,
                            solve_result=solve_result)
        per_band.append(res)
        if res["grid"]["n_points"] == 0:
            raise PredictError("floor '%s' produced an empty grid" % floor_id)
    base = per_band[0]
    key = _grid_key(base["grid"])
    for res in per_band[1:]:
        if _grid_key(res["grid"]) != key:
            raise PredictError("cannot composite bands with differing grids")
    n_pts = base["grid"]["n_points"]

    values = []
    best_band = []
    best_server = []
    union_cnt = 0
    for i in range(n_pts):
        top = None
        top_band = None
        thr_covers = set()
        for res in per_band:
            v = res["values_dbm"][i]
            if v is not None and (top is None or v > top + 1e-12):
                top = v
                top_band = res["band"]["id"]
            if v is not None and v >= res["kpi"]["threshold_dbm"] - 1e-9:
                thr_covers.add(i)
        values.append(top)
        best_band.append(top_band)
        bi = next((k for k, r in enumerate(per_band)
                   if r["band"]["id"] == top_band), 0)
        best_server.append(per_band[bi]["best_server"][i] if top is not None else "")
        if i in thr_covers:
            union_cnt += 1
    union_pct = round(100.0 * union_cnt / n_pts, 2) if n_pts else 0.0

    per_band_meta = []
    warnings = []
    seen_warn = set()
    for res in per_band:
        kpi = res["kpi"]
        finite = [v for v in res["values_dbm"] if v is not None]
        per_band_meta.append({
            "band": res["band"],
            "kpi": {
                "threshold_dbm": kpi["threshold_dbm"],
                "covered_pct": kpi["covered_pct"],
                "pass": kpi["presets"][0]["pass"],
                "presets": kpi["presets"],
            },
            "n_points": res["grid"]["n_points"],
            "min_dbm": round(min(finite), 1) if finite else None,
            "max_dbm": round(max(finite), 1) if finite else None,
        })
        for w in res.get("model_warnings") or []:
            if w not in seen_warn:
                seen_warn.add(w)
                warnings.append(w)

    thresholds = {item["kpi"]["threshold_dbm"] for item in per_band_meta}
    uniform_thr = thresholds.pop() if len(thresholds) == 1 else None

    return {
        "view": "multiband_composite",
        "project_environment": base["project_environment"],
        "n_exponent": base["n_exponent"],
        "n_exponent_source": base.get("n_exponent_source"),
        "floor": base["floor"],
        "grid": base["grid"],
        "bands": bids,
        "per_band": per_band_meta,
        "values_dbm": values,
        "best_band": best_band,
        "best_server": best_server,
        "union_kpi": {
            "n_points": n_pts,
            "n_covered": union_cnt,
            "covered_pct": union_pct,
            "threshold_dbm_per_band": [item["kpi"]["threshold_dbm"]
                                       for item in per_band_meta],
            "threshold_dbm": uniform_thr,
            "rule": "OR: a point counts when ANY selected band is at/above "
                    "that band's primary (single-tier) threshold",
            "pass": bool(union_pct >= float((config.get("kpi_presets")
                                             or {}).get("single_tier", {})
                                            .get("min_area_pct", 95.0))),
        },
        "model_warnings": warnings,
        "floor_geom": base["floor_geom"],
        "antennas": base["antennas"],
        "color_scale": base.get("color_scale") or [],
    }


def composite_heatmap_view(multi_result):
    """Shape a multiband result so report.render_heatmap_png can draw it."""
    thresholds = sorted({item["kpi"]["threshold_dbm"] for item in
                         multi_result["per_band"]})
    covered = multi_result["union_kpi"]["covered_pct"]
    uniform = thresholds[0] if len(thresholds) == 1 else min(thresholds)
    kpi = {
        "threshold_dbm": uniform,
        "covered_pct": covered,
        "presets": [{
            "pass": multi_result["union_kpi"]["pass"],
        }],
        "critical_areas": {
            "evaluated": False,
            "count": 0,
            "n_points_union": None,
            "covered_pct_union": None,
            "pass_union": None,
            "polygons": [],
        },
    }
    return {
        "grid": multi_result["grid"],
        "values_dbm": multi_result["values_dbm"],
        "band": {"id": "MULTI", "label": " + ".join(multi_result["bands"]),
                 "freq_mhz": None, "technology": "5G NR (composite)"},
        "kpi": kpi,
        "antennas": multi_result.get("antennas") or [],
        "floor_geom": multi_result.get("floor_geom") or {},
        "color_scale": multi_result.get("color_scale") or [],
    }
