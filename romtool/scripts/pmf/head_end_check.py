"""
Head end kit configuration builder for the OEM uplink comparison.

Builds the head-end equipment configuration for every OEM design, from the run
data the design/stack/uplink modules consume (stack_results.json and
oem_results.json) and the vendor sheets. Reconciled 2026-11-25 against the real
ROM BOMs: the iBwave equipment list reports for the Sugar House comparison
(Design/Comparisons in the job folder, designer P. DelPizzo), the ADRF Fort
Worth City Hall ROM BOM, and the ADRF Yan Feng ROM BOM. The BOMs are the source
for part names, the card classes, and the donor-side hardware every ROM
carries; the remote counts stay the ROM tool's, and the head end is sized to
support them.

Scoping rules (owner, 2026-11-25):
- The Part 20 consumer rows kit the whole maxed single system with the coverage
  gap flagged: QUATRA (1 active DAS host + 1 Fiber Hub + up to 12 CUs),
  SignalMax (1 MU + 4 RUs), 5X Max (one system per building, the standard
  rated kit with the required splitter tree), SDRC front end only (CHA-5 +
  CHA-1 per the design, PSU-5, one repeater per carrier in BDA/DFE mode with
  the donor and server channel combiners). ComFlex kit whole per owner OK.
- The ADX V and JMA rows are head end kits only, per sector: the remote side is
  the ROM tool's design (count in remote_basis), the head end is sized to
  support it (chassis counts, POIs per band, channel combiners, optic donor
  units, fiber termination).
- Kit hardware classes every reference BOM carries: Westell CS60-024-002 surge
  arrestors on the donor ports (one per public donor port on every design report), Polyphaser
  TSX-NFF lightning protectors with P/N BFN brackets on the ADRF-family donor
  antennas (same quantity as donor antennas, per the ROM precedent), and the
  OCC fiber termination kit where the head end terminates fiber.
- JMA POIs: TAPOI per band family per the design, with TLPPOI carried as the
  low-input-power alternate; exactly one POI path installs per build.

Config and counts only. Prices stay blank (None) by the phase 2 decision.
Config estimates (grade E). No prices, no customer or job names.

Sheet and BOM sources (all read directly, 2026-11-25):
- ComFlex NG: MU-Chassis-AC DS (8 RF + 2 FOU slots), MU-BDA20-2B cards, MU-FOU,
  MU-DEMOD (the 11-18 the design reports carry it), Consumer OG 0-0-5 (16 HUBs per MU
  with FOU expansion, 256 ARU), the design reports' P/N MRU-7851719c-DC.
- QUATRA 4000c: design BOM P/Ns Q44-1M34CNU (host), Q40-1234FNU (Fiber Hub),
  Q41-RECU (CU), CAT-5 to 331 ft.
- SignalMax: design BOM P/Ns SignalMax Fiber_MU / Fiber_RU; the design reports' splitter tree
  uses Westell CS04-072-429 (4-way), CS04-070-429 (2-way), CS04-041-429 (3-way).
- 5X Max: product page; the design reports' splitter tree: Westell 3-way CS04-071-429, 2-way, and
  a Microlab DN-44FE tapper at the host.
- ADX V: Series DS + both reference BOMs: ADXV-H-NMS-AC (12 standard modules),
  ADXV-H-CHC-88-DL-X / UL-X (8x8 combiners per 8 trunk groups), ADXV-H-ODU-4-X
  (4-port optic donor unit, one strand per RU, <=5 dBo), POIs per band per
  carrier (POI-7L x1, POI-P x3, POI-A x3 per the Fort Worth ROM); the remote
  site kit per the ROM precedent is one HPR-CHA / R-CHA + PSU + ORU fiber unit +
  band modules + ADXV-R-CHC-U.
- SDRC: both designs build the front end as SDRC-17-A x3 + SDRC-17-P x3
  (BDA/DFE modes per band) with SDR-CHC-V donor x3 and server x3, SDRC-CHA-5 +
  SDRC-CHA-1, SDRC-PSU-5; the Fort Worth ROM adds SDRC-17-7F x1 and a second
  CHA-5 + PSU-5.
- JMA TEKO: design BOM P/Ns TTRU4W-S-M (4-way MU optical Tx/Rx), T19AWE19AWEWWAT
  (RU 2-band PCS1900/AWS3 MIMO 40W), TAPOI-PCS-F-4, TAPOI-AWSE-F-4 (marked
  discontinued in the design), SUB-TRX-PSU5N (owner P/N; houses the modules),
  TSPV-NBB, TPSU/AC x2, TPANEL-14TE, TESA-UW2-L (SMR700 esaplexer), T-HC3-4
  (3x3 hybrid coupler) on the server side.
"""
import argparse, json, math, os, re

BAND_FAMILY = {
    "B12": "700L", "B17": "700L", "B13": "700H", "B71": "600", "B5": "850",
    "B2": "PCS", "B25": "PCS", "B66": "AWS", "B4": "AWS",
}
SDRC_MODEL = {"600": "SDRC-17-7F", "700L": "SDRC-17-7F", "700H": "SDRC-17-7F",
              "850": "SDRC-17-C", "PCS": "SDRC-17-P", "AWS": "SDRC-17-A"}
