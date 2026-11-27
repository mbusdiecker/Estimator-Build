# Sugar House 26-01038: mW power-budget multi-floor check (pmf.py)

Scripts are in this folder (pmf.py, ibwloss.py, combine_oems.py). The floor trace and run outputs are in sugarhouse-26-01038/. Example: `uv run python scripts/pmf/pmf.py scripts/pmf/sugarhouse-26-01038/floor4_soup.json adxv_mid_high 37 --bands B2_1900,B66_2100 --kmax 24 --span 30` from the romtool folder.
Outputs: pmf_{q4000c,signalmax,5xmax,adxv_mid33,adxv_mid37,adxv_low33,adxv_high37,adxv_lpr_lo,adxv_lpr_hi,adxv_hpr_lo,adxv_hpr_hi,comba_mruc,jma_t19awe,sdrc_only}.json/.log plus oem_results.json. Floor 4 soup (10,831 sq ft), iBwave drywall table, thr -103, offset -30.8, ops 3, target 98% min band, 45 floors, even seed capped at N.

Method: P_min(N) is the lowest DL (bisection, 0.25 dB) where all bands reach 98%. It is computed by the ROMtool coverage engine (autoplace_floor), which builds the real per-floor chain: a splitter tree, Manhattan branch cable and branch hardware, solved through the cascade (trunk taps off by default, equal splitters). The formula part of pmf is only the between-floors slice: one port feeds k floors if P_min_mw * 10^(loss/10) <= port mW (the k-way split is part of the loss). Loss for k=2 is 3.01 + 0.5 + 0.58 riser = 4.09 dB. Each fit was confirmed by a placement at the delivered DL. Design N starts at the first N with coverage and area, advances while the next N buys at least 5 coverage points or one more floor per port, and stops once one design feeds all 45 floors (2026-11-25 refinement per request). The planner prune is disabled (a no-op patch below the even seed), so the layout is exactly N evenly placed antennas; the design numbers were verified unchanged with prune disabled, and the only prune artifacts were the at-port coverage readings and the floor tree BOM.

## Q4000C (1 port, 16 dBm)
| N | P_min dBm | headroom dB | k_fit | cov at k_fit | cov at k+1 |
|---|---|---|---|---|---|
| 3 | none (95.7% at port) | | 0 | | |
| 4 | 15.69 | 0.31 | 1 | 98.2 | 84.8 |
| 5 | 15.38 | 0.62 | 1 | 98.8 | 86.2 |
| 6 | 14.75 | 1.25 | 1 | 99.2 | 88.4 |
| 7 | 16.00 | 0.00 | 1 | 98.3 | 85.0 |
| 8 | 15.69 | 0.31 | 1 | 98.7 | 84.1 |
Design N=4, 1 floor per remote, 45 remotes, 180 antennas. Floor 2 needs its own remote.

## SignalMax (4 ports, 17 dBm)
| N | P_min dBm | headroom dB | k_fit per port | cov at k_fit | cov at k+1 |
|---|---|---|---|---|---|
| 3 | none (97.4% at port) | | 0 | | |
| 4 | 15.75 | 1.25 | 1 | 99.0 | 89.8 |
| 5 | 15.44 | 1.56 | 1 | 99.6 | 90.7 |
| 6 | 14.81 | 2.19 | 1 | 99.5 | 92.2 |
| 7 | 16.06 | 0.94 | 1 | 99.0 | 88.5 |
| 8 | 15.75 | 1.25 | 1 | 99.4 | 89.6 |
Design N=4, 1 floor per port, 4 floors per remote, 12 remotes (ceil 45/4), 180 antennas.

## Notes
- Largest headroom (2.19 dB) is below the 4.09 dB a 2-way split costs, so no N lets one port feed 2 floors.
- 5% rule: N=4 is the last N with gain >= 5 points over a failing N. N=5..8 gain under 1 point, so N=4 stands.
- P_min is not monotone in N (N=7 needs more than N=6) because even-seed positions shift with N. Day-2 item.
- Earlier mf2 result (SignalMax k=4 on one port with 5 antennas) is superseded: mf2 let the planner add antennas and fed all 4 floors from the 4 ports with no per-port split check.

## SureCall 5X Max (2026-11-25)

Inputs: +16 dBm downlink (5X Max sheet "Downlink Power: +16 dBm"), 1 output, ops 3, same floor and model as the other runs. The sheet does not say whether +16 dBm is per band or composite. This run treats it as per band, like the Q4000C. Output: pmf_5xmax.json / pmf_5xmax.log.

| N | P_min dBm | headroom dB | k_fit | cov confirmed | cov next floor |
|---|---|---|---|---|---|
| 3 | none (95.7%) | | 0 | | |
| 4 | 15.69 | 0.31 | 1 | 98.2 | 84.8 |
| 5 | 15.38 | 0.62 | 1 | 98.8 | 86.2 |
| 6 | 14.75 | 1.25 | 1 | 99.2 | 88.4 |
| 7 | 16.00 | 0.00 | 1 | 98.3 | 85.0 |
| 8 | 15.69 | 0.31 | 1 | 98.7 | 84.1 |

Design: N=4, 1 floor per unit, 45 units, 180 antennas. The result matches the Q4000C exactly because the inputs are the same (16 dBm, 1 port). The iBwave 5X Max design feeds 20 antennas over 5 floors (8F to 4F) from one unit. This model says one unit holds 98% on only one floor of this plate, so the two disagree. SureCall markets 12 antennas per unit. Matt approved the per-band reading of +16 dBm on 2026-11-25, so the 1 floor per unit result stands for the ROM check.

## ADX V mid power remote (2026-11-25)

Datasheet DL composite for the medium power remote is 33 dBm on 600/700/800 and 37 dBm on PCS, AWS, WCS and C-Band. Runs used one port, design N=3 antennas per floor, kmax 24, bisection span 30 dB.

Fix made before these runs: the k fit in pmf.py multiplied P_min by k and also added 10log10(k) in loss_db, so the split was counted twice. The fit is now P_min_mw * 10^(loss/10) <= port_mw. The SureCall 5X Max result (k=1, 0.31 dB headroom) does not change. Q4000C was rerun with the fix on 2026-11-25 and matches the earlier result: N=4, k=1, 98.2% confirmed, 45 remotes. SignalMax (17 dBm, 4 ports) has at most 2.19 dB headroom, so its k=1 result is also unchanged.

| Port | N | P_min dBm | k fit | Confirmed % | Next floor % | Remotes (45 fl) |
|---|---|---|---|---|---|---|
| 33 dBm | 3 | 17.53 | 13 | 98.3 | 97.4 | 4 |
| 33 dBm | 4 | 15.66 | 16 | 98.2 | 98.0 | 3 |
| 37 dBm | 3 | 17.55 | 20 | 98.3 | 98.2 | 3 |
| 37 dBm | 4 | 15.67 | 24 | 98.2 | 98.1 | 2 |

