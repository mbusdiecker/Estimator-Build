"""RF link: draft an estimate definition from an RF project (phase E4, v0.7.0).

Reads a stored RF project model (floors -> elements: antennas, splitters /
couplers / tappers, cable runs, source/repeater, critical areas) and drafts
an estimate definition the estimator reviews/edits BEFORE pricing. Every
catalog mapping and allowance multiplier lives in config/rflink.json;
nothing is hard-coded here.

Draft basis (all data-driven from the model walk, no RF math re-run):
- Antenna point counts per floor -> E06 ceiling omni default card (the
  estimator swaps to E07/E08 per room), definition antenna_points for bid
  unit prices, H02 PIM per antenna.
- Cable_run lengths per cable_id -> coax footage E01 horizontal, E02
  cross-floor riser (run owned by the from-element's floor), E10 riser fire
  wrap, E03 connector ends (both ends per run), E04 jumpers at antennas and
  splitter/tap connections, D05 pathway supports proportional to footage.
- Splitter/coupler/tapper floors -> hub closets: C01 backboard, C02 rack,
  C06 bonding, C07 sleeves + firestop, E05 splitter installs.
- source/repeater floors -> the headend: B-zone build-out (+ donor-side
  A-zone rows), J-zone power feed, and the booster DC load block ->
  romtool/estimate.py's O-zone auto-sizer (Ah = runtime x load, strings
  ceil, rectifiers N+1) fed from rflink.json booster_load config.
- L-zone allowances proportional: L02 ceiling tiles per antenna point,
  L03 patches per penetration-row qty (A04/C07/D06/D07) on the same
  location.
- H-zone testing per floor/band: H01 sweep per run, H02 PIM per antenna,
  H04 benchmark + H05 acceptance per floor x solved band (per_band config),
  H06 critical-area point tests, H07 functional, H09 per carrier, H10
  as-builts. I-zone engineering lines round out the draft
  (include_engineering).

The result is a REVIEW DRAFT: returned as a definition (validated against
the labor catalog, not stored); POST /projects/{id}/draft-estimate?store=true
computes + stores it as a real estimate revision 1.
"""

import json
import os

from romtool.estimate import catalog_index, load_labor_catalog

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_MODULE_DIR)
DEFAULT_RFLINK_PATH = os.path.join(_REPO_ROOT, "config", "rflink.json")

HUB_TYPES = ("splitter", "coupler", "tapper")


class RfLinkError(ValueError):
    """Draft rejected; .errors is a list of messages (API maps to 422)."""

    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def load_rflink_config(path=None):
    p = path or os.environ.get("ROMTOOL_RFLINK") or DEFAULT_RFLINK_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------- model walk

def _floors(model):
    return [fl for fl in (model.get("floors") or []) if fl.get("id")]


def _floor_of(floors, el_id):
    for fl in floors:
        for el in fl.get("elements") or []:
            if el.get("id") == el_id:
                return fl
    return None


def _count_by_floor(model, types):
    per_floor, total = {}, 0
    for fl in _floors(model):
        n = sum(1 for el in fl.get("elements") or []
                if el.get("type") in types)
        if n:
            per_floor[fl["id"]] = n
            total += n
    return per_floor, total


