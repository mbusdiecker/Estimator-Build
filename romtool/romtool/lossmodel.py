"""Indoor propagation model selection for a floor + band.

Three sources, in priority order (a stored floor clutter map, when there are
no walls, replaces the uniform clutter term; see clutter_map.py):

1. walls_drawn   - the floor has at least `building_types_min_walls` drawn walls
                   (default 1): the project environment's N (per-band override
                   wins) plus per-wall crossing losses, exactly as before.
2. building_type - no walls drawn and a building type is set (floor value, else
                   project value): the building type's N and optional
                   per-metre clutter loss (dB/m) from config `building_types`.
                   A band `n_exponent_overrides[<building_type>]` entry wins.
3. environment   - no walls, no building type: the project environment's N,
                   as before.

System type (optional; autoplace request, else floor, else project) swaps the
building type's uniform clutter for clutter_by_system[<system>] when that
building type has a value for it. N is unchanged. Unknown system types raise.

Path loss then is
    PL = 20*log10(f_MHz) + N*log10(d_m) + clutter_db_per_m * d_m - 28
         (+ floor loss for other levels, + wall crossings when walls drawn)

Zones (zones.py) apply to the no-walls case only. A floor with zones gets its
clutter map with the zone classes applied (or a zones-only grid when it has
no stored map); the building type clutter fills squares outside every zone.
Zone ceiling heights change the distance term in predict and autoplace, not N.
"""

from .physics import scale_wall_loss_db
from .zones import effective_clutter_map

DEFAULT_MIN_WALLS = 1
LOSS_MODES = ("calibrated", "physics")


def active_loss_mode(model, floor):
    """'physics' or 'calibrated' (default). Floor value wins over the project."""
    m = (floor or {}).get("loss_mode") or (model or {}).get("loss_mode") or "calibrated"
    if m not in LOSS_MODES:
        raise LossModelError("unknown loss_mode '%s' (known: %s)" % (m, ", ".join(LOSS_MODES)))
    return m


def _physics(config, cmap, band):
    """Physics mode: free-space distance exponent plus measured wall crossings x
    material loss. No fitted values."""
    ph = config.get("physics_loss") or {}
    mats = config.get("wall_materials") or {}
    mat = ph.get("wall_material", "drywall")
    if mat not in mats:
        raise LossModelError("physics_loss.wall_material '%s' is not in wall_materials" % mat)
    if not cmap or not cmap.get("cpm"):
        raise LossModelError("loss_mode physics needs a clutter map built from the floor plan "
                             "(it carries the measured wall crossings per metre)")
    ref = float(config.get("wall_loss_ref_mhz", 2000.0))
    wall_db = scale_wall_loss_db(float(mats[mat]["loss_db"]), float(band["freq_mhz"]), ref)
    return {
        "source": "physics", "n": float(ph.get("n", 20.0)), "n_source": "physics_loss",
        "clutter_db_per_m": 0.0, "clutter_source": "physics_walls", "clutter_ref": ph.get("ref"),
        "grade_n": ph.get("n_grade"), "grade_clutter": ph.get("wall_grade"),
        "reference_n": ph.get("n_ref"), "clutter_map": cmap,
        "physics_wall_db": round(wall_db, 4), "physics_wall_material": mat,
    }


class LossModelError(Exception):
    pass


def _bt_clutter_value(entry, system_type):
    """Building type clutter dB/m, with the system-type value when it has one."""
    by_system = entry.get("clutter_by_system") or {}
    if system_type is not None and system_type in by_system:
        return float(by_system[system_type])
    return float(entry.get("clutter_db_per_m") or 0.0)


def floor_clutter_map(model, config, floor, system_type=None):
    """Clutter map used when the floor has no walls: the stored map with zones
    applied, a zones-only grid, or None. Squares outside every zone fall back to
    the building type clutter (0 when no building type is set)."""
    bt_id = active_building_type(model, floor)
    st = active_system_type(model, floor, system_type)
    types = building_types(config)
    fallback_db = _bt_clutter_value(types[bt_id], st) if bt_id in types else 0.0
    return effective_clutter_map(floor or {}, config, fallback_db)