The first two rows put all four bands at 33 dBm and the last two put all four at 37 dBm. Neither matches the datasheet.

Split-band run (datasheet powers): B12_700 and B5_850 at 33 dBm, B2_1900 and B66_2100 at 37 dBm, k taken as the lower of the two groups. The low bands reach 100% coverage at the bottom of the search range (P_min 3.23 dBm, so the true value is lower) and fit k=24 at every N, which is the cap. The high bands set every result, and the combined answer equals the 37 dBm rows: N=3 gives k=20, 98.3% confirmed, 98.2% next floor, 3 remotes. N=4 gives k=24 (cap), 98.2% confirmed, 98.1% next floor, 2 remotes. With the design rule (see Method), the design row is N=4: k=24 on the kmax cap, so the true floors per port is higher, 2 remotes for 45 floors, 180 antennas. Outputs: pmf_adxv_low33.json and pmf_adxv_high37.json.

At 37 dBm, N=4 to 8 reach the kmax cap of 24, so the real k there can be higher. Combiner insertion loss is not included (CHC-U 0.4 dB, CHC-X2 up to 2.2 dB). The riser model uses one middle-floor remote and half-inch coax at 2.0 GHz. These are grade E config estimates, not measured values. A one-port split across 13 or more floors is a power answer only. Port count, riser cable and fiber layout will set the practical remote count.

## ADX V low power remote (LPR) (2026-11-25)

Datasheet DL composite for the low power remote is 17 dBm on 600 MHz, 700 MHz Commercial and Cellular + SMR800, and 20 dBm on PCS, AWS, WCS and BRS (ADXV Series_Datasheet_Ver 1.0_SS.pdf page 4). One port. Runs used kmax 24 and span 30, with the band groups run separately: B12_700,B5_850 at 17 dBm and B2_1900,B66_2100 at 20 dBm.

| Port | N | P_min dBm | k | Confirmed % | Next floor % |
|---|---|---|---|---|---|
| 17 dBm (B12, B5) | 3 | 0.12 | 15 | 99.0 | 98.0 |
| 20 dBm (B2, B66) | 3 | 17.66 | 1 | 99.6 | 95.4 |
| 20 dBm (B2, B66) | 4 | 15.78 | 2 | 98.2 | 94.1 |

The low bands are not binding at any N (P_min 0.12 dBm at N=3 and negative at N=4 to 8). At N=3 the high-band headroom of 2.34 dB sits below the 4.09 dB a 2-way split costs, so k=1 there. At N=4 the headroom crosses that cost, k=2, and the design rule (2026-11-25 refinement: credit a one-floor-per-port gain) selects N=4: 2 floors per remote, 23 LPRs for 45 floors, 180 antennas, which is the iBwave low power layout of one LPR-P per 2 floors. Outputs: pmf_adxv_lpr_lo.json, pmf_adxv_lpr_hi.json.

## ADX V high power remote (HPR) (2026-11-25)

Datasheet DL composite for the high power remote is 43 dBm on 600 MHz, 700 MHz Commercial (+B14), Cellular + SMR800 and BRS (TDD), 46 dBm on PCS, AWS, BRS (FDD) and C-Band, and 45 dBm on WCS (same datasheet page). One port. Band groups run separately: B12_700,B5_850 at 43 dBm (kmax 80, span 45) and B2_1900,B66_2100 at 46 dBm (kmax 48, span 45). The first run pair used kmax 24 with a 30 dB span; the wider search replaced it because the 30 dB span pinned P_min near its floor and capped k at 24 on both groups. The first runs are kept in old-span30/.

| Port | N | P_min dBm | k | Confirmed % | Next floor % |
|---|---|---|---|---|---|
| 43 dBm (B12, B5) | 3 | 0.11 | 80 (kmax cap) | 98.8 | 98.6 |
| 46 dBm (B2, B66) | 3 | 17.52 | 41 | 98.3 | 97.6 |
| 46 dBm (B2, B66) | 4 | 15.77 | 45 | 98.5 | 98.1 |
| 46 dBm (B2, B66) | 6 | 14.89 | 47 | 99.0 | 98.4 |

The low bands are far from binding: one 43 dBm port feeds more than the kmax 80 cap on power alone, which is more floors than the 45-floor building has. The 46 dBm high bands bind: 41 floors per port at N=3 and 45 to 47 at N=4 to 8. The design rule selects N=4, where one port feeds all 45 floors: 1 remote for the building, 180 antennas, 98.5% confirmed. The walk stops there because a design that feeds the whole building gains nothing from more antennas. Outputs: pmf_adxv_hpr_lo.json, pmf_adxv_hpr_hi.json.

## JMA T19AWE remote (2026-11-25)

JMA publishes no output power for the T19AWE family. The 46 dBm per output figure was confirmed in writing (2 x 40 W outputs, two band paths: PCS on one, AWS on the other). The remote has no 700/850 module. The run models one port carrying B2_1900 and B66_2100 at 46 dBm, kmax 48 and span 45 (the first run used kmax 24 and span 30, which pinned P_min and capped k at 24; it is kept in old-span30/). Both outputs feed the same floors on different bands, so floors per remote is k, not 2k.

| Port | N | P_min dBm | k | Confirmed % | Next floor % |
|---|---|---|---|---|---|
| 46 dBm (B2, B66) | 3 | 17.52 | 41 | 98.3 | 97.6 |
| 46 dBm (B2, B66) | 4 | 15.77 | 45 | 98.5 | 98.1 |
| 46 dBm (B2, B66) | 6 | 14.89 | 47 | 99.0 | 98.4 |

The numbers match the ADX V high power high-band run because the input power and bands are the same. The design rule selects N=4, where one port feeds all 45 floors (k=45): 1 remote for the building, 180 antennas. Output: pmf_jma_t19awe.json.

## Comba ComFlex NG MRUc (2026-11-25)

Port count and power from ComFlex NG MRUc DS 1-0-2: 8 output ports (8 x Nex10-Female), FCC Part 20 consumer, downlink 17 dBm per band per port, bands 700/850/1900/2100 (EAWS), P/N MRU-7851719c-AC or -DC, FCC grant PX8CFNG-MRUc, and up to 16 MRUc per Master Unit. Same treatment as the SignalMax run: all four bands at 17 dBm per port, --ports 8, kmax 4, default band list.

| N | P_min dBm | headroom dB | k per port | cov confirmed | cov next | floors per remote |
|---|---|---|---|---|---|---|
| 3 | none (97.4% at port) | | 0 | | | |
| 4 | 15.75 | 1.25 | 1 | 99.0 | 89.8 | 8 |
| 5 | 15.44 | 1.56 | 1 | 99.6 | 90.7 | 8 |
| 6 | 14.81 | 2.19 | 1 | 99.5 | 92.2 | 8 |
| 7 | 16.06 | 0.94 | 1 | 99.0 | 88.5 | 8 |
| 8 | 15.75 | 1.25 | 1 | 99.4 | 89.6 | 8 |