FAMILY_LABEL = {"600": "600 MHz", "700L": "700 MHz (Lower/Commercial)",
                "700H": "700 MHz (Upper)", "850": "Cellular 850",
                "PCS": "PCS 1900", "AWS": "AWS 1700/2100"}
CARRIERS = 3  # operators sharing the composite: ATT, VZW, TMO (design rule)
BILIBOM = "iBwave comparison BOM 11-18/19 (P. DelPizzo)"

DESIGN = {"sugarhouse": 45, "floors": 45}


def parse_bands(bands_in_run):
    fams = []
    for tok in re.findall(r"B\d+", (bands_in_run or "")):
        fam = BAND_FAMILY.get(tok)
        if fam and fam not in fams:
            fams.append(fam)
    return fams


def make_kits(dirpath, floors_bldg, sectors):
    with open(os.path.join(dirpath, "stack_results.json")) as f:
        stack = {r["oem"]: r for r in json.load(f)["stack"]}
    with open(os.path.join(dirpath, "oem_results.json")) as f:
        design = {r["oem"]: r for r in json.load(f)["design"]}
    kits = {}

    kits["q4000c"] = kit_q4000c(stack.get("q4000c", {}), design, floors_bldg)
    kits["signalmax"] = kit_signalmax(stack.get("signalmax", {}), design, floors_bldg)
    kits["surecall_5xmax"] = kit_5xmax(floors_bldg)
    kits["adxv_mpr"] = kit_adxv(stack.get("adxv_mpr", {}), design, floors_bldg, sectors, family="mid")
    kits["adxv_hpr"] = kit_adxv(stack.get("adxv_hpr", {}), design, floors_bldg, sectors, family="high")
    kits["adxv_lpr"] = kit_adxv_lpr(stack.get("adxv_lpr", {}), design, floors_bldg, sectors)
    kits["comba_mruc"] = kit_comba(stack.get("comba_mruc", {}), design, floors_bldg)
    kits["jma_t19awe"] = kit_jma(stack.get("jma_t19awe", {}), design, floors_bldg, sectors)
    kits["sdrc_only"] = kit_sdrc_only(floors_bldg)
    return kits


def blank_kit(architecture, count=None):
    kit = {"architecture": architecture, "items": [], "constraints": [],
           "open_items": [], "sources": []}
    if architecture is None:
        kit["architecture"] = ""
    if count is not None:
        kit["covered_floors"] = count
    return kit


class Builder:
    def __init__(self, kit):
        self.kit = kit
        self.kit["items"] = []
        self.kit["constraints"] = self.kit.get("constraints", [])
        self.kit["open_items"] = self.kit.get("open_items", [])

    def item(self, part, label, qty, role, source, variant=None):
        if qty:
            entry = {"part": part, "label": label, "qty": int(qty),
                     "role": role, "unit_price": None, "source": source}
            if variant:
                entry["variant"] = variant
            self.kit["items"].append(entry)

    def note(self, c):
        self.kit["constraints"].append(c)

    def open_item(self, o):
        self.kit["open_items"].append(o)

    def donor_hardware(self, donor_count):
        # classes every reference BOM carries on the donor side
        self.item("Westell CS60-024-002", "RF coaxial surge arrestor, N female to N female, one per public donor port",
                  donor_count, "donor protection", "%s: per donor port on every design" % BILIBOM)

    def adrf_donor_protection(self, donor_count):
        # the ADRF-family protectors: the Fort Worth ROM precedent; the SureCall
        # designs carry only the Westell arrestors
        self.item("Polyphaser TSX-NFF", "698-2700 MHz lightning DC blocked protector, one per donor antenna",
                  donor_count, "donor protection", "Fort Worth ADRF ROM: TSX-NFF same qty as donor antennas")
        self.item("Polyphaser BFN", "TSX-NFF mount bracket, same quantity as the protector", donor_count,
                  "donor protection", "ROM precedent: TSX-NFF needs P/N BFN at the same qty")


