"""Ships the preloaded example project: a small 2-floor office, 4 antennas,
off-air chain (source -> repeater -> 7/8 in trunk -> two 2-way splitters ->
branch coax -> omni antennas). Floor images are generated programmatically
with the stdlib PNG writer, so the repo carries no binary assets.
"""

import base64

from .report import (INK, WHITE, draw_disc, draw_line, draw_text, encode_png,
                     make_canvas)

W = 120
H = 92
PPM = 10.0  # pixels per meter -> 12.0 m x 9.2 m floor plate

OUTER = [
    {"x1": 2, "y1": 2, "x2": 118, "y2": 2},
    {"x1": 118, "y1": 2, "x2": 118, "y2": 90},
    {"x1": 118, "y1": 90, "x2": 2, "y2": 90},
    {"x1": 2, "y1": 90, "x2": 2, "y2": 2},
]

F1_WALLS = (
    [{"id": "exf1_w%d" % (i + 1), "material": "concrete", **seg} for i, seg in enumerate(OUTER)]
    + [
        {"id": "exf1_w5", "x1": 2, "y1": 46, "x2": 60, "y2": 46, "material": "drywall"},
        {"id": "exf1_w6", "x1": 60, "y1": 46, "x2": 60, "y2": 90, "material": "drywall"},
        {"id": "exf1_w7", "x1": 60, "y1": 2, "x2": 60, "y2": 46, "material": "glass"},
    ]
)

F2_WALLS = (
    [{"id": "exf2_w%d" % (i + 1), "material": "concrete", **seg} for i, seg in enumerate(OUTER)]
    + [
        {"id": "exf2_w5", "x1": 60, "y1": 2, "x2": 60, "y2": 90, "material": "drywall"},
    ]
)


def _wall_color(material):
    return {
        "concrete": (40, 40, 40),
        "drywall": (120, 96, 60),
        "glass": (60, 100, 140),
        "cmu": (110, 90, 70),
        "low_e_glass": (30, 70, 100),
        "wood": (140, 100, 50),
        "metal": (20, 20, 20),
    }.get(material, (60, 60, 60))


EX_CA1 = {
    "id": "exf1_ca1",
    "label": "IT room (critical)",
    # west portion of the upper room on floor 1, bounded by outer concrete
    # north/west, drywall w5 (south) and glass w7 (east)
    "points": [[2, 2], [60, 2], [60, 46], [2, 46]],
}