Every per-port number equals the SignalMax run (same 17 dBm per band). Design N=4: 1 floor per port, 8 floors per remote, 6 MRUc for 45 floors, 180 antennas. The 16-MRUc-per-MU limit is a head-end ceiling, not a floor-side limit, and it does not bind at 6 units per MU for this model. Output: pmf_comba_mruc.json.

## ADRF SDRC Only (2026-11-25)

Inputs from the Part 20 OEM configurator: SDRC-17 all-in-one in BDA mode, 17 dBm downlink shared by the carriers on each module, one SDRC module per band, so one output per band at 17 dBm. Same floor and model as the other runs, --ports 1, kmax 4, default band list. Output: pmf_sdrc_only.json / pmf_sdrc_only.log.

| N | P_min dBm | headroom dB | k | cov confirmed | cov next floor |
|---|---|---|---|---|---|
| 3 | none (97.4% at port) | | 0 | | |
| 4 | 15.75 | 1.25 | 1 | 99.0 | 89.8 |
| 5 | 15.44 | 1.56 | 1 | 99.6 | 90.7 |
| 6 | 14.81 | 2.19 | 1 | 99.5 | 92.2 |
| 7 | 16.06 | 0.94 | 1 | 99.0 | 88.5 |
| 8 | 15.75 | 1.25 | 1 | 99.4 | 89.6 |

The per-port numbers equal one SignalMax or MRUc port because the input is the same 17 dBm per band. Design N=4: 1 floor per SDRC set, 45 sets for 45 floors, 180 antennas.

The configurator's two JMA combos (5X Max + JMA, SDRC + JMA) use the JMA T19AWE row for B2 and B66. Their B12 and B5 come from the base unit (5X Max or SDRC), so read the base unit's row for the low bands. The JMA high bands are not binding at N=4 and the base unit sets the floor count for the low bands.

## All-OEM design rows (2026-11-25)

`combine_oems.py` merges the run JSONs and prints one design row per OEM: for OEMs run once per band group, the combined k per N is the smaller k_fit and the binding run is named; k_cap marks a run that sat on its kmax. `cov confirmed` is the smaller of the band groups for the split runs. Usage from this folder: `uv run --no-project python -I combine_oems.py --dir sugarhouse-26-01038 --out sugarhouse-26-01038/oem_results.json`.

Design basis (owner direction, 2026-11-25): the ADRF ADX V mid power, high power and JMA T19AWE rows are solved at -95 dBm RSRP at 98 percent area; every other row stays at -103 dBm RSRP at 98 percent. The superseded -103 runs for those three are kept in sugarhouse-26-01038/old-edge-103/, and their per-OEM sections above remain as the -103 record.

| OEM | edge | N | k | floors per remote | remotes (45 fl) | cov confirmed % | cov next % |
|---|---|---|---|---|---|---|---|
| Nextivity QUATRA 4000c | -103 | 4 | 1 | 1 | 45 | 98.2 | 84.8 |
| SureCall SignalMax | -103 | 4 | 1 | 4 | 12 | 99.0 | 89.8 |
| SureCall 5X Max | -103 | 4 | 1 | 1 | 45 | 98.2 | 84.8 |
| ADRF ADX V mid power | -95 | 6 | 11 | 11 | 5 | 98.8 | 96.7 |
| ADRF ADX V low power | -103 | 4 | 2 | 2 | 23 | 98.2 | 94.1 |
| ADRF ADX V high power | -95 | 4 | 27 | 27 | 2 | 98.2 | 97.2 |
| Comba ComFlex NG MRUc | -103 | 4 | 1 | 8 | 6 | 99.0 | 89.8 |
| JMA T19AWE | -95 | 4 | 27 | 27 | 2 | 98.2 | 97.2 |
| ADRF SDRC only | -103 | 4 | 1 | 1 | 45 | 99.0 | 89.8 |

The SDRC-only row was added from a parallel session on 2026-11-25 (a pmf run of the SDRC-17 front end at 17 dBm per band in BDA mode) and models it as one floor per port; kept for now, pending the owner's call on whether the front-end-only curve belongs in the power table. Under the -95 basis the design walk moved the mid power row to N=6 and pulled the high power and JMA flat k from the kmax cap of 45 down to 27; the design rule and walk are unchanged.

Design rule note (2026-11-25, applied at Matt's request): the design row now credits a one-floor-per-port gain, so the LPR row moved from N=3 (1 floor per remote, 45 LPRs) to N=4 (2 floors per remote, 23 LPRs, the iBwave low power layout), and the high power and JMA rows moved to N=4, where one remote feeds all 45 floors. The mid power row moved to N=4 with k=24 on the kmax cap. The per-N rows in the sections above still show both readings.

At the mid power design row both band groups sit on their kmax cap, so the true floors per port is higher than 24. The LPR k=2 comes from the 20 dBm high-band group crossing the two-way split cost at N=4. Head-end ceilings outside the power model: QUATRA 12 CUs (6 NU plus 6 fiber hub), SignalMax up to 4 RUs per MU, Comba up to 16 MRUc per MU, ADX V HUB-4 groups for the LPRs. One remote covering the whole building is a power answer only; port counts, riser cable and fiber layout set the practical count before anything goes in a budget. These are grade E config estimates.

## Trunk-tap stack check (stack_check.py) (2026-11-25)

The flat formula models the between-floors slice as a k-way star split plus riser coax. A real multi-floor coax run is a trunked tap chain: the remote floor taps off first, then a single 1/2 inch trunk serves the other floors, each tap ratio the commercial value nearest that floor's equal share (the same ratios the trunk4 planner uses, 3.0 to 20.0 dB plus a 0.3 dB insertion excess), and the trunk end floor fed straight through with no tap. stack_check.py re-solves k against those trunk losses with P_min from the pmf runs (which already carry each floor's own chain) and writes the counted stack BOM per OEM to stack_results.json.

Per riser segment: 1/2 inch at 2.0 GHz (0.144 dB/m, owner direction) over the 4.0 m floor height plus 2 mated connectors at 0.1 dB, so 0.776 dB per floor of climb, and one trunk tap per floor off the equal share. Trunk taps enter from a 2-floor split upward.

