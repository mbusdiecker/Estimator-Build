# Public safety ROM phase (day 2, opened 2026-11-26)

New iteration clone of Estimator-Build-headend (full copy including .git). Goal: the
ROM tool sizes and prices public safety / ERCES ROMs from spec sheets and code,
covering VHF, UHF, 700 MHz, 800 MHz and FirstNet Band 14, with the code-driven
requirements (UL 2524 listing, NEMA enclosures, battery autonomy, annunciator,
survivability) enforced by the tool rather than remembered.

## Completed public safety jobs to model from (found via qmd, on disk)

1. **26-00908 TRC PSEGLI Melville ERCES** (budget submitted 10/27/26). ADRF
   MyADRF ROMBOM run 2026-10-20 (Public Safety, 77,000 sq ft, 2 buildings,
   700/800 PS + FirstNet, 12 h battery): FiRe-78-8-U1 digital dual-band 700/800
   PS fiber repeater (UL 2524, P25 Phase 2, up to 8 remotes), ADXV-R-3378P-U2
   2 W 700/800 IP66 UL2524 PS remote modules, ADRF-BBU-W-045-2S x2 +
   ADRF-BBU-W-120-2S x1 (24 V wall BBUs), low-PIM N passives (ADRF-2WS-20W-N-LPIM,
   DCPL-6/10-N-LPIM), SC/APC SMF + OCC terminations, plenum coax
   (ICA12-50JPLLR), donor AD-PA-617-960-D + Polyphaser IS-50NX-C2 (125-1000 MHz)
   + Rohn mast/mount/grounding/weatherproofing. Files: Jobs/26-00908 TRC PSEGLI
   Melville/ (ROM BOM xlsx, 26-00908_ADRF ROMBOM tool results_10-20-2026.md,
   Budget/, ROM/, Sources/), and the ERCES budget md indexed in qmd.
2. **Fort Worth City Hall (26-XXX, in build 2026-11)**. ADRF ROM BOM
   (ADRF_Fort Worth City Hall_ROMBOM_11_23_2026.xlsx): SDRC + PSR front end,
   ADX V PS, PSR-ANN annunciator, BBU. The FirstNet B14 add-on was built
   11/24 on the GCL template: PSR-78-9533-U1-27 into ADXV-H-POI-7U (B14 via the
   PS HPR modules). 47 CFR 90.219 (Part 90 booster) applies; the FirstNet
   Authority B14 consent form is a job step. Files: Jobs/26-XXX Fort Worth City
   Hall ROM/(Drawings, Budget, ROM, ROM Tool runs).
3. **FLETC Glynco** (2023, five buildings for WidePoint): ADRF + Nextivity, one
   PO, clearance facts. Quote sources: Jobs/908src-FLETC-ADRF/ (ANS-ADRF
   25-01372, Graybar quotation 248450032).
4. **Binghamton University ITC 26-01037** (public safety ROM hand-off 11/18):
   Andy sales, Anoosha engineering, not Matt's, but the same PS product family.

## PS hardware sheets on disk (Vendors/ADRF Specs/)

- PSR-78-9533 + PSR-78-9537 series (v7/v8/v9 + U/U1/X/-27 variants): the
  700/800 PS repeater family; PSR-78-9533-U1-27 sheet read 11/24 for the Fort
  Worth kit.
- PSR-VU-9537 and PSR-U-VU-9537 / PSR-U-VU-9537-U1: VHF/UHF variants.
- PSR-ANN Series (annunciator), PSR-EPO-PKG (emergency power off packages),
  PSR-U-AAI-CBL (auxiliary alarm interface cable).
- ADRF-BBU-W battery sheets + two installation guides; BBX battery banks and
  cables (BBX-CBL-12/13); BBx-N4 (NEMA 4) variants; ADRF-BBL-N4/X.
- ADRF-BPF-V-1 (VHF) and ADRF-BPF-U-1 (UHF) band pass filters; ADRF-BSF-14
  (band selective filter, Band 14).
- ADRF-ENC-N4-1 (NEMA 4 enclosure); ADXV-R-3378P-U2 PS remote (UL 2524, in the
  Melville run); low-PIM passives: CHC-U-LPIM, CHC-V-LPIM, CBC-x-LPIM,
  HCPL-3-LPIM, DCPL-N-LPIM, 2_3_4-WS-20W; SDR-ICS (interference cancellation)
  sheets and SDR-ICS-ENC-2, RMK-SDR rack kit.