def kit_q4000c(row, design, floors_bldg):
    cus = min(int(row.get("remotes_bldg") or 0), 12)
    covered = min(cus, floors_bldg) if cus else 0
    kit = blank_kit("Active DAS, one maxed system: 1 active DAS host (CNU) + 1 Fiber Hub host up to "
                    "12 Coverage Units (6 per host, 6 per Fiber Hub), one CU per covered floor with 4 "
                    "interior antennas; donor side on the host's 4x N-type ports (left to right: "
                    "Private, Verizon, T-Mobile, AT&T)")
    b = Builder(kit)
    b.kit["sector_count"] = 1
    ant_total = covered * 4
    b.item("Q41-RECU", "Coverage Unit, one per covered floor (4 interior antennas per floor); design P/N from the ROM BOM",
           cus, "remote", "QUATRA 4000c design BOM; system max 12 CUs (6 per CNU + 6 per FNU)")
    b.item("Interior antenna", "SC-528W Ultra Thin in the design (%d of the design's antennas per floor floor BOM)" % 4,
           ant_total, "interior antenna", "the design reports carry SC-528W x26 across their floors")
    b.item("Q44-1M34CNU", "Active DAS host (CNU), 6 CUs, 4x N-type donor ports; design P/N from the ROM BOM",
           1, "head end", BILIBOM)
    b.item("Q40-1234FNU", "Fiber Hub, 6 CUs per hub", 1, "head end", BILIBOM)
    b.item("Cat5e run", "Per CU, 328 ft (100 m) max; the design runs 331.31 ft total", cus, "cable", BILIBOM)
    b.item("Donor antenna (SC-530W)", "One per public-carrier port: VZW, TMO, ATT (kit basis per the owner donor rule; "
           "the iBwave the design reports carry SC-230W x3, 10 dBi yagis, at the roof)", 3, "donor antenna",
           "donor basis 11-25 (SC-530W 7/8/9/9 dBi)")
    b.donor_hardware(3)
    b.note("Part 20 single-sector class: one system set per building")
    b.note("Port order left to right on the host: Private, Verizon, T-Mobile, AT&T (owner 11-25); the private "
           "port is unused by the 3-carrier design, available for a private or BDA feed")
    b.note("System max is 12 CUs (6 per host + 6 per Fiber Hub); with %d floors this system covers %d of %d "
           "floors - add system sets at the ROM level to go deeper" % (floors_bldg, covered, floors_bldg))
    b.note("Reference only (the head end look): the 11-18 iBwave design carries 8 CUs, 26 SC-528W antennas and "
           "331.31 ft CAT-5 on its floor layout; this kit carries the ROM tool's count (%d CUs), per the owner's "
           "scoping" % cus)
    b.kit["covered_floors"] = covered
    return kit


def kit_signalmax(row, design, floors_bldg):
    rus = min(int(row.get("remotes_bldg") or 0), 4)
    covered = min(rus, floors_bldg) if rus else 0
    ant_total = covered * 4
    kit = blank_kit("Fiber DAS, one maxed system: 1 Master Unit + up to 4 remote units (one remote per "
                    "covered floor, 4 interior antennas per remote); one outside donor antenna per MU")
    b = Builder(kit)
    b.kit["sector_count"] = 1
    b.item("SignalMax Fiber_MU", "Master Unit, 1 N female outside port + 4 SC/APC fiber ports (4 RUs per MU); design P/N from the ROM BOM",
           1, "head end", BILIBOM)
    b.item("SignalMax Fiber_RU", "Remote Unit, 4 ports x up to 4 antennas (the 11-18 the design reports carry 2 RUs for its floor BOM; this kit maxes at 4)",
           rus, "remote", BILIBOM)
    b.item("Interior antenna", "One per RU port (SC-528W Ultra Thin in the design)", ant_total, "interior antenna",
           "the design reports carry SC-528W")
    b.item("Distribution splitter set (Westell CS04)", "The head end look per the 11-18 design BOM: 4-way CS04-072-429 x5, "
           "2-way CS04-070-429 x3, 3-way CS04-041-429 x1; the ROM tool computes its own tree when the design runs",
           9, "interior distribution", "reference layout from the %s" % BILIBOM)
    b.item("Donor antenna (SC-530W)", "Three donor antennas combined into the wideband MU port, one per carrier "
           "direction (kit basis per the owner donor rule; the design reports carry SC-230W x3 at the roof)",
           3, "donor antenna", "donor basis 11-25")
    b.donor_hardware(3)
    b.note("Part 20 single-sector class: one system set per building")
    b.note("System max is 1 MU with 4 RUs; with %d floors this system covers %d of %d floors - add system sets at the ROM level to go deeper"
           % (floors_bldg, covered, floors_bldg))
    b.note("SC/APC single-mode fiber MU to RU (the design reports carry Corning 006E81-33131-24 riser, 6 fiber)")
    b.kit["covered_floors"] = covered
    return kit