| OEM | edge | N | k flat (pmf) | k stack | remotes (45 fl) | worst margin dB | taps | riser 1/2 m |
|---|---|---|---|---|---|---|---|---|
| Nextivity QUATRA 4000c | -103 | 4 | 1 | 1 | 45 | 0.31 | 0 | 0 |
| SureCall SignalMax | -103 | 4 | 1 | 1 | 12 | 1.25 | 0 | 0 |
| SureCall 5X Max | -103 | 4 | 1 | 1 | 45 | 0.31 | 0 | 0 |
| ADRF ADX V mid power | -95 | 6 | 11 | 6 | 8 | 0.24 | 40 | 160 |
| ADRF ADX V low power | -103 | 4 | 2 | 2 | 23 | 0.12 | 23 | 92 |
| ADRF ADX V high power | -95 | 4 | 27 | 11 | 5 | 1.24 | 50 | 200 |
| Comba ComFlex NG MRUc | -103 | 4 | 1 | 1 | 6 | 1.25 | 0 | 0 |
| JMA T19AWE | -95 | 4 | 27 | 11 | 5 | 1.24 | 50 | 200 |
| ADRF SDRC only | -103 | 4 | 1 | 1 | 45 | 1.25 | 0 | 0 |

Taps and riser are building totals for 45 floors (per remote trunk times remotes). The 1/2 inch riser (owner direction) costs 0.576 dB of coax per floor plus 0.2 dB of connector pairs, and the -95 dBm edge costs the trunk designs another 8 dB of P_min, so the trunk numbers tightened twice: the mid power remote holds 6 floors per remote, 8 remotes, on a real tap trunk; the high power and JMA remotes hold 11, 5 remotes; the LPR holds 2 floors, 23 remotes; and every port-fed unit (QUATRA CU, SignalMax RU, 5X Max, Comba MRUc) needs no trunk because each already feeds one floor per port.