- UL 2524 3rd edition PS remote package precedent from the price list:
  ADXV-R-3378P-U2, ADXV-R-25VU-U2, and the NEMA 4 head end variant
  ADXV-H-NMS-AC-N4.

## Code requirements and where they live (pulled/verified 11/24, restated)

- NFPA 1225 (2022) chapter 18 is the ERCES chapter (web text at
  https://oci.georgia.gov/document/document/nfpa-1225-chapter-18/download; no
  local PDF on disk). Requirements: 99 percent of critical areas and 95 percent
  of general areas (18.8.3/18.8.4), DAQ 3.0 uplink and downlink (18.9), UL 2524
  listed components (18.12.1.3), booster enclosures NEMA 4 or 4X (18.3.1.2),
  batteries NEMA 3R (18.3.1.1), oscillation detection with a supervisory signal
  (18.3.2), donor isolation 20 dB above system gain (18.3.3.2), 12 h at 100
  percent on a dedicated battery, or 2 h battery plus a generator carrying 12 h
  (18.13.2), fire alarm supervision with a dedicated annunciator in the fire
  command center (18.14), and AHJ plus license-holder approval before
  installation and activation (18.2.1, 18.7.2). Backbone cable needs a 1 h or 2 h
  rating only in non-sprinklered, partly sprinklered or high-rise buildings, and
  none in a fully NFPA 13 sprinklered building (18.12.3.3/18.12.3.4).
- The -95 dBm figure is IFC 510.4.1.1 (iccsafe.org), which many states do not
  adopt; NFPA 1225 chapter 18 carries no dBm figure. Insist on reading the
  adopted edition per jurisdiction past sessions: Vermont builds on NFPA
  1/101/IBC (not the IFC); 2021 IFC 510 cites NFPA 1221.
- NFPA 1 11.10.2/11.10.4: an approved ERCES is required where radio strength
  cannot be maintained, and components must be UL 2524 listed.
- FCC: 47 CFR 90.219 governs Part 90 signal boosters (Class A/B, registration
  90.219(d)(5)); 47 CFR 20.21 is the commercial side. FirstNet Band 14
  retransmission needs FirstNet Authority consent, separate from AT&T's
  commercial consent.
- These extracts live in the 11/24 Claude review
  (IBW-Estimating-Tool-HANDOFF.../Part20 Comparison/26-01038 Sugar House Part20
  OEM Comparison Review 11-24-26.md, section F) and in the 2026-11-17 Insight HQ
  research transcript. Re-read the primary text per jurisdiction before a ROM
  relies on it (standing rule: never assume code requirements).

## Phase status (2026-11-26, owner answers applied)

- Battery: built into the engine, code-based (Ah = load x hours / the vendor sizing constant from the
  sheets: Westell 12 V x 0.85 at 123 W, Comba 48.1 per its own formula at 51.2 V, ADRF 24 V x 0.85 at
  the FiRe 121 W); 12 h default per NFPA 1225 18.13.2 and IFC 510.4.2.3 verbatim text, 24 h is the
  owner option. Vendor bundles verified against the draws (Westell 1x/2x 145 Ah, Comba 60/100 Ah, ADRF
  2x/3x 120 Ah). Per-remote draws not published (Comba V3 remote, ADXV-R-3378P-U2): 45 W noted each.