def kit_5xmax(floors_bldg):
    kit = blank_kit("Consumer ERT booster, one system per building: outdoor ERT unit + indoor host "
                    "(2 interior ports, up to 12 internal antennas) + one donor antenna")
    b = Builder(kit)
    b.kit["sector_count"] = 1
    b.item("5x Max Outside", "Outdoor amplifier with Extended Range Technology (the design reports carry 1 at the roof)", 1, "remote-donor",
           "%s: 5x Max Outside" % BILIBOM)
    b.item("5X Max Inside Host", "Indoor host, 2 high-power interior ports (design places it on the host floor)", 1, "remote",
           BILIBOM)
    b.item("Interior antenna", "Up to 12 internal antennas in the expanded rated kit (base kit ships 4)", 12, "interior antenna",
           "5X Max product page")
    b.item("Westell CS04-070-429 (SPD2/450-2.7K/N)", "Required by the 6-antennas-per-port balanced tree (owner 11-25: list what is required); "
           "the 11-18 the design reports' splitter tree for 20 antennas instead used 4-way x5 + 3-way x1 + 2-way x1 + a Microlab DN-44FE tapper",
           6, "interior distribution", "owner 11-25 splitter call")
    b.item("Westell CS04-071-429 (SPD3/698-2.7K/N)", "Same tree, one per interior port", 2, "interior distribution",
           "owner 11-25 splitter call")
    b.item("Donor antenna (SC-530W)", "Three donor antennas per the design, combined ahead of the host input "
           "(kit basis per the owner donor rule; the design reports carry SC-230W x3)", 3,
           "donor antenna", "donor basis 11-25")
    b.donor_hardware(3)
    b.note("Part 20 single-sector class: one system set per building")
    b.note("Rated 20,000 sq ft base (4 antennas) to 75,000 sq ft expanded (12 antennas); the 12-antenna tree "
           "takes 8 splitters (6 two-way + 2 three-way), 6 antennas per interior port")
    b.note("One system covers the building footprint the survey confirms; the comparison design's floor-set "
           "multiplication is not part of the kit")
    b.kit["covered_floors"] = floors_bldg
    return kit


def kit_adxv(row, design, floors_bldg, sectors, family):
    remotes = int(row.get("remotes_bldg") or 0)
    odus = int(math.ceil(remotes / 4.0)) if remotes else 0
    ram = "MPR" if family == "mid" else "HPR"
    kit = blank_kit("Head-end DAS, head end ONLY per sector, sized to the ROM tool RU count (%d %s remote units): "
                    "POIs per band per carrier, the 8x8 DL/UL channel combiner pair, optic donor units at one "
                    "strand per RU; the remote side is the ROM tool's design (remote_basis)" % (remotes, ram))
    b = Builder(kit)
    b.kit["sector_count"] = sectors
    b.item("ADXV-H-NMS-AC", "Head End NMS package, chassis + NMS + PSU (AC), up to 12 POI/POIL/ODU/CHC standard modules", 1, "head end", "ADXV Series datasheet")
    # POIs per band per carrier (Fort Worth ROM pattern: POI-7L x1, POI-P x3, POI-A x3)
    b.item("ADXV-H-POI-P", "PCS point of interface, one per carrier", CARRIERS, "head end", "Fort Worth ADRF ROM: POI-P x3")
    b.item("ADXV-H-POI-A", "AWS point of interface, one per carrier", CARRIERS, "head end", "Fort Worth ADRF ROM: POI-A x3")
    b.item("ADXV-H-POI-7L", "700 MHz Lower point of interface (owner 11-26: the 700 POI is required with the 850)", 1, "head end", "Fort Worth ADRF ROM: POI-7L x1; owner 11-26")
    b.item("ADXV-H-POI-C", "Cellular 850 point of interface (owner 11-26: the 850 POI is required); P/N class per the 7L/P/A pattern", 1, "head end", "owner 11-26")
    b.item("ADXV-H-CHC-88-DL-X", "8x8 DL channel combiner (600 MHz to C-Band; the non-X variant covers 600 MHz to BRS)",
           1, "head end", "ADXV Series datasheet; both reference BOMs carry the -X for the Sugar House design")
    b.item("ADXV-H-CHC-88-UL-X", "8x8 UL channel combiner (600 MHz to C-Band)", 1, "head end", "ADXV Series datasheet")
    b.item("ADXV-H-ODU-4-X", "4-port optic donor unit, one strand per remote unit, <=5 dBo; sized to the ROM tool RU count",
           odus, "head end", "ADXV Series datasheet; reference BOMs size 1 ODU-4(-X) per up to 4 RUs")
    slots = 2 * CARRIERS + 2 + odus + 2
    b.note("NMS slots: 3 POI-P + 3 POI-A + 1 POI-7L + 1 POI-C + %d ODU-4-X + 2 CHC-88-X = %d of the 12 standard "
           "modules; per-carrier low-band POIs beyond these would need an ADXV-H-RACK (2 ODU/CHC slots each, up to 8 racks)"
           % (odus, slots))
    b.item("Donor antenna pair (AD-PA)", "AD-PA-617-960-D (low band, 15.5/16.8 dBi) + AD-PA-1900-2600-DIN-X (19.1 dBi, "
           "1710-2170); the Fort Worth ROM donor family is the same AD-PA class (AD-PA-1695-4200-D x3 + AD-PA-617-960-D x1)",
           1, "donor antenna", "donor basis 11-25 (AD-PA pair, sheet-verified)")
    b.donor_hardware(2)
    b.adrf_donor_protection(2)  # one protector + bracket per AD-PA donor antenna in the pair
    b.item("Fiber termination kit (OCC)", "2U RTS cabinet + 6-port SC/APC adapter floor BOMs + pigtails + patch leads; "
           "sized to the ODU fiber ports", 1, "fiber", "Fort Worth ADRF ROM: OCC 616SCAPC / RTS2U-6APB / P8SCAPC06-3M / D8SCAPC-SCAPC-3M")
    b.item("Jumper kit (ADRF)", "Head-end interconnect jumpers: J-SF141-1H-4MR-NMR 1.5 ft + internal SMB jumpers", 1,
           "head end", "%s: J-SF141-1H-4MR-NMR x6 and J-SMB-SMB x14 (internal, [DO NOT ORDER] label)" % BILIBOM)
    # remote side: the ROM tool owns it; the kit carries the basis
    if family == "mid":
        rb = {"source": "ROM tool remote design (stack_results.json)", "remote_units": remotes,
              "note": "per the ADRF ROM precedent (Fort Worth, Yan Feng), each remote site is one remote chassis "
                      "(ADXV-R-CHA, 7 slots) + remote chassis power supply + an optical remote unit (ORU) + the "
                      "band modules (ADXV-R-33X/37X RAM class for the mid family) + one ADXV-R-CHC-U universal "
                      "channel combiner; the fan kit ADXV-R-FAN-DC is required above 4 RAMs per chassis; the "
                      "ROM tool owns the site count"}
        b.note("Head end sizing rule: optic donor units = ceil(RUs / 4); %d RUs -> %d ODU-4-X" % (remotes, odus))
    else:
        rb = {"source": "ROM tool remote design (stack_results.json)", "remote_units": remotes,
              "note": "per the ADRF ROM precedent, each high-power remote site is one ADXV-HPR-CHA + HPR-PSU-AC + "
                      "HPR-ORU(-X) optical remote unit + the band module set (HPR-437F/46A/46P class, one per "
                      "band family per carrier) + one ADXV-R-CHC-U universal channel combiner; the ROM tool owns "
                      "the site count"}
        b.note("Head end sizing rule: optic donor units = ceil(RUs / 4); %d RUs -> %d ODU-4-X" % (remotes, odus))
    kit["remote_basis"] = rb
    kit["constraints"].append("The ADRF chain is not Part 20 sector-capped: parts are listed per sector and scale "
                              "by the sector_count parameter (%d here)" % sectors)
    kit["open_items"].append("Exact ADX V POI-7L and POI-C (Cellular 850) part numbers: the reference BOMs carry POI-7L "
                             "only; the kit names the 850 card POI-C per the 7L/P/A suffix pattern - confirm both P/Ns "
                             "with ADRF")
    kit["covered_floors"] = min(remotes * 1, floors_bldg) if remotes else 0
    return kit