The per-floor distribution is identical across curves at N=4: four omni antennas on one 4-way splitter with 70 m of 1/2 in branch coax (229.7 ft) and 8 N connectors per floor, so 180 antennas, 45 four-way splitters, 3,150 m of 1/2 in coax and 360 connectors building-wide, before the OEM trunk and head-end parts. Per remote, add the OEM fiber run from the head end (36.95 m of single mode plus 2 fiber connector pairs per remote in the model; the real length set by the riser path). The counted BOM lines are in stack_results.json under floor_bom (per floor), taps_detail (per trunk tap with its ratio and through loss) and per_remote_fiber_bom (per remote, at the model's own fiber length).

The prune finding from the same session: with the planner prune active, the tree planned at port power pruned to one antenna per floor, so the first floor BOM was wrong; with prune disabled the layout is exactly the even seed and the tree counts above came out. The design rows, P_min and k were verified identical with prune disabled and active, so nothing above the Method line changes.

Caveats carried into this check: the tap ratios are the modeled commercial set, combiner insertion loss is still excluded, and one trunk per remote with taps at every floor is one of several possible riser topologies (a fiber-to-each-remote layout, which is what QUATRA, SignalMax and Comba MRUc actually use, needs no coax trunk at all). The riser is 1/2 inch throughout per owner direction (2026-11-25), so each segment is 0.576 dB of coax plus 0.2 dB of connectors, 0.776 dB per floor. These are grade E config estimates.

## Up-link verification (ul_check.py) (2026-11-25)

The uplink goes end to end: the user stands at the far point of the design, at the edge of acceptable power (the RSRP target: -103 dBm as designed, -95 dBm as the alternative), and the budget runs UE to unit input, through the unit and donor side to the chosen signal source (the cell tower for all eight Part 20 curves; the module hooks a carrier-provided source for later curves). By reciprocity the uplink pays the same one-way loss the downlink paid, so the far-point loss is locked by geometry and the -95 versus -103 choice only moves the DL design port by 8 dB; the UE's uplink arrival at the unit is unchanged by that choice.

Verified uplink figures (datasheets checked directly): Comba MRUc UL output -15 dBm per band per port, gain 35 dB, NF 6 dB; QUATRA 4000c UL power 20 to 22 dBm per band, NF 7 dB; SignalMax Fiber per-band UL caps 20.89 to 23.49 dBm; ADX V NF 5 dB at max gain (max UL gain not published); 5X Max and JMA publish no uplink figures. UE Tx 23 dBm (power class 3 assumption for the survey to replace). Tower UL sensitivity default -120 dBm for the service line, and the tower path loss itself is a survey input; the module emits the maximum allowed tower path loss rather than inventing one.

| OEM | edge | DL port dBm | far-point loss dB | UL in at unit dBm | SNR at 10/5/1.4 MHz dB | UL out | tower line |
|---|---|---|---|---|---|---|---|
| QUATRA 4000c | -103 | 16 | 114.2 | -91.2 | 5.8 / 8.8 / 14.3 | far-user out 8.8 dBm (gain 100, band cap 22) | PL <= 115/118/123 dB; dmax 7.5/10.6/20.1 km |
| SureCall SignalMax | -103 | 17 | 115.2 | -92.2 | 3.8 / 6.8 / 12.3 | far-user out -20.2 dBm (gain 72, band cap 20.89) | PL <= 86/89/94 dB; dmax 267 m/377 m/713 m |
| SureCall 5X Max | -103 | 16 | 114.2 | -91.2 | 4.8 / 7.8 / 13.3 | far-user out -19.2 dBm (gain 72, cap 26) | PL <= 87/90/95 dB; dmax 299 m/424 m/800 m |
| ADRF ADX V mid power | -95 | 37 | 127.2 | -104.2 | -5.6 / -2.6 / +2.9 | system out -24.2 dBm (SDRC included) | PL <= 92/95/100 dB; dmax 496 m/701 m/1.3 km |
| ADRF ADX V low power | -103 | 20 | 118.2 | -95.2 | 3.4 / 6.4 / 11.9 | system out -15.2 dBm (SDRC included) | PL <= 101/104/109 dB; dmax 1.4/2.0/3.7 km |
| ADRF ADX V high power | -95 | 46 | 136.2 | -113.2 | -14.6 / -11.6 / -6.1 | system out -33.2 dBm (SDRC included) | PL <= 83/86/91 dB; dmax 176 m/249 m/470 m |
| Comba ComFlex NG MRUc | -103 | 17 | 115.2 | -92.2 | 5.8 / 8.8 / 14.3 | far-user out 19.0 dBm (MRUc 35 + BDA 80, caps -15/+19) | PL <= 125/128/134 dB; dmax 24.4/34.5/65.3 km |
| JMA T19AWE | -95 | 46 | 136.2 | -113.2 | -24.2 / -21.2 / -15.7 | far-user out 5.8 dBm (MU 47 + host 72) | PL <= 122/125/130 dB; dmax 15.7/22.2/41.9 km |
| ADRF SDRC only | -103 | 17 | 115.2 | -92.2 | 5.8 / 8.8 / 14.3 | system out -21.2 dBm (AWS gain 71) | PL <= 95/98/103 dB; dmax 761 m/1.1/2.0 km |

Tower lines are per allocation width (10/5/1.4 MHz). The tower sensitivity is the 3GPP eNB reference for a wide-area base station (-101.5 dBm at 10 MHz, QPSK), scaled to each width by the noise floor: -104.0 dBm at 5 MHz and -110.0 dBm at 1.4 MHz (the standard's own 1.4 MHz row is -106.8, 3.2 dB more conservative than the pure scaling). Donor antennas are per row family, as the owner directed: the SureCall rows (QUATRA, SignalMax, 5X Max) and the Comba row keep the SC-530W wideband yagi (7 dBi at 700 MHz, 8 dBi at 850, 9 dBi at 1900 and 2100), and the ADRF family rows (ADX V, SDRC only, JMA) use the ADRF donor pairs, sheet-verified: AD-PA-617-960-D (15.5 dBi in the 698-806 MHz uplink segment) and AD-PA-1900-2600-DIN-X (19.1 dBi across 1710-2170 MHz), each fed by 30 m of 1/2 inch corrugated coax from the head end to the roof plus two mated connectors, at each band's loss, an assumption the survey replaces with the real run. Distances are free-space path loss from the donor antenna at the band's uplink center frequency; real macro paths add clutter and terrain, so these are upper bounds, and the ordering between rows carries the comparison.

Real macro paths add clutter and terrain, so these are upper bounds, and the ordering between rows carries the comparison. (A 1 km coverage-rule variant was tried here on 11-25 and rolled back at the owner's direction; the verdict logic was removed. Comba's uplink chain is under re-audit.)

The uplink path carries no carrier split: one UE transmits its full 23 dBm on its own carrier, and the DAS is a linear passive chain, so the far user's signal returns to the unit at full single-carrier strength. The split that does matter sits on the downlink side of the reciprocity: the design edge is set by ONE operator's share of the composite (37 and 46 dBm are composite ratings; with 3 operators sharing, the coverage edge is delivered at port minus 4.77 dB), so the physical far-point loss is the port minus the operator split minus the edge, and every uplink input reads 4.77 dB better than a composite-to-edge read would give.

The finding on that corrected basis: the LPR two-floor trunk now closes at every allocation width (uplink SNR +3.4 dB at 10 MHz at the far floor with the composed system noise figure); the mid power trunk closes only at a 1.4 MHz edge allocation (+2.9 dB) and fails at 5 and 10 MHz; the high power trunk fails at every width (-6.1 dB at 1.4 MHz), and the JMA trunk fails harder (-15.7 dB at 1.4 MHz) because its digital remote's 15 dB noise figure is worse than the ADX V analog system at the same input. The port-fed designs close with real margin everywhere (+3.8 to +14.3 dB) with SignalMax verified from its user guide, and the SDRC front-end-only architecture joins them (+5.8 dB at 10 MHz). Gain on the unit cannot restore any of these numbers, because gain lifts signal and input noise together; only a nearer uplink termination (the LPR pattern, or port-fed units at one floor per port) or a lower design edge moves them.

Uplink gain and output, per published figures and the owner's corrections: the ADX V's -20 to +10 dB uplink range is end-to-end system gain with the head end included (maximum +10 dB, noise figure 5 dB at max gain), and the SDRC donor front end adds its own uplink stage. The SDRC figures are now sheet-verified from SDRC_Datasheet_v3_SS.pdf (v0.2, 9/23/2024): Maximum Gain (DL/UL) per band 57 dB at 700 MHz (B12/13), 62 dB at Cellular 850 (B5), 70 dB at PCS (B25), 71 dB at AWS-1 (B4); Composite UL Output Power 22 dBm max; Noise Figure 11 dB at 700/850 and 6 dB at PCS/AWS-1 at max gain; Part 20 certified, so its 22 dBm sits inside the 30 dBm consumer composite cap. Composing the SDRC noise figure behind the ADX V's 10 dB of preceding gain through Friis raises the ADX V system noise figure from 5.0 to 5.4 dB on the high bands, which shifts the ADX V SNR rows by about 0.4 dB (verdicts unchanged). The 5X Max figures are sheet-verified from surecall.com (Maximum Gain 72 dB, Max Uplink Power 26.0 dBm, Noise Figure <8 dB with 8.0 conservatively used, Maximum Output Power 1 W EIRP), which closes its own row at every allocation width and supplies the missing last stage of the JMA combo chain. The SignalMax figures are verified from SignalMax_Fiber_DAS_User_Guide.pdf (Noise Figure <8 dB with 8.0 used, Maximum Uplink Power 30 dBm EIRP as the composite ceiling, Maximum Gain 64 dB below 1 GHz and 72 dB above), which closes its SNR column (+3.8 / +6.8 / +12.3 dB) and joins the port-fed closers. For the JMA combo the owner directed assuming a maximum uplink output of +10 dBm at the POI; that sits between the master unit and the host and never binds for a single far user (the far user's payload is -66.2 dBm at the POI, 76 dB below it), so it is a loaded-case ceiling, not a service line. With the full chains in, the tower lines compute per allocation width: the mid power far user allows 86/89/94 dB of tower path at 10/5/1.4 MHz, the low power 95/98/103, the high power 77/80/85, the 5X Max 91/94/99 (its own far user transmits -19.2 dBm under the 72 dB gain), the SignalMax 90/93/98, and the JMA combo 116/119/124 (the far user at the remote input -113.2 dBm plus the master unit's 47 dB plus the host's 72 dB reaches the donor at +14 dBm). The tower line closing does not rescue the uplink signal quality: the far user's own SNR at the first active stage is unchanged, so the mid power still serves only narrow uplink allocations, the high power still fails at every width, and the JMA combo still fails harder (-15.7 dB at 1.4 MHz) because the digital remote's 15 dB noise figure is worse than the ADX V analog system's 5.4 dB composed at the same input. The analog rows above carry the FAR-USER line (that user's own gain-limited output); the loaded ceilings are capacity notes, not service lines (Comba's composite -15 dBm cap corresponds to a 113 dB loaded line, the 5X Max's 26 dBm uplink power a 119 dB loaded line). The JMA figures come from the TEKO TRU and TRL platform datasheets in the JMA Specs folder: uplink noise figure 14 to 15 dB per band and "Uplink RF gain, out Master Unit Rx 47 dB" (a master-unit figure, not a remote gain); the T19AWE itself still publishes no power. The "Optical uplink output power 6 dBm" line in those sheets is light on the 1550 nm uplink wavelength, not radio frequency: it sits in the Optical block with the fiber type, the +6 to -4 dBm nominal optical input window and the 10 dB optical link budget that the AGC holds to constant RF gain across the span. It is a fiber link spec, not the remote's maximum uplink output, and it does not enter the RF budget; its practical effect is that fiber loss up to 10 dB costs no uplink level within the AGC range. What remains owed for JMA: written T19AWE power and UL figures, and the master unit's composite uplink output cap beyond the owner's +10 dBm POI assumption (the TRU sheets publish the 47 dB gain but no output power).

