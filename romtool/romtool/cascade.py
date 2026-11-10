"""DAS cascade solver.

Chain topology (phase 1, off-air LTE):
    source (donor antenna + feeder budget) -> repeater -> trunk cable ->
    splitters/taps/couplers -> branch cables -> antennas.

The graph is built from element records in the project model:
- Nodes:  source, repeater, splitter, tapper, coupler, antenna.
- Edges:  cable_run elements ({from_id, to_id, cable_id, length_m}, optional
  "port" role for tap devices: "through" | "tap" | "coupled").

Everything numeric (band frequencies/ambient levels, cable loss tables,
splitter/tap/coupler IL, repeater gain/max-out/ports, antenna gain/port cap)
comes from the data-driven config. Phase 2/3 bands slot in without code
changes.

Outputs per band:
- per-node/edge power trace (in/out dBm)
- per-antenna port power and EIRP (port power capped at the antenna port max)
- validation errors (fatal, unsolvable) and warnings (e.g. loss budget < 0 dBm)
"""

import math
from collections import deque

CABLE_TYPE = "cable_run"
NODE_TYPES = ("source", "repeater", "splitter", "tapper", "coupler", "antenna")
HUB_TYPES = ("source", "repeater", "splitter", "tapper", "coupler")
PORT_THROUGH = "through"
PORT_TAP = "tap"
PORT_COUPLED = "coupled"


class CascadeError(Exception):
    """Raised when a project cannot be solved; .errors carries the details."""

    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


def element_label(el):
    return el.get("label") or el.get("id") or "?"


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def cable_loss_db(cable_cfg, length_m, f_mhz):
    """Cable loss (dB) for a length in meters at f_mhz.

    loss_db_per_100m_ghz holds dB/100m anchors; between anchors we interpolate
    linearly in the log-frequency domain (same domain as the P.2040 wall-loss
    scaling). Outside the anchor range the value is clamped to the nearest
    anchor (conservative, and exact at the anchors themselves).
    """
    anchors = sorted(
        (float(g) * 1000.0, float(db))
        for g, db in cable_cfg["loss_db_per_100m_ghz"].items()
    )
    if not anchors:
        raise ValueError("cable has no loss anchors")
    if len(anchors) == 1:
        per_100 = anchors[0][1]
    else:
        f = float(f_mhz)
        if f <= anchors[0][0]:
            per_100 = anchors[0][1]
        elif f >= anchors[-1][0]:
            per_100 = anchors[-1][1]
        else:
            per_100 = anchors[-1][1]
            for (f0, d0), (f1, d1) in zip(anchors, anchors[1:]):
                if f0 <= f <= f1:
                    lf, lf0, lf1 = math.log10(f), math.log10(f0), math.log10(f1)
                    span = lf1 - lf0
                    w = (lf - lf0) / span if span > 0 else 0.0
                    per_100 = d0 + (d1 - d0) * w
                    break
    return per_100 * float(length_m) / 100.0


