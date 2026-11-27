# UL, near-far, and grid testing: research note for the public safety ROM

Researched 2026-11-26. No code changes. Sources: the SBC ERCES Standard (2026-10-20 draft),
IFC 510.5.4 via Durham and Murray AHJ publications, the DC OUC Technical Requirements v7.1,
Los Angeles County Fire v7-C3-S21, Collier County P25 ERCES Guidelines (2025 and 2026
versions), the NTIA TR-11-480 report, MobileNet ERCES case studies, PCTEL OPT014-1, the
Nextivity SHIELD BDA overview, and the iBwave public safety workflow pages. Web cache copies
of the OUC PDF and the Lexico page are under AppData/Local/hermes/cache/web/.

## What the standards and AHJs actually require

### The uplink (talk-in) is where designs are won or lost

- Code: IFC 510.4.1.2 requires outbound DAQ 3.0 with no dBm floor; the AHJs fill the gap.
- DC OUC: balance talk-in and talk-out link budgets at hip level; target the signal at the
  host site receiver equal to the site noise floor plus 18 dB from the cell edge; cap every
  BDA transmission so nothing arrives at an OUC site above -45 dBm; keep the BDA contribution
  to the site noise floor 15 dB below the pre-installation site noise; set the uplink squelch
  5 dB above the noise measured at the uplink input from the DAS; hard limits of 3 W portables
  and 35 W mobiles in the budgets.
- LA County: the BDA idle-state noise at the donor site shall not exceed -150 dBm; ERP noise
  inside the authorized passband -43 dBm (10 kHz) and -70 dBm at 1 MHz outside; muting and
  squelch place the uplink power amplifier dormant unless the input is at least -95 dBm; a
  passing P25 instrument result is SINR 20 dB or greater, BER no greater than 2 percent.
- MobileNet worked example: base station NF 6 dB gives a noise floor near -127 dBm in 12.5 kHz;
  DAQ 3.0 needs SINR about 17 to 18 dB, so the portable must arrive at about -110 dBm; a 6 dB
  noise rise at the site shrinks the uplink budget 6 dB and roughly halves the usable portable
  distance. Multiple BDAs feeding one site aggregate their noise.
- FCC 47 CFR 90.219 via the SBC primer: radiated power capped at 5 W ERP per retransmitted
  channel, booster noise figure no more than 9 dB, the -43/-70 dBm noise ERP limits above,
  Class B boosters registered in the FCC database.
- Collier County requires a no-harm test at the donor site (uplink noise floor, uplink maximum
  signal level) plus an outdoor signal leakage test, performed with the county's managed
  services provider. Cross-building isolation: any ERRCS within 300 feet of another needs at
  least 100 dB isolation (PCTEL OPT014-1 procedure).

### Near-far

- The mechanism: a wideband (Class B) uplink AGC reduces gain for everyone when one strong
  near radio keys up, which can starve a far radio (MobileNet case study 1; NTIA TR-11-480
  section 3.2 describes the same gain-reduction problem with multiple channels).
- The fixes the practice uses: Class A channelized amplification with per-channel AGC; uplink
  squelch so noise and idle channels never reach the site; and higher antenna density so the
  input signal spread at the BDA is small (Comba and Lexico both state the density principle:
  more antennas make the signal range the BDA must handle smaller).
- DC OUC makes it a test requirement: the system must pass voice quality with multiple
  simultaneous portables in worst-case locations, and the near test caps the input at the BDA
  at 5 dB below the maximum operating power and 10 dB below the maximum damaging threshold
  when a radio is right at an antenna.
- Nextivity cites SHIELD dynamic range over 50 dB with fast AGC as the class-A countermeasure.
- Isolation: the code and every AHJ require isolation at least 20 dB above system gain; DC
  uses 15 dB minimum on the serving side drawing; oscillation supervisory and shutdown rules
  apply (IFC 510.4.2.4(5), NFPA 1225 18.3.2; NTIA notes AGC can still oscillate, so the
  oscillation gain reduction is separate from the AGC action).

### Grid testing (acceptance method)

- 2021/2024 IFC 510.5.4: each floor divides into 20 approximately equal test areas (an 80 ft
  maximum grid dimension, so a floor over 128,000 sq ft subdivides into sectors of 20 cells
  each); more than one failed area fails the floor; two failures permit one 40-area retest
  where no more than two non-adjacent areas may fail.
- Critical areas (stairs, exit passageways, elevator lobbies, fire pump rooms, fire command
  centers, plus any AHJ additions) are tested as separate discrete locations and do not count
  in the 20-cell grid (SBC ERCES Standard section 5.3); the adjacent-area allowance may be
  used only where the adopted code permits it and never for critical areas.
- Thresholds by jurisdiction: RSSI at least -95 dBm quantitative OR SINR at least 18 dB OR
  BER no more than 2.5 percent (Murray, Central Jackson County) or SINR 20 dB on instrumented
  P25 decode (LA County) or DAQ 3.0 plus BER 2.5 percent and RSSI (Collier, with RSSI reported
  but not pass/fail). DAQ 3.0 is the qualitative target both ways.
