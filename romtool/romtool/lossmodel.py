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

Path loss then is
    PL = 20*log10(f_MHz) + N*log10(d_m) + clutter_db_per_m * d_m - 28
         (+ floor loss for other levels, + wall crossings when walls drawn)
"""

DEFAULT_MIN_WALLS = 1


class LossModelError(Exception):
    pass


def building_types(config):
    bt = config.get("building_types") or {}
    return {k: v for k, v in bt.items() if isinstance(v, dict)}


def active_building_type(model, floor):
    """Floor building_type wins over the project value; None when unset."""
    bt = (floor or {}).get("building_type") or (model or {}).get("building_type")
    return bt or None


def min_walls(config):
    try:
        return max(0, int(config.get("building_types_min_walls", DEFAULT_MIN_WALLS)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_WALLS


def resolve_loss_model(model, config, floor, band, env):
    """Return the loss-model parameters for this floor and band.

    `env` is the already-validated project environment id. Raises
    LossModelError for an unknown building type.
    """
    envs = config.get("environments", {})
    overrides = band.get("n_exponent_overrides") or {}
    n_walls = len(floor.get("walls") or [])
    bt_id = active_building_type(model, floor)
    types = building_types(config)

    if bt_id is not None and bt_id not in types:
        raise LossModelError("unknown building_type '%s' (known: %s)"
                             % (bt_id, ", ".join(sorted(types)) or "none"))

    no_walls = n_walls < max(1, min_walls(config))
    # a stored clutter map replaces the uniform clutter term (walls drawn win)
    cmap = (floor or {}).get("clutter_map") if no_walls else None
    use_bt = bt_id is not None and no_walls
    if use_bt:
        entry = types[bt_id]
        if bt_id in overrides:
            n, src = float(overrides[bt_id]), "building_type_band_override"
        else:
            n, src = float(entry["n"]), "building_type"
        clutter = float(entry.get("clutter_db_per_m") or 0.0)
        res = {
            "source": "building_type",
            "n": n,
            "n_source": src,
            "clutter_db_per_m": clutter,
            "building_type": bt_id,
            "grade_n": entry.get("n_grade"),
            "grade_clutter": entry.get("clutter_grade"),
            "reference_n": entry.get("n_ref"),
            "clutter_map": None,
        }
        if cmap:
            res.update({"source": "clutter_map", "clutter_db_per_m": 0.0,
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
        "building_type": bt_id,
        "grade_n": None,
        "grade_clutter": "E" if cmap else None,
        "reference_n": None,
        "clutter_map": cmap or None,
    }