def build_graph(model, config):
    """Validate the element set and assemble the cascade graph.

    Returns {"nodes", "runs", "children", "sources", "errors"}.
    """
    errors = []
    nodes = {}
    runs = []
    for fl in model.get("floors", []):
        for el in fl.get("elements", []):
            t = el.get("type")
            eid = el.get("id")
            if t == CABLE_TYPE:
                if eid and any(r.get("id") == eid for r in runs):
                    errors.append("duplicate cable run id '%s'" % eid)
                runs.append(el)
                continue
            if t not in NODE_TYPES:
                errors.append("element '%s': unknown type '%s'" % (eid, t))
                continue
            if eid in nodes:
                errors.append("duplicate element id '%s'" % eid)
                continue
            nodes[eid] = {
                "el": el,
                "floor_id": fl.get("id"),
                "level": fl.get("level", 0),
            }

    children = {}
    fanin = {}
    cable_ids = config.get("cables", {})
    for run in runs:
        f, t = run.get("from_id"), run.get("to_id")
        if f not in nodes or t not in nodes:
            errors.append(
                "cable run '%s': from/to must reference chain nodes (%s -> %s)"
                % (run.get("id"), f, t)
            )
            continue
        if f == t:
            errors.append("cable run '%s': from and to are the same element" % run.get("id"))
            continue
        if run.get("cable_id") not in cable_ids:
            errors.append(
                "cable run '%s': unknown cable_id '%s'" % (run.get("id"), run.get("cable_id"))
            )
            continue
        try:
            length = float(run.get("length_m", 0.0))
        except (TypeError, ValueError):
            length = -1.0
        if length < 0:
            errors.append(
                "cable run '%s': length_m must be a number >= 0" % run.get("id")
            )
        children.setdefault(f, []).append(run)
        fanin[t] = fanin.get(t, 0) + 1

    for nid, nd in nodes.items():
        t = nd["el"]["type"]
        fi = fanin.get(nid, 0)
        if t == "source":
            if fi:
                errors.append("source '%s' must not be fed by a cable run" % element_label(nd["el"]))
        elif fi == 0:
            errors.append("%s '%s' has no feeding cable run" % (t, element_label(nd["el"])))
        elif fi > 1:
            errors.append(
                "%s '%s' is fed by %d cable runs (a node takes exactly one feed)"
                % (t, element_label(nd["el"]), fi)
            )

    for nid, nd in nodes.items():
        el = nd["el"]
        t = el["type"]
        if t == "antenna" and children.get(nid):
            errors.append("antenna '%s' cannot feed further chain elements" % element_label(el))
        if t == "source":
            sub = el.get("subtype")
            if sub is not None and str(sub) not in _source_subtypes(config):
                errors.append(
                    "source '%s': unknown subtype '%s' (known: %s)"
                    % (element_label(el), sub, ", ".join(sorted(_source_subtypes(config))))
                )
        if t == "antenna":
            ants = config.get("antennas", {})
            if el.get("antenna_id") not in ants:
                errors.append(
                    "antenna '%s': unknown antenna_id '%s'" % (element_label(el), el.get("antenna_id"))
                )
        if t == "splitter":
            ways = el.get("ways", 2)
            key = "%dway" % ways
            has_il = el.get("il_db") is not None
            if not has_il and key not in config.get("parts", {}).get("splitters", {}):
                errors.append(
                    "splitter '%s': no part defined for ways=%s and no il_db override"
                    % (element_label(el), ways)
                )
        if t == "repeater":
            out_deg = len(children.get(nid, []))
            ports = int(_num(el.get("ports", config.get("repeater", {}).get("ports", 2)), 2))
            if out_deg > ports:
                errors.append(
                    "repeater '%s' has %d trunk branches but only %d port(s)"
                    % (element_label(el), out_deg, ports)
                )

    sources = [nid for nid, nd in nodes.items() if nd["el"]["type"] == "source"]
    if not sources:
        errors.append("no source element in the chain")

    seen = set(sources)
    queue = deque(sources)
    while queue:
        nid = queue.popleft()
        for run in children.get(nid, []):
            child = run.get("to_id")
            if child in nodes and child not in seen:
                seen.add(child)
                queue.append(child)
    for nid, nd in nodes.items():
        if nid not in seen:
            errors.append(
                "%s '%s' is not connected to a source" % (nd["el"]["type"], element_label(nd["el"]))
            )

    return {
        "nodes": nodes,
        "runs": runs,
        "children": children,
        "sources": sources,
        "errors": errors,
    }


def _ambient_dbm(el, band):
    amb = el.get("ambient_dbm")
    band_default = band.get("ambient_dbm", 0.0)
    if isinstance(amb, dict):
        return _num(amb.get(band["id"], band_default), band_default)
    if amb is None:
        return _num(band_default)
    return _num(amb)


def _source_subtypes(config):
    """Accepted source subtypes: what the engine implements plus any extras
    declared in config source.subtypes."""
    ids = {"off_air", "direct", "direct_inject"}
    ids.update(str(sid) for sid in config.get("source", {}).get("subtypes", {}))
    return ids