def kit_adxv_lpr(row, design, floors_bldg, sectors):
    remotes = int(row.get("remotes_bldg") or 0)
    odus = int(math.ceil(remotes / 4.0)) if remotes else 0
    kit = blank_kit("Fiber-to-the-edge DAS, head end ONLY per sector, sized to the ROM tool primary count (%d "
                    "LPR sites): optic donor units at 4 primaries per card, the SDRC front end, POIs per "
                    "carrier in non-DFE mode; the primary/secondary pairs are the ROM tool's design (remote_basis)"
                    % remotes)
    b = Builder(kit)
    b.kit["sector_count"] = sectors
    b.item("ADXV-H-NMS-AC", "Head End NMS package, chassis + NMS + PSU (AC), up to 12 standard modules", 1, "head end", "ADXV Series datasheet")
    b.item("ADXV-H-ODU-4-X", "4-port optic donor unit, one strand per primary, <=5 dBo; sized to the ROM tool count "
           "(the 11-19 the design reports carry 1 ODU-4-X for 2 LPR sites); the ADXV-L-HUB-4-AC is the alternate head end hub "
           "(4 primaries + DC power to 8)", odus, "head end", "ADXV Series datasheet + %s" % BILIBOM)
    b.item("ADXV-H-POI-P", "PCS point of interface, one per carrier (non-DFE mode only; DFE mode removes these)", CARRIERS, "head end", "ADXV Series datasheet", variant="non-DFE only")
    b.item("ADXV-H-POI-A", "AWS point of interface, one per carrier (non-DFE mode only)", CARRIERS, "head end", "ADXV Series datasheet", variant="non-DFE only")
    b.item("ADXV-H-POI-7L", "700 MHz Lower point of interface (owner 11-26: required with the 850; non-DFE mode only)", 1, "head end", "Fort Worth ADRF ROM: POI-7L x1; owner 11-26", variant="non-DFE only")
    b.item("ADXV-H-POI-C", "Cellular 850 point of interface (owner 11-26: required; P/N per the 7L/P/A pattern; non-DFE mode only)", 1, "head end", "owner 11-26", variant="non-DFE only")
    b.item("SDRC front end", "SDRC-CHA-5 + SDRC-CHA-1 chassis, SDRC-PSU-5, per-carrier modules SDRC-17-A x3 + SDRC-17-P x3 "
           "(BDA/DFE modes per band), SDR-CHC-V donor x3 + server x3 - one kit, full detail in the SDRC front end block "
           "of the JSON", 1, "donor front end", "%s floor 8: CHA-5 x1 + CHA-1 x1 + PSU-5 x1 + 17-A x3 + 17-P x3 + CHC-V donor x3 + server x3" % BILIBOM)
    b.item("Donor antenna pair (AD-PA)", "AD-PA-617-960-D + AD-PA-1900-2600-DIN-X", 1, "donor antenna", "donor basis 11-25")
    b.donor_hardware(2)
    b.adrf_donor_protection(2)
    b.item("Fiber termination kit (OCC)", "2U RTS cabinet + 6-port SC/APC floor BOMs + pigtails + patch leads, sized to the ODU ports", 1, "fiber", "Fort Worth ADRF ROM fiber equipment")
    b.item("Jumper kit (ADRF)", "Head-end interconnect jumpers (4.3-10 and internal SMB)", 1, "head end", BILIBOM)
    kit["remote_basis"] = {"source": "ROM tool remote design (stack_results.json)", "primaries": remotes,
                           "note": "each site is one ADXV-L-20PAWBT-P Mid Band SISO Primary (PCS/AWS/WCS/BRS, 20 dBm/band) "
                                   "paired 1:1 with its ADXV-L-1767S8C-S Low Band SISO Secondary (600/700/SMR800+Cellular, "
                                   "17 dBm/band); the pair cross-connects over the SMA ports both modules carry; the "
                                   "C-Band MIMO pair (ADXV-L-23N77HM-P + ADXV-L-23N77LM-S) repeats the pattern; the ROM "
                                   "tool owns the count, this kit only sizes the head end to it"}
    kit["constraints"].append("Head end sizing rule: optic donor units = ceil(primaries / 4); %d primaries -> %d ODU-4-X"
                              % (remotes, odus))
    kit["constraints"].append("LPR fiber: <=5 dBo optical budget per the sheet; the ADXV-L-OPT-CPL-S coupler can feed "
                              "two primaries on one strand if the fiber run wants it")
    kit["covered_floors"] = floors_bldg
    return kit