def make_floorplan_png(floor):
    """Tiny floorplan illustration: walls by material + antenna dots."""
    scale = 4
    canvas = make_canvas(W * scale, H * scale, WHITE)
    for wl in floor["walls"]:
        draw_line(canvas,
                  int(wl["x1"]) * scale, int(wl["y1"]) * scale,
                  int(wl["x2"]) * scale, int(wl["y2"]) * scale,
                  _wall_color(wl.get("material", "drywall")),
                  thickness=max(1, scale // 2))
    for el in floor["elements"]:
        if el.get("type") == "antenna":
            draw_disc(canvas, int(el.get("x", 0)) * scale, int(el.get("y", 0)) * scale,
                      scale + 1, (160, 20, 20))
        elif el.get("type") == "repeater":
            draw_disc(canvas, int(el.get("x", 0)) * scale, int(el.get("y", 0)) * scale,
                      scale + 1, (20, 60, 160))
    draw_text(canvas, 4, H * scale - 14,
              "".join(c for c in floor["name"].upper() if c in
                      (set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ()-") )
                      )[:24] or "FLOOR",
              INK, scale=1)
    return encode_png(canvas, W * scale, H * scale)


def build_example_model():
    floor1 = {
        "id": "exf1",
        "name": "Ground",
        "level": 0,
        "ppm": PPM,
        "w_px": W,
        "h_px": H,
        "image": None,  # filled below
        "walls": [dict(w) for w in F1_WALLS],
        "critical_areas": [dict(EX_CA1)],
        "elements": [
            {"id": "ex_src", "type": "source", "label": "SRC", "x": 114, "y": 22,
             "subtype": "off_air", "donor_gain_db": 11.0, "feeder_loss_db": 0.0},
            {"id": "ex_rep", "type": "repeater", "label": "RPTR", "x": 112, "y": 32},
            {"id": "ex_sp1", "type": "splitter", "label": "SP1", "x": 54, "y": 80, "ways": 2},
            {"id": "ex_sp2", "type": "splitter", "label": "SP2", "x": 28, "y": 80, "ways": 2},
            {"id": "ex_sp3", "type": "splitter", "label": "SP3", "x": 90, "y": 80, "ways": 2},
            {"id": "ex_a1", "type": "antenna", "label": "A1", "antenna_id": "omni_das",
             "x": 16, "y": 24},
            {"id": "ex_a2", "type": "antenna", "label": "A2", "antenna_id": "omni_das",
             "x": 42, "y": 68},
        ],
    }

    floor2 = {
        "id": "exf2",
        "name": "Level 1",
        "level": 1,
        "ppm": PPM,
        "w_px": W,
        "h_px": H,
        "image": None,
        "walls": [dict(w) for w in F2_WALLS],
        "elements": [
            {"id": "ex_a3", "type": "antenna", "label": "A3", "antenna_id": "omni_das",
             "x": 30, "y": 24},
            {"id": "ex_a4", "type": "antenna", "label": "A4", "antenna_id": "omni_das",
             "x": 96, "y": 24},
        ],
    }

    runs = [
        # donor feeder run: source -> repeater (part of the donor budget)
        {"id": "ex_r_fd", "type": "cable_run", "from_id": "ex_src", "to_id": "ex_rep",
         "cable_id": "lmr400", "length_m": 2.0},
        # trunk: repeater -> SP1 (7/8 in)
        {"id": "ex_r_tr", "type": "cable_run", "from_id": "ex_rep", "to_id": "ex_sp1",
         "cable_id": "coax_7_8in", "length_m": 8.0},
        # SP1 -> SP2 -> A1, A2 (floor 1)
        {"id": "ex_r_b1", "type": "cable_run", "from_id": "ex_sp1", "to_id": "ex_sp2",
         "cable_id": "lmr400", "length_m": 2.7},
        {"id": "ex_r_b2", "type": "cable_run", "from_id": "ex_sp2", "to_id": "ex_a1",
         "cable_id": "lmr400", "length_m": 5.8},
        {"id": "ex_r_b3", "type": "cable_run", "from_id": "ex_sp2", "to_id": "ex_a2",
         "cable_id": "lmr400", "length_m": 2.7},
        # SP1 -> SP3 (riser to floor 2) -> A3, A4
        {"id": "ex_r_riser", "type": "cable_run", "from_id": "ex_sp1", "to_id": "ex_sp3",
         "cable_id": "coax_7_8in", "length_m": 9.0},
        {"id": "ex_r_b4", "type": "cable_run", "from_id": "ex_sp3", "to_id": "ex_a3",
         "cable_id": "lmr400", "length_m": 3.2},
        {"id": "ex_r_b5", "type": "cable_run", "from_id": "ex_sp3", "to_id": "ex_a4",
         "cable_id": "lmr400", "length_m": 1.0},
    ]
    floor1["elements"].extend(runs)
    # cable runs live in the model as elements; keep them attached to floor 1
    # records but they are shared graph edges (chain spans both floors).

    model = {
        "name": "Example Small Office (LTE)",
        "description": ("Preloaded example: small office, 2 floors, off-air donor "
                        "+ repeater, 7/8 in trunk, two 2-way splitters, "
                        "4 omni antennas (2 per floor). Ships phase-3 demo: a "
                        "critical-area polygon (IT room, west) on the ground "
                        "floor for NFPA tier-2 evaluation; solve covers LTE + "
                        "FirstNet B14 + legacy LMR + 5G NR (n71 600, n41 2500, "
                        "n77 C-band) bands from default config: per-band "
                        "predict carries the SS-RSRP preset on the NR bands, "
                        "and composite?bands= views span the NR selection."),
        "example": True,
        "environment": "office",
        "config_overrides": {},
        "floors": [floor1, floor2],
    }
    for fl in (floor1, floor2):
        png = make_floorplan_png(fl)
        fl["image"] = {"mime": "image/png", "data_b64": base64.b64encode(png).decode("ascii")}
    return model


def example_project_name():
    return "Example Small Office (LTE)"