Digital regeneration explains none of the spread: no row in the table receives a regeneration shortcut. Every unit's far-user line is its own gain-limited payload through the full published chain, with each stage's caps applied in order: the QUATRA's 20-22 dBm per-band values are loaded ceilings behind its 100 dB system gain, the SignalMax per-band table is a loaded ceiling behind the 72 dB user-guide gain, and the ComFlex NG's -15 dBm is the MRUc mid-stage ceiling before its MU BDA card adds 80 dB of gain and a +19 dBm output. On that basis SignalMax lands at 267 m to 713 m by width, the QUATRA at 7.5 to 20.1 km, and the corrected Comba row at 51.7 to 138.3 km, because its far user exits the BDA card at the +19 dBm output and the Part 20 consumer EIRP ceiling of 1 W including the donor antenna then caps the radiated level. The 1 W EIRP ceiling (47 CFR 20.21(e)(8)(i)(D)) is applied to all Part 20 consumer rows: QUATRA, SignalMax, 5X Max and the ComFlex NG; the industrial ADRF chain is not EIRP-capped by it. The longer reaches remain free-space upper bounds: at those distances the tower's downlink at the donor antenna becomes the deployment bound, which the survey settles. The JMA combo's 15.7 to 41.9 km is the same residue (its far-user payload of +5.8 dBm through the master unit and host gains is gain-based already, and the +10 dBm POI ceiling never binds a single user).

The composite rule applies where the unit transmits, not on the path: a unit's uplink output is the sum of all carriers' users plus its own noise, so the FCC 1 W composite cap of 47 CFR 20.21(e)(8)(i)(D) and the OEM caps (the ComFlex NG MRUc's -15 dBm mid-stage ceiling and its MU BDA card's +19 dBm output, the SDRC's +22 dBm, the 5X Max's 26.0 dBm uplink power, the SignalMax 30 dBm EIRP system ceiling) bind on the composite, while the tower service line for each carrier uses that carrier's own signal level. The 1 W composite EIRP ceiling, donor antenna included, is applied to the Part 20 consumer rows (QUATRA, SignalMax, 5X Max, ComFlex NG); the industrial ADRF chain is not EIRP-capped by it. What remains open is limited to JMA: the written T19AWE figures, with the +10 dBm POI output carried as the owner's assumption.

What follows for the comparison: the rebuild's "one high power remote reaches all levels from mid-stack" claim fails on the uplink at both edges; a -95 standard with these ports shrinks both the trunk and the covered building depth; and the -95 edge buys 8 dB of uplink margin over -103 at the same ports because the UE stands 8 dB closer in. The far-point convention in the module is the conservative one: the far-point loss is the design port minus the operator share minus the design edge, with no coverage margin credited.

## Next steps (phase 2)

Owner queue, 2026-11-25, in order. Phase 2a (head end kit config) started in this iteration: scripts/pmf/head_end_check.py builds all 9 kit configurations, counts and config only, prices blank, emits sugarhouse-26-01038/head_end_results.json. Open items were checked against the spec sheets (2026-11-25). The ADRF module chassis is now grounded: ADXV-R-CHA takes 7 standard-size remote modules (CHA-3 and CHA-4 variants; the sheet requires a fan above 4 RAMs per chassis, part ADXV-R-FAN-DC), and ADXV-HPR-CHA takes 6 HPR modules (HPR-RACK and HPR-PMK expand beyond it). The ComFlex MRUc ceiling is grounded at 16 HUBs per MU with FOU expansion (Consumer OG 0-0-5). The SDRC donor side is per module: each SDRC-17 module carries its own 4.3-10 donor and server ports. Owner answers closed the rest (2026-11-25): the TEKO Master Unit sub-rack is SUB-TRX-PSU5N (owner P/N), the QUATRA NU donor ports run left to right Private, Verizon, T-Mobile, AT&T, so the kit mounts one SC-530W per public-carrier port (3, private port reserved), and the SDRC server side is 1 antenna total for shared BDAs and 1 per carrier (3) for non-shared BDAs. The 5X Max 12-antenna tree needs 8 splitters (6 two-way + 2 three-way, 6 antennas per interior port), now listed as kit parts, and the LPR kit is head end only, sized to the ROM tool's remote count: the remote side is the ROM tool's design, and the kit lists 6 hubs (4 primaries per hub, ceil(23/4)), the NMS, the POIs and the front end. The remote_basis field carries the ROM tool remote count (23 LPR sites) and the module-pair reference (one ADXV-L-20PAWBT-P primary plus its ADXV-L-1767S8C-S secondary per site, cross-connected over the SMA ports; secondary count equals primary count). The ADX V kits also carry the combining and fiber cards the sheet names: one ADXV-H-CHC-88-DL and one ADXV-H-CHC-88-UL 8x8 combiner per sector (600 MHz to BRS, the -X variant covers to C-band) and ADXV-H-ODU-4 optic donor units at ceil(RUs/4) = 2, leaving the 12-slot NMS at 7 of its slots; the JMA kit carries one JMA MU-OTRX fiber card per remote link (5). No open items remain in head_end_results.json.  2. Public safety ROM. ERCES designs built from code requirements by AHJ. Pull the actual code chain (IFC through NFPA 1225 and whatever the AHJ layer adds) rather than assume it. The Fort Worth FirstNet PSR-78 work is the first candidate to generalize.
3. Configurable ROMs, the end state: the pmf design, the head end kit and the BOM wired into the estimator's budget flow, so a job hand-off produces a ROM without manual sheets. The survey placeholders carried in this module (30 m donor line, antenna heights, UE power class, tower path) are the inputs a ROM replaces with survey data.