- Converged option: built (owner: separate donor and actives, shared coax, splitters and antennas; the
  PS remotes ride the design's shared coax and tap plates). The PS packages emit the shared-plate
  combiner (ADRF-CBC-700-2600 class, x3 per sector group) only in the PS pass (ps_mode).
- Packages live: adrf_fire_erces, westell_cs45_ps, comba_ps_v3, adrf_psr_vu (PSR-VU-9537-X/-U1 +
  BPF-V-1 + BPF-U-1 + BBx battery), adrf_firstnet_b14 (BSF-14 + PSR-78-9533-U1-27 into ADXV-H-POI-7U,
  the Fort Worth pattern). Code table publishing (owner): every PS package emits the
  standards-followed matrix (16 rows: topic, IFC cite, NFPA cite, requirement, enforcement) into the
  BOM notes; the same table is PS_CODE_TABLE in romtool/oem.py. Suite: 457 passed, 17 skipped.
- Codes on disk: codes/NFPA-1225-2022-Chapter-18.pdf + nfpa1225_ch18_key_sections.md,
  ifc510_2024_sections.md (verbatim from codes.iccsafe.org), codes_sbc_quick_reference.md.
- Committed per the owner (2026-11-26 'commit'): romtool-public-safety @ 8be481f (this phase, all of it),
  romtool-headend @ 970e700 (the head end wiring; the PS files dropped there - they live on this tree),
  romtool-physics-ul @ cca7284 (the comparison layer). None pushed. IMPORTANT topology note: the
  iteration trees are git worktrees of the Estimator Build repo; the new phase branch is
  romtool-public-safety, and future iterations should be git worktrees, not folder copies.

## Build plan (this phase)

1. oem_specs.json: public safety packages (first: adrf_fire_erces, the
   FiRe-78 + PS ADX V remote + BBU + annunciator kit per the Melville
   precedent), plus VHF/UHF (PSR-VU-9537 + BPF filters) and Band 14 (BSF-14 +
   the 9533-U1-27 + POI-7U pattern) variants as add-ons.
2. Code-driven requirements in the tool: a 12 h battery sizing step (BBU Ah
   against the load), an annunciator line, NEMA 4 enclosure where the BDA
   housing requires it, UL 2524 listing enforced on PS part selection, donor
   isolation and oscillation notes, and a survivability switch (backbone cable
   rating + cable path) keyed to the building's sprinkler status.
3. Bands: PS 700 (757-768/776-787 + B14 758-775), 800/SMR (851-869/821-824?),
   UHF (450-470), VHF (150-174); spec sheets per band family on disk.
4. Open items: ADRF quotes and confirmations (the Melville pricing request
   draft in the job folder), the UL 2524 edition question (3rd edition listed
   packages in the price list), per-jurisdiction AHJ adoption, and the shared
   commercial + PS DAS question (NFPA 1225 18.6.3 + the FCC DAS carve-out).

## Channel-count convention (2026-11-26)

Owner rule: the channel count is an EXPLICIT entry on every PS run; there is
no default in ps_ul, ps_grid, ps_compliance, or ps_rom. The composite port
splits by 10log10(channels) for every DL and ERP calc. Today's saved runs
assume 32 channels in each passband.

Consequences recorded in the artifacts: the antenna plate advances to N=4 per
floor (180 antennas) on the 700/800 rows, the B14 trunk shrinks to k=40 at its
32-channel assumption, the VHF row needs 2 repeaters at 32 channels, and every
ERP-per-channel plate passes (17.9-26.9 dBm against the 37 dBm cap), which
reverses the earlier composite-as-channel finding. Caveats carried: the B14
passband is technically one LTE block (Class B registration; 32 channels is
the owner's working assumption, not the passband reality), and 32 channels in
VHF is likewise the owner assumption. The FiRe-78 UL composite output figure
used by the out-cap check (-21 dBm) is a grade D read of the SS composite
table; at 32 channels it makes the per-channel UL out-cap bind, so the figure
needs vendor confirmation (the ps_ul rows carry ul_out_cap_binds). The SBC
architecture verdicts (from the owner's message): about 487,395 sq ft total
puts the building on the active-DAS / dense-high-rise side of the passive
boundary and over the 250,000 sq ft fiber-backbone line, favoring the fiber
architectures. Converged orders must list the PS channel-specific filters as
order lines (recorded on all five PS packages and in the ROM output).

## Owner answers applied (2026-11-26, session 2)

1. AHJ site inputs (site ERP, site NF, donor ambient cal): REQUIRED, entered
   per site, researched or given pre-ROM. No defaults remain in ps_ul or
   ps_grid; the saved artifacts used the explicit placeholder entries noted
   in the docs.
2. FiRe-78 uplink: UL composite output 30 dBm, total system gain 85 dB
   (owner-provided). The ps_ul OEM table uses them; at 32 channels the
   per-channel out cap does not bind and the near/far pair passes with the
   input pad.
3. Same as 1 for ps_grid.
4. Near/far validation: implemented as the simultaneous pair (one portable at
   the farthest covered point, one directly under an antenna), with the Class
   B shared-AGC starvation model: the far channel loses whatever the near
   channel's excess over the per-channel output cap forces the AGC to remove,
   after the near-test input pad. The code itself does not define the pair
   test; it comes from the AHJ technical requirements (DC OUC near-test
   margins -5 dB operating / -10 dB damaging, the DC Sheriff per-antenna near
   and far uplink budgets, the SBC simultaneous-portable requirement). Under
   discussion with the owner: the loading case (10 simultaneous per the DC
   OUC rule) and the AHJ threshold inputs.
5. Remote-coverage gate: industry practice about 250,000 sq ft max per BDA
   or remote is now a toggle (--caps-remote-sqft); report-only until turned
   on. At Sugar House's about 487,395 sq ft it reports a 2-remote minimum.
   The antenna-area cap stays open (the owner has not set a number).
6. Coverage threshold: minus 95 dBm stands regardless of DAQ wording; SINR is
   checked separately (the grid already reports the per-cell SINR).
7. Deliverable: BOMs for the budget tool; pricing is day 3.
8. Pushed: romtool-public-safety and romtool-headend created on
   github.com/mbusdiecker/Estimator-Build (push only, no merges); romtool-physics-ul
   was already current on the remote. Claude review pending.
Vendor draws applied: ADXV-R-3378P-U2 102 W per remote; Comba V3 2 W Class A
100 W. The 2-remote kit battery sized on them: 2x 120 Ah at 12 h, 4x at 24 h.
Tool force (the fixed placement): NOT built; the design plates stay the
right-sizing input per the owner.

## Session 3 (2026-11-26 evening): load-case option, AHJ profiles, PIM field gate, BOM rebalance

- ps_ul --load-case pair|loading: the pair (farthest point + directly under an
  antenna, the default) and the loading instant (DC OUC style, 10 channels
  active, per-channel caps widen to -10log10(10)). Both report the Class B
  shared-AGC effect on the far channel and flag Class A when it fails.
- ps_ahj_profiles.json + ps_profiles.py: per-AHJ threshold profiles
  (ifc_default, dc_ouc, la_county, sf, collier) driving the arrival margin,
  the noise rule, the near-test margins, the SINR threshold, the delay cap,
  the composite cap, the UL donor ERP cap, and the squelch offset. The CLI
  arguments always override; minus 95 dBm stands in every profile (owner
  rule).
- UL ERP at the donor per retransmitted channel checked in ps_ul against the
  profile cap (+24 dBm per channel in the SF profile).
- ps_pim_checklist.py: the field procedure for converged plates: the IM3
  screen hits carried into a 7-step field gate (order filters, plate PIM at
  2x43 dBm tones against the project limit, the donor and head end, the
  half-split hunt, the hardware and torque rules, the post-activation ERCES
  re-check, the acceptance record).
- BOM rebalance in ps_rom: equal per remote at emission (the owner example:
  21 antennas over 2 remotes -> 11 and 10; the splitters and riser coax split
  the same way). Day 3 owner note: re-running the count with the floors split
  into equal zones per remote may reduce the total; the first run balances
  the design as-is.
- Verified artifacts at 32 channels: the VHF row rebalance 90/90 over 2
  remotes, the 250k gate reports a 2-remote minimum at 487,403 sq ft, the SF
  profile passes the 75 kHz filter at 13.6 us, the Collier profile flags the
  12.5 kHz filter and recommends 75 kHz at both the 32 us line.

## Near-far levers finalized (2026-11-26, session 4)

Owner correction drove the model: "the coupler is essentially an attenuator
for that path back to the remote." The branch loss on the nearest antenna's
return path is the primary near-test lever, and it comes from the plate
design itself: a tap plate specs the near taps high (the equal-loss
convention already gives the closest branches the most dB); a pure splitter
tree embeds 10log10(N) plus insertions. Three levers now reported per band in
ps_ul (near_levers): the branch loss needed, the input pad, and the
density route.

At the DC OUC margins (-5 dB operating / -10 dB damaging) with a 12 dB near
branch spec and a 3 dB near-chain: 700 PS needs 11.1 dB branch (PASS with the
12 dB spec), 800 PS 10.1 (PASS), UHF 15.7 (the spec must say 16+), VHF 25.1
(beyond standard taps; the 25 dB pad is the honest verdict there, and the
density route is off the table: it exceeds the 24/floor cap). The density
route numbers: 7 antennas per floor for 700 PS, 6 for 800, 19 for UHF - all
impractical, which is why the branch/coupler and pad levers lead.

ps_rom --caps-near-density applies max(coverage N, density N) when the owner
asks; report-only. The ps_ul --near-branch-loss-db feeds the tap spec check.
