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