FCC framing: the consumer composite uplink cap of 1 W (30 dBm) under 47 CFR 20.21(e)(8)(i)(D) and the 1 W composite EIRP ceiling including the donor antenna are both satisfied within the rules by the published values in every chain here (the ComFlex NG's binding figures are its own: the MRUc's -15 dBm mid-stage cap and the MU BDA card's +19 dBm output); the uplink noise and gain rules of 20.21(e)(9) and the tower path loss need the survey; industrial units run under 20.21(c) licensee consent.

Open items from this check: the written T19AWE uplink figures from JMA (the +10 dBm POI output is carried as the owner's assumption), and the survey inputs: tower path loss, the real donor run length, antenna heights, and the UE transmit power class. Every other figure was verified against vendor sheets on 2026-11-25. Outputs are in sugarhouse-26-01038/ul_results.json.

## Head end composition reference (2026-11-25)

The head end kits were shaped to what a real head end looks like, against three sources shared by the owner. The counts stay the ROM tool's and the facts stay from the spec sheets; the BOM part names and hardware classes fill in the look: the iBwave
equipment list reports in the Sugar House job folder (11/18-11/19, designer P. DelPizzo: Nextivity,
SureCall SignalMax, 5X Max Only, 5X Max-JMA, ADXV LP Only, SDRC Only, Comba), the ADRF Fort Worth City
Hall ROM BOM, and the ADRF Yan Feng ROM BOM. What the BOMs put in the kits (the look): real part names (Q44-1M34CNU
host, Q41-RECU CUs, Q40-1234FNU Fiber Hub, SignalMax Fiber_MU/Fiber_RU, MRU-7851719c-DC, TSPV-NBB,
TPSU/AC x2, TPANEL-14TE, TESA-UW2-L, T-HC3-4, SUB-TRX-PSU5N, T19AWE19AWEWWAT); the ADX V head end cards
(ADXV-H-CHC-88-DL-X and UL-X 8x8 combiner pair per sector, ADXV-H-ODU-4-X optic donor units at ceil(RUs/4),
one strand per RU at 5 dBo or less, replacing the HUB-4 in the LPR kit); POIs per band per carrier
(POI-P x3, POI-A x3, POI-7L x1 per the Fort Worth pattern; the DFE-mode designs carry none); the SDRC
front end composition from the designs (SDRC-CHA-5 + CHA-1 + PSU-5, SDRC-17-A x3 + SDRC-17-P x3 in
BDA/DFE modes, SDR-CHC-V donor x3 and server x3); the JMA transport card (TTRU4W-S-M is itself the 4-way
MU optical card at one card per up to 4 links, so the separate MU-OTRX line is gone and 5 links take 2
cards); the ComFlex additions (MU-DEMOD x1, MU-BDA20-2B-HH x3 one per carrier); the classes every BOM
carries (Westell CS60-024-002 arrestors per donor port, Polyphaser TSX-NFF plus BFN brackets on the
AD-PA donor pairs per the Fort Worth precedent, the OCC fiber termination kit where the head end
terminates fiber, ADRF jumper kits, the Westell CS04 splitter family on the antenna distribution). The
ADRF mid/high kits are head end only now: the remote site kit (R-CHA or HPR-CHA, PSU, ORU fiber unit,
band modules, ADXV-R-CHC-U) moved into remote_basis with the ROM tool owning the count. For context: the
11-18 design reports show fewer units on their layouts than the comparison tool's whole-building runs
(8 CUs vs 12, 2 SignalMax RUs vs 4, 1 MRU and 1 FOU vs 6 and 2, 1 T19AWE vs 5 links); those references stay
flagged in the kit constraints and the kits carry the ROM tool counts. Owner closes (2026-11-26): the 850 POI is required with the 700 POI, and the SDR-CHC-V quad carries the
Cellular 850 port for the front end routing, so the kits now carry POI-7L x1 and POI-C x1 (slots at 12 of 12
exactly, no expansion rack) and the SDRC front end routes 700 and 850 through the CHC-V quads with no
separate 7F/C module. Remaining kit opens: the exact POI-7L/POI-C part numbers with ADRF, and the
TAPOI-AWSE-F-4 replacement (marked discontinued in the design). The floor-BOM versus tool-count
deltas stay flagged in the kit constraints: the kits carry the ROM tool counts, and the design reports are
the same tool's output for its own (subset) floor BOMs, so no reconciliation action is pending. Day 2 requests
(public safety ROM, estimator wiring, pricing) are on hold at the owner's direction. The committed-state
open (both repos' 11/25 work) remains awaiting the owner's go.

## Estimator wiring complete (2026-11-26)

The confirmed head end kits are built into the ROM tool. The wiring point is romtool/oem.py: the tool's
own expansion engine (expand_oem, called from project_items in grades.py with the run's settings) now
sizes the confirmed kits itself. Changes: (1) config/oem_specs.json carries eight new spec packages, one
per confirmed kit row - nextivity_q4000c, surecall_5xmax, adrf_adxv_mid, adrf_adxv_high,
adrf_adxv_lpr, comba_comflex, jma_teko_5xmaxhost, adrf_sdrc_front_end - each with the sheet-verified parts,
kit quantities, limits and a head_end block with sizing rules; SignalMax was already in as
surecall_fibermax. (2) _expand_spec gained the head_end sizing rules: project (fixed qty), when_remotes
(one site set when the tool has remotes), beyond_master_max (the QUATRA Fiber Hub per 6 CUs beyond the
CNU's six), remotes_per_N (ceil(rus/N): the ODU-4-X per 4 remotes, the TTRU4W-S-M 4-way card per 4
links), and per_remote entries multiplied by the tool's modeled remote count; the kit's own donor side
drops the generic off-air source line. (3) Tolerances: _expand_spec no longer requires the antenna
density limit keys (they are optional), and _line honors a per-part unit. (4) Verified two ways: a
run-through per package sizes the kits from the tool's inputs (QUATRA 12 CUs -> CNU 1 + Fiber Hub 1 + 12
CUs + 3,936 ft Cat5e + 3 donors + 3 arrestors; ADX V mid 8 RUs -> 8 POIs + combiner pair + 2 ODU-4-X +
AD-PA pair + 2 arrestors + 2 TSX-NFF/BFN + fiber kit + 8 remote site kits; JMA 5 links -> sub-rack set +
2 transport cards + POIs + 5 T19AWEs; SDRC front end per the floor 8 design), and the tool's own test
suite passes: 457 passed, 17 skipped, 0 failures (with pytest, httpx, starlette, pytest-asyncio, openpyxl,
pillow, numpy, defusedxml, cryptography added for the run; the API tests need the httpx stack). The
head_end_check.py comparison builder stays as the comparison artifact; the estimator source of truth is
now the tool's own expansion. Open items travel with the kits in oem_specs.json (POI-7L/POI-C exact P/Ns,
TAPOI-AWSE-F-4 replacement). Day 2: the public safety ROM phase and the pricing phase (fresh ask) remain
held.

## Public safety ROM per OEM and frequency (2026-11-26)

Runs in sugarhouse-26-01038: pmf_ps_*.json (nine runs), ps_rom_results.json (the table), superseding
ps_ul_results.json (its chain math was wrong; the DL side of the ROMs is what the ROM table carries).

Basis: the same pmf machinery as the cellular comparison; the coverage preset is the code one
(95 percent floor area / 99 percent critical area at -95 dBm at the portable, IFC 510.4.1 /
NFPA 1225 18.8-18.9 per PS_CODE_TABLE). Port powers are sheet-verified per OEM: FiRe-78 33 dBm
per remote DL (30 dBm for the Band 14 composite run), Westell CS45 34 dBm per band, Comba V3
33 dBm per band, PSR-VU-9537 28 dBm VHF / 37 dBm UHF. N = 3 antennas per floor by the advance rule.

Findings: every OEM and frequency covers the whole 45-floor building from one port (P_min about
-11.5 to -11.8 dBm per floor, k = 48 taps, 100 percent coverage confirmed); the LMR -95 dBm
threshold is far easier than the LTE RSRP basis, so the ROMs are head-end-bound, not port-bound.
The first span-20 pass (k = 21) had bisect clipping; superseded by the span-60 reruns recorded
in the provenance note.

Open (grade E and sheet gaps): the -95 dBm figure is the IFC 2015/2021 wording used as the
design input (the 2024 IFC and NFPA 1225 say DAQ 3.0 without a number); the critical-area tier
applies where the fire code official designates it; the uplink side needs the AHJ site criteria
(510.4.2.2) and is outside these DL numbers; the PSR-VU 9537-X gain is range-only in the ds
(85-95 dB) so the 38 dBm DL port row is the UHF composite top of range; per-remote PSU draws
for the ADXV-R and the Comba V3 remote remain the 45 W placeholders.

## UL, near-far, grid, compliance, IMD, and the owner gates (2026-11-26, day 2)

New modules in scripts/pmf, all grade E estimates, no prices:
- ps_ul.py: the talk-in engine. Band-reciprocal chain (ul at the BDA = portable
  EIRP - DL port + predicted DL at the point), the AHJ site inputs (ERP, site
  NF, ambient cal, donor gain), the arrival target = the site noise floor +
  18 dB (DC), the noise coordination against the -150 dBm LA rule or the -
  15 dB DC rule, the squelch threshold = the distribution input noise + 5 dB,
  and the near test with the input pad recommendation. Runs per OEM per band
  group; vendor UL gain/NF/output caps from the sheets with placeholders
  flagged.
- ps_grid.py: the IFC 510.5.4 grid-method self-check from the same floor
  model: 20 cells honoring the 24.4 m max grid dimension, cell-center
  predictions through the planner's own propagation (_lin_from/_contrib),
  pass/fail per cell on RSSI and the SINR/BER equivalents, the 40-cell retest
  flag, the calculated uplink per cell (LA County method) with the UL gain
  derived from the worst cell, the critical-area discrete points when the
  floor carries critical areas, the per-floor level spread (min/p5/p95/max)
  and the per-antenna 1.5 m near-ring level.
- ps_compliance.py: the FCC 90.219 ERP-per-channel plate (37 dBm cap), the
  noise figure plate (9 dB), the quiescent noise ERP notes, and the
  propagation delay budget (filter + fiber at 5 us/km + coax) against the
  15/32 us AHJ limits with the Collier filter-width recommendation.
- ps_imd.py: a two-tone IM3 screen for converged cellular+PS plates; band
  centers stand for the carriers; flags the products landing in the PS
  passbands (the PIM test trigger the AHJs cite).
- ps_rom.py: the assembler that folds the pmf runs, the UL, the grid, the
  compliance plates, and the IMD screen into one ps_rom_results.json, plus
  the owner gates.

The owner gates (2026-11-26: 'antenna square footage and remote coverage
should be limited at some point, build it in but do not turn it on yet'):
the antenna-area cap (--caps-area-sqft, evaluated per floor in the grid
module: it reports would_require_min_antennas) and the floors-per-port cap
(--caps-k, reported in the ROM rows and applied by --caps-on in ps_rom).
They are OFF in every saved run; the saved demo ps_grid_capsdemo.json shows
the gate output with a 10,000 sq ft example. Turning them on as build rules
is a later owner decision.

Findings the modules produced on the Sugar House floor model (design plate
N=3 per floor, k=48):
- The grid cells pass everywhere (20/20) with the engine's own placement;
  the placement optimizer settles for 1 antenna per floor at these port
  powers while the design plate says 3, so the placement tension is real and
  the grid module reports which placement it used.
- The UL solution exists at every OEM/band (recommended gain 19-30 dB class,
  the squelch around -125 dBm at the BDA input) and the site noise sits far
  below the -150 dBm rule at the default 50 dBm ERP / -85 dBm ambient cal.
- The near test FAILS by default everywhere: a portable 1.5 m off the
  antenna drives the BDA input to about -1 to -5 dBm; the modules emit the
  input pad (about 10-14 dB) that fixes it, per the DC near-test rule.
- ERP per channel busts the 5 W cap on the 33 dBm ports with 5 dBi antennas
  (38 dBm; the UHF PSR-VU at 42 dBm needs a 5 dB pad): the plate emits the
  pad per run.
- The 12.5 kHz Class A filter alone is 80 us of delay: over both the SBC 15
  us and the Collier 32 us limits; the plate recommends the 75 kHz filter
  (13.6 us) per Collier's own practice.
- The converged-plate IM3 screen found 2 products landing in UHF (387.5 MHz
  from N71+B5, 450 MHz from B66+N77): the PIM test trigger for shared plates.

## Roadmap and state (2026-11-26, owner directives applied)

Day 2 builds, done and verified:
- PS ROM per OEM and frequency with the near-far engine (branch loss / pad /
  density levers, the balanced model as the default), the grid-test self-check
  on the real floor plan (best-server cells, union coverage, the near test at
  the nearest walkable 3D point), the loading-case option, the per-AHJ
  profiles, the PIM field gate, the compliance plates, the intermod screen.
- BOM export: ps_rom --bom-out/--bom-csv writes the flat BOM (the kit lines
  from the package expansion at the design remote count plus the design plate
  lines), rebalanced equally per remote. Counts and config only.
- Floors split and mash (the owner approach): run the chosen floor groups as
  separate designs (ps_rom --floors N), then --combine mashes the quantities
  (the remotes and antennas add, the rebalance re-splits).
- Owner gates report-only: 250k sq ft per remote/BDA; the antenna-area cap
  still open (the owner has not set a number).

Day 2, dormant by owner decision: the estimator configurator surface for the
PS packages (the expansion works; the estimator side waits).

Day 3: PRICING. Starts only on the owner's fresh ask. The BOM export above is
the ingestion point for the budget tool; the pricing phase layers cost on
these groups and then hands off to the budget workbook.

Vendor spec list still missing (grade D placeholders in the tool until they
land):
- All four units: maximum operating input power and damaging threshold (the
  near-test line is -10/-0 placeholders); the AGC behavior mode (the active
  vs programmed channel sharing).
- ADRF FiRe-78-8-U1: UL gain range per band, UL composite output confirmation
  at other gains, the squelch capability and threshold range.
- ADRF ADXV-R-3378P-U2: per-band UL gain, per-band PSU draw split (102 W total
  provided), the noise figure per band.
- Westell CS45-734-834-A0: the CS45 NF (the CS40 figure is standing in), the
  fiber-DAS remote variant draw and specs.
- Comba V3 PS: UL output per band at the specified gain, the ALC mode default
  (the active vs programmed sharing), the RU UL NF per band.
- ADRF PSR-VU-9537-X: UL output cap, max input, the duplexing-loss figure.
- Per site (the AHJ): the channel counts per band, the site ERP/NF/antenna
  data, the coverage threshold wording adopted (the -95 stands per the owner).

Owner build preferences: profiles ride along as the engagements name them; the
split-and-mash replaces a sectors flag; the estimator stays out of this clone
until the owner moves.
