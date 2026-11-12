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
- Parts catalog, grade tiers, assembly builders (rooftop, closet, riser, GPS, power, monitoring) and the iBwave-style BOM export.
- OEM packages for ten OEMs (config/oem_packages.json). Selecting one replaces the generic head end, remote and repeater lines with that OEM's most-quoted parts, counted from ANS quote-history ratios. A ratio is applied only when at least three jobs support it.
- Calibration run 5 (2026-11-11): building-type clutter fitted on 32 ANS designs, with floors and areas taken from the design documents instead of the budgets. Leave-one-out, 14 of 32 jobs land within 15% of the design antenna count, with a median error of 17% (run 4a was 10 of 33 and 36%).

Calibration, still to do before the accuracy bar (8 of 10 jobs within 15%) is met:
1. Turn fiber remotes on by default in auto-placement, or per system type. The run 5 clutter values were fitted with remotes on, and auto-placement still defaults to remotes off.
2. Load the OEM remote specs (Matt's Thursday download) in place of the 33 dBm and +10 dB stand-in remote defaults, then refit clutter.
3. Add a covered square footage and floor count input. Budget area and floor counts were wrong on most of the office test jobs (25 floors on a 3-level parking deck, 13 floors on a 4-floor building), so the ROM should take them from the design documents or the estimator.
4. Grow the dense office set. It rests on three FLETC buildings and misses both ways.
5. Look at the step size of remote sizing. On Burnett Plaza the count jumps from 33 to 77 to 121 as remotes are added, which limits how close one clutter value can land. NYC OTI, a DRAN design at about 2,000 sq ft per antenna, also misses badly.
6. Calibrate parking garage and school, which have one job each (grade E).
7. Rerun the 15% test after items 1 to 3.

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