def kit_comba(row, design, floors_bldg):
    remotes = int(row.get("remotes_bldg") or 0)
    fous = max(int(math.ceil(remotes / 4.0)), 1)
    covered = min(remotes * 1, floors_bldg) if remotes else 0
    ant_total = covered * 4
    kit = blank_kit("Modular analog fiber DAS (whole system per owner OK): MU-Chassis-AC with the BDA cards for the "
                    "Part 20 off-air path and FOU cards toward the MRUc remotes (8 RF modules + 2 FOU slots per "
                    "chassis; 4 remotes or hubs per FOU)")
    b = Builder(kit)
    b.kit["sector_count"] = 1
    b.item("MU-Chassis-AC", "Master Unit chassis, 10 slots (8 RF modules + 2 FOU), power and modem included", 1, "head end", "MU-Chassis-AC DS")
    b.item("MU-DEMOD", "Demodulation module (the 11-18 the design reports carry 1)", 1, "head end", BILIBOM)
    b.item("MU-BDA20-2B-HH", "Part 20 off-air BDA card, two high-band channels, one per carrier (the 11-18 the design reports carry 3)", CARRIERS, "head end", BILIBOM)
    b.item("MU-FOU", "Fiber Optic Unit, 4 remotes or hubs per card; 2 FOU slots per chassis; sized to the ROM tool remote count",
           fous, "head end", "MU-FOU DS")
    b.item("ComFlex NG MRUc", "Medium-power remote (the design reports' P/N MRU-7851719c-DC); the 11-18 the design reports carry 1 for its floor BOM",
           remotes, "remote", "ROM tool count")
    b.item("Interior antenna", "Per covered floor per the design (SC-528W Ultra Thin in the design reports)", ant_total, "interior antenna", "design floor BOM")
    b.item("Donor antenna (SC-530W)", "One per off-air BDA channel group (kit basis per the owner donor rule; the design reports carry SC-230W x3)",
           CARRIERS, "donor antenna", "donor basis 11-25 (SC-530W per owner)")
    b.donor_hardware(3)
    b.note("MU hosts up to 8 BDA cards / 16 off-air channels; up to 32 RF inputs per sector with 8 low-power POI cards (carrier-fed path)")
    b.note("MRUc ceiling per MU with FOU expansion: 16 HUBs per MU with FOU expansion (Consumer OG 0-0-5: up to 16 RF "
           "inputs, 16 HUBs per MU, 256 ARU); the chassis baseline carries 8 optical ports, FOU-EXP adds 8")
    b.note("Part 20 single-sector class: one system set per building; with %d floors this system covers %d of %d floors"
           % (floors_bldg, covered, floors_bldg) if covered < floors_bldg else "Part 20 single-sector class: one system set per building")
    b.note("Reference only (the head end look): the 11-18 iBwave design carries 3 MU-BDA20-2B-HH + 1 MU-DEMOD + "
           "1 MRU-7851719c-DC + 1 MU-FOU on its floor layout; this kit carries the ROM tool's counts")
    kit["covered_floors"] = covered
    return kit