- Los Angeles County's procedure computes the uplink from the downlink per grid cell: measure
  the average downlink per cell, then calculate the uplink with the link budget and record the
  formula. Annual testing repeats the grid plus a one-hour battery load test.
- DC OUC additionally requires the downlink coverage maps be produced for one frequency while
  assuming 10 frequencies transmitting simultaneously per band, and the uplink assuming
  simultaneous channels; maps need a scale.

### Delay (a real Class A constraint)

- SBC ERCES Standard: propagation delay donor port to the most distant service antenna no
  more than 15 microseconds unless the fire marshal approves otherwise; Collier allows 32
  microseconds and recommends 25 kHz or 75 kHz Class A filters to stay inside it.
- NTIA: a 12.5 kHz channel filter alone has at least 80 microseconds of delay; widening to
  200 kHz brings the floor to 5 microseconds. Fiber adds about 5 microseconds per km. Radios
  tolerate the delayed plus undelayed combination only with enough power difference between
  the two paths.

## What the tool does today against those requirements

- Coverage: the NFPA two-tier preset (95/99 at -95 dBm) matches the code matrix and the runs
  pass with large margin; that is the DL story only.
- The ROM table (ps_rom_results.json) has no uplink budget, no noise coordination, no squelch
  threshold design, no near-test level check, and no talk-in/talk-out balance. The first uplink
  attempt (ps_ul_results.json) was withdrawn because its chain math was wrong and the AHJ site
  inputs were missing; that gap is correct behavior but the budget still needs building.
- Nothing in the output speaks the grid language: no cells, no cell-center predictions, no
  pass/fail counts, no critical-area discrete points, no 40-area retest analysis.
- No near-far spread metric: N=3 per floor comes from the coverage rule, not from any
  uniformity or input-spread criterion. No near-ring level at each antenna.
- No delay budget, no FCC 90.219 plate (ERP per channel, NF limit, noise ERP), no cross-building
  isolation item, no intermod/PIM study trigger for converged plates.

## Improvement suggestions, ranked

1. Build the uplink engine first (ps_ul.py): per band per OEM, take the AHJ inputs (site ERP,
   site antenna gain, site NF, site distance or path), solve the maximum uplink gain subject to
   the noise rules (-150 dBm at the donor site or 15 dB below the site noise floor, whichever
   the adopted local rule states), emit the squelch threshold (DAS uplink input noise + 5 dB),
   the far-user arrival at the site (target the noise floor plus 18 dB), the near-user input
   check (5 dB below max operating, 10 dB below damaging), and the talk-in/talk-out balance at
   hip height. Deciding reason: this is the gate every AHJ reviews, it can change the ROM
   hardware (Class A choice, pad schedule, battery draw under squelch), and the DL-only ROM is
   the weaker half of the story.
