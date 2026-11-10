"""Data-driven configuration loading for ROMtool.

Every band, wall material, cable, splitter/tap/coupler parameter, KPI preset and
report color lives in config/default.json (or a file pointed to by the
ROMTOOL_CONFIG env var). Projects can layer a `config_overrides` dict on top of
the global config; overrides deep-merge recursively. Phase 2 (LMR/FirstNet) and
phase 3 (5G NR) are additive data changes, not code changes.
"""

import copy
import json
import os

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.normpath(
    os.path.join(_MODULE_DIR, os.pardir, "config", "default.json")
)


def load_config(path=None):
    """Load the global tool config as a fresh dict (editors get their own copy)."""
    p = path or os.environ.get("ROMTOOL_CONFIG") or DEFAULT_CONFIG_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def merged_copy(base, overrides):
    """Deep-merge `overrides` into a copy of `base`.

    Dicts merge recursively (keys in overrides win, keys only in base survive).
    Any non-dict value (numbers, strings, whole lists) replaces the base value.
    """
    if isinstance(base, dict) and isinstance(overrides, dict):
        out = {}
        for key, val in base.items():
            if key in overrides:
                out[key] = merged_copy(val, overrides[key])
            else:
                out[key] = copy.deepcopy(val)
        for key, val in overrides.items():
            if key not in base:
                out[key] = copy.deepcopy(val)
        return out
    return copy.deepcopy(overrides)


def with_overrides(config, overrides=None):
    """Return config with a project's optional config_overrides applied."""
    if not overrides:
        return copy.deepcopy(config)
    return merged_copy(config, overrides)


def get_band(config, band_id):
    for band in config["bands"]:
        if band["id"] == band_id:
            return dict(band)
    raise KeyError("unknown band '%s'" % band_id)


def get_material(config, material_id):
    mats = config["wall_materials"]
    if material_id not in mats:
        raise KeyError("unknown wall material '%s'" % material_id)
    return dict(mats[material_id])


def get_cable(config, cable_id):
    cables = config["cables"]
    if cable_id not in cables:
        raise KeyError("unknown cable type '%s'" % cable_id)
    return dict(cables[cable_id])


def get_antenna_type(config, antenna_id):
    ants = config["antennas"]
    if antenna_id not in ants:
        raise KeyError("unknown antenna type '%s'" % antenna_id)
    return dict(ants[antenna_id])