def kit_jma(row, design, floors_bldg, sectors):
    remotes = int(row.get("remotes_bldg") or 0)
    trus = int(math.ceil(remotes / 4.0)) if remotes else 0
    kit = blank_kit("5X Max donor host feeding a JMA TEKO transport, head end ONLY per sector, sized to the ROM tool "
                    "remote count (%d T19AWE links): sub-rack with supervision and power, the 4-way MU optical "
                    "transport cards, POIs per band, SMR700 esaplexer and the server-side hybrid coupler; the "
                    "T19AWE remotes are the ROM tool's design (remote_basis)" % remotes)
    b = Builder(kit)
    b.kit["sector_count"] = sectors
    b.item("5x Max Outside", "Donor-side outdoor amplifier for the off-air source", 1, "remote-donor", BILIBOM)
    b.item("5X Max Inside Host", "Donor host feeding the JMA POIs and the TTRU4W transport", 1, "remote", BILIBOM)
    b.item("Donor antenna (SC-530W)", "One per public-carrier port of the donor host (kit basis per the owner donor rule; "
           "the design reports carry SC-230W x3)", 3, "donor antenna", "donor basis 11-25")
    b.item("Westell CS60-024-002", "RF coaxial surge arrestor on the donor feed (the design JMA group carries 1)", 1, "donor protection", BILIBOM)
    b.item("TLPPOI", "Low-power POI at the TEKO input when the host output lands at or below its range (alternate; pick "
           "per the measured input power)", 1, "head end", "TEKO Master Unit Components doc; owner 11-25 both options", variant="low input power")
    b.item("TAPOI-PCS-F-4", "Active POI for PCS (the design reports carry it for the PCS band of the host feed)", 1, "head end", BILIBOM, variant="per band")
    b.item("TAPOI-AWSE-F-4", "Active POI for AWS (the design P/N is marked discontinued; confirm the current P/N)", 1, "head end", BILIBOM, variant="per band")
    b.item("JMA SUB-TRX-PSU5N", "Master Unit sub-rack, houses the TSPV and TTRU4W modules (owner-provided P/N)", 1, "head end", "owner 11-25")
    b.item("JMA TSPV-NBB", "Supervision module Evo, C-Band, HP (the design reports carry TSPV-NBB)", 1, "head end", BILIBOM)
    b.item("JMA TPSU/AC", "Master Unit power supply module (the design reports carry 2)", 2, "power", BILIBOM)
    b.item("JMA TPANEL-14TE", "Blank panel for the Master Unit sub-rack", 1, "head end", BILIBOM)
    b.item("TTRU4W-S-M", "4-way MU optical transmitter/receiver module for the transport (380-2700 MHz), one card per up to "
           "4 remote links, WDM single strand; sized to the ROM tool remote count", trus, "head end", BILIBOM)
    b.item("JMA TESA-UW2-L", "Esaplexer SDRU SMR700 extended, AMPS+SMR (the design reports carry 1)", 1, "head end", BILIBOM)
    b.item("JMA T-HC3-4", "3x3 hybrid coupler 698-2700 MHz, 4.3-10 (the design reports carry it on the server side)", 1, "head end", BILIBOM)
    kit["remote_basis"] = {"source": "ROM tool remote design (stack_results.json)", "remotes": remotes,
                           "note": "each remote link is a JMA T19AWE (the the design reports' P/N T19AWE19AWEWWAT: RU 2-band "
                                   "PCS1900/AWS3 MIMO, 40 W, AC powered); the ROM tool owns the link count, this kit "
                                   "sizes the head end to it"}
    kit["constraints"].append("POI selection: one POI path installs per sector, picked by the measured input power at the "
                              "TEKO input from the host; the 11-18 design used TAPOI per band (PCS and AWS)")
    kit["constraints"].append("The TTRU4W-S-M itself is the Master Unit optical card (4-way optical Tx/Rx, WDM single "
                              "strand per link); the OTRX family in the TEKO doc (A.2.4.1) is the same card class, so the "
                              "kit lists the transport card, not a separate OTRX line")
    kit["constraints"].append("T19AWE optical: WDM over single strand, <=10 dBo link budget, AGC holds RF gain constant "
                              "across the span; the T19AWE optical uplink output of 6 dBm is the 1550 nm light level (fiber "
                              "link spec, not an RF UL cap)")
    kit["constraints"].append("Uplink: the +10 dBm POI output is the owner's assumption pending written T19AWE figures")
    kit["open_items"].append("TAPOI-AWSE-F-4 is marked Discontinued in the design BOM: confirm the current AWS POI part "
                             "number with JMA")
    kit["covered_floors"] = min(remotes * 1, floors_bldg) if remotes else 0
    return kit


