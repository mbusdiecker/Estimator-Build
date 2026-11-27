"""Per-AHJ profile loader (owner build request 2026-11-26). A profile is a
small jurisdiction key in ps_ahj_profiles.json; the explicit CLI arguments
always override profile values. Falls back to ifc_default-style numbers when
neither a profile nor an explicit value is given (with a note).
"""
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "ps_ahj_profiles.json")
FALLBACKS = {"arrival_margin_db": 18.0, "noise_rule": "la",
             "near_operating_margin_db": 5.0, "near_damage_margin_db": 10.0,
             "sinr_threshold_db": 18.0, "delay_limit_us": 15.0,
             "composite_cap_dbm": 37.0, "ul_erp_donor_cap_dbm": 24.0,
             "squelch_offset_db": 5.0}

def load_profiles(path=None):
    with open(path or DEFAULT_PATH, encoding="utf-8") as f:
        return json.load(f)["profiles"]

def resolve(profile_name, asks, path=None):
    """asks: {'arg_name': (profile_key, fallback_key, cli_value_or_None)}.
    Returns (values dict, provenance dict, profile_dict)."""
    profiles = load_profiles(path)
    name = profile_name or "ifc_default"
    if name not in profiles:
        names = ", ".join(sorted(profiles))
        raise SystemExit("unknown AHJ profile '%s' (known: %s)" % (name, names))
    prof = profiles[name]
    values, where = {}, {}
    for arg, (key, fb, cli) in asks.items():
        if cli is not None:
            values[arg] = cli
            where[arg] = "explicit CLI"
        elif prof.get(key) is not None:
            values[arg] = prof[key]
            where[arg] = "profile %s" % name
        else:
            values[arg] = FALLBACKS[fb] if fb in FALLBACKS else cli
            where[arg] = "generic fallback"
    return values, where, prof

def split_db(ch):
    return 10.0 * math.log10(ch)
