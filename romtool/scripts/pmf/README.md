# Sugar House 26-01038: mW power-budget multi-floor check (pmf.py)

Scripts are in this folder (pmf.py, ibwloss.py). The floor trace and run outputs are in sugarhouse-26-01038/. Example: `uv run python scripts/pmf/pmf.py scripts/pmf/sugarhouse-26-01038/floor4_soup.json adxv_mid_high 37 --bands B2_1900,B66_2100 --kmax 24 --span 30` from the romtool folder.
Outputs: pmf_q4000c.json/.log, pmf_signalmax.json/.log. Floor 4 soup (10,831 sq ft), iBwave drywall table, thr -103, offset -30.8, ops 3, target 98% min band, 45 floors, even seed capped at N.

Method: P_min(N) is the lowest DL (bisection, 0.25 dB) where all bands reach 98%. One port feeds k floors if P_min_mw * 10^(loss/10) <= port mW (the k-way split is part of the loss). Loss for k=2 is 3.01 + 0.5 + 0.58 riser = 4.09 dB. Each fit was confirmed by a placement at the delivered DL.

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
| 7 | 16.06 | 0.94 | 1 | 99.6 | 88.5 |
| 8 | 15.75 | 1.25 | 1 | 99.1 | 89.6 |
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

Design: N=4, 1 floor per unit, 45 units, 180 antennas. The result matches the Q4000C exactly because the inputs are the same (16 dBm, 1 port). The iBwave 5X Max design feeds 20 antennas over 5 floors (8F to 4F) from one unit. This model says one unit holds 98% on only one floor of this plate, so the two disagree. SureCall markets 12 antennas per unit.

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

Split-band run (datasheet powers): B12_700 and B5_850 at 33 dBm, B2_1900 and B66_2100 at 37 dBm, k taken as the lower of the two groups. The low bands reach 100% coverage at the bottom of the search range (P_min 3.23 dBm, so the true value is lower) and fit k=24 at every N, which is the cap. The high bands set every result, and the combined answer equals the 37 dBm rows: N=3 gives k=20, 98.3% confirmed, 98.2% next floor, 3 remotes. N=4 gives k=24 (cap), 98.2% confirmed, 98.1% next floor, 2 remotes. Outputs: pmf_adxv_low33.json and pmf_adxv_high37.json.

At 37 dBm, N=4 to 8 reach the kmax cap of 24, so the real k there can be higher. Combiner insertion loss is not included (CHC-U 0.4 dB, CHC-X2 up to 2.2 dB). The riser model uses one middle-floor remote and half-inch coax at 2.0 GHz. These are grade E config estimates, not measured values. A one-port split across 13 or more floors is a power answer only. Port count, riser cable and fiber layout will set the practical remote count.