def building_types(config):
    bt = config.get("building_types") or {}
    return {k: v for k, v in bt.items() if isinstance(v, dict)}


def active_building_type(model, floor):
    """Floor building_type wins over the project value; None when unset."""
    bt = (floor or {}).get("building_type") or (model or {}).get("building_type")
    return bt or None


def system_types(config):
    return [str(s) for s in (config.get("system_types") or [])]


def active_system_type(model, floor, request=None):
    """Request value wins, then floor, then project value; None when unset."""
    st = request or (floor or {}).get("system_type") or (model or {}).get("system_type")
    return st or None


def min_walls(config):
    try:
        return max(0, int(config.get("building_types_min_walls", DEFAULT_MIN_WALLS)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_WALLS


def resolve_loss_model(model, config, floor, band, env, system_type=None):
    """Return the loss-model parameters for this floor and band.

    `env` is the already-validated project environment id. `system_type` is an
    optional request-level override; without it the floor value, then the
    project value, applies. Raises LossModelError for an unknown building type
    or system type.
    """
    envs = config.get("environments", {})
    overrides = band.get("n_exponent_overrides") or {}
    n_walls = len(floor.get("walls") or [])
    bt_id = active_building_type(model, floor)
    types = building_types(config)
    st = active_system_type(model, floor, system_type)
    known_systems = system_types(config)

    if bt_id is not None and bt_id not in types:
        raise LossModelError("unknown building_type '%s' (known: %s)"
                             % (bt_id, ", ".join(sorted(types)) or "none"))
    if st is not None and st not in known_systems:
        raise LossModelError("unknown system_type '%s' (known: %s)"
                             % (st, ", ".join(known_systems) or "none"))

    no_walls = n_walls < max(1, min_walls(config))
    # a stored clutter map (with zones applied) replaces the uniform clutter
    # term; walls drawn win. Squares outside every zone fall back to the
    # building type clutter when the grid is built from zones alone.
    cmap = floor_clutter_map(model, config, floor, system_type=st) if no_walls else None
    if no_walls and active_loss_mode(model, floor) == "physics":
        res = _physics(config, cmap, band)
        res.update({"system_type": st, "building_type": bt_id})
        return res
    use_bt = bt_id is not None and no_walls
    if use_bt:
        entry = types[bt_id]
        if bt_id in overrides:
            n, src = float(overrides[bt_id]), "building_type_band_override"
        else:
            n, src = float(entry["n"]), "building_type"
        clutter = float(entry.get("clutter_db_per_m") or 0.0)
        grade_clutter = entry.get("clutter_grade")
        clutter_ref = entry.get("clutter_ref")
        clutter_source = "building_type"
        by_system = entry.get("clutter_by_system") or {}
        if st is not None and st in by_system:
            clutter = float(by_system[st])
            grade_clutter = entry.get("clutter_by_system_grade")
            clutter_ref = entry.get("clutter_by_system_ref")
            clutter_source = "building_type_system"
        res = {
            "source": "building_type",
            "n": n,
            "n_source": src,
            "clutter_db_per_m": clutter,
            "clutter_source": clutter_source,
            "clutter_ref": clutter_ref,
            "system_type": st,
            "building_type": bt_id,
            "grade_n": entry.get("n_grade"),
            "grade_clutter": grade_clutter,
            "reference_n": entry.get("n_ref"),
            "clutter_map": None,
        }
        if cmap:
            res.update({"source": "clutter_map", "clutter_db_per_m": 0.0,
                        "clutter_source": "clutter_map", "clutter_ref": None,
                        "grade_clutter": "E", "clutter_map": cmap})
        return res

    env_n = float(envs[env]["n"])
    if env in overrides:
        n, src = float(overrides[env]), "band_override"
    else:
        n, src = env_n, "environment"
    return {
        "source": ("walls_drawn" if n_walls else
                   ("clutter_map" if cmap else "environment")),
        "n": n,
        "n_source": src,
        "clutter_db_per_m": 0.0,
        "clutter_source": "clutter_map" if cmap else None,
        "clutter_ref": None,
        "system_type": st,
        "building_type": bt_id,
        "grade_n": None,
        "grade_clutter": "E" if cmap else None,
        "reference_n": None,
        "clutter_map": cmap or None,
    }
