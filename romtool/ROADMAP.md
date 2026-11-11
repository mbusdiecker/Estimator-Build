# ROMtool roadmap

The goal is a standalone ROM tool whose output is an iBwave-style BOM for the ANS estimating tool, with a one-line diagram and a floor layout alongside it. Status is tracked in Matt's personal task tracker in Smartsheet (Site "ROMtool").

## Day 1

Matt's items (2026-11-10):
- Check the tool against OEM spec sheets and installation manuals. Matt downloads them from the checklist in romtool-test/oem_docs.
- Downlink link budget from source or remote to the coverage edge.
- Uplink link budget, to the tower site when a donor site is given, otherwise to the POI.
- User inputs for the RSRP target (dBm) and the coverage percentage.
- Ancillaries from each OEM's installation manual, such as brackets, grounding, weatherproofing, jumpers and power supplies.
- Core counter and sleeve counter.

Added at Matt's request (2026-11-10), ranked:
1. Carrier and band setup per job: which carriers, which bands, SISO or MIMO. It drives the POI count, band modules per remote and the link budgets.
2. Donor isolation check for off-air BDA and repeater jobs. Isolation between the donor and service antennas must exceed BDA gain by at least 15 dB. Required for ERCES.
3. ROM summary sheet: inputs, affirmative assumptions, link budget tables and counts, for the estimator's review next to the iBwave BOM.
4. ERCES flags: the 2-hour rated cable and enclosure caveat, and a battery runtime check against the AHJ requirement.
5. Per-remote limits from the spec sheets: maximum antennas and ports per remote, for each OEM.

Already built:
- Auto-placement, with remotes on fiber, taps and couplers, and branch losses.
- L-shaped cable routes.
- Building-type and zone loss, with zones carrying ceiling height and pathway.
- Clutter map, plan import and building-box perimeter.
- One-line and floor layout drawings.
- Parts catalog, grade tiers (in progress), assembly builders (rooftop, closet, riser, GPS, power, monitoring) and the iBwave-style BOM export.

## Day 2

Deferred from Day 1:
- PIM modeling, including flags for parts below the job's PIM spec and connector or cable mismatches.
- MIMO antenna spacing, done together with painted placement areas.
- Handoff and interference between carriers, including sector and dominance planning for multi-sector venues.
- Campus feeder between buildings, for several buildings sharing a head end.

Matt's Day 2 items:
- Paint cable pathways.
- Paint acceptable antenna placement areas.

Further Day 2 items:
- Import walls and pathways from CAD (DXF) and iBwave exports.
- Side-by-side OEM comparison on the same building.
- Export zones to the estimating tool's Labor Areas.
- Calibration loop from closed jobs.
- Revision compare: the ROM against the final design.
- Quote documents from the ROM. No design or BOM goes to the customer before commitment.
- Standalone desktop installer (Tauri), offline, with Excel as the pricing engine.