def _source_out_dbm(el, band, config):
    """Source output dBm.

    off_air (default): ambient + donor antenna gain - inline feeder loss.
    direct: a base-station/P25 direct-inject feed puts a configured carrier
    level straight on the port: out = injection_dbm (donor gain / ambient do
    not apply). injection_dbm comes from the element, else the subtype's
    config default, else config source.injection_dbm_dbm.
    """
    src_cfg = config.get("source", {})
    subtype = el.get("subtype") or src_cfg.get("subtype_default", "off_air")
    if str(subtype).strip().lower() in ("direct", "direct_inject"):
        sub_cfg = (src_cfg.get("subtypes", {}).get("direct", {})
                   or src_cfg.get("subtypes", {}).get("direct_inject", {}))
        level = el.get("injection_dbm")
        if level is None:
            level = sub_cfg.get("injection_dbm", 5.0)
        return _num(level, 5.0), subtype
    donor = _num(el.get("donor_gain_db", src_cfg.get("donor_gain_db", 11.0)))
    feeder = _num(el.get("feeder_loss_db", src_cfg.get("feeder_loss_db", 0.0)))
    amb = _ambient_dbm(el, band)
    return amb + donor - feeder, subtype


def solve_cascade(model, config):
    """Solve every configured band. Returns:
    {
      "errors": [...],          # fatal graph errors (bands empty if any)
      "warnings": [...],        # flattened warnings across bands
      "bands": {band_id: {freq_mhz, nodes, edges, antennas, warnings}}
    }
    """
    g = build_graph(model, config)
    if g["errors"]:
        return {"errors": g["errors"], "warnings": [], "bands": {}}

    nodes = g["nodes"]
    children = g["children"]
    src_cfg = config.get("source", {})
    rep_cfg = config.get("repeater", {})
    parts = config.get("parts", {})
    ants_cfg = config.get("antennas", {})
    cables = config.get("cables", {})
    warnings = []
    bands_out = {}

    for band in config.get("bands", []):
        bid = band["id"]
        f_mhz = float(band["freq_mhz"])
        band_warn = []
        node_rows = []
        edge_rows = []
        antennas_out = []

        queue = deque((sid, float("nan")) for sid in g["sources"])
        while queue:
            nid, arrival = queue.popleft()
            nd = nodes[nid]
            el = nd["el"]
            t = el["type"]
            label = element_label(el)
            kids = children.get(nid, [])
            detail = ""
            thru = tap = coup = 0.0
            edge_pow = 0.0

            if t == "source":
                out, subtype = _source_out_dbm(el, band, config)
                if subtype in ("direct", "direct_inject"):
                    detail = "direct inject %.1f dBm (P25/base-station feed)" % out
                else:
                    donor = _num(el.get("donor_gain_db", src_cfg.get("donor_gain_db", 11.0)))
                    feeder = _num(el.get("feeder_loss_db", src_cfg.get("feeder_loss_db", 0.0)))
                    amb = _ambient_dbm(el, band)
                    detail = "ambient %.1f dBm + gain %.1f dBi - feeder %.1f dB" % (amb, donor, feeder)
                node_rows.append(
                    {"element_id": nid, "label": label, "type": t, "subtype": subtype,
                     "in_dbm": None, "out_dbm": round(out, 3), "detail": detail}
                )
                edge_pow = out
            elif t == "repeater":
                per_band = el.get("dl_gain_db_per_band") or {}
                gain = _num(
                    per_band.get(bid, el.get("dl_gain_db", rep_cfg.get("dl_gain_db", 65.0)))
                )
                raw = arrival + gain
                max_out = _num(el.get("max_dl_out_dbm", rep_cfg.get("max_dl_out_dbm", 33.0)))
                out = min(raw, max_out)
                if raw > max_out + 1e-9:
                    band_warn.append(
                        "Repeater '%s': clamped, input %.1f + %.1f dB gain exceeds "
                        "max out %.1f dBm" % (label, arrival, gain, max_out)
                    )
                detail = "in %.2f + %.1f dB gain, max out %.1f dBm" % (arrival, gain, max_out)
                node_rows.append(
                    {"element_id": nid, "label": label, "type": t,
                     "in_dbm": round(arrival, 3), "out_dbm": round(out, 3), "detail": detail}
                )
                edge_pow = out
            elif t == "splitter":
                ways = int(el.get("ways", 2))
                part = parts.get("splitters", {}).get("%dway" % ways, {})
                il = el.get("il_db")
                il = _num(il if il is not None else part.get("il_db", 3.5))
                edge_pow = arrival - il
                detail = "%.1f dB IL per branch (%d-way)" % (il, ways)
                node_rows.append(
                    {"element_id": nid, "label": label, "type": t,
                     "in_dbm": round(arrival, 3), "out_dbm": round(edge_pow, 3),
                     "detail": detail}
                )
            elif t in ("tapper", "coupler"):
                key = "tappers" if t == "tapper" else "couplers"
                dflt = parts.get(key, {}).get("default", {})
                if t == "tapper":
                    thru = _num(el.get("through_il_db", dflt.get("through_il_db", 0.8)))
                    tap = _num(el.get("tap_loss_db", dflt.get("tap_loss_db", 12.0)))
                else:
                    thru = _num(el.get("through_il_db", dflt.get("through_il_db", 0.6)))
                    coup = _num(el.get("coupled_loss_db", dflt.get("coupled_loss_db", 10.0)))
                tap_loss = tap if t == "tapper" else coup
                detail = "through %.1f dB, %s-port %.1f dB" % (
                    thru, "tap" if t == "tapper" else "coupled", tap_loss)
                node_rows.append(
                    {"element_id": nid, "label": label, "type": t,
                     "in_dbm": round(arrival, 3), "out_dbm": None, "detail": detail}
                )
            elif t == "antenna":
                acfg = ants_cfg[el["antenna_id"]]
                gain = _num(el.get("gain_db", acfg.get("gain_db", 2.0)))
                max_port = _num(el.get("max_port_dbm", acfg.get("max_port_dbm", 20.0)))
                port = min(arrival, max_port)
                capped = arrival > max_port + 1e-9
                eirp = port + gain
                if arrival < 0:
                    band_warn.append(
                        "Antenna '%s': port power %.1f dBm is below the 0 dBm budget"
                        % (label, arrival)
                    )
                if capped:
                    band_warn.append(
                        "Antenna '%s': port power capped at %.1f dBm (in was %.1f dBm)"
                        % (label, max_port, arrival)
                    )
                node_rows.append(
                    {"element_id": nid, "label": label, "type": t,
                     "in_dbm": round(arrival, 3), "out_dbm": round(port, 3),
                     "eirp_dbm": round(eirp, 3), "detail":
                     "port %.2f dBm + %.1f dBi" % (port, gain)}
                )
                antennas_out.append(
                    {"element_id": nid, "label": label,
                     "floor_id": nd["floor_id"], "level": nd["level"],
                     "in_dbm": round(arrival, 3), "port_dbm": round(port, 3),
                     "eirp_dbm": round(eirp, 3), "capped": capped}
                )
                edge_pow = None  # antennas terminate the chain
            else:  # pragma: no cover - build_graph rejects unknown types
                edge_pow = None

            for run in kids:
                cid = run["cable_id"]
                length = _num(run.get("length_m", 0.0))
                loss = cable_loss_db(cables[cid], length, f_mhz)
                role = run.get("port")
                if t == "tapper":
                    role = role or PORT_TAP
                elif t == "coupler":
                    role = role or PORT_COUPLED
                if t in ("source", "repeater", "splitter"):
                    branch_pow = edge_pow
                elif t == "tapper":
                    branch_pow = arrival - (thru if role == PORT_THROUGH else tap)
                elif t == "coupler":
                    branch_pow = arrival - (thru if role == PORT_THROUGH else coup)
                else:  # antennas have no children (enforced in build_graph)
                    branch_pow = None
                arrival_child = branch_pow - loss
                edge_rows.append(
                    {"element_id": run.get("id"), "from_id": nid, "to_id": run.get("to_id"),
                     "cable_id": cid, "cable_label": cables[cid].get("label", cid),
                     "length_m": round(length, 3), "loss_db": round(loss, 3),
                     "port_role": role or "out",
                     "in_dbm": round(branch_pow, 3), "out_dbm": round(arrival_child, 3)}
                )
                queue.append((run["to_id"], arrival_child))

        if band_warn:
            warnings.extend(band_warn)
        bands_out[bid] = {
            "band_id": bid,
            "label": band.get("label", bid),
            "freq_mhz": f_mhz,
            "nodes": node_rows,
            "edges": edge_rows,
            "antennas": antennas_out,
            "warnings": band_warn,
        }

    return {"errors": [], "warnings": warnings, "bands": bands_out}