def model_basis(model, config):
    """The RF-side numbers the draft is built from (returned alongside the
    draft so the estimator sees where every quantity came from)."""
    floors = _floors(model)
    antennas_per_floor, antennas_total = _count_by_floor(model, ("antenna",))
    hubs_per_floor, hubs_total = _count_by_floor(model, HUB_TYPES)
    repeaters_per_floor, _ = _count_by_floor(model, ("repeater",))
    sources_per_floor, _ = _count_by_floor(model, ("source",))
    runs = []
    cables_total = {}
    for fl in _floors(model):
        for el in fl.get("elements") or []:
            if el.get("type") != "cable_run":
                continue
            meters = float(el.get("length_m") or 0.0)
            cid = el.get("cable_id")
            if (config.get("cables", {}).get(cid) or {}).get("medium") == "fiber":
                continue  # fiber footage is not coax labor; no rflink catalog line
            cables_total[cid] = cables_total.get(cid, 0.0) + meters
            from_fl = _floor_of(floors, el.get("from_id"))
            to_fl = _floor_of(floors, el.get("to_id"))
            riser = False
            if from_fl is not None and to_fl is not None:
                riser = from_fl.get("level") != to_fl.get("level")
            runs.append({"owner_floor": fl["id"], "cable_id": cid,
                         "meters": meters, "riser": riser})
    return {
        "floors": [{"id": fl["id"], "name": fl.get("name"),
                    "level": fl.get("level")} for fl in floors],
        "antennas_per_floor": dict(sorted(antennas_per_floor.items(),
                                          key=lambda kv: str(kv[0]))),
        "antennas_total": antennas_total,
        "hub_elements_per_floor": dict(sorted(hubs_per_floor.items(),
                                              key=lambda kv: str(kv[0]))),
        "hub_elements_total": hubs_total,
        "hub_floors": sorted(hubs_per_floor, key=str),
        "headend_floors": sorted({**repeaters_per_floor, **sources_per_floor},
                                 key=str),
        "repeater_floors": sorted(repeaters_per_floor, key=str),
        "source_floors": sorted(sources_per_floor, key=str),
        "cable_runs": len(runs),
        "cable_meters_by_id": {k: round(v, 3) for k, v in
                               sorted(cables_total.items(),
                                      key=lambda kv: str(kv[0]))},
        "solved_bands": [b["id"] for b in (config.get("bands") or [])],
        "critical_areas_total": sum(len(fl.get("critical_areas") or [])
                                    for fl in floors),
    }


# ------------------------------------------------------------- draft build

def _line(cid, qty, derivation):
    return {"catalog_id": cid, "qty": qty, "derivation": derivation}