def kit_sdrc_only(floors_bldg):
    kit = blank_kit("Front-end-only mid-power DAS, one system per building sized for the carrier ask: the SDRC front "
                    "end (per-carrier modules in BDA/DFE mode with donor and server channel combiners), the server-side "
                    "hybrid coupler, and the donor side; server distribution follows the design")
    b = Builder(kit)
    b.kit["sector_count"] = 1
    b.item("SDRC-CHA-5", "19 in rack/wall mount chassis, up to 5 SDRC modules (the design reports carry 1)", 1, "head end", BILIBOM)
    b.item("SDRC-CHA-1", "19 in wall mount chassis, 1 module (holds the 6th module in the design)", 1, "head end", BILIBOM)
    b.item("SDRC-PSU-5", "Power supply unit, supports up to 5 SDRC modules (the design reports carry 1)", 1, "power", BILIBOM)
    b.item("SDRC-17-A", "AWS modular repeater, one per carrier (BDA/DFE mode per band; the design reports carry 3; 71 dB max gain, 17 dBm DL)", CARRIERS, "remote", BILIBOM)
    b.item("SDRC-17-P", "PCS modular repeater, one per carrier (the design reports carry 3; 58 dB max gain)", CARRIERS, "remote", BILIBOM)
    b.item("SDR-CHC-V (Donor)", "SDR channel combiner for 700 MHz, Cell, PCS and AWS, donor side, one per carrier (the design reports carry 3)", CARRIERS, "donor front end", BILIBOM)
    b.item("SDR-CHC-V (Server)", "Same combiner, server side, one per carrier (the design reports carry 3)", CARRIERS, "head end", BILIBOM)
    b.item("JMA T-HC3-4", "3x3 hybrid coupler 698-2700 MHz on the server side (the design reports carry 1)", 1, "head end", BILIBOM)
    b.item("Donor antenna pair (AD-PA)", "AD-PA-617-960-D + AD-PA-1900-2600-DIN-X (the design reports carry SC-230W x3 at the roof)", 1, "donor antenna", "donor basis 11-25")
    b.donor_hardware(2)
    b.adrf_donor_protection(2)
    b.note("One system per building sized for the carrier ask: one repeater per carrier or a shared front end; as many "
           "CHCs as the channels need")
    b.note("Donor side is per module: each SDRC-17 module carries its own 4.3-10 donor and server ports (sheet); a shared "
           "donor feed through dividers is the integration choice, not a kit part")
    b.note("Server-side antenna count by build: shared BDAs 1 total, non-shared BDAs 1 per carrier, so 3 for the "
           "per-carrier kit (owner 11-25); the per-floor floor BOM distribution (SC-528W antennas and the Westell CS04 "
           "splitters) is the design side")
    b.note("Low-carrier routing (owner 11-26): the SDR-CHC-V quad carries the Cellular 850 port, so 700 and 850 ride "
           "the channel combiners alongside PCS and AWS; neither reference the design reports carry a separate 7F/C SDRC module "
           "or POIs in this front end")
    b.open_item("Exact POI part numbers if the carrier plan wants POIs ahead of the front end (POI-7L and POI-C): "
                "confirm with ADRF")
    return kit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="sugarhouse-26-01038")
    ap.add_argument("--out")
    ap.add_argument("--sectors", type=int, default=1)
    a = ap.parse_args()
    out = a.out or os.path.join(a.dir, "head_end_results.json")
    with open(os.path.join(a.dir, "oem_results.json")) as f:
        floors_bldg = int(json.load(f)["floors"])
    kits = make_kits(a.dir, floors_bldg, a.sectors)
    doc = {"basis": "head end kit configurations per OEM, reconciled against the iBwave comparison BOMs (11-18/19), the "
                    "Fort Worth ADRF ROM BOM and the ADRF Yan Feng ROM BOM; config and counts only (grade E); unit "
                    "prices intentionally blank",
           "floors_in_building": floors_bldg,
           "sector_note": "ADX V and JMA rows are head end only with parts listed PER SECTOR behind sector_count; the "
                          "remote side is in remote_basis per the ROM tool; Part 20 consumer rows are whole-system kits "
                          "at their stated maximum",
           "sector_count": a.sectors, "kits": kits}
    with open(out, "w") as f:
        json.dump(doc, f, indent=1)
    print("Head end kits, BOM-reconciled (%s): %d floors, %d sector(s); config and counts only; prices blank"
          % (a.dir, floors_bldg, a.sectors))
    for oem, k in kits.items():
        sc = "per sector x%d" % k.get("sector_count", 1) if k.get("sector_count", 1) != 1 else "single system"
        cov = k.get("covered_floors")
        cov_s = ""
        if oem in ("q4000c", "signalmax", "comba_mruc") and cov is not None and cov < floors_bldg:
            cov_s = " (covers %d of %d floors)" % (cov, floors_bldg)
        print()
        print("== %s [%s]%s" % (oem, sc, cov_s))
        print("   %-44s %8s  %s" % ("ITEM", "QTY", "ROLE"))
        for it in k["items"]:
            v = " [%s]" % it["variant"] if it.get("variant") else ""
            print("   %-44s %8d  %s%s" % (it["label"][:44], it["qty"], it["role"], v))
        rb = k.get("remote_basis", {}).get("note", "")
        if rb:
            print("   REMOTE BASIS: %s..." % rb[:150])
        for c in k["constraints"]:
            print("   NOTE: %s" % c)
        for o in k["open_items"]:
            print("   OPEN: %s" % o)
    print()
    print("Wrote %s" % out)


if __name__ == "__main__":
    main()