2. Build the grid module (ps_grid.py): divide each floor of the floor model into 20
   approximately equal cells honoring the 80 ft maximum dimension, predict the level at each
   cell center with the existing ray/prediction machinery, emit pass/fail per cell against
   RSSI -95, SINR 18 (or the AHJ's local threshold), and the 40-cell retest simulation; emit
   the critical-area discrete points from the floor tags; add the calculated-uplink per cell
   and the formula, since LA County's method does exactly that. This produces both the design
   self-check and the acceptance test sheets the installer will live with.
3. Add the near-far spread metrics: per floor per band report min, p5, p95, max of the
   predicted level; per antenna report the near-ring level (the closest walkable point) and
   the far-corner level. Flag floors whose spread exceeds a configured ceiling (start at 20 dB,
   the Class B AGC range issue), suggest more antennas or a Class A note, and carry the near
   test margin per antenna. Deciding reason: it turns the N=3 result from a coverage opinion
   into an input-dynamic-range statement, which is what actually drives antenna count in the
   field.
4. Add small compliance plates that are cheap to compute: FCC 90.219 ERP per channel from the
   sheet EIRP plus antenna gain, the NF limit flag, the -43/-70 dBm noise ERP note, the delay
   budget line (filter plus fiber plus coax group delay against 15 or the AHJ's microseconds),
   and the cross-building isolation checklist item (300 feet, 100 dB). These come from data
   already on disk.
5. For the converged shared-plate option, add the intermod/PIM trigger: when cellular and PS
   share plates, list the IM products that land in the PS passbands and note the PIM test
   possibility the DC rule implies. This protects Matt's converged architecture before it hits
   commissioning.
6. Model-behavior finding worth flagging: the model says one port covers 45 floors (k=48).
   Correct against the model, but real designs split trunks for survivability, serviceability,
   and grid margin; once the UL and grid checks exist, revisit whether the tool should cap k by
   a trunk-plate convention instead of pure power fit. Not a bug; a presentation and design
   practice question.

Sources kept on disk: this note plus the web cache copies noted at the top. The SBC ERCES
Standard PDF link (d16k74nzx9emoe.cloudfront.net, 2026-10-20 version) and the NTIA report are
the two most quotable references for future AHJ conversations.


## Follow-up research (2026-11-26, same day): per-channel versus composite power, and the trunk-count standards

### The definitive answer on output power

FCC 47 CFR 90.219(d)(3)(i): the radiated power limit of 5 W ERP applies "of
each retransmitted channel", forward and reverse. 90.219(e)(1): the device must
be designed so radiated power does not exceed 5 W ERP for EACH retransmitted
channel. The cap is per channel, never per band.

The rated output of the device is COMPOSITE and the industry convention splits
it by the channel count (dBm per channel = composite dBm - 10log10(N)):
- The Jack Daniel in-building primer (the long-standing industry reference):
  a 10 W Class B amplifier with 10 equal-level channels delivers 1 W per
  channel; 30 dBm/ch at 1 carrier, 27 at 2, 24 at 4, 20 at 8, 18.5 at 16,
  14 at 32, 10 at 50. Every doubling of channels costs 3 dB per channel, and
  the design should assume future channels inside the passband.
- The Comba commissioning guide names the AGC/ALC modes explicitly: Mode 1
  shares the total power by the number of ACTIVE channels; Modes 2 and 3 share
  it by the number of PROGRAMMED channels.
- NTIA TR-11-480 models the same effect: adding channels under AGC reduces
  gain and per-channel output; a clean system can absorb about 114 channels
  before starving, but 7 channels with one strong interferer can do it.

The AHJs codify the split:
- San Francisco ERRCS Technical Requirements v2.0 (2024): the BDA must be set
  to a derating mode that equalizes power per filter at full loading (6.7);
  the manual derates for their plan: minus 12.5 dB per channel in the 800 PS
  band and minus 8 dB per channel in the 700 PS band (6.8); all filters at
  75 kHz (6.2) with at most 20 us group delay (4.1.8); gains at most 80 dB
  (6.4); squelch fixed at -75 dBm (6.5); the peak downlink COMPOSITE capped at
  +37 dBm (9.5.4.1) - stricter than the FCC per-channel cap - and the uplink
  ERP at the donor at most +24 dBm per channel (9.5.3.1).
- Durham NC BDA permit requirements: the commissioning math states max output
  power per channel = the BDA output power minus 10*log10(24); uplink gain
  capped 65 dB; the tower must receive the uplink between -95 and -80 dBm and
  the uplink noise below -153 dBm.
- DC OUC: the downlink coverage maps must be produced for one frequency while
  assuming 10 frequencies transmitting simultaneously in each band, and the
  uplink assuming simultaneous channels.
- DC Sheriff ERCES guidelines: "RF composite power (the effect of combining
  multiple channels) making proper BDA selection [depends on the] maximum
  number of RF channels"; uplink gain at most 65 dB; per-channel levels at the
  donor site between -100 and -80 dBm.

Consequence for the tool: the ROM plates treated the sheet port power as if it
were per-channel. Under the composite convention the per-channel available
power = the sheet composite minus 10log10(channels). Worked example at the 33
dBm FiRe port: 10 channels leaves 23 dBm/ch, 16 leaves 21 dBm/ch; the ERP per
channel lands at 26-28 dBm (PASS against the 37 dBm cap, reversing the "ERP
FAIL" plate), and the coverage must be re-run at the per-channel power, with
the remote-count answer following from the loss budget at that power.

### The trunk and remote count standards

No standard or AHJ document caps floors-per-BDA directly. What the standards
do set, in the order that matters:
1. Pathway survivability: NFPA 1225 treats the BACKBONE (riser/trunk) cable as
   the protected pathway while the individual antenna distribution runs are
   not the same requirement (A.18.2.1(9) pathway survivability design; the
   NFPA 1225-2027 fire-rated cable clarification article is explicit that
   backbone survivability and distribution antenna cabling are separate
   requirements). For a 45-floor trunk this is the standard that pushes toward
   more than one riser.
2. Per-service-antenna link budgets are the AHJ norm: the DC Sheriff requires
   the UL far-field, the UL near-field, the UL noise, and the DL down to each
   service antenna in tabular form; the Durham rules require the near/far
   budgets for the full system. A budget that only speaks at the port does not
   meet the submittal practice.
3. High-rise design practice: zone the building vertically (the design
   references cite 5-10 floors per zone as the practical range) so that the
   distribution loss stays inside the budget and the riser survivability can
   be satisfied with separated pathways; dual BDAs and separate donor paths
   are cited as the redundancy pattern where the AHJ allows.
4. The BDA selection itself is sized by the composite power against the
   channel count (the DC Sheriff and the SF sections above): more channels
   means less per-channel power, which shortens the achievable trunk and
   raises the remote count even though none of the documents say "N floors per
   BDA" outright.

So the industry-standard framing for the tool is: pick the channel list per
the AHJ plan, split the composite by it, budget per service antenna, satisfy
the per-channel ERP cap, and let the survivability/zoning practice (5-10
floors per zone) set the remote count instead of a pure power fit. The owner
gates (floors-per-port cap) are exactly the mechanism for that; the missing
input is the channel count, which is an AHJ plan question, not a standards
guess.