def draft_from_model(model, romtool_cfg, catalog=None, rflink_cfg=None):
    """Build the draft estimate definition + basis from an RF project model.
    Raises RfLinkError when the model cannot be drafted or when a mapped
    catalog id is missing from the labor catalog."""
    rf = rflink_cfg or load_rflink_config()
    catalog = catalog or load_labor_catalog()
    idx = catalog_index(catalog)["by_id"]
    errors = []
    basis = model_basis(model, romtool_cfg)
    floors = _floors(model)
    if not floors:
        raise RfLinkError(["project has no floors to draft from"])
    if basis["antennas_total"] <= 0:
        raise RfLinkError(["project has no antenna elements to draft from"])

    m2ft = float(rf["meters_to_feet"])
    prefix = rf.get("location_prefix", "floor_")
    cable_map = rf.get("cable_catalog_map") or {}
    riser_id = rf.get("riser_catalog_id", "E02")
    ant_id = rf.get("antenna_catalog_id", "E06")
    split_id = rf.get("splitter_install_catalog_id", "E05")
    conn_id = rf.get("connector_catalog_id", "E03")
    jump_id = rf.get("jumper_catalog_id", "E04")

    def _check(cid):
        if cid and cid not in idx:
            errors.append("rflink config maps catalog id %r which is not in"
                          " the labor catalog" % cid)

    for cid in basis["cable_meters_by_id"]:
        if cid not in cable_map:
            errors.append("cable_id %r is not mapped in rflink.json"
                          " cable_catalog_map" % cid)
    for cid in [s["catalog_id"] for s in cable_map.values()] + \
            [riser_id, ant_id, split_id, conn_id, jump_id]:
        _check(cid)
    headend_lines = rf.get("headend_lines") or []
    power_feed = rf.get("power_feed_lines") or []
    hub_lines = rf.get("hub_closet_lines") or []
    eng = rf.get("engineering_lines") or []
    ps = rf.get("pathway_support") or {}
    lz = rf.get("l_zone_allowance") or {}
    riser_wrap = rf.get("riser_fire_wrap") or {}
    tst = rf.get("testing") or {}
    _check(ps.get("catalog_id"))
    _check(lz.get("tiles_catalog_id"))
    _check(lz.get("patches_catalog_id"))
    _check(riser_wrap.get("catalog_id"))
    for spec in headend_lines + power_feed + hub_lines + eng:
        _check(spec.get("catalog_id"))
    for key in ("sweep_catalog_id", "pim_catalog_id", "benchmark_catalog_id",
                "acceptance_catalog_id", "critical_point_catalog_id",
                "functional_catalog_id", "carrier_catalog_id",
                "asbuilts_catalog_id"):
        _check(tst.get(key))
    if errors:
        raise RfLinkError(errors)

    # split cable runs into horizontal vs riser footage per owner floor
    runs = _collect_runs(model, floors)
    hub_fid = _floor_counts(model, HUB_TYPES)
    ant_fid = _floor_counts(model, ("antenna",))
    repeater_fid = _floor_counts(model, ("repeater",))
    headend_fid = _floor_counts(model, ("source", "repeater"))
    floor_ids = [fl["id"] for fl in floors]
    floor_names = {fl["id"]: fl.get("name") or fl["id"] for fl in floors}
    n_bands = len(basis["solved_bands"])
    n_floors = len(floors)
    carriers = tst.get("carriers_override") or n_bands
    per_band = bool(tst.get("per_band", True))
    per_run = float(rf.get("connectors_per_run", 2))
    j_ants = float(rf.get("jumpers_per_antenna", 1))
    j_hubs = float(rf.get("jumpers_per_splitter_or_tap", 1))
    run_count_total = len(runs)

    locations = []
    for fid in floor_ids:
        loc_id = "%s%s" % (prefix, fid)
        n_ants = int(ant_fid.get(fid, 0))
        n_hubs = int(hub_fid.get(fid, 0))
        is_headend = fid in headend_fid
        lines = []

        if n_ants:
            lines.append(_line(ant_id, n_ants,
                               "%d antenna point(s) on this floor (model"
                               " walk); ceiling omni default card" % n_ants))
        if n_hubs:
            lines.append(_line(split_id, n_hubs,
                               "%d splitter/coupler/tapper element(s)" % n_hubs))
        if fid in basis["hub_floors"]:
            for spec in hub_lines:
                lines.append(_line(spec["catalog_id"],
                                   float(spec.get("qty", 1)),
                                   spec.get("derivation") or "hub closet row"))
        if is_headend:
            if rf.get("include_donor_entry", True):
                for spec in headend_lines:
                    if spec.get("when_repeater") and not repeater_fid.get(fid):
                        continue
                    lines.append(_line(spec["catalog_id"],
                                       float(spec.get("qty", 1)),
                                       spec.get("derivation") or "headend row"))
            for spec in power_feed:
                lines.append(_line(spec["catalog_id"],
                                   float(spec.get("qty", 1)),
                                   spec.get("derivation") or "headend power row"))

        # coax footage per cable id (horizontal + riser), then supports
        floor_runs = sum(1 for r in runs if r["owner_floor"] == fid)
        floor_coax_ft = 0.0
        for cid in sorted({r["cable_id"] for r in runs if r["cable_id"] in cable_map}):
            hm = sum(r["meters"] for r in runs
                     if r["owner_floor"] == fid and not r["riser"]
                     and r["cable_id"] == cid)
            rm = sum(r["meters"] for r in runs
                     if r["owner_floor"] == fid and r["riser"]
                     and r["cable_id"] == cid)
            if hm > 0:
                ft = round(hm * m2ft, 2)
                floor_coax_ft += ft
                lines.append(_line(cable_map[cid]["catalog_id"], ft,
                                   "%s %0.2f m horizontal footage from"
                                   " cascade cable_run lengths"
                                   % (cid, hm)))
            if rm > 0:
                ft = round(rm * m2ft, 2)
                floor_coax_ft += ft
                lines.append(_line(riser_id, ft,
                                   "%s %0.2f m cross-floor riser footage"
                                   % (cid, rm)))
                if riser_wrap.get("enabled") and riser_wrap.get("catalog_id"):
                    lines.append(_line(riser_wrap["catalog_id"], ft,
                                       "2-hr riser fire wrap proportional to"
                                       " riser footage"))
        if rf.get("include_pathway_support", True) and floor_coax_ft > 0 and \
                ps.get("catalog_id"):
            fps = float(ps.get("ft_per_support", 5.0))
            lines.append(_line(ps["catalog_id"],
                               round(floor_coax_ft / fps, 2),
                               "%0.2f ft pulled coax / %s ft per support"
                               % (floor_coax_ft, fps)))
        if floor_runs:
            lines.append(_line(conn_id,
                               round(floor_runs * per_run, 2),
                               "%d cable run(s) originating on this floor x"
                               " %s connector end(s) (both ends counted"
                               " separately)" % (floor_runs, per_run)))
        n_jumps = n_ants * j_ants + n_hubs * j_hubs
        if n_jumps:
            lines.append(_line(jump_id, round(n_jumps, 2),
                               "jumpers: %d antenna(s) x %s + %d splitter/tap"
                               " connection(s) x %s"
                               % (n_ants, j_ants, n_hubs, j_hubs)))

        # L-zone allowances (proportional), before testing rows
        if rf.get("include_l_zone_allowance", True):
            if n_ants and lz.get("tiles_catalog_id"):
                lines.append(_line(lz["tiles_catalog_id"],
                                   round(n_ants * float(lz.get("tiles_per_antenna", 0)), 2),
                                   lz.get("tiles_note") or
                                   "ceiling tiles per antenna point"))
            pen_qty = sum(float(ln["qty"]) for ln in lines
                          if ln["catalog_id"] in
                          (lz.get("penetration_catalog_ids") or []))
            if pen_qty and lz.get("patches_catalog_id"):
                lines.append(_line(lz["patches_catalog_id"],
                                   round(pen_qty *
                                         float(lz.get("patches_per_penetration", 1)), 2),
                                   lz.get("patches_note") or
                                   "patch/finish allowance per penetration"
                                   " row on this location"))

        # floor-scope testing rows: sweep per run + PIM per antenna
        if floor_runs and tst.get("sweep_catalog_id"):
            lines.append(_line(tst["sweep_catalog_id"],
                               round(floor_runs * float(tst.get("sweep_per_run", 1)), 2),
                               "coax sweep per cable run on this floor"))
        if n_ants and tst.get("pim_catalog_id"):
            lines.append(_line(tst["pim_catalog_id"],
                               round(n_ants * float(tst.get("pim_per_antenna", 1)), 2),
                               "PIM test per antenna point"))
        has_scope = n_ants or n_hubs or floor_runs
        if has_scope and (tst.get("benchmark_catalog_id") or
                          tst.get("acceptance_catalog_id")):
            # per-band: this floor's line carries one floor-band pass per
            # solved band (qty = n_bands); estimate-wide total across floors
            # = n_floors x n_bands, never double-counted per floor.
            grid_qty = n_bands if per_band else 1
            grid_note = ("one per solved band on this floor (%d bands;"
                         " %d floors x %d = %d floor-band passes"
                         " estimate-wide)"
                         % (n_bands, n_floors, n_bands, n_floors * n_bands)
                         if per_band else
                         "one walk covering all %d bands" % n_bands)
            if tst.get("benchmark_catalog_id"):
                lines.append(_line(tst["benchmark_catalog_id"], grid_qty,
                                   "benchmark grid test per floor: %s"
                                   % grid_note))
            if tst.get("acceptance_catalog_id"):
                lines.append(_line(tst["acceptance_catalog_id"], grid_qty,
                                   "acceptance grid test per floor: %s"
                                   % grid_note))

        loc_out = {"id": loc_id, "name": floor_names[fid], "lines": lines}
        if is_headend and rf.get("include_power_plant", True):
            bl = rf["booster_load"]
            loc_out["power_plant"] = {
                "load_amps": float(bl["load_amps"]),
                "runtime_hours": float(bl["runtime_hours"]),
            }
        locations.append(loc_out)

    if rf.get("include_engineering", True) and eng:
        eng_lines = []
        for s in eng:
            if s.get("qty_token"):
                tokens = {"floors": n_floors, "bands": n_bands,
                          "antennas": basis["antennas_total"],
                          "runs": run_count_total}
                if s["qty_token"] not in tokens:
                    errors.append("rflink engineering qty_token %r is not a"
                                  " known token" % s["qty_token"])
                    continue
                qty = tokens[s["qty_token"]]
                base = s.get("derivation") or "engineering row"
                eng_lines.append(_line(s["catalog_id"], float(qty), base))
            else:
                eng_lines.append(_line(s["catalog_id"],
                                       float(s.get("qty", 1)),
                                       s.get("derivation") or "engineering row"))
        locations[0]["lines"].extend(eng_lines)

    # estimate-level testing rows on the first location
    if tst:
        first = locations[0]["lines"]
        crit = basis["critical_areas_total"]
        if crit and tst.get("critical_point_catalog_id"):
            first.append(_line(tst["critical_point_catalog_id"], float(crit),
                               "critical-area point tests (%d polygon(s))"
                               % crit))
        if tst.get("functional_catalog_id"):
            first.append(_line(tst["functional_catalog_id"], 1,
                               "functional test, whole system"))
        if tst.get("carrier_catalog_id") and carriers:
            first.append(_line(tst["carrier_catalog_id"], float(carriers),
                               "carrier acceptance/optimization (%s)"
                               % (tst.get("carriers_override") or
                                  "%d solved band(s) as acceptance scopes"
                                  % n_bands)))
        if tst.get("asbuilts_catalog_id"):
            first.append(_line(tst["asbuilts_catalog_id"], 1,
                               "as-builts / closeout / turnover training"))

    if errors:
        raise RfLinkError(errors)
    for loc in locations:
        if not loc["lines"]:
            raise RfLinkError(["floor %r produced no draft lines" % loc["id"]])

    # merge duplicate catalog ids within a location (sum qty, join derivations)
    for loc in locations:
        merged, out = {}, []
        for ln in loc["lines"]:
            cid = ln["catalog_id"]
            if cid in merged:
                m = merged[cid]
                m["qty"] = round(m["qty"] + ln["qty"], 4)
                m["derivation"] = m["derivation"] + "; " + ln["derivation"]
            else:
                merged[cid] = dict(ln)
                out.append(merged[cid])
        loc["lines"] = out

    antenna_points = {"%s%s" % (prefix, fid): float(ant_fid.get(fid, 0))
                      for fid in floor_ids if ant_fid.get(fid, 0) > 0}
    definition = {
        "name": "%s (RF auto-draft)" % (model.get("name") or "RF project"),
        "locations": locations,
    }
    if antenna_points:
        definition["antenna_points"] = antenna_points
    return {"definition": definition, "basis": basis, "warnings": []}


# ------------------------------------------------------------ model helpers

def _collect_runs(model, floors):
    out = []
    for fl in floors:
        for el in fl.get("elements") or []:
            if el.get("type") != "cable_run":
                continue
            from_fl = _floor_of(floors, el.get("from_id"))
            to_fl = _floor_of(floors, el.get("to_id"))
            riser = bool(from_fl and to_fl and
                         from_fl.get("level") != to_fl.get("level"))
            out.append({"owner_floor": fl["id"],
                        "cable_id": el.get("cable_id"),
                        "meters": float(el.get("length_m") or 0.0),
                        "riser": riser})
    return out


def _floor_counts(model, types):
    out = {}
    for fl in _floors(model):
        n = sum(1 for el in fl.get("elements") or []
                if el.get("type") in types)
        if n:
            out[fl["id"]] = n
    return out


def draft_for_project(project, romtool_cfg, catalog=None, rflink_cfg=None):
    """API-level wrapper: project row -> draft payload."""
    out = draft_from_model(project["model"], romtool_cfg, catalog,
                           rflink_cfg=rflink_cfg)
    out["project_id"] = project["id"]
    out["project_name"] = project["name"]
    return out
